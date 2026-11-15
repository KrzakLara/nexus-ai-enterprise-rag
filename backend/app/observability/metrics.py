"""Prometheus metrics (low-cardinality labels only).

Labels are restricted to small, fixed vocabularies: HTTP method, route TEMPLATE (never the raw
path), status code, retrieval mode, job type, outcome, error category, storage operation, OCR /
document format. User ids, document ids, tenant ids and free text are never labels.

The metrics are served on a separate internal port (METRICS_PORT) that Docker Compose does not
publish to the host; Prometheus scrapes it over the internal network.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from prometheus_client import REGISTRY, Counter, Gauge, Histogram, start_http_server
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

log = logging.getLogger("nexus.metrics")

LATENCY = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)
LONG = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600)

HTTP_REQUESTS = Counter("nexus_http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_DURATION = Histogram("nexus_http_request_duration_seconds", "HTTP request duration",
                          ["method", "route"], buckets=LATENCY)
HTTP_IN_PROGRESS = Gauge("nexus_http_requests_in_progress", "HTTP requests being served")
AUTH_FAILURES = Counter("nexus_auth_failures_total", "Rejected authentications and sessions",
                        ["method", "reason"])
AUTH_LOGINS = Counter("nexus_auth_logins_total", "Successful logins", ["mode"])
JOBS_ENQUEUED = Counter("nexus_jobs_enqueued_total", "Background jobs accepted", ["job_type"])
JOBS_FINISHED = Counter("nexus_jobs_finished_total", "Background job attempts that ended",
                        ["job_type", "outcome"])
JOB_DURATION = Histogram("nexus_job_duration_seconds", "Background job attempt duration",
                         ["job_type", "outcome"], buckets=LONG)
JOB_QUEUE_WAIT = Histogram("nexus_job_queue_wait_seconds", "Time from run_after to claim",
                           ["job_type"], buckets=LONG)
JOB_RETRIES = Counter("nexus_job_retries_total", "Job attempts scheduled for retry", ["job_type", "error_code"])
JOB_FAILURES = Counter("nexus_job_failures_total", "Jobs that ended in failed (dead letter)",
                       ["job_type", "error_code"])
STAGE_DURATION = Histogram("nexus_processing_stage_duration_seconds", "Document processing stage",
                           ["stage"], buckets=LONG)
INGESTION_DURATION = Histogram("nexus_document_ingestion_duration_seconds",
                               "End-to-end ingestion of one document version (worker)", ["format"],
                               buckets=LONG)
OCR_PAGES = Counter("nexus_ocr_pages_total", "Pages processed with OCR")
OCR_DURATION = Histogram("nexus_ocr_document_parse_seconds", "Parse time of documents that needed OCR",
                         buckets=LONG)
EMBEDDING_DURATION = Histogram("nexus_embedding_duration_seconds", "Embedding generation", ["kind"],
                               buckets=LATENCY + (60, 120))
RETRIEVAL_DURATION = Histogram("nexus_retrieval_duration_seconds", "Permission-scoped retrieval",
                               ["mode"], buckets=LATENCY)
RAG_DURATION = Histogram("nexus_rag_answer_duration_seconds", "End-to-end /api/ask", ["answer_mode"],
                         buckets=LATENCY)
STORAGE_OPS = Counter("nexus_object_storage_operations_total", "Object storage operations",
                      ["operation", "outcome"])
STORAGE_DURATION = Histogram("nexus_object_storage_duration_seconds", "Object storage latency",
                             ["operation"], buckets=LATENCY)
EXTRACTIONS = Counter("nexus_extractions_total", "Structured extractions", ["provider", "doc_type", "outcome"])
REVIEW_ACTIONS = Counter("nexus_review_actions_total", "Human review actions", ["action"])
DOCUMENTS_UPLOADED = Counter("nexus_documents_uploaded_total", "Accepted uploads", ["format"])
DOCUMENTS_DELETED = Counter("nexus_documents_deleted_total", "Soft-deleted documents", ["reason"])
DOCUMENTS_PURGED = Counter("nexus_documents_purged_total", "Documents purged by the worker")
WORKER_HEARTBEAT = Gauge("nexus_worker_last_heartbeat_timestamp_seconds", "Last worker heartbeat")
WORKER_BUSY = Gauge("nexus_worker_busy", "1 while the worker executes a job")


class DbPoolCollector(Collector):
    """Connection-pool gauges per engine role (api / worker)."""

    def __init__(self):
        self._engines: dict[str, object] = {}

    def register(self, label: str, engine) -> None:
        self._engines[label] = engine

    def collect(self):
        fam = GaugeMetricFamily("nexus_db_pool_connections", "SQLAlchemy pool connections",
                                labels=["engine", "state"])
        for label, engine in list(self._engines.items()):
            pool = getattr(engine, "pool", None)
            for state, fn in (("checked_out", "checkedout"), ("checked_in", "checkedin"),
                              ("overflow", "overflow"), ("size", "size")):
                getter = getattr(pool, fn, None)
                if callable(getter):
                    try:
                        fam.add_metric([label, state], float(getter()))
                    except Exception:  # noqa: BLE001
                        continue
        yield fam


class QueueDepthCollector(Collector):
    """Queue depth per job type / status, computed at scrape time (aggregates only)."""

    def __init__(self):
        self._source: Callable[[], list[tuple[str, str, int, float]]] | None = None

    def set_source(self, source: Callable[[], list[tuple[str, str, int, float]]]) -> None:
        self._source = source

    def collect(self):
        depth = GaugeMetricFamily("nexus_jobs_queue_depth", "Jobs by type and status",
                                  labels=["job_type", "status"])
        oldest = GaugeMetricFamily("nexus_jobs_oldest_age_seconds", "Age of the oldest job by status",
                                   labels=["job_type", "status"])
        up = GaugeMetricFamily("nexus_jobs_queue_stats_up", "1 if queue statistics could be read")
        ok = 0.0
        if self._source is not None:
            try:
                for job_type, status, count, age in self._source():
                    depth.add_metric([job_type, status], float(count))
                    oldest.add_metric([job_type, status], float(age or 0))
                ok = 1.0
            except Exception as exc:  # noqa: BLE001 - a scrape must not fail on a DB outage
                log.warning("queue statistics unavailable: %s", type(exc).__name__)
        up.add_metric([], ok)
        yield depth
        yield oldest
        yield up


DB_POOL = DbPoolCollector()
QUEUE_DEPTH = QueueDepthCollector()
_registered = False
_server_lock = threading.Lock()
_server_started = False


def register_collectors() -> None:
    global _registered
    if not _registered:
        REGISTRY.register(DB_POOL)
        REGISTRY.register(QUEUE_DEPTH)
        _registered = True


def start_metrics_server(port: int) -> bool:
    """Serve /metrics on an internal port (0 = disabled). Idempotent per process."""
    global _server_started
    if port <= 0:
        return False
    with _server_lock:
        if _server_started:
            return True
        start_http_server(port, addr="0.0.0.0")  # inside the container network only (not published)
        _server_started = True
    log.info("metrics_server_started", extra={"event": "metrics_server_started", "port": port})
    return True
