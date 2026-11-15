"""Document lifecycle and job HTTP API (Phase 4C / 4D).

Authorization: every document route resolves the document through the ACL
(security.permitted_document; PostgreSQL RLS enforces the same rule underneath). A document the
user may not read, a deleted one and a non-existent one all return the same 404.
    read versions / jobs / download         any reader
    new version, delete, retention          owner or tenant admin
    reindex                                 owner, reviewer or admin
    job retry / cancel                      the submitter or a tenant admin
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from . import lifecycle
from .auth import current_user
from .database import get_db
from .docintel.parsing import parse_document
from .embeddings import Embedder, get_embedder
from .jobs import queue
from .models import Document, DocumentVersion, ProcessingJob, User
from .runtime import get_settings
from .security import audit, authorized_documents, can_manage, permitted_document
from .storage import ObjectMissing, StorageError, store_for
from .uploads import UploadRejected, validate_upload

router = APIRouter(tags=["document lifecycle"])
REVIEWER_ROLES = frozenset({"admin", "reviewer"})


class ReindexIn(BaseModel):
    force: bool = False


class RetentionIn(BaseModel):
    retention_days: int | None = Field(default=None, ge=1, le=36500)  # None clears the retention date


def _doc(db: Session, user: User, document_id: str) -> Document:
    doc = permitted_document(db, user, document_id)
    if doc is None:
        raise HTTPException(404, "Document not found")
    return doc


def content_disposition(filename: str) -> str:
    from urllib.parse import quote

    ascii_name = "".join(c if c.isascii() and c.isprintable() and c not in '"\\;' else "_" for c in filename)[:150]
    return f"attachment; filename=\"{ascii_name or 'document'}\"; filename*=UTF-8''{quote(filename[:150])}"


def stream_object(backend: str, key: str, filename: str, content_type: str | None) -> StreamingResponse:
    cfg = get_settings()
    try:
        chunks = store_for(backend, cfg).stream(key)
    except ObjectMissing:
        raise HTTPException(404, "Original file missing") from None
    except StorageError:
        raise HTTPException(503, "Object storage is unavailable") from None
    return StreamingResponse(chunks, media_type=(content_type or "application/octet-stream").split(";")[0],
                             headers={"Content-Disposition": content_disposition(filename),
                                      "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})


def job_response(outcome, job: ProcessingJob, db: Session) -> dict:
    db.expire_all()
    fresh = db.get(ProcessingJob, job.id) or job
    return queue.view(fresh) | {"inline_outcome": outcome.status if outcome else None}


# ----------------------------------------------------------------------------- versions

@router.get("/api/documents/{document_id}/versions")
def list_versions(document_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    doc = _doc(db, user, document_id)
    rows = db.scalars(select(DocumentVersion).where(DocumentVersion.document_id == doc.id)
                      .order_by(DocumentVersion.version.desc())).all()
    return {"document_id": doc.id, "current_version": doc.current_version, "versions": [
        {"version": v.version, "state": v.state, "filename": v.original_filename, "sha256": v.sha256,
         "size_bytes": v.size_bytes, "content_type": v.content_type, "created_by": v.created_by,
         "created_at": lifecycle._aware(v.created_at).isoformat()} for v in rows]}


@router.post("/api/documents/{document_id}/versions", status_code=202)
def upload_version(document_id: str, file: UploadFile = File(...), user: User = Depends(current_user),
                   db: Session = Depends(get_db), sessions: sessionmaker = Depends(lifecycle.get_worker_sessions),
                   embedder: Embedder = Depends(get_embedder)):
    cfg = get_settings()
    doc = _doc(db, user, document_id)
    if not can_manage(user, doc):
        raise HTTPException(403, "Only the owner or an admin can upload a new version")
    if queue.active_job(db, doc.id, "ingest") is not None:
        raise HTTPException(409, "A version of this document is still being processed")
    data = file.file.read(cfg.max_upload_bytes + 1)
    try:
        upload = validate_upload(file.filename, data, cfg)
    except UploadRejected as exc:
        raise HTTPException(exc.status, exc.message) from None
    parsed = None
    if cfg.processing_mode == "inline":  # v0.3 contract: unparseable input stores nothing
        try:
            parsed = parse_document(upload.filename, data)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(422, f"Document could not be parsed: {str(exc)[:180]}") from None
    number = lifecycle.next_version_number(db, doc)
    backend, key = lifecycle.put_original(cfg, doc.tenant_id, doc.id, number, upload, data)
    try:
        version, job = lifecycle.create_version(db, user, doc, upload, number=number, backend=backend,
                                                key=key, cfg=cfg)
        db.commit()
    except Exception:
        db.rollback()
        lifecycle.discard_object(cfg, key)
        raise
    outcome = None
    if cfg.processing_mode == "inline":
        outcome = lifecycle.execute_inline(job.id, cfg, sessions, embedder, preparsed=parsed, preloaded=data)
    return {"document_id": doc.id, "version": number, "job": job_response(outcome, job, db)}


@router.get("/api/documents/{document_id}/versions/{number}/source")
def download_version(document_id: str, number: int, user: User = Depends(current_user),
                     db: Session = Depends(get_db)):
    doc = _doc(db, user, document_id)
    version = db.scalar(select(DocumentVersion).where(DocumentVersion.document_id == doc.id,
                                                      DocumentVersion.version == number))
    if version is None or version.object_deleted_at is not None:
        raise HTTPException(404, "Version not found")
    audit(db, user, "source_download", doc.id)
    db.commit()
    return stream_object(version.storage_backend, version.storage_key, version.original_filename,
                         version.content_type)


@router.get("/api/documents/{document_id}/download-url")
def presigned_download(document_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Short-lived presigned GET URL, issued only after the ACL check (PRESIGNED_DOWNLOADS=true)."""
    cfg = get_settings()
    doc = _doc(db, user, document_id)
    if not cfg.presigned_downloads or doc.storage_backend != "s3":
        raise HTTPException(404, "Presigned downloads are disabled; use /source")
    try:
        url = store_for("s3", cfg).presigned_get(doc.storage_key, doc.filename, doc.content_type,
                                                 cfg.presigned_ttl_seconds)
    except StorageError:
        raise HTTPException(503, "Object storage is unavailable") from None
    audit(db, user, "source_presigned", doc.id)
    db.commit()
    return {"url": url, "expires_in": cfg.presigned_ttl_seconds}


