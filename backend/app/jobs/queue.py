"""Durable PostgreSQL-backed job queue (Phase 4C). Design: docs/background-processing.md.

Why PostgreSQL and not Redis + RQ / Dramatiq: the job row is inserted in the SAME transaction as
the document change that needs it (upload, new version, delete), so a committed upload always
has its job and a rolled-back upload never has one. That removes the dual-write problem between
a database commit and a broker publish (a transactional outbox whose outbox IS the queue), adds
no infrastructure, keeps jobs under row-level security, and survives restarts of every process.

Lifecycle:   queued -> running -> completed
                         |  `-> retrying (exponential backoff + jitter) -> running ...
                         |  `-> failed   (permanent error or max attempts = dead letter)
                         `----> cancelled (document deleted / user request)
Claiming:    nexus_rls.claim_job(): FOR UPDATE SKIP LOCKED, so two workers never claim the same
             job; it also recovers jobs whose lease expired (worker crash) first.
Fencing:     every claim gets a fresh lease_token. Every write of the worker takes the job row
             lock with `id = :id AND lease_token = :token AND status = 'running'`; a worker whose
             lease was taken over (e.g. it stalled past its lease) cannot commit anything. The RLS
             job scope uses the same token.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from ..database import SecurityContext, set_security_context
from ..models import ProcessingJob
from ..observability.metrics import JOBS_ENQUEUED
from ..observability.tracing import inject_current

JOB_TYPES = frozenset({"ingest", "reprocess", "reindex", "purge"})
ACTIVE_STATES = ("queued", "running", "retrying")
FINAL_STATES = ("completed", "failed", "cancelled")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class LeaseLost(RuntimeError):
    """The job lease expired or was taken over: nothing may be written for this attempt."""


@dataclass
class ClaimedJob:
    id: str
    tenant_id: str
    user_id: str
    document_id: str | None
    document_version: int | None
    job_type: str
    payload: dict
    attempts: int
    max_attempts: int
    lease_token: str
    trace_context: dict | None
    run_after: datetime
    created_at: datetime
    result: dict | None

    def context(self) -> SecurityContext:
        return SecurityContext(tenant_id=self.tenant_id, user_id=self.user_id, kind="worker",
                               job_id=self.id, lease_token=self.lease_token)


def enqueue(db: Session, *, tenant_id: str, user_id: str, job_type: str, document_id: str | None = None,
            document_version: int | None = None, payload: dict | None = None,
            idempotency_key: str | None = None, priority: int = 100, max_attempts: int = 5,
            run_after: datetime | None = None) -> ProcessingJob:
    """Add a job in the CALLER'S transaction (committed atomically with the caller's changes)."""
    if job_type not in JOB_TYPES:
        raise ValueError(f"unknown job type {job_type!r}")
    now = now_utc()
    job = ProcessingJob(
        id=str(uuid4()), tenant_id=tenant_id, user_id=user_id, document_id=document_id,
        document_version=document_version, job_type=job_type, status="queued", stage="queued",
        priority=priority, attempts=0, max_attempts=max_attempts, run_after=run_after or now,
        idempotency_key=idempotency_key, payload=payload or {}, trace_context=inject_current() or None,
        cancel_requested=False, created_at=now, updated_at=now,
    )
    db.add(job)
    db.flush()
    JOBS_ENQUEUED.labels(job_type).inc()
    return job


def active_job(db: Session, document_id: str, job_type: str) -> ProcessingJob | None:
    """An unfinished job of this type for the document (duplicate-submission guard)."""
    return db.scalar(select(ProcessingJob).where(
        ProcessingJob.document_id == document_id, ProcessingJob.job_type == job_type,
        ProcessingJob.status.in_(ACTIVE_STATES)).order_by(ProcessingJob.created_at.desc()).limit(1))


def by_idempotency_key(db: Session, tenant_id: str, key: str) -> ProcessingJob | None:
    return db.scalar(select(ProcessingJob).where(ProcessingJob.tenant_id == tenant_id,
                                                 ProcessingJob.idempotency_key == key))


def _claimed(row) -> ClaimedJob:
    m = row._mapping if hasattr(row, "_mapping") else row
    return ClaimedJob(id=m["id"], tenant_id=m["tenant_id"], user_id=m["user_id"],
                      document_id=m["document_id"], document_version=m["document_version"],
                      job_type=m["job_type"], payload=m["payload"] or {}, attempts=m["attempts"],
                      max_attempts=m["max_attempts"], lease_token=m["lease_token"],
                      trace_context=m["trace_context"], run_after=_aware(m["run_after"]),
                      created_at=_aware(m["created_at"]), result=m["result"])


def claim(db: Session, worker_id: str, lease_seconds: int, job_types: list[str] | None = None,
          job_id: str | None = None) -> ClaimedJob | None:
    """Claim the next runnable job (or one specific job) and commit the claim."""
    if db.get_bind().dialect.name == "postgresql":
        row = db.execute(text("SELECT * FROM nexus_rls.claim_job(CAST(:w AS text), CAST(:l AS integer), "
                              "CAST(:t AS text[]), CAST(:j AS text))"),
                         {"w": worker_id[:120], "l": int(lease_seconds), "t": job_types, "j": job_id}).first()
        db.commit()
        return _claimed(row) if row else None
    return _claim_portable(db, worker_id, lease_seconds, job_types, job_id)


def _claim_portable(db: Session, worker_id: str, lease_seconds: int, job_types, job_id) -> ClaimedJob | None:
    """Same semantics for SQLite (single writer; used by unit tests and the SQLite demo)."""
    now = now_utc()
    for job in db.scalars(select(ProcessingJob).where(ProcessingJob.status == "running")):
        if _aware(job.locked_until) and _aware(job.locked_until) < now:
            final = job.attempts >= job.max_attempts
            job.status, job.error_code = ("failed" if final else "retrying"), "lease_expired"
            job.error_message = "The worker lease expired before the job finished (worker crash or stall)."
            job.locked_by = job.locked_until = job.lease_token = None
            job.run_after, job.updated_at = now, now
            job.finished_at = now if final else None
    q = select(ProcessingJob).where(ProcessingJob.status.in_(("queued", "retrying")),
                                    ProcessingJob.cancel_requested.is_(False))
    if job_id is not None:
        q = q.where(ProcessingJob.id == job_id)
    else:
        q = q.where(ProcessingJob.run_after <= now)
        if job_types:
            q = q.where(ProcessingJob.job_type.in_(job_types))
        q = q.order_by(ProcessingJob.priority, ProcessingJob.run_after, ProcessingJob.created_at).limit(1)
    job = db.scalar(q)
    if job is None:
        db.commit()
        return None
    job.status, job.attempts = "running", job.attempts + 1
    job.locked_by, job.locked_until = worker_id[:120], now + timedelta(seconds=lease_seconds)
    job.lease_token = uuid4().hex
    job.heartbeat_at = job.last_started_at = job.updated_at = now
    job.started_at = job.started_at or now
    db.commit()
    return _claimed({c.name: getattr(job, c.name) for c in ProcessingJob.__table__.columns})


def _fenced(db: Session, job: ClaimedJob):
    return (ProcessingJob.id == job.id, ProcessingJob.lease_token == job.lease_token,
            ProcessingJob.status == "running")


def bind(db: Session, job: ClaimedJob) -> None:
    set_security_context(db, job.context())


def lock(db: Session, job: ClaimedJob) -> ProcessingJob:
    """Take the job row lock for this transaction; LeaseLost if the lease is no longer ours."""
    bind(db, job)
    row = db.scalar(select(ProcessingJob).where(*_fenced(db, job)).with_for_update())
    if row is None:
        raise LeaseLost(f"lease of job {job.id} lost")
    return row


def heartbeat(db: Session, job: ClaimedJob, lease_seconds: int) -> bool:
    """Extend the lease. Returns cancel_requested. Raises LeaseLost if the lease is gone."""
    bind(db, job)
    now = now_utc()
    result = db.execute(update(ProcessingJob).where(*_fenced(db, job))
                        .values(locked_until=now + timedelta(seconds=lease_seconds), heartbeat_at=now,
                                updated_at=now)
                        .returning(ProcessingJob.cancel_requested)).first()
    db.commit()
    if result is None:
        raise LeaseLost(f"lease of job {job.id} lost")
    return bool(result[0])


def set_stage(db: Session, job: ClaimedJob, stage: str, result: dict | None = None) -> None:
    row = lock(db, job)
    row.stage, row.updated_at = stage, now_utc()
    if result is not None:
        row.result = result
    db.commit()


def backoff_seconds(attempt: int, base: float, maximum: float, rng: random.Random | None = None) -> float:
    """Exponential backoff with equal jitter: base * 2^(attempt-1), capped, in [50 %, 100 %]."""
    raw = min(maximum, base * (2 ** max(0, attempt - 1)))
    return raw * (0.5 + (rng or random).random() / 2)


def finish(db: Session, job: ClaimedJob, status: str, *, result: dict | None = None,
           error_code: str | None = None, error_message: str | None = None,
           retry_in: float | None = None) -> str:
    """Final transition of this attempt (call inside a transaction that holds `lock`)."""
    row = lock(db, job)
    now = now_utc()
    if status == "retry":
        if job.attempts < job.max_attempts:
            row.status, row.run_after, row.finished_at = "retrying", now + timedelta(seconds=retry_in or 0), None
        else:
            row.status, row.finished_at = "failed", now
    else:
        row.status, row.finished_at = status, now
    row.stage = {"completed": "completed", "cancelled": "cancelled"}.get(row.status, row.stage)
    if result is not None:
        row.result = result
    if row.status == "completed":
        row.error_code = row.error_message = None
    else:
        row.error_code = (error_code or row.error_code or "internal_error")[:40]
        row.error_message = (error_message or "")[:500] or None
    row.locked_by = row.locked_until = row.lease_token = None
    row.updated_at = now
    return row.status


# ------------------------------------------------------------------ user-facing operations (API)

def cancel(db: Session, job: ProcessingJob) -> str:
    """Queued / retrying: cancelled at once. Running: flagged; the worker stops at the next stage."""
    now = now_utc()
    if job.status in ("queued", "retrying"):
        job.status, job.stage, job.finished_at, job.updated_at = "cancelled", "cancelled", now, now
        job.error_code, job.error_message = "cancelled_by_user", "Cancelled before it ran."
    elif job.status == "running":
        job.cancel_requested, job.updated_at = True, now
    return job.status


def retry(db: Session, job: ProcessingJob, budget: int) -> None:
    """Failed (dead-lettered) or cancelled job back to the queue with a fresh attempt budget.
    The attempt counter is kept (history); max_attempts grows by `budget`."""
    now = now_utc()
    job.status, job.stage, job.run_after = "queued", "queued", now
    job.max_attempts = job.attempts + max(1, budget)
    job.cancel_requested, job.finished_at, job.updated_at = False, None, now
    job.result = None


def view(job: ProcessingJob) -> dict:
    def iso(value):
        value = _aware(value)
        return value.isoformat() if value else None

    return {"id": job.id, "type": job.job_type, "status": job.status, "stage": job.stage,
            "document_id": job.document_id, "document_version": job.document_version,
            "attempts": job.attempts, "max_attempts": job.max_attempts,
            "error_code": job.error_code, "error_message": job.error_message,
            "cancel_requested": bool(job.cancel_requested), "result": job.result,
            "created_at": iso(job.created_at), "updated_at": iso(job.updated_at),
            "started_at": iso(job.started_at), "finished_at": iso(job.finished_at),
            "run_after": iso(job.run_after),
            "retry_available": job.status in ("failed", "cancelled")}


def queue_stats(db: Session) -> list[tuple[str, str, int, float]]:
    """Aggregated depth across tenants (metrics only; no tenant labels)."""
    if db.get_bind().dialect.name == "postgresql":
        rows = db.execute(text("SELECT job_type, status, jobs, oldest_seconds FROM nexus_rls.queue_stats()")).all()
        return [(r[0], r[1], int(r[2]), float(r[3] or 0)) for r in rows]
    now = now_utc()
    out: dict[tuple[str, str], list] = {}
    for job in db.scalars(select(ProcessingJob).where(ProcessingJob.status.in_(
            ("queued", "retrying", "running", "failed")))):
        entry = out.setdefault((job.job_type, job.status), [0, 0.0])
        entry[0] += 1
        entry[1] = max(entry[1], (now - _aware(job.run_after)).total_seconds())
    return [(k[0], k[1], v[0], v[1]) for k, v in out.items()]
