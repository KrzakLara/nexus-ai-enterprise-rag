"""Human-in-the-loop review: pending / needs_review -> (corrections) -> approved | rejected.

Authorization (server-side; the UI only mirrors it):
  * read an extraction    = may read the document (security.authorized_documents: tenant AND
                            (admin OR owner OR group grant)), the same rule as RAG retrieval;
  * correct / approve /
    reject / re-process   = may read the document AND has role 'reviewer' or 'admin'.
An extraction the user may not read is reported exactly like a non-existent one (HTTP 404).

Concurrency: every mutation must send `expected_version`; a stale version gets HTTP 409 and
the current version (optimistic locking), so two reviewers cannot silently overwrite each other.
Approved and rejected extractions are final; re-processing them requires an explicit force.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Document, Extraction, ExtractionField, ReviewEvent, User
from ..security import authorized_documents
from . import grounding
from .rules import _parse_value
from .schemas import Evidence, ExtractedField, check_canonical, value_type_of
from .service import ExtractionLocked, effective, load_parsed, now, record, revalidate

REVIEWER_ROLES = frozenset({"admin", "reviewer"})


class ReviewError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


def can_review(user: User) -> bool:
    return user.role in REVIEWER_ROLES


def authorized_extraction(db: Session, user: User, extraction_id: str) -> tuple[Extraction, Document] | None:
    allowed = authorized_documents(user).with_only_columns(Document.id)
    row = db.execute(select(Extraction, Document).join(Document, Document.id == Extraction.document_id)
                     .where(Extraction.id == extraction_id, Extraction.document_id.in_(allowed))).first()
    return (row[0], row[1]) if row else None


def _require(user: User, extraction: Extraction, expected_version: int, editable: bool = True) -> None:
    if not can_review(user):
        raise ReviewError(403, "Reviewer or admin role required")
    if expected_version != extraction.version:
        raise ReviewError(409, "The extraction was changed by someone else; reload it",
                          current_version=extraction.version)
    if editable and extraction.status in ("approved", "rejected"):
        raise ReviewError(409, f"Extraction is already {extraction.status}")


def normalize_correction(value_type: str, value: str | None) -> str | None:
    """Accept a canonical value or a human-written one ('31.03.2026.', '1.234,56 EUR')."""
    if value is None:
        return None
    value = value.strip()
    try:
        check_canonical(value_type, value)
        return value
    except ValueError:
        pass
    kind = {"decimal": "money", "percent": "money"}.get(value_type, value_type)
    parsed = _parse_value(kind, value, strict=False)
    if parsed is None:
        raise ReviewError(422, f"'{value}' is not a valid {value_type}")
    try:
        check_canonical(value_type, parsed[0])
    except ValueError as exc:
        raise ReviewError(422, str(exc)) from None
    return parsed[0]


def correct(db: Session, user: User, extraction: Extraction, corrections: list[dict],
            expected_version: int) -> None:
    _require(user, extraction, expected_version)
    parsed = load_parsed(db, extraction.document_id)
    fields = {f.name: f for f in db.scalars(select(ExtractionField)
                                           .where(ExtractionField.extraction_id == extraction.id))}
    for c in corrections:
        name = c["name"]
        try:
            vtype = value_type_of(extraction.doc_type, name)
        except KeyError as exc:
            raise ReviewError(422, str(exc).strip("'\"")) from None
        new_value = normalize_correction(vtype, c.get("value"))
        evidence = None
        if c.get("evidence_quote"):
            located = grounding.locate_quote(parsed, c.get("page"), c["evidence_quote"]) if parsed else None
            if located is None:
                raise ReviewError(422, f"evidence quote for '{name}' was not found in this document")
            evidence = Evidence(document_id=extraction.document_id, page=located[0], text=located[1]).model_dump(mode="json")
        f = fields.get(name)
        if f is None:  # a field the extractor missed
            f = ExtractionField(extraction_id=extraction.id, name=name, position=len(fields) + 1000,
                                value_type=vtype, value=None, raw=None, evidence=None, method="reviewer",
                                grounding="no_evidence", issues=[])
            db.add(f)
            fields[name] = f
        old_value = effective(f)[0]
        ref = evidence or f.evidence
        if new_value is None:
            ground = "no_evidence"
        elif ref and parsed:
            candidate = ExtractedField(name=name, value_type=vtype, value=new_value,
                                       evidence=Evidence.model_validate(ref), method="reviewer")
            g, problems = grounding.ground(candidate, parsed, extraction.document_id)
            ground = g if not problems else "reviewer_asserted"
        else:
            ground = "reviewer_asserted"  # no evidence in the document: the reviewer vouches for it
        f.corrected_value, f.corrected_evidence, f.corrected_grounding = new_value, evidence, ground
        f.corrected_by, f.corrected_at = user.id, now()
        extraction.version += 1
        record(db, user, extraction, "correct", field_name=name, old=old_value, new=new_value,
               comment=c.get("comment"))
    extraction.updated_at = now()
    db.flush()
    revalidate(db, extraction, parsed)


def approve(db: Session, user: User, extraction: Extraction, expected_version: int,
            comment: str | None = None, override_reason: str | None = None) -> None:
    _require(user, extraction, expected_version)
    parsed = load_parsed(db, extraction.document_id)
    revalidate(db, extraction, parsed)
    fields = list(db.scalars(select(ExtractionField).where(ExtractionField.extraction_id == extraction.id)))
    errors = [i for f in fields for i in (f.issues or []) if i["severity"] == "error"]
    errors += [i for i in (extraction.issues or []) if i["severity"] == "error"]
    if extraction.doc_type == "unknown":
        raise ReviewError(409, "Unknown document type: re-process with an explicit type, or reject it")
    if errors and not (override_reason and len(override_reason.strip()) >= 10):
        raise ReviewError(409, "Validation errors must be corrected before approval "
                               "(or give an override_reason of at least 10 characters)",
                          errors=errors)
    extraction.status, extraction.version = "approved", extraction.version + 1
    extraction.reviewed_by, extraction.reviewed_at, extraction.review_comment = user.id, now(), comment
    extraction.updated_at = now()
    record(db, user, extraction, "approve",
           comment=(comment or "") + (f" | OVERRIDE ({len(errors)} errors): {override_reason}" if errors else ""))


def reject(db: Session, user: User, extraction: Extraction, expected_version: int, comment: str) -> None:
    _require(user, extraction, expected_version)
    if not comment or len(comment.strip()) < 3:
        raise ReviewError(422, "A rejection needs a reason (comment)")
    extraction.status, extraction.version = "rejected", extraction.version + 1
    extraction.reviewed_by, extraction.reviewed_at, extraction.review_comment = user.id, now(), comment
    extraction.updated_at = now()
    record(db, user, extraction, "reject", comment=comment)


def history(db: Session, extraction: Extraction) -> list[dict]:
    rows = db.scalars(select(ReviewEvent).where(ReviewEvent.extraction_id == extraction.id)
                      .order_by(ReviewEvent.id))
    return [{"id": e.id, "action": e.action, "user_id": e.user_id, "field": e.field_name,
             "old_value": e.old_value, "new_value": e.new_value, "comment": e.comment,
             "version": e.extraction_version, "created_at": e.created_at.isoformat()} for e in rows]


__all__ = ["ExtractionLocked", "ReviewError", "approve", "authorized_extraction", "can_review",
           "correct", "history", "reject"]
