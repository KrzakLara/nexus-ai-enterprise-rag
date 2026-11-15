"""Document lifecycle services (Phase 4D): upload, versions, soft delete -> purge, retention,
job submission, and the inline / queued execution switch shared by every route.

Database / object-storage consistency (PostgreSQL and S3 have no common transaction):
  upload        1. validate  2. PUT object (server-side SHA-256 check)  3. ONE DB transaction:
                document + version + grants + audit + ingest job.
                * PUT fails            -> HTTP 503, nothing in the database
                * DB transaction fails -> the object is deleted (best effort); a leftover object
                  without a version row is an orphan that scripts/storage_reconcile.py removes
                * duplicate request    -> Idempotency-Key header returns the original document
  processing    the worker downloads and verifies the checksum; crashes are retried from the
                last committed checkpoint
  delete        ONE transaction: purge job + soft delete (invisible to every query and to RLS
                immediately); the worker then removes derived data, objects, and keeps a tombstone
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import Depends, HTTPException
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .database import get_db, worker_engine
from .embeddings import get_embedder
from .jobs import queue
from .jobs.runner import JobOutcome, run_inline
from .models import (AuditEvent, Document, DocumentGrant, DocumentVersion, ProcessingJob, User)
from .observability.metrics import DOCUMENTS_DELETED, DOCUMENTS_UPLOADED
from .runtime import get_settings
from .storage import StorageError, default_store, object_key
from .uploads import ValidatedUpload

IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def get_worker_sessions(db: Session = Depends(get_db)) -> sessionmaker:
    """Sessions for inline job execution: the worker role when WORKER_DATABASE_URL is set,
    otherwise the API's own engine (local demo, SQLite tests, evaluation harness)."""
    cfg = get_settings()
    bind = worker_engine() if cfg.worker_database_url else db.get_bind()
    return sessionmaker(bind=bind, expire_on_commit=False)


def checked_idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    if not IDEMPOTENCY_KEY.match(value):
        raise HTTPException(400, "Idempotency-Key must be 8-128 characters of [A-Za-z0-9._:-]")
    return value


def retention_until(days: int | None, cfg: Settings) -> datetime | None:
    days = days if days is not None else cfg.default_retention_days
    if days is None:
        return None
    if not 1 <= days <= cfg.max_retention_days:
        raise HTTPException(422, f"retention_days must be between 1 and {cfg.max_retention_days}")
    return now_utc() + timedelta(days=days)


def put_original(cfg: Settings, tenant_id: str, document_id: str, version: int,
                 upload: ValidatedUpload, data: bytes) -> tuple[str, str]:
    store = default_store(cfg)
    key = object_key(tenant_id, document_id, version)
    try:
        store.put(key, data, upload.content_type, upload.sha256,
                  {"document-id": document_id, "tenant-id": tenant_id, "version": str(version)})
    except StorageError as exc:
        raise HTTPException(503, f"Object storage is unavailable; nothing was stored ({exc.code})") from None
    return store.backend, key


def discard_object(cfg: Settings, key: str) -> None:
    try:
        default_store(cfg).delete(key)
    except StorageError:
        pass  # orphan: removed later by scripts/storage_reconcile.py


def create_document(db: Session, user: User, upload: ValidatedUpload, groups: list[str], *,
                    document_id: str, backend: str, key: str, cfg: Settings,
                    retention: datetime | None, idempotency_key: str | None) -> tuple[Document, ProcessingJob]:
    now = now_utc()
    doc = Document(id=document_id, tenant_id=user.tenant_id, owner_id=user.id,
                   title=upload.filename.rsplit(".", 1)[0][:200], filename=upload.filename,
                   storage_key=key, sha256=upload.sha256, page_count=0, extracted_fields={},
                   created_at=now, lifecycle_state="uploaded", storage_backend=backend,
                   content_type=upload.content_type, size_bytes=upload.size, current_version=1,
                   updated_at=now, retention_until=retention)
    db.add(doc)
    db.flush()  # parent row first (FK children below)
    for group in groups:
        db.add(DocumentGrant(document_id=document_id, group_name=group))
    db.add(DocumentVersion(id=str(uuid4()), document_id=document_id, tenant_id=user.tenant_id, version=1,
                           storage_backend=backend, storage_key=key, sha256=upload.sha256,
                           size_bytes=upload.size, content_type=upload.content_type,
                           original_filename=upload.filename, state="stored", created_by=user.id,
                           created_at=now))
    db.add(AuditEvent(user_id=user.id, tenant_id=user.tenant_id, action="upload", document_id=document_id))
    job = queue.enqueue(db, tenant_id=user.tenant_id, user_id=user.id, job_type="ingest",
                        document_id=document_id, document_version=1,
                        payload={"version": 1, "reason": "upload"}, idempotency_key=idempotency_key,
                        max_attempts=cfg.job_max_attempts)
    DOCUMENTS_UPLOADED.labels(upload.format).inc()
    return doc, job


def next_version_number(db: Session, doc: Document) -> int:
    latest = db.scalar(select(func.max(DocumentVersion.version)).where(DocumentVersion.document_id == doc.id))
    return int(latest or doc.current_version or 1) + 1


