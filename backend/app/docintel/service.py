"""Document-intelligence pipeline and persistence.

upload -> parse (parsing.py, OCR when needed) -> store_structure()   [DocumentProcessing, tables]
       -> process(): classify -> extract (rules | llm) -> ground -> validate -> upsert Extraction

`process` is idempotent: one Extraction per document (unique constraint). Re-processing replaces
the fields of a pending / needs_review extraction and bumps its version. An approved or rejected
extraction is locked unless `force=True`, which is recorded in the review history. Failures are
stored as status 'failed' with the error and an attempt counter, and never replaced by output
from another provider.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import (AuditEvent, Document, DocumentProcessing, DocumentTable, Extraction,
                      ExtractionField, ReviewEvent, User)
from . import grounding, rules
from .classify import classify
from .llm import ExtractionProviderError
from .llm import extract as llm_extract
from .schemas import SCHEMA_VERSION, Evidence, ExtractedField, ValidationIssue, typed
from .structure import ParsedDocument
from .validation import validate

log = logging.getLogger("nexus.docintel")
ACCEPTED_GROUNDING = {"exact", "normalized", "reviewer_asserted"}


class ExtractionLocked(RuntimeError):
    pass


def now() -> datetime:
    return datetime.now(timezone.utc)


def store_structure(db: Session, document: Document, parsed: ParsedDocument) -> DocumentProcessing:
    db.execute(delete(DocumentTable).where(DocumentTable.document_id == document.id))
    row = db.get(DocumentProcessing, document.id)
    if row is None:
        row = DocumentProcessing(document_id=document.id, attempts=0)
        db.add(row)
    row.status, row.error = "parsed", None
    row.doc_format, row.parser = parsed.format, parsed.parser[:200]
    row.page_count, row.ocr_pages = len(parsed.pages), parsed.ocr_pages
    row.structure, row.updated_at = parsed.model_dump(mode="json"), now()
    for table in parsed.tables():
        db.add(DocumentTable(id=f"{document.id}:{table.id}", document_id=document.id, table_key=table.id,
                             page_number=table.page, n_rows=table.n_rows, n_cols=table.n_cols,
                             header_rows=table.header_rows,
                             cells=[c.model_dump(mode="json") for c in table.cells],
                             bbox=table.bbox.model_dump() if table.bbox else None, source=table.source))
    db.flush()
    return row


def load_parsed(db: Session, document_id: str) -> ParsedDocument | None:
    row = db.get(DocumentProcessing, document_id)
    return ParsedDocument.model_validate(row.structure) if row and row.structure else None


# ---------------------------------------------------------------------------- field helpers

def effective(f: ExtractionField) -> tuple[str | None, dict | None, str]:
    """(value, evidence, grounding) after review: a correction wins over the extracted value."""
    if f.corrected_by is not None:
        return f.corrected_value, f.corrected_evidence or f.evidence, f.corrected_grounding or "reviewer_asserted"
    return f.value, f.evidence, f.grounding


def field_state(f: ExtractionField, issues: list[dict]) -> str:
    value, _, ground = effective(f)
    if any(i["severity"] == "error" for i in issues):
        return "invalid"
    if value is None:
        return "missing"
    if ground not in ACCEPTED_GROUNDING or any(i["severity"] == "warning" for i in issues):
        return "needs_review"
    return "ok"


def revalidate(db: Session, extraction: Extraction, parsed: ParsedDocument | None = None) -> list[dict]:
    """Re-run business validation over effective values; store issues per field and on the
    extraction; recompute status unless the extraction is already approved / rejected."""
    fields = list(db.scalars(select(ExtractionField).where(ExtractionField.extraction_id == extraction.id)))
    values = {f.name: effective(f)[0] for f in fields}
    evidence_text = {f.name: (effective(f)[1] or {}).get("text", "") for f in fields}
    issues = [i.model_dump() for i in validate(extraction.doc_type, values, evidence_text)]
    # extraction-time issues (e.g. unsupported evidence) describe the EXTRACTED value; once a
    # reviewer has corrected the field they no longer apply (the original stays in the history)
    base_issues = {f.id: [] if f.corrected_by is not None else
                   [i for i in (f.issues or []) if i.get("origin") == "extraction"] for f in fields}
    by_field: dict[str, list[dict]] = {}
    for issue in issues:
        by_field.setdefault(issue["field"], []).append(issue | {"origin": "validation"})
    for f in fields:
        f.issues = base_issues[f.id] + by_field.pop(f.name, [])
    doc_level = [i for values_ in by_field.values() for i in values_]  # e.g. required_missing
    extraction.issues = [i for i in (extraction.issues or []) if i.get("origin") == "extraction"] + doc_level
    if extraction.status not in ("approved", "rejected"):
        states = [field_state(f, f.issues) for f in fields]
        blocking = (extraction.doc_type == "unknown" or any(s in ("invalid", "needs_review") for s in states)
                    or any(i["severity"] == "error" for i in extraction.issues))
        extraction.status = "needs_review" if blocking else "pending"
    return issues


def process(db: Session, user: User, document: Document, provider: str = "rules", force: bool = False,
            doc_type_override: str | None = None) -> Extraction:
    proc = db.get(DocumentProcessing, document.id)
    parsed = load_parsed(db, document.id)
    if proc is None or parsed is None:
        raise ValueError("document has no parsed structure; re-upload or re-parse it first")
    existing = db.scalar(select(Extraction).where(Extraction.document_id == document.id))
    if existing and existing.status in ("approved", "rejected") and not force:
        raise ExtractionLocked(f"extraction is {existing.status}; pass force=true to re-process")
    proc.attempts += 1
    classification = classify(parsed)
    doc_type = doc_type_override or classification["type"]
    try:
        if provider == "llm":
            fields, provider_issues = llm_extract(parsed, document.id, doc_type)
            provider_name = f"llm:{_llm_model()}"
        else:
            fields, provider_issues = rules.extract(parsed, document.id, doc_type), []
            provider_name = rules.VERSION
    except ExtractionProviderError as exc:
        proc.status, proc.error, proc.updated_at = "failed", str(exc)[:500], now()
        db.flush()
        raise
    proc.status, proc.error, proc.classification, proc.updated_at = "processed", None, classification, now()
    typed(doc_type, fields)  # schema validation of the provider output (raises on violation)
    for f in fields:
        if f.value is not None and f.method.startswith("rules"):
            f.grounding, problems = grounding.ground(f, parsed, document.id)  # type: ignore[assignment]
            f.issues.extend(problems)
    extraction = existing or Extraction(id=str(uuid4()), document_id=document.id, tenant_id=document.tenant_id,
                                        version=0, created_at=now())
    if existing is None:
        db.add(extraction)
    previous_status = existing.status if existing else None
    extraction.doc_type, extraction.schema_version = doc_type, SCHEMA_VERSION
    extraction.provider, extraction.status = provider_name, "pending"
    extraction.version += 1
    extraction.updated_at = now()
    extraction.reviewed_by = extraction.reviewed_at = extraction.review_comment = None
    doc_issues = [i.model_dump() | {"origin": "extraction"} for i in provider_issues]
    if doc_type == "unknown":
        doc_issues.append(ValidationIssue(field="document_type", code="document_type_unknown", severity="warning",
                                          message=f"classifier decision: {classification['decision']}; "
                                                  "only contracts and invoices are extracted").model_dump()
                          | {"origin": "extraction"})
    extraction.issues = doc_issues
    db.flush()
    db.execute(delete(ExtractionField).where(ExtractionField.extraction_id == extraction.id))
    for position, f in enumerate(fields):
        db.add(ExtractionField(
            extraction_id=extraction.id, name=f.name, position=position, value_type=f.value_type,
            value=f.value, raw=f.raw, evidence=f.evidence.model_dump(mode="json") if f.evidence else None,
            method=f.method, grounding=f.grounding,
            issues=[i.model_dump() | {"origin": "extraction"} for i in f.issues]))
    db.flush()
    revalidate(db, extraction, parsed)
    record(db, user, extraction, "reprocess" if existing else "process",
           comment=f"provider={provider_name}; type={doc_type}"
                   + (f"; forced from {previous_status}" if force and previous_status in ("approved", "rejected") else "")
                   + (f"; type set by user (classifier: {classification['type']})" if doc_type_override else ""))
    return extraction


def _llm_model() -> str:
    from ..config import settings

    return settings.openai_model


def record(db: Session, user: User, extraction: Extraction, action: str, field_name: str | None = None,
           old: str | None = None, new: str | None = None, comment: str | None = None) -> None:
    """Review history + the global audit trail (no document text in either)."""
    db.add(ReviewEvent(extraction_id=extraction.id, document_id=extraction.document_id,
                       tenant_id=extraction.tenant_id, user_id=user.id, action=action,
                       field_name=field_name, old_value=old, new_value=new, comment=comment,
                       extraction_version=extraction.version, created_at=now()))
    db.add(AuditEvent(user_id=user.id, tenant_id=user.tenant_id, action=f"extraction_{action}",
                      document_id=extraction.document_id))
    log.info("extraction %s %s by %s (v%s)", extraction.id, action, user.id, extraction.version)


def serialize(db: Session, extraction: Extraction) -> dict:
    fields = list(db.scalars(select(ExtractionField).where(ExtractionField.extraction_id == extraction.id)
                             .order_by(ExtractionField.position, ExtractionField.id)))
    out_fields = []
    for f in fields:
        value, evidence, ground = effective(f)
        out_fields.append({
            "name": f.name, "value_type": f.value_type, "value": value, "extracted_value": f.value,
            "raw": f.raw, "method": f.method, "evidence": evidence, "extracted_evidence": f.evidence,
            "grounding": ground, "extracted_grounding": f.grounding, "issues": f.issues or [],
            "state": field_state(f, f.issues or []),
            "corrected": f.corrected_by is not None, "corrected_by": f.corrected_by,
            "corrected_at": f.corrected_at.isoformat() if f.corrected_at else None,
        })
    return {"id": extraction.id, "document_id": extraction.document_id, "doc_type": extraction.doc_type,
            "schema_version": extraction.schema_version, "provider": extraction.provider,
            "status": extraction.status, "version": extraction.version, "issues": extraction.issues or [],
            "fields": out_fields, "reviewed_by": extraction.reviewed_by,
            "reviewed_at": extraction.reviewed_at.isoformat() if extraction.reviewed_at else None,
            "review_comment": extraction.review_comment,
            "updated_at": extraction.updated_at.isoformat() if extraction.updated_at else None,
            "note": "grounding is exact / normalized string matching against the cited evidence; "
                    "no field carries a probability"}


def to_extracted(f: ExtractionField) -> ExtractedField:
    value, evidence, ground = effective(f)
    return ExtractedField(name=f.name, value_type=f.value_type, value=value, raw=f.raw,
                          evidence=Evidence.model_validate(evidence) if evidence else None,
                          method=f.method, grounding=ground)


def audit_view(db: Session, user: User, document_id: str | None, action: str) -> None:
    db.add(AuditEvent(user_id=user.id, tenant_id=user.tenant_id, action=action, document_id=document_id))


def mark_failed(db: Session, document: Document, parsed: ParsedDocument | None, exc: Exception) -> None:
    """Record a processing failure (after a rollback) without touching the stored document."""
    row = db.get(DocumentProcessing, document.id)
    if row is None:
        row = DocumentProcessing(document_id=document.id, attempts=0, doc_format=parsed.format if parsed else "?",
                                 parser=(parsed.parser if parsed else "?")[:200],
                                 page_count=len(parsed.pages) if parsed else 0,
                                 ocr_pages=parsed.ocr_pages if parsed else [],
                                 structure=parsed.model_dump(mode="json") if parsed else {})
        db.add(row)
    row.status, row.error = "failed", f"{type(exc).__name__}: {str(exc)[:400]}"
    row.attempts += 1
    row.updated_at = now()
    log.warning("document %s processing failed: %s", document.id, type(exc).__name__)
