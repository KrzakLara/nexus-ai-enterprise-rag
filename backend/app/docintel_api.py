"""Document-intelligence HTTP API (Phase 3). Mounted by main.py; the RAG endpoints are unchanged.

All routes are plain `def`: FastAPI runs them in its worker threadpool, so parsing, OCR and
extraction never block the event loop. Every route resolves documents through the same ACL as
retrieval (security.authorized_documents). A document or extraction the user may not read gets
HTTP 404, exactly like a non-existent one.
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .auth import current_user
from .config import settings
from .database import get_db
from .docintel import compare as cmp
from .docintel import review as rv
from .docintel import service
from . import lifecycle
from .jobs import queue
from .lifecycle import get_worker_sessions
from .models import Document, DocumentProcessing, Extraction, ExtractionField, User
from .observability.metrics import REVIEW_ACTIONS
from .runtime import get_settings
from .security import authorized_documents, permitted_document

router = APIRouter(tags=["document intelligence"])


class ProcessIn(BaseModel):
    provider: Literal["rules", "llm"] = "rules"
    force: bool = False                      # re-process an approved / rejected extraction
    doc_type: Literal["contract", "invoice"] | None = None   # explicit human override of the classifier
    reparse: bool = False                    # parse the stored original again (e.g. after enabling OCR)


class Correction(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    value: str | None = Field(default=None, max_length=2000)   # None clears the value
    evidence_quote: str | None = Field(default=None, max_length=2000)
    page: int | None = Field(default=None, ge=1)
    comment: str | None = Field(default=None, max_length=1000)


class CorrectIn(BaseModel):
    expected_version: int
    corrections: list[Correction] = Field(min_length=1, max_length=100)


class DecisionIn(BaseModel):
    expected_version: int
    comment: str | None = Field(default=None, max_length=2000)
    override_reason: str | None = Field(default=None, max_length=2000)


class CompareIn(BaseModel):
    document_ids: list[str] = Field(min_length=2, max_length=10)
    fields: list[str] | None = Field(default=None, max_length=30)


class QueryIn(BaseModel):
    type: Literal["expiring_within", "auto_renewal", "invalid_invoices", "conflicts"]
    days: int = Field(default=90, ge=1, le=3650)
    as_of: date | None = None
    document_ids: list[str] | None = Field(default=None, max_length=200)


class AskIn(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    as_of: date | None = None
    document_ids: list[str] | None = Field(default=None, max_length=200)


def _doc(db: Session, user: User, document_id: str) -> Document:
    doc = permitted_document(db, user, document_id)
    if doc is None:
        raise HTTPException(404, "Document not found")
    return doc


def _extraction(db: Session, user: User, extraction_id: str) -> tuple[Extraction, Document]:
    found = rv.authorized_extraction(db, user, extraction_id)
    if found is None:
        raise HTTPException(404, "Extraction not found")
    return found


def _review_call(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except rv.ReviewError as exc:
        raise HTTPException(exc.status, {"message": exc.message, **exc.extra}) from None


def summary(db: Session, doc: Document) -> dict:
    proc = db.get(DocumentProcessing, doc.id)
    ext = db.scalar(select(Extraction).where(Extraction.document_id == doc.id))
    return {
        "processing_status": proc.status if proc else "not_processed",
        "format": proc.doc_format if proc else None,
        "ocr_pages": proc.ocr_pages if proc else [],
        "doc_type": ext.doc_type if ext else ((proc.classification or {}).get("type") if proc else None),
        "extraction_id": ext.id if ext else None,
        "extraction_status": ext.status if ext else None,
        "review_status": ext.status if ext and ext.status in ("approved", "rejected") else
                         ("awaiting_review" if ext else None),
    }


@router.get("/api/intelligence/documents")
def library(user: User = Depends(current_user), db: Session = Depends(get_db)):
    docs = db.scalars(authorized_documents(user).order_by(Document.created_at.desc())).all()
    return [{"id": d.id, "title": d.title, "filename": d.filename, "pages": d.page_count,
             "created_at": d.created_at.isoformat(), **summary(db, d)} for d in docs]


@router.post("/api/documents/{document_id}/process")
def process_document(document_id: str, payload: ProcessIn, response: Response,
                     user: User = Depends(current_user), db: Session = Depends(get_db),
                     sessions: sessionmaker = Depends(get_worker_sessions)):
    """(Re-)run extraction as a durable `reprocess` job. Inline mode returns the extraction (v0.3
    contract); queue mode answers 202 with the job to poll."""
    cfg = get_settings()
    doc = _doc(db, user, document_id)
    if not (rv.can_review(user) or doc.owner_id == user.id):
        raise HTTPException(403, "Only the owner, a reviewer or an admin can (re-)process a document")
    if payload.provider == "llm" and not settings.openai_api_key:
        raise HTTPException(409, "LLM extraction is not configured (OPENAI_API_KEY); use provider 'rules'")
    existing = db.scalar(select(Extraction).where(Extraction.document_id == doc.id))
    if existing and existing.status in ("approved", "rejected") and not payload.force:
        raise HTTPException(409, f"extraction is {existing.status}; pass force=true to re-process")
    job, _ = lifecycle.submit(db, user, "reprocess", doc, payload.model_dump(), cfg, dedupe=False)
    db.commit()
    if cfg.processing_mode != "inline":
        response.status_code = 202
        return {"document_id": doc.id, "job": queue.view(job)}
    outcome = lifecycle.execute_inline(job.id, cfg, sessions)
    if outcome.status != "completed":
        raise HTTPException(outcome.http_status if outcome.http_status >= 400 else 500,
                            outcome.error_message or outcome.error_code or "Processing failed")
    db.expire_all()
    extraction = db.scalar(select(Extraction).where(Extraction.document_id == doc.id))
    return service.serialize(db, extraction) | {"processing": summary(db, doc), "job_id": job.id}


@router.get("/api/documents/{document_id}/processing")
def processing_status(document_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    doc = _doc(db, user, document_id)
    proc = db.get(DocumentProcessing, doc.id)
    if proc is None:
        return {"document_id": doc.id, "status": "not_processed"}
    return {"document_id": doc.id, "status": proc.status, "format": proc.doc_format, "parser": proc.parser,
            "page_count": proc.page_count, "ocr_pages": proc.ocr_pages, "classification": proc.classification,
            "error": proc.error, "attempts": proc.attempts, "updated_at": proc.updated_at.isoformat()}


@router.get("/api/documents/{document_id}/structure")
def document_structure(document_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    doc = _doc(db, user, document_id)
    proc = db.get(DocumentProcessing, doc.id)
    if proc is None:
        raise HTTPException(404, "Document has not been processed")
    service.audit_view(db, user, doc.id, "structure_view")
    db.commit()
    return {"document_id": doc.id, "title": doc.title, **proc.structure}


@router.get("/api/documents/{document_id}/extraction")
def document_extraction(document_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    doc = _doc(db, user, document_id)
    ext = db.scalar(select(Extraction).where(Extraction.document_id == doc.id))
    if ext is None:
        raise HTTPException(404, "No extraction for this document")
    service.audit_view(db, user, doc.id, "extraction_view")
    db.commit()
    return service.serialize(db, ext) | {"can_review": rv.can_review(user)}


@router.get("/api/extractions/{extraction_id}/fields/{name}/evidence")
def field_evidence(extraction_id: str, name: str, user: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    ext, doc = _extraction(db, user, extraction_id)
    f = db.scalar(select(ExtractionField).where(ExtractionField.extraction_id == ext.id,
                                                ExtractionField.name == name))
    if f is None:
        raise HTTPException(404, "Field not found")
    value, evidence, ground = service.effective(f)
    parsed = service.load_parsed(db, doc.id)
    page_text = None
    if evidence and parsed and 1 <= evidence.get("page", 0) <= len(parsed.pages):
        page_text = parsed.pages[evidence["page"] - 1].text
    return {"field": name, "value": value, "grounding": ground, "evidence": evidence,
            "extracted_evidence": f.evidence, "page_text": page_text}


@router.post("/api/extractions/{extraction_id}/validate")
def validate_extraction(extraction_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ext, doc = _extraction(db, user, extraction_id)
    issues = service.revalidate(db, ext, service.load_parsed(db, doc.id))
    body = {"extraction_id": ext.id, "status": ext.status, "version": ext.version, "issues": issues}
    if rv.can_review(user):
        db.commit()  # reviewers persist the recomputed issues / status
    else:
        db.rollback()  # readers get the result; RLS lets only reviewers write extractions
        body["persisted"] = False
    return body


@router.patch("/api/extractions/{extraction_id}/fields")
def correct_fields(extraction_id: str, payload: CorrectIn, user: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    ext, _ = _extraction(db, user, extraction_id)
    _review_call(rv.correct, db, user, ext, [c.model_dump() for c in payload.corrections], payload.expected_version)
    db.commit()
    REVIEW_ACTIONS.labels("correct").inc()
    return service.serialize(db, ext)


@router.post("/api/extractions/{extraction_id}/approve")
def approve_extraction(extraction_id: str, payload: DecisionIn, user: User = Depends(current_user),
                       db: Session = Depends(get_db)):
    ext, _ = _extraction(db, user, extraction_id)
    _review_call(rv.approve, db, user, ext, payload.expected_version, payload.comment, payload.override_reason)
    db.commit()
    REVIEW_ACTIONS.labels("approve").inc()
    return service.serialize(db, ext)


@router.post("/api/extractions/{extraction_id}/reject")
def reject_extraction(extraction_id: str, payload: DecisionIn, user: User = Depends(current_user),
                      db: Session = Depends(get_db)):
    ext, _ = _extraction(db, user, extraction_id)
    _review_call(rv.reject, db, user, ext, payload.expected_version, payload.comment or "")
    db.commit()
    REVIEW_ACTIONS.labels("reject").inc()
    return service.serialize(db, ext)


@router.get("/api/extractions/{extraction_id}/history")
def review_history(extraction_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ext, _ = _extraction(db, user, extraction_id)
    return {"extraction_id": ext.id, "events": rv.history(db, ext)}


def _cmp_call(fn, *args):
    try:
        return fn(*args)
    except cmp.ComparisonError as exc:
        raise HTTPException(exc.status, exc.message) from None


@router.post("/api/intelligence/compare")
def compare_documents(payload: CompareIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    result = _cmp_call(cmp.compare, db, user, payload.document_ids, payload.fields)
    service.audit_view(db, user, None, "intelligence_compare")
    db.commit()
    return result


@router.post("/api/intelligence/query")
def structured_query(payload: QueryIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    as_of = payload.as_of or date.today()
    ids = payload.document_ids
    fn = {"expiring_within": lambda: cmp.expiring_within(db, user, payload.days, as_of, ids),
          "auto_renewal": lambda: cmp.auto_renewal(db, user, ids),
          "invalid_invoices": lambda: cmp.invalid_invoices(db, user, ids),
          "conflicts": lambda: cmp.conflicts(db, user, ids)}[payload.type]
    result = _cmp_call(fn)
    service.audit_view(db, user, None, "intelligence_query")
    db.commit()
    return result


@router.post("/api/intelligence/ask")
def intelligence_ask(payload: AskIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    result = _cmp_call(cmp.answer_question, db, user, payload.question, payload.as_of or date.today(),
                       payload.document_ids)
    service.audit_view(db, user, None, "intelligence_ask")
    db.commit()
    return result
