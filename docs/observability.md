# Observability (Phase 4E)

Three independent signals, all optional for the lightweight demo and all free of document content.

```mermaid
flowchart LR
  API[nexus-api] -- JSON logs --> STDOUT[(container stdout)]
  W[nexus-worker xN] -- JSON logs --> STDOUT
  API -- ":9100/metrics (internal)" --> P[Prometheus]
  W -- ":9100/metrics (internal)" --> P
  API -- OTLP/HTTP --> J[Jaeger]
  W -- OTLP/HTTP --> J
  P --> G[Grafana dashboard]
  J --> G
  API -. traceparent stored in processing_jobs.trace_context .-> W
```

## Structured logs

`backend/app/observability/logging.py`; one JSON object per line on stdout (`LOG_FORMAT=json`,
`text` for local reading):

```json
{"timestamp": "2026-11-15T19:04:11.912+00:00", "severity": "INFO", "service": "nexus-worker",
 "environment": "local", "logger": "nexus.jobs", "message": "job_finished",
 "job_id": "6d0c…", "job_type": "ingest", "tenant_id": "northstar", "document_id": "4a1e…", "attempt": 1,
 "trace_id": "8f3c…", "span_id": "1b7a…",
 "fields": {"event": "job_finished", "status": "completed", "error_code": null, "duration_ms": 4123.4}}
```

Context fields come from a per-request / per-job context (`request_id`, `tenant_id`, `user_id`,
`auth` method, `job_id`, `job_type`, `document_id`, `attempt`) and the active OTel span. Events:
`http_request` (method, **route template**, status, duration), `auth_failed` (method, reason
category), `login`, `backchannel_logout`, `job_finished`, `job_database_error` (SQLSTATE and table,
never the statement parameters), `extraction_failed`, `heartbeat_failed`, `retention_*`,
`database_unavailable`, `api_started` / `worker_started`.

**Never logged**: access / refresh / ID / logout tokens, authorization codes, PKCE verifiers,
passwords, API keys, cookies, CSRF tokens, document text, extracted values, prompts, questions.
Two layers: call sites log identifiers / counts / categories only, and the formatter redacts secret
shapes (bearer and JWT strings, `password=`-style pairs, OpenAI / AWS keys, presigned-URL
signatures) and drops fields named like content or credentials (`content`, `text`, `prompt`,
`value`, `question`, `token`, `password`, ...). SQLAlchemy errors are never stringified (they embed
statement parameters). `test_observability.py` feeds a canary through upload, ask, extraction,
correction and failed logins and asserts that neither the canary, the password nor the tokens
appear in the captured output.

## Metrics (Prometheus)

Served by `prometheus_client` on an **internal** port (`METRICS_PORT=9100`, not published by
Compose); `docker/observability/prometheus.yml` scrapes `api:9100` and every worker replica (DNS
service discovery). Labels are fixed small vocabularies: method, route template, status code,
retrieval mode, job type, outcome, error category, storage operation, format. Never user, document
or tenant ids (`test_metrics_use_low_cardinality_labels`).

| Metric | Type | Labels |
|---|---|---|
| `nexus_http_requests_total`, `nexus_http_request_duration_seconds`, `nexus_http_requests_in_progress` | counter, histogram, gauge | method, route, status |
| `nexus_auth_failures_total`, `nexus_auth_logins_total` | counter | method, reason / mode |
| `nexus_jobs_enqueued_total`, `nexus_jobs_finished_total`, `nexus_job_duration_seconds` | counter, counter, histogram | job_type, outcome |
| `nexus_job_queue_wait_seconds` | histogram | job_type |
| `nexus_job_retries_total`, `nexus_job_failures_total` (dead letter) | counter | job_type, error_code |
| `nexus_jobs_queue_depth`, `nexus_jobs_oldest_age_seconds`, `nexus_jobs_queue_stats_up` | gauge (at scrape, aggregated across tenants) | job_type, status |
| `nexus_processing_stage_duration_seconds` | histogram | stage |
| `nexus_document_ingestion_duration_seconds` | histogram | format |
| `nexus_ocr_pages_total`, `nexus_ocr_document_parse_seconds` | counter, histogram | — |
| `nexus_embedding_duration_seconds` | histogram | kind |
| `nexus_retrieval_duration_seconds`, `nexus_rag_answer_duration_seconds` | histogram | mode / answer_mode |
| `nexus_object_storage_operations_total`, `nexus_object_storage_duration_seconds` | counter, histogram | operation, outcome |
| `nexus_db_pool_connections` | gauge | engine (api / worker), state |
| `nexus_db_pool_timeouts_total` (requests shed with 503 because no pooled connection became free within `DB_POOL_TIMEOUT_SECONDS`) | counter | — |
| `nexus_extractions_total`, `nexus_review_actions_total` | counter | provider, doc_type, outcome / action |
| `nexus_documents_uploaded_total`, `nexus_documents_deleted_total`, `nexus_documents_purged_total` | counter | format / reason |
| `nexus_worker_busy`, `nexus_worker_last_heartbeat_timestamp_seconds` | gauge | — |
| process / Python runtime collectors | default | — |