# ----------------------------------------------------------------------------- delete / retention

@router.delete("/api/documents/{document_id}", status_code=202)
def delete_document(document_id: str, user: User = Depends(current_user), db: Session = Depends(get_db),
                    sessions: sessionmaker = Depends(lifecycle.get_worker_sessions)):
    cfg = get_settings()
    doc = _doc(db, user, document_id)
    if not can_manage(user, doc):
        raise HTTPException(403, "Only the owner or an admin can delete this document")
    job = lifecycle.soft_delete(db, user, doc, cfg)
    db.commit()
    outcome = None
    if cfg.processing_mode == "inline" and cfg.deletion_grace_seconds == 0:
        outcome = lifecycle.execute_inline(job.id, cfg, sessions)
    return {"document_id": document_id, "deleted": True, "purge_job": job_response(outcome, job, db)}


@router.patch("/api/documents/{document_id}/retention")
def set_retention(document_id: str, payload: RetentionIn, user: User = Depends(current_user),
                  db: Session = Depends(get_db)):
    cfg = get_settings()
    doc = _doc(db, user, document_id)
    if not can_manage(user, doc):
        raise HTTPException(403, "Only the owner or an admin can change the retention")
    doc.retention_until = lifecycle.retention_until(payload.retention_days, cfg) if payload.retention_days else None
    doc.updated_at = lifecycle.now_utc()
    audit(db, user, "retention_change", doc.id)
    db.commit()
    return {"document_id": doc.id, "retention_until": doc.retention_until.isoformat() if doc.retention_until else None}


# ----------------------------------------------------------------------------- reindex

@router.post("/api/documents/{document_id}/reindex", status_code=202)
def reindex_document(document_id: str, payload: ReindexIn | None = None, user: User = Depends(current_user),
                     db: Session = Depends(get_db), sessions: sessionmaker = Depends(lifecycle.get_worker_sessions),
                     embedder: Embedder = Depends(get_embedder)):
    cfg = get_settings()
    doc = _doc(db, user, document_id)
    if not (can_manage(user, doc) or user.role in REVIEWER_ROLES):
        raise HTTPException(403, "Only the owner, a reviewer or an admin can re-index a document")
    job, created = lifecycle.submit(db, user, "reindex", doc, {"force": bool(payload and payload.force)}, cfg)
    if created:
        audit(db, user, "reindex_request", doc.id)
    db.commit()
    outcome = None
    if created and cfg.processing_mode == "inline":
        outcome = lifecycle.execute_inline(job.id, cfg, sessions, embedder)
    return {"document_id": doc.id, "deduplicated": not created, "job": job_response(outcome, job, db)}


