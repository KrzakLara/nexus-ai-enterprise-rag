"""Job handlers: ingest (download -> parse incl. OCR -> chunk -> extract -> embed), reprocess
(document-intelligence extraction), reindex (embeddings) and purge (staged deletion).

Rules every handler follows
  * the worker database session carries the job's RLS scope (tenant + job id + lease token), so
    the handler can only read / write rows of THIS job's document;
  * CPU / network work (download, parsing, OCR, LLM calls) runs outside long transactions where
    possible; every write transaction first takes the fenced job-row lock (queue.lock), so a
    worker that lost its lease writes nothing;
  * user-submitted jobs re-check that the submitter may (still) act on the document
    (nexus_rls.user_document_access) before writing: a revoked grant between submission and
    execution fails the job with `authorization_revoked`;
  * a document deleted while queued or running cancels the job; nothing is written for it;
  * re-execution is idempotent: deterministic chunk ids, unchanged chunks are kept (so are their
    embeddings), embeddings are keyed by (chunk, model) with content hashes, extraction is an
    upsert, and the `checkpoint` in job.result skips completed stages on retry;
  * error messages are fixed categories plus our own parser messages, never document text.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..docintel import service as docintel
from ..docintel.llm import ExtractionProviderError
from ..docintel.parsing import UnsupportedFormat, parse_document
from ..docintel.structure import ParsedDocument
from ..embeddings import EmbeddingError
from ..indexing import embed_pending_chunks
from ..models import (AuditEvent, Chunk, ChunkEmbedding, Document, DocumentGrant, DocumentProcessing,
                      DocumentTable, DocumentVersion, Extraction, User)
from ..observability.metrics import (DOCUMENTS_PURGED, EMBEDDING_DURATION, EXTRACTIONS,
                                     INGESTION_DURATION, OCR_DURATION, OCR_PAGES, STAGE_DURATION)
from ..observability.tracing import span
from ..parsers import extract_contract_fields, split_page
from ..storage import ObjectMissing, StorageError, sha256_hex, store_for
from . import queue
from .queue import ClaimedJob

log = logging.getLogger("nexus.jobs")


class JobError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool, http_status: int):
        super().__init__(message)
        self.code, self.message, self.retryable, self.http_status = code, message, retryable, http_status


def permanent(code: str, message: str, http_status: int = 422) -> JobError:
    return JobError(code, message, retryable=False, http_status=http_status)


def transient(code: str, message: str, http_status: int = 503) -> JobError:
    return JobError(code, message, retryable=True, http_status=http_status)


class JobCancelled(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class JobContext:
    job: ClaimedJob
    cfg: Settings
    sessions: sessionmaker
    embedder_factory: Callable[[], object]
    preparsed: ParsedDocument | None = None
    preloaded: bytes | None = None
    lease_lost: threading.Event = field(default_factory=threading.Event)
    cancel_requested: threading.Event = field(default_factory=threading.Event)

    @contextmanager
    def session(self):
        db = self.sessions()
        try:
            queue.bind(db, self.job)
            yield db
        finally:
            db.close()

    def check(self) -> None:
        if self.lease_lost.is_set():
            raise queue.LeaseLost(f"lease of job {self.job.id} lost")
        if self.cancel_requested.is_set():
            raise JobCancelled("cancel_requested")

    @contextmanager
    def stage(self, name: str):
        self.check()
        with self.session() as db:
            queue.set_stage(db, self.job, name)
        start = time.perf_counter()
        with span(f"job.stage.{name}", **{"job.id": self.job.id, "job.type": self.job.job_type}):
            yield
        STAGE_DURATION.labels(name).observe(time.perf_counter() - start)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def user_access(db: Session, job: ClaimedJob, document_id: str) -> tuple[str, str | None]:
    """(access, role) of the job's submitter: none | read | owner | admin (system jobs: system)."""
    if job.user_id == "system":
        return "system", None
    if db.get_bind().dialect.name == "postgresql":
        row = db.execute(text("SELECT access, role FROM nexus_rls.user_document_access(:u, :d)"),
                         {"u": job.user_id, "d": document_id}).first()
        return (row[0], row[1]) if row else ("none", None)
    user, doc = db.get(User, job.user_id), db.get(Document, document_id)
    if user is None or doc is None or user.status != "active" or doc.tenant_id != user.tenant_id \
            or doc.deleted_at is not None:
        return "none", None
    if user.role == "admin":
        return "admin", user.role
    if doc.owner_id == user.id:
        return "owner", user.role
    groups = set(db.scalars(select(DocumentGrant.group_name).where(DocumentGrant.document_id == doc.id)))
    return ("read" if groups & set(user.groups or []) else "none"), user.role