def create_version(db: Session, user: User, doc: Document, upload: ValidatedUpload, *, number: int,
                   backend: str, key: str, cfg: Settings) -> tuple[DocumentVersion, ProcessingJob]:
    version = DocumentVersion(id=str(uuid4()), document_id=doc.id, tenant_id=doc.tenant_id, version=number,
                              storage_backend=backend, storage_key=key, sha256=upload.sha256,
                              size_bytes=upload.size, content_type=upload.content_type,
                              original_filename=upload.filename, state="stored", created_by=user.id,
                              created_at=now_utc())
    db.add(version)
    db.add(AuditEvent(user_id=user.id, tenant_id=user.tenant_id, action="document_version_upload",
                      document_id=doc.id))
    job = queue.enqueue(db, tenant_id=doc.tenant_id, user_id=user.id, job_type="ingest", document_id=doc.id,
                        document_version=number, payload={"version": number, "reason": "new_version"},
                        max_attempts=cfg.job_max_attempts)
    return version, job


def soft_delete(db: Session, user: User, doc: Document, cfg: Settings) -> ProcessingJob:
    """Immediately invisible; purge scheduled. The purge job is inserted BEFORE the soft delete
    (it references a document the user can still see)."""
    key = f"purge:{doc.id}"
    job = queue.by_idempotency_key(db, doc.tenant_id, key) or queue.enqueue(
        db, tenant_id=doc.tenant_id, user_id=user.id, job_type="purge", document_id=doc.id,
        payload={"reason": "user_delete"}, idempotency_key=key, priority=50,
        max_attempts=max(cfg.job_max_attempts, 10),
        run_after=now_utc() + timedelta(seconds=cfg.deletion_grace_seconds))
    now = now_utc()
    db.add(AuditEvent(user_id=user.id, tenant_id=user.tenant_id, action="document_delete", document_id=doc.id))
    db.flush()
    if db.get_bind().dialect.name == "postgresql":
        # RLS: the soft-deleted row would no longer satisfy the SELECT policy, so the change is
        # made by nexus_rls.soft_delete_document(), which re-applies the owner / admin rule.
        if not db.execute(text("SELECT nexus_rls.soft_delete_document(:d)"), {"d": doc.id}).scalar():
            raise HTTPException(403, "Only the owner or an admin can delete this document")
    else:
        db.execute(update(Document).where(Document.id == doc.id, Document.deleted_at.is_(None))
                   .values(deleted_at=now, deleted_by=user.id, lifecycle_state="deleted", updated_at=now))
    DOCUMENTS_DELETED.labels("user").inc()
    return job


def submit(db: Session, user: User, job_type: str, doc: Document, payload: dict, cfg: Settings,
           dedupe: bool = True) -> tuple[ProcessingJob, bool]:
    """Enqueue (or return the unfinished job of the same type for this document)."""
    if dedupe:
        existing = queue.active_job(db, doc.id, job_type)
        if existing is not None:
            return existing, False
    job = queue.enqueue(db, tenant_id=user.tenant_id, user_id=user.id, job_type=job_type, document_id=doc.id,
                        document_version=doc.current_version, payload=payload,
                        max_attempts=cfg.job_max_attempts)
    return job, True


def execute_inline(job_id: str, cfg: Settings, sessions: sessionmaker, embedder=None,
                   preparsed=None, preloaded: bytes | None = None) -> JobOutcome:
    factory = (lambda: embedder) if embedder is not None else get_embedder
    return run_inline(job_id, cfg=cfg, sessions=sessions, embedder_factory=factory,
                      preparsed=preparsed, preloaded=preloaded)


def latest_jobs(db: Session, document_ids: list[str]) -> dict[str, ProcessingJob]:
    if not document_ids:
        return {}
    ranked = (select(ProcessingJob.id, func.row_number().over(
        partition_by=ProcessingJob.document_id, order_by=ProcessingJob.created_at.desc()).label("n"))
        .where(ProcessingJob.document_id.in_(document_ids), ProcessingJob.job_type != "purge")
        .subquery())
    rows = db.scalars(select(ProcessingJob).join(ranked, ranked.c.id == ProcessingJob.id).where(ranked.c.n == 1))
    return {job.document_id: job for job in rows}


def lifecycle_view(doc: Document, job: ProcessingJob | None = None) -> dict:
    def iso(value):
        value = _aware(value)
        return value.isoformat() if value else None

    processing = None
    if job is not None:
        processing = {"job_id": job.id, "type": job.job_type, "status": job.status, "stage": job.stage,
                      "attempts": job.attempts, "max_attempts": job.max_attempts,
                      "error_code": job.error_code, "error_message": job.error_message,
                      "updated_at": iso(job.updated_at),
                      "retry_available": job.status in ("failed", "cancelled")}
    return {"lifecycle_state": doc.lifecycle_state, "current_version": doc.current_version,
            "size_bytes": doc.size_bytes, "content_type": doc.content_type,
            "storage_backend": doc.storage_backend, "retention_until": iso(doc.retention_until),
            "updated_at": iso(doc.updated_at), "processing": processing}