@router.post("/api/admin/reindex", status_code=202)
def reindex_tenant(payload: ReindexIn | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Tenant-wide re-index (e.g. after an embedding model change). Always queued."""
    cfg = get_settings()
    if user.role != "admin":
        raise HTTPException(403, "Admin role required")
    docs = db.scalars(authorized_documents(user).where(Document.lifecycle_state.in_(("ready", "failed")))
                      .limit(5000)).all()
    queued = 0
    for doc in docs:
        _, created = lifecycle.submit(db, user, "reindex", doc, {"force": bool(payload and payload.force)}, cfg)
        queued += int(created)
    audit(db, user, "tenant_reindex_request")
    db.commit()
    return {"documents": len(docs), "queued": queued, "processing_mode": cfg.processing_mode}


# ----------------------------------------------------------------------------- jobs

def _visible_jobs(db: Session, user: User):
    readable = authorized_documents(user).with_only_columns(Document.id)
    q = select(ProcessingJob).where(ProcessingJob.tenant_id == user.tenant_id)
    if user.role != "admin":
        q = q.where(or_(ProcessingJob.user_id == user.id, ProcessingJob.document_id.in_(readable)))
    return q


def _job(db: Session, user: User, job_id: str) -> ProcessingJob:
    job = db.scalar(_visible_jobs(db, user).where(ProcessingJob.id == job_id))
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


@router.get("/api/jobs")
def list_jobs(status: str | None = None, document_id: str | None = None, limit: int = 50,
              user: User = Depends(current_user), db: Session = Depends(get_db)):
    q = _visible_jobs(db, user)
    if status:
        q = q.where(ProcessingJob.status == status)
    if document_id:
        q = q.where(ProcessingJob.document_id == document_id)
    rows = db.scalars(q.order_by(ProcessingJob.created_at.desc()).limit(max(1, min(limit, 200)))).all()
    return [queue.view(j) for j in rows]


@router.get("/api/jobs/{job_id}")
def get_job(job_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return queue.view(_job(db, user, job_id))


@router.get("/api/documents/{document_id}/jobs")
def document_jobs(document_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    doc = _doc(db, user, document_id)
    rows = db.scalars(select(ProcessingJob).where(ProcessingJob.document_id == doc.id)
                      .order_by(ProcessingJob.created_at.desc()).limit(20)).all()
    return [queue.view(j) for j in rows]


@router.post("/api/jobs/{job_id}/retry", status_code=202)
def retry_job(job_id: str, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db),
              sessions: sessionmaker = Depends(lifecycle.get_worker_sessions),
              embedder: Embedder = Depends(get_embedder)):
    cfg = get_settings()
    job = _job(db, user, job_id)
    if job.user_id != user.id and user.role != "admin":
        raise HTTPException(403, "Only the submitter or an admin can retry this job")
    if job.status not in ("failed", "cancelled"):
        raise HTTPException(409, f"Only failed or cancelled jobs can be retried (job is {job.status})")
    if job.job_type != "purge" and job.document_id and permitted_document(db, user, job.document_id) is None:
        raise HTTPException(409, "The document of this job is no longer available")
    queue.retry(db, job, cfg.job_max_attempts)
    audit(db, user, "job_retry", job.document_id)
    db.commit()
    outcome = lifecycle.execute_inline(job.id, cfg, sessions, embedder) if cfg.processing_mode == "inline" else None
    return job_response(outcome, job, db)


@router.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    job = _job(db, user, job_id)
    if job.user_id != user.id and user.role != "admin":
        raise HTTPException(403, "Only the submitter or an admin can cancel this job")
    if job.job_type == "purge":
        raise HTTPException(409, "A deletion cannot be cancelled once requested")
    if job.status in queue.FINAL_STATES:
        raise HTTPException(409, f"Job already {job.status}")
    status = queue.cancel(db, job)
    audit(db, user, "job_cancel", job.document_id)
    db.commit()
    return queue.view(job) | {"cancellation": "immediate" if status == "cancelled" else "requested"}