def _document(db: Session, job: ClaimedJob, *, allow_deleted: bool = False) -> Document:
    doc = db.get(Document, job.document_id) if job.document_id else None
    if doc is None:
        raise permanent("document_missing", "The document no longer exists.", 404)
    if doc.deleted_at is not None and not allow_deleted:
        raise JobCancelled("document_deleted")
    return doc


def _version(db: Session, doc: Document, number: int) -> DocumentVersion:
    version = db.scalar(select(DocumentVersion).where(DocumentVersion.document_id == doc.id,
                                                      DocumentVersion.version == number))
    if version is None:
        raise permanent("version_missing", f"Version {number} of the document is not stored.", 404)
    return version


def _audit(db: Session, job: ClaimedJob, action: str) -> None:
    db.add(AuditEvent(user_id=job.user_id, tenant_id=job.tenant_id, action=action,
                      document_id=job.document_id, created_at=now_utc()))


def download(ctx: JobContext, version: DocumentVersion) -> bytes:
    if ctx.preloaded is not None and sha256_hex(ctx.preloaded) == version.sha256:
        return ctx.preloaded
    try:
        data = store_for(version.storage_backend, ctx.cfg).get(version.storage_key)
    except ObjectMissing:
        raise transient("object_missing", "The original file is missing from object storage.") from None
    except StorageError as exc:
        raise transient("storage_unavailable", f"Object storage is unavailable ({exc.code}).") from None
    if sha256_hex(data) != version.sha256:
        raise permanent("checksum_mismatch", "The stored original does not match its recorded SHA-256.", 500)
    return data


def parse(ctx: JobContext, filename: str, data: bytes) -> ParsedDocument:
    if ctx.preparsed is not None:
        return ctx.preparsed
    start = time.perf_counter()
    try:
        parsed = parse_document(filename, data)
    except UnsupportedFormat as exc:
        raise permanent("unsupported_format", str(exc)[:300], 415) from None
    except ValueError as exc:  # our parser's own messages (no document text)
        raise permanent("parse_failed", f"Document could not be parsed: {str(exc)[:180]}", 422) from None
    except Exception as exc:  # noqa: BLE001 - third-party parser failure: type name only
        raise permanent("parse_failed", f"Document could not be parsed: {type(exc).__name__}", 422) from None
    if parsed.ocr_pages:
        OCR_PAGES.inc(len(parsed.ocr_pages))
        OCR_DURATION.observe(time.perf_counter() - start)
    return parsed


def chunk_id(document_id: str, version: int, page: int, position: int) -> str:
    return str(uuid5(NAMESPACE_URL, f"nexus:{document_id}:v{version}:{page}:{position}"))


def replace_chunks(db: Session, doc: Document, version: int, pages: list[str]) -> tuple[int, bool]:
    desired = [(chunk_id(doc.id, version, p, i), p, i, piece)
               for p, page in enumerate(pages, 1) for i, piece in enumerate(split_page(page))]
    existing = {c.id: c.content for c in db.scalars(select(Chunk).where(Chunk.document_id == doc.id))}
    if existing and existing == {cid: content for cid, _, _, content in desired}:
        return len(desired), False  # idempotent retry: keep chunks and their embeddings
    db.execute(delete(Chunk).where(Chunk.document_id == doc.id))  # embeddings cascade
    db.add_all(Chunk(id=cid, document_id=doc.id, page_number=p, position=i, content=content)
               for cid, p, i, content in desired)
    db.flush()
    return len(desired), True


def _embed(ctx: JobContext, db: Session, doc: Document) -> tuple[str, int]:
    if not ctx.cfg.embed_on_upload:
        return "disabled", 0
    if db.get_bind().dialect.name != "postgresql":
        return "skipped_requires_postgresql", 0
    start = time.perf_counter()
    try:
        stats = embed_pending_chunks(db, ctx.embedder_factory(), document_ids=[doc.id])
    except EmbeddingError as exc:
        raise transient("embedding_unavailable", f"failed: {str(exc)[:160]}") from None
    EMBEDDING_DURATION.labels("passages").observe(time.perf_counter() - start)
    # chunks of this document that now have an up-to-date vector for the configured model
    return f"embedded:{stats.embedded + stats.skipped_up_to_date}", stats.embedded


# ----------------------------------------------------------------------------------- ingest

