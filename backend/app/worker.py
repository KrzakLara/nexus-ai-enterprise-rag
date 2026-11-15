"""Background worker process:  python -m backend.app.worker

Loop: heartbeat (worker_heartbeats + optional heartbeat file for the container health check)
-> periodic retention sweep (expired documents are soft-deleted and get purge jobs)
-> claim one job (FOR UPDATE SKIP LOCKED) -> execute -> repeat; sleep WORKER_POLL_SECONDS when idle.

SIGTERM / SIGINT: stop claiming, finish the current job, exit 0. A hard kill (SIGKILL, OOM,
power loss) leaves the job `running` with a lease that expires after JOB_LEASE_SECONDS; the next
claim by any worker moves it to `retrying` (or `failed` after the last attempt) and it is
re-executed idempotently.

Run several worker processes / containers to scale; each executes one job at a time (parsing,
OCR and embedding are CPU-bound and already use several threads internally).

    python -m backend.app.worker --once         # process at most one job and exit (tests, drills)
    python -m backend.app.worker --healthcheck  # exit 0 if the heartbeat file is fresh
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from .config import assert_secure_configuration, settings
from .database import assert_runtime_role, worker_engine
from .embeddings import get_embedder
from .jobs import queue
from .jobs.runner import default_worker_id, execute
from .migrate import assert_schema_current
from .models import WorkerHeartbeat
from .observability.logging import configure_logging
from .observability.metrics import (DB_POOL, QUEUE_DEPTH, WORKER_HEARTBEAT, register_collectors,
                                    start_metrics_server)
from .observability.tracing import init_tracing

log = logging.getLogger("nexus.worker")


class Worker:
    def __init__(self, sessions: sessionmaker, cfg=settings, worker_id: str | None = None,
                 embedder_factory=get_embedder):
        self.sessions, self.cfg = sessions, cfg
        self.worker_id = worker_id or cfg.worker_id or default_worker_id()
        self.embedder_factory = embedder_factory
        self.stopping = threading.Event()
        self.completed = self.failed = 0
        self.started_at = datetime.now(timezone.utc)
        self._last_sweep = 0.0
        self._last_beat = 0.0
        self.current_job: str | None = None

    # ------------------------------------------------------------------ liveness
    def beat(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_beat < 5:
            return
        self._last_beat = now
        WORKER_HEARTBEAT.set(time.time())
        if self.cfg.worker_heartbeat_file:
            try:
                Path(self.cfg.worker_heartbeat_file).write_text(str(int(time.time())), encoding="ascii")
            except OSError:
                pass
        try:
            with self.sessions() as db:
                row = db.get(WorkerHeartbeat, self.worker_id)
                stamp = datetime.now(timezone.utc)
                if row is None:
                    row = WorkerHeartbeat(worker_id=self.worker_id, started_at=self.started_at,
                                          version=self.cfg.service_version, jobs_completed=0, jobs_failed=0)
                    db.add(row)
                row.last_seen_at, row.current_job_id = stamp, self.current_job
                row.jobs_completed, row.jobs_failed = self.completed, self.failed
                db.commit()
        except Exception as exc:  # noqa: BLE001 - database outage: keep running, retry later
            log.warning("worker_heartbeat_failed", extra={"event": "worker_heartbeat_failed",
                                                          "error_type": type(exc).__name__})

    def sweep(self) -> None:
        """Retention: expire documents whose retention_until passed (PostgreSQL)."""
        if time.monotonic() - self._last_sweep < self.cfg.retention_sweep_seconds:
            return
        self._last_sweep = time.monotonic()
        try:
            with self.sessions() as db:
                if db.get_bind().dialect.name != "postgresql":
                    return
                expired = db.execute(text("SELECT nexus_rls.expire_documents(100, :m)"),
                                     {"m": self.cfg.job_max_attempts}).scalar()
                db.commit()
            if expired:
                log.info("retention_expired_documents", extra={"event": "retention_expired_documents",
                                                               "documents": expired})
        except Exception as exc:  # noqa: BLE001
            log.warning("retention_sweep_failed", extra={"event": "retention_sweep_failed",
                                                         "error_type": type(exc).__name__})

    # ------------------------------------------------------------------ main loop
    def run_once(self) -> str | None:
        """Claim and execute at most one job. Returns the outcome status or None if idle."""
        try:
            with self.sessions() as db:
                job = queue.claim(db, self.worker_id, self.cfg.job_lease_seconds)
        except Exception as exc:  # noqa: BLE001 - database unavailable: back off, never crash
            log.warning("claim_failed", extra={"event": "claim_failed", "error_type": type(exc).__name__})
            return "claim_failed"
        if job is None:
            return None
        self.current_job = job.id
        self.beat(force=True)
        outcome = execute(job, cfg=self.cfg, sessions=self.sessions, embedder_factory=self.embedder_factory)
        self.current_job = None
        if outcome.status == "completed":
            self.completed += 1
        elif outcome.status in ("failed", "retrying"):
            self.failed += 1
        self.beat(force=True)
        return outcome.status

    def run(self, once: bool = False) -> int:
        log.info("worker_started", extra={"event": "worker_started", "worker_id": self.worker_id})
        idle_delay = max(0.05, self.cfg.worker_poll_seconds)
        while not self.stopping.is_set():
            self.beat()
            self.sweep()
            status = self.run_once()
            if once:
                break
            if status in (None, "claim_failed"):
                self.stopping.wait(idle_delay if status is None else min(10.0, idle_delay * 5))
        log.info("worker_stopped", extra={"event": "worker_stopped", "completed": self.completed,
                                          "failed": self.failed})
        return 0


def _healthcheck(cfg) -> int:
    path = cfg.worker_heartbeat_file
    if not path:
        print("WORKER_HEARTBEAT_FILE is not set", file=sys.stderr)
        return 1
    try:
        age = time.time() - int(Path(path).read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return 1
    return 0 if age < cfg.worker_stale_seconds else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NEXUS AI background worker")
    parser.add_argument("--once", action="store_true", help="process at most one job and exit")
    parser.add_argument("--healthcheck", action="store_true", help="check the heartbeat file and exit")
    args = parser.parse_args(argv)
    cfg = settings
    if args.healthcheck:
        return _healthcheck(cfg)
    configure_logging("nexus-worker", cfg.app_env, cfg.log_level, cfg.log_format)
    assert_secure_configuration(cfg)
    engine = worker_engine()
    assert_runtime_role(engine, cfg.db_require_rls_role)
    assert_schema_current(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    register_collectors()
    DB_POOL.register("worker", engine)

    def stats():
        with sessions() as db:
            return queue.queue_stats(db)

    QUEUE_DEPTH.set_source(stats)
    start_metrics_server(cfg.metrics_port)
    init_tracing(cfg, "nexus-worker", engines=[engine])
    worker = Worker(sessions, cfg)

    def stop(signum, _frame):
        log.info("worker_stopping", extra={"event": "worker_stopping", "signal": signum})
        worker.stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if engine.dialect.name == "postgresql" and cfg.embedding_warmup:
        try:
            get_embedder().embed_query("warm-up probe")
        except Exception as exc:  # noqa: BLE001 - jobs then fail with embedding_unavailable and retry
            log.warning("embedding_warmup_failed", extra={"event": "embedding_warmup_failed",
                                                          "error_type": type(exc).__name__})
    return worker.run(once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