Alert examples (`docker/observability/alerts.yml`, validated with `promtool`): 5xx share > 5 %,
p95 latency > 2 s, queue backlog, oldest queued job > 15 min, dead-lettered jobs, no worker scraped,
authentication failure spikes, storage errors.

## Tracing (OpenTelemetry)

`OTEL_ENABLED=true` + `OTEL_EXPORTER_OTLP_ENDPOINT` (OTLP/HTTP; Jaeger in
`compose.observability.yaml`). Instrumentation: FastAPI (server spans; health probes, the OIDC
callback and back-channel logout are excluded so authorization codes never become span
attributes), SQLAlchemy (statements without parameters), httpx (IdP calls) and manual spans
(`retrieval`, `rag.answer`, `document.parse`, `storage.put|get|delete`, `job.execute`,
`job.stage.<stage>`).

**Across the queue**: `queue.enqueue()` stores the W3C `traceparent`/`tracestate` of the active span
in `processing_jobs.trace_context`; the worker starts `job.execute` as a **child** of that context,
so one trace contains the upload request and the asynchronous processing it caused
(`test_trace_context_crosses_the_queue_boundary`; verified in Docker by `scripts/e2e_phase4.py`
through the Jaeger API). Sampling: `OTEL_TRACES_SAMPLER_RATIO` (parent-based). Disabled tracing
uses the no-op tracer.

## Health

| Endpoint | Semantics |
|---|---|
| `GET /api/health/live` | the process serves HTTP; touches no dependency (no restart loops during a database blip) |
| `GET /api/health/ready` | 200 when database, schema revision, object storage and (OIDC) the IdP discovery are OK, else 503. Body: component states only (`ok` / `unavailable` / `outdated`; `workers`: `ok` / `none_active`, `embedding_model`: `loaded` / `not_loaded` mark `degraded`). No hosts, versions, credentials or error texts |
| `GET /api/health` | public capability summary for the UI (unchanged contract + `auth_mode`, `processing_mode`, `storage_backend`) |
| worker | `python -m backend.app.worker --healthcheck` checks the heartbeat file (Compose healthcheck); heartbeats are also stored in `worker_heartbeats` for readiness |

Connection-level database failures return **503** with `Retry-After` (never a 500 with details)
(`test_api_answers_503_not_500_when_the_database_is_unreachable`, `test_readiness_*`).

## Local stack

```bash
docker compose -f compose.yaml -f compose.observability.yaml up -d --build
# Grafana    http://localhost:3001  (grafana-local-admin / grafana-local-admin-secret, local only)
# Prometheus http://localhost:9090   Jaeger http://localhost:16686
```

The provisioned dashboard **NEXUS AI — Operations** (`docker/observability/grafana/dashboards/`,
generated by `scripts/build_grafana_dashboard.py`, 18 panels) shows request rate by route, error
share, latency p50/p95/p99, retrieval p95 by mode, queue depth, job throughput and duration, queue
wait, stage durations, OCR pages and parse time, worker failures / retries / heartbeat, RAG latency,
authentication failures, storage operations, DB pool, ingestion duration, extraction and review
counts. Every panel is a PromQL query over live metrics; a screenshot captured from the running
stack is referenced in [performance-benchmarks.md](performance-benchmarks.md) when available.