def ingest(ctx: JobContext) -> dict:
    job = ctx.job
    number = job.document_version or int(job.payload.get("version", 1))
    reason = job.payload.get("reason", "upload")
    checkpoint = (job.result or {}).get("checkpoint")
    started = time.perf_counter()
    with ctx.session() as db:
        queue.lock(db, job)
        doc = _document(db, job)
        version = _version(db, doc, number)
        access, _ = user_access(db, job, doc.id)
        if access == "none":
            raise permanent("authorization_revoked", "The submitting user can no longer access this document.", 403)
        filename, fmt_hint = version.original_filename, (version.content_type or "")
        if checkpoint != "indexed":
            doc.lifecycle_state, doc.updated_at = "processing", now_utc()
        db.commit()
    result = dict(job.result or {})
    if checkpoint != "indexed":
        with ctx.stage("downloading"):
            with ctx.session() as db:
                version = _version(db, _document(db, job), number)
                data = download(ctx, version)
        with ctx.stage("parsing"):
            parsed = parse(ctx, filename, data)
        with ctx.stage("indexing"):
            with ctx.session() as db:
                queue.lock(db, job)
                doc = _document(db, job)
                pages = parsed.page_texts()
                n_chunks, changed = replace_chunks(db, doc, number, pages)
                doc.page_count = len(pages)
                doc.extracted_fields = extract_contract_fields(pages)
                extraction_status = _extract(db, job, doc, parsed, force=number > 1 or reason == "new_version")
                result.update({"checkpoint": "indexed", "pages": len(pages), "chunks": n_chunks,
                               "chunks_replaced": changed, "ocr_pages": list(parsed.ocr_pages),
                               "format": parsed.format, "extraction": extraction_status})
                row = queue.lock(db, job)
                row.result, row.stage = result, "embedding"
                db.commit()
    with ctx.stage("embedding"):
        with ctx.session() as db:
            queue.lock(db, job)
            doc = _document(db, job)
            embedding_status, embedded_now = _embed(ctx, db, doc)
            version = _version(db, doc, number)
            for other in db.scalars(select(DocumentVersion).where(DocumentVersion.document_id == doc.id,
                                                                   DocumentVersion.state == "active",
                                                                   DocumentVersion.version != number)):
                other.state = "superseded"
            version.state = "active"
            doc.current_version, doc.storage_backend, doc.storage_key = number, version.storage_backend, version.storage_key
            doc.sha256, doc.size_bytes, doc.content_type = version.sha256, version.size_bytes, version.content_type
            doc.filename = version.original_filename
            doc.lifecycle_state, doc.updated_at = "ready", now_utc()
            _audit(db, job, "document_processed" if reason == "upload" else "document_version_processed")
            result.update({"checkpoint": "done", "embedding_status": embedding_status,
                           "embedded_now": embedded_now, "version": number})
            db.flush()
            queue.finish(db, job, "completed", result=result)
            db.commit()
    INGESTION_DURATION.labels(result.get("format") or fmt_hint.split("/")[-1][:20] or "unknown").observe(
        time.perf_counter() - started)
    return result


def _extract(db: Session, job: ClaimedJob, doc: Document, parsed: ParsedDocument, force: bool) -> str:
    """Structure + offline rules extraction. A failure here never undoes the ingestion (v0.3
    semantics): it is stored as processing status 'failed' and can be retried via /process."""
    user = db.get(User, job.user_id) if job.user_id != "system" else None
    savepoint = db.begin_nested()
    try:
        docintel.store_structure(db, doc, parsed)
        if user is None:
            raise RuntimeError("system jobs do not run extraction")
        extraction = docintel.process(db, user, doc, force=force)
        savepoint.commit()
        EXTRACTIONS.labels("rules", extraction.doc_type, "ok").inc()
        return extraction.status
    except Exception as exc:  # noqa: BLE001
        savepoint.rollback()
        docintel.mark_failed(db, doc, parsed, exc)
        EXTRACTIONS.labels("rules", "unknown", "failed").inc()
        log.warning("extraction_failed", extra={"event": "extraction_failed", "error_type": type(exc).__name__})
        return "failed"


# ----------------------------------------------------------------------------------- reprocess

def reprocess(ctx: JobContext) -> dict:
    job, p = ctx.job, ctx.job.payload
    with ctx.session() as db:
        queue.lock(db, job)
        doc = _document(db, job)
        access, role = user_access(db, job, doc.id)
        if access == "none" or not (access in ("owner", "admin") or role in ("reviewer", "admin")):
            raise permanent("authorization_revoked", "The submitting user may no longer re-process this document.", 403)
        needs_parse = bool(p.get("reparse")) or db.get(DocumentProcessing, doc.id) is None
        version = _version(db, doc, doc.current_version) if needs_parse else None
        filename = doc.filename
        db.commit()
    parsed = None
    if needs_parse:
        with ctx.stage("downloading"):
            data = download(ctx, version)
        with ctx.stage("parsing"):
            parsed = parse(ctx, filename, data)
    with ctx.stage("extracting"):
        with ctx.session() as db:
            queue.lock(db, job)
            doc = _document(db, job)
            if parsed is not None:
                docintel.store_structure(db, doc, parsed)
            user = db.get(User, job.user_id)
            provider = p.get("provider", "rules")
            try:
                extraction = docintel.process(db, user, doc, provider, bool(p.get("force")), p.get("doc_type"))
            except docintel.ExtractionLocked as exc:
                raise permanent("extraction_locked", str(exc), 409) from None
            except ExtractionProviderError as exc:
                db.commit()  # keep the 'failed' processing status and attempt count (v0.3 contract)
                EXTRACTIONS.labels("llm", "unknown", "failed").inc()
                raise permanent("extraction_provider_failed",
                                f"Extraction provider failed (no fallback was used): {exc}", 502) from None
            EXTRACTIONS.labels(provider, extraction.doc_type, "ok").inc()
            result = {"extraction_id": extraction.id, "extraction_version": extraction.version,
                      "extraction_status": extraction.status, "provider": extraction.provider}
            db.flush()
            queue.finish(db, job, "completed", result=result)
            db.commit()
    return result


