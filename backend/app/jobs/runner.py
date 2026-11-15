"""Execute one claimed job attempt: tracing (child of the enqueueing request), log context, lease
heartbeat, error classification, retry with backoff, dead-lettering and metrics.

Used by the background worker (backend/app/worker.py) and by PROCESSING_MODE=inline, where the
API claims the job it has just committed and runs exactly the same code inside the request.
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import sessionmaker

from ..config import Settings
from ..models import Document, DocumentVersion
from ..observability.logging import log_context
from ..observability.metrics import (JOB_DURATION, JOB_FAILURES, JOB_QUEUE_WAIT, JOB_RETRIES, JOBS_FINISHED,
                                     WORKER_BUSY)
from ..observability.tracing import span
from . import handlers, queue
from .handlers import JobCancelled, JobContext, JobError
from .queue import ClaimedJob, LeaseLost

log = logging.getLogger("nexus.jobs")


@dataclass
class JobOutcome:
    job_id: str
    status: str                 # completed | retrying | failed | cancelled | lease_lost | not_claimed
    result: dict | None = None
    error_code: str | None = None
    error_message: str | None = None
    http_status: int = 200


def default_worker_id() -> str:
    return f"{socket.gethostname()[:60]}:{os.getpid()}"


class Heartbeat(threading.Thread):
    """Extends the lease while a long stage (OCR, embedding) runs; flags lease loss / cancellation."""

    def __init__(self, ctx: JobContext, interval: float, lease_seconds: int):
        super().__init__(daemon=True, name=f"heartbeat-{ctx.job.id[:8]}")
        self.ctx, self.interval, self.lease_seconds = ctx, interval, lease_seconds
        self.stopped = threading.Event()

    def run(self) -> None:
        while not self.stopped.wait(self.interval):
            try:
                with self.ctx.session() as db:
                    if queue.heartbeat(db, self.ctx.job, self.lease_seconds):
                        self.ctx.cancel_requested.set()
            except LeaseLost:
                self.ctx.lease_lost.set()
                return
            except Exception as exc:  # noqa: BLE001 - a DB blip must not kill the job
                log.warning("heartbeat_failed", extra={"event": "heartbeat_failed", "error_type": type(exc).__name__})


def _mark_document_failed(db, job: ClaimedJob) -> None:
    if job.job_type != "ingest" or not job.document_id:
        return
    doc = db.get(Document, job.document_id)
    if doc is not None and doc.deleted_at is None:
        doc.lifecycle_state = "failed"
        number = job.document_version or job.payload.get("version", 1)
        for version in doc_versions(db, doc.id, number):
            version.state = "failed"
    db.flush()


def doc_versions(db, document_id: str, number: int):
    from sqlalchemy import select

    return db.scalars(select(DocumentVersion).where(DocumentVersion.document_id == document_id,
                                                    DocumentVersion.version == number,
                                                    DocumentVersion.state == "stored"))


def _close(ctx: JobContext, status: str, *, error: JobError | None = None, reason: str | None = None,
           cfg: Settings) -> str:
    """Record the end of a failed / cancelled attempt in its own fenced transaction."""
    job = ctx.job
    with ctx.session() as db:
        if status == "cancelled":
            final = queue.finish(db, job, "cancelled", error_code=reason[:40] if reason else "cancelled",
                                 error_message={"document_deleted": "The document was deleted.",
                                                "cancel_requested": "Cancelled on request."}.get(reason or "", None))
        elif error is not None and error.retryable and job.attempts < job.max_attempts:
            delay = queue.backoff_seconds(job.attempts, cfg.job_backoff_base_seconds, cfg.job_backoff_max_seconds)
            final = queue.finish(db, job, "retry", error_code=error.code, error_message=error.message, retry_in=delay)
        else:
            queue.lock(db, job)
            _mark_document_failed(db, job)
            final = queue.finish(db, job, "failed", error_code=error.code if error else "internal_error",
                                 error_message=error.message if error else None)
        db.commit()
    return final


def execute(job: ClaimedJob, *, cfg: Settings, sessions: sessionmaker,
            embedder_factory: Callable[[], object], preparsed=None, preloaded: bytes | None = None,
            heartbeat: bool = True) -> JobOutcome:
    ctx = JobContext(job=job, cfg=cfg, sessions=sessions, embedder_factory=embedder_factory,
                     preparsed=preparsed, preloaded=preloaded)
    token = log_context.set({"job_id": job.id, "job_type": job.job_type, "tenant_id": job.tenant_id,
                             "document_id": job.document_id, "attempt": job.attempts,
                             **({"request_id": (log_context.get() or {}).get("request_id")}
                                if log_context.get() else {})})
    start = time.perf_counter()
    if job.run_after:
        JOB_QUEUE_WAIT.labels(job.job_type).observe(max(0.0, time.time() - job.run_after.timestamp()))
    beat = None
    if heartbeat and sessions.kw.get("bind") is not None and sessions.kw["bind"].dialect.name == "postgresql":
        beat = Heartbeat(ctx, cfg.job_heartbeat_seconds, cfg.job_lease_seconds)
        beat.start()
    WORKER_BUSY.set(1)
    outcome: JobOutcome
    try:
        with span("job.execute", parent_carrier=job.trace_context, **{
                "job.id": job.id, "job.type": job.job_type, "job.attempt": job.attempts}):
            handler = handlers.HANDLERS.get(job.job_type)
            if handler is None:
                raise handlers.permanent("unknown_job_type", "Unknown job type.", 500)
            result = handler(ctx)
        outcome = JobOutcome(job.id, "completed", result=result, http_status=200)
    except JobCancelled as exc:
        status = _close(ctx, "cancelled", reason=exc.reason, cfg=cfg)
        outcome = JobOutcome(job.id, status, error_code=exc.reason, http_status=409)
    except JobError as exc:
        try:
            status = _close(ctx, "failed", error=exc, cfg=cfg)
        except LeaseLost:
            status = "lease_lost"
        outcome = JobOutcome(job.id, status, error_code=exc.code, error_message=exc.message,
                             http_status=exc.http_status)
    except LeaseLost:
        log.warning("job_lease_lost", extra={"event": "job_lease_lost"})
        outcome = JobOutcome(job.id, "lease_lost", error_code="lease_lost", http_status=409)
    except (OperationalError, DBAPIError) as exc:
        # Never log str(exc): SQLAlchemy appends the statement parameters (document text).
        orig = getattr(exc, "orig", None)
        sqlstate = getattr(orig, "sqlstate", None)
        log.warning("job_database_error", extra={
            "event": "job_database_error", "error_type": type(orig or exc).__name__, "sqlstate": sqlstate,
            "table": getattr(getattr(orig, "diag", None), "table_name", None)})
        if sqlstate == "42501":  # insufficient privilege / RLS violation: a bug, not an outage
            error = handlers.permanent("database_permission_denied",
                                       "The database refused the operation (row-level security).", 500)
        else:
            error = handlers.transient("database_unavailable", f"Database error ({type(orig or exc).__name__}).")
        try:
            status = _close(ctx, "failed", error=error, cfg=cfg)
        except Exception:  # noqa: BLE001 - DB still down: the expired lease is recovered later
            status = "lease_pending_recovery"
        outcome = JobOutcome(job.id, status, error_code=error.code, error_message=error.message, http_status=503)
    except Exception as exc:  # noqa: BLE001 - unknown failure: retry, type name only (no content)
        log.exception("job_internal_error", extra={"event": "job_internal_error", "error_type": type(exc).__name__})
        error = handlers.transient("internal_error", f"Unexpected {type(exc).__name__} while processing.", 500)
        try:
            status = _close(ctx, "failed", error=error, cfg=cfg)
        except Exception:  # noqa: BLE001
            status = "lease_pending_recovery"
        outcome = JobOutcome(job.id, status, error_code=error.code, error_message=error.message, http_status=500)
    finally:
        WORKER_BUSY.set(0)
        if beat is not None:
            beat.stopped.set()
            beat.join(timeout=5)
    elapsed = time.perf_counter() - start
    JOB_DURATION.labels(job.job_type, outcome.status).observe(elapsed)
    JOBS_FINISHED.labels(job.job_type, outcome.status).inc()
    if outcome.status == "retrying":
        JOB_RETRIES.labels(job.job_type, outcome.error_code or "unknown").inc()
    elif outcome.status == "failed":
        JOB_FAILURES.labels(job.job_type, outcome.error_code or "unknown").inc()
    log.log(logging.INFO if outcome.status == "completed" else logging.WARNING, "job_finished", extra={
        "event": "job_finished", "status": outcome.status, "error_code": outcome.error_code,
        "duration_ms": round(elapsed * 1000, 1)})
    log_context.reset(token)
    return outcome


def run_inline(job_id: str, *, cfg: Settings, sessions: sessionmaker, embedder_factory,
               preparsed=None, preloaded: bytes | None = None) -> JobOutcome:
    """PROCESSING_MODE=inline: claim the given job and run its first attempt in this process."""
    with sessions() as db:
        claimed = queue.claim(db, f"inline:{default_worker_id()}", cfg.job_lease_seconds, job_id=job_id)
    if claimed is None:  # already taken by a background worker
        return JobOutcome(job_id, "not_claimed", http_status=202)
    return execute(claimed, cfg=cfg, sessions=sessions, embedder_factory=embedder_factory,
                   preparsed=preparsed, preloaded=preloaded)