# ----------------------------------------------------------------------------------- reindex

def reindex(ctx: JobContext) -> dict:
    job = ctx.job
    with ctx.stage("embedding"):
        with ctx.session() as db:
            queue.lock(db, job)
            doc = _document(db, job)
            access, role = user_access(db, job, doc.id)
            if access == "none" or not (access in ("owner", "admin", "system") or role in ("reviewer", "admin")):
                raise permanent("authorization_revoked", "The submitting user may no longer re-index this document.", 403)
            removed = 0
            if job.payload.get("force") and db.get_bind().dialect.name == "postgresql":
                model = ctx.embedder_factory().model_name
                ids = select(Chunk.id).where(Chunk.document_id == doc.id)
                removed = db.execute(delete(ChunkEmbedding).where(ChunkEmbedding.chunk_id.in_(ids),
                                                                  ChunkEmbedding.model_name == model)).rowcount
            status, embedded = _embed(ctx, db, doc)
            _audit(db, job, "document_reindexed")
            result = {"embedding_status": status, "embedded_now": embedded, "removed": removed or 0}
            db.flush()
            queue.finish(db, job, "completed", result=result)
            db.commit()
    return result


# ----------------------------------------------------------------------------------- purge

def purge(ctx: JobContext) -> dict:
    """Staged, idempotent deletion of a SOFT-DELETED document (refuses a live document):
    1. derived data (chunks -> embeddings, tables, processing, extraction -> fields, review history,
       grants) in one transaction; 2. every stored version object; 3. tombstone (ids, tenant, owner,
       checksum, timestamps; no title, filename or content). Audit events are retained."""
    job = ctx.job
    with ctx.stage("purging_index"):
        with ctx.session() as db:
            queue.lock(db, job)
            doc = _document(db, job, allow_deleted=True)
            if doc.deleted_at is None:
                raise permanent("not_deleted", "Refusing to purge a document that is not deleted.", 409)
            removed = {}
            for model in (Chunk, DocumentTable, DocumentProcessing, Extraction, DocumentGrant):
                removed[model.__tablename__] = db.execute(
                    delete(model).where(model.document_id == doc.id)).rowcount or 0
            versions = [(v.id, v.storage_backend, v.storage_key) for v in db.scalars(
                select(DocumentVersion).where(DocumentVersion.document_id == doc.id,
                                              DocumentVersion.object_deleted_at.is_(None)))]
            db.commit()
    deleted_objects = 0
    with ctx.stage("purging_objects"):
        for version_id, backend, key in versions:
            ctx.check()
            try:
                store_for(backend, ctx.cfg).delete(key)  # idempotent: a missing object is fine
            except StorageError as exc:
                raise transient("storage_unavailable", f"Object deletion failed ({exc.code}); will retry.") from None
            deleted_objects += 1
            with ctx.session() as db:
                queue.lock(db, job)
                version = db.get(DocumentVersion, version_id)
                version.object_deleted_at, version.state = now_utc(), "purged"
                version.original_filename = "[purged]"
                db.commit()
    with ctx.session() as db:
        queue.lock(db, job)
        doc = _document(db, job, allow_deleted=True)
        doc.title, doc.filename, doc.extracted_fields, doc.page_count = "[deleted]", "[deleted]", {}, 0
        doc.lifecycle_state, doc.purged_at, doc.updated_at = "purged", now_utc(), now_utc()
        _audit(db, job, "document_purged")
        result = {"removed_rows": removed, "objects_deleted": deleted_objects, "reason": job.payload.get("reason")}
        db.flush()
        queue.finish(db, job, "completed", result=result)
        db.commit()
    DOCUMENTS_PURGED.inc()
    return result


HANDLERS: dict[str, Callable[[JobContext], dict]] = {
    "ingest": ingest, "reprocess": reprocess, "reindex": reindex, "purge": purge,
}
