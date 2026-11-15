"""Phase 4E: structured logging and redaction, metrics, readiness, request ids and trace
propagation across the durable queue."""
import io
import json
import logging
import re
from dataclasses import replace

import pytest
from prometheus_client import REGISTRY, generate_latest
from sqlalchemy import create_engine

from backend.app.observability.logging import JsonFormatter, configure_logging, redact, redact_fields

from .fixtures import make_pdf

CANARY = "ZEBRA-CANARY-9931 confidential salary 777,777 EUR"


# ----------------------------------------------------------------------------- redaction

@pytest.mark.parametrize("raw, forbidden", [
    ("Authorization: Bearer eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl", "eyJhbGciOiJSUzI1NiJ9"),
    ("token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1In0.abc", "eyJzdWIiOiJ1In0"),
    ("login failed password=Hunter2!! for user", "Hunter2!!"),
    ('{"client_secret": "nexus-bff-secret"}', "nexus-bff-secret"),
    ("refresh_token=8f1e2d3c4b5a69788796", "8f1e2d3c4b5a69788796"),
    ("OPENAI key sk-proj-ABCDEFGHIJKLMNOP1234", "sk-proj-ABCDEFGHIJKLMNOP1234"),
    ("aws AKIAABCDEFGHIJKLMNOP leaked", "AKIAABCDEFGHIJKLMNOP"),
    ("url ?X-Amz-Signature=deadbeef1234&X-Amz-Credential=AKIA", "deadbeef1234"),
])
def test_secret_shapes_are_redacted(raw, forbidden):
    assert forbidden not in redact(raw)


def test_content_fields_are_dropped_and_identifiers_kept():
    fields = redact_fields({"document_id": "d1", "content": CANARY, "prompt": CANARY, "value": CANARY,
                            "nested": {"excerpt": CANARY, "count": 3}, "question": CANARY, "duration_ms": 1.5})
    dumped = json.dumps(fields)
    assert CANARY not in dumped and fields["document_id"] == "d1" and fields["nested"]["count"] == 3


def test_json_formatter_shape():
    record = logging.LogRecord("nexus.test", logging.WARNING, __file__, 1, "job %s failed", ("j1",), None)
    record.event, record.error_code, record.text = "job_failed", "parse_failed", CANARY
    payload = json.loads(JsonFormatter("nexus-api", "test").format(record))
    assert {"timestamp", "severity", "service", "environment", "logger", "message"} <= set(payload)
    assert payload["severity"] == "WARNING" and payload["service"] == "nexus-api"
    assert payload["fields"]["error_code"] == "parse_failed" and payload["fields"]["text"] == "[REDACTED]"


# ----------------------------------------------------------------------------- canary through the API

def test_document_content_tokens_and_passwords_never_reach_the_logs(scenario):
    client, token, factory = scenario
    stream = io.StringIO()
    configure_logging("nexus-api", "test", "DEBUG", "json", stream=stream)
    try:
        login = client.post("/api/auth/login", json={"email": "reviewer@northstar.demo", "password": "TestPass123!"})
        bearer = {"Authorization": "Bearer " + login.json()["access_token"]}
        client.cookies.clear()
        body = [f"Agreement KR-CANARY-1. {CANARY}.", "Counterparty: Canary Partner d.o.o.", "End date: 2029-01-31"]
        up = client.post("/api/documents", headers=bearer, data={"groups": "finance"},
                         files={"file": ("canary.pdf", make_pdf([body]), "application/pdf")})
        assert up.status_code == 201
        doc_id = up.json()["id"]
        ask = client.post("/api/ask", headers=bearer, json={"question": "ZEBRA-CANARY-9931 salary", "mode": "bm25"})
        assert CANARY.split()[0] in ask.text  # the user DOES get the evidence ...
        ext = client.get(f"/api/documents/{doc_id}/extraction", headers=bearer).json()
        client.patch(f"/api/extractions/{ext['id']}/fields", headers=bearer, json={
            "expected_version": ext["version"], "corrections": [{"name": "counterparty", "value": CANARY}]})
        client.post("/api/auth/login", json={"email": "hr@northstar.demo", "password": "wrong-password-123"})
        client.get(f"/api/documents/{doc_id}", headers={"Authorization": "Bearer not.a.token"})
    finally:
        logging.getLogger().handlers.clear()
    output = stream.getvalue()
    lines = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    assert lines, "no structured log lines captured"
    for needle in ("ZEBRA-CANARY-9931", "777,777", "Canary Partner", "TestPass123!", "wrong-password-123",
                   login.json()["access_token"], login.json()["csrf_token"]):
        assert needle not in output, needle  # ... but none of it is logged
    access = [line for line in lines if line.get("message") == "http_request"]
    assert access and all(re.fullmatch(r"[0-9a-f]{32}", line["request_id"]) for line in access)
    assert any(line["fields"]["route"] == "/api/documents/{document_id}" for line in access)
    assert any(line.get("tenant_id") == "northstar" for line in access)
    assert any(line["message"] == "auth_failed" for line in lines)


def test_request_id_is_echoed_and_security_headers_are_set(scenario):
    client, _, _ = scenario
    r = client.get("/api/health/live", headers={"X-Request-ID": "req-12345678"})
    assert r.headers["x-request-id"] == "req-12345678"
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    injected = client.get("/api/health/live", headers={"X-Request-ID": "bad id\r\nX-Evil: 1"})
    assert re.fullmatch(r"[0-9a-f]{32}", injected.headers["x-request-id"])
    assert client.get("/api/auth/config").headers["cache-control"] == "no-store"


# ----------------------------------------------------------------------------- metrics

def test_metrics_use_low_cardinality_labels(scenario):
    client, token, _ = scenario
    finance = token("finance@northstar.demo")
    client.get("/api/documents/d_fin", headers=finance)
    client.get("/api/documents/does-not-exist", headers=finance)
    client.post("/api/ask", headers=finance, json={"question": "Atlas notice period", "mode": "bm25"})
    client.get("/api/documents", headers={"Authorization": "Bearer garbage"})
    text = generate_latest(REGISTRY).decode()
    assert 'route="/api/documents/{document_id}"' in text
    for name in ("nexus_http_requests_total", "nexus_http_request_duration_seconds_bucket",
                 'nexus_retrieval_duration_seconds_bucket{le="0.005",mode="bm25"}',
                 "nexus_rag_answer_duration_seconds_bucket", 'nexus_auth_failures_total{method="demo_bearer"'):
        assert name in text, name
    samples = [line for line in text.splitlines() if line.startswith("nexus_") and "{" in line]
    for line in samples:
        labels = line[line.index("{"):line.index("}")]
        for forbidden in ("d_fin", "does-not-exist", "northstar", "u1", "finance@", "Atlas"):
            assert forbidden not in labels, line


def test_queue_depth_and_pool_collectors(scenario):
    from backend.app.observability.metrics import DB_POOL, QUEUE_DEPTH, register_collectors

    client, token, factory = scenario
    register_collectors()
    from backend.app.jobs import queue

    def source():
        with factory() as db:
            return queue.queue_stats(db)
    QUEUE_DEPTH.set_source(source)
    DB_POOL.register("test", factory.kw["bind"])
    with factory() as db:
        queue.enqueue(db, tenant_id="northstar", user_id="u1", job_type="reindex", document_id="d_fin")
        db.commit()
    text = generate_latest(REGISTRY).decode()
    assert 'nexus_jobs_queue_depth{job_type="reindex",status="queued"} 1.0' in text
    assert "nexus_jobs_queue_stats_up 1.0" in text
    assert "# TYPE nexus_db_pool_connections gauge" in text
    if factory.kw["bind"].dialect.name == "postgresql":  # QueuePool (SQLite tests use StaticPool)
        assert 'nexus_db_pool_connections{engine="test",state="checked_out"}' in text


# ----------------------------------------------------------------------------- health

def test_liveness_never_touches_dependencies_and_readiness_reports_components(scenario):
    client, _, factory = scenario
    assert client.get("/api/health/live").json() == {"status": "alive"}
    ready = client.get("/api/health/ready")
    checks = ready.json()["checks"]
    assert checks["database"] == "ok" and checks["object_storage"] == "ok"
    if factory.kw["bind"].dialect.name == "postgresql":  # migrated with Alembic
        assert ready.status_code == 200 and checks["schema"] == "ok", ready.text
    else:  # the SQLite unit-test schema comes from create_all(): correctly reported as not current
        assert ready.status_code == 503 and checks["schema"] == "outdated"


def test_readiness_is_503_when_the_database_is_down_and_leaks_no_details(scenario):
    from backend.app.database import get_db
    from backend.app.main import app
    from sqlalchemy.orm import sessionmaker

    client, _, _ = scenario
    dead = create_engine("postgresql+psycopg://nobody:secret-pw@127.0.0.1:9/nowhere",
                         connect_args={"connect_timeout": 1})
    factory = sessionmaker(dead)

    def broken_db():
        with factory() as db:
            yield db
    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = broken_db
    try:
        r = client.get("/api/health/ready")
    finally:
        app.dependency_overrides[get_db] = previous
        dead.dispose()
    assert r.status_code == 503 and r.json()["checks"]["database"] == "unavailable"
    for secret in ("secret-pw", "nobody", "127.0.0.1", "nowhere", "psycopg"):
        assert secret not in r.text


def test_readiness_reports_unavailable_object_storage(scenario, monkeypatch):
    from backend.app import main

    client, _, _ = scenario
    monkeypatch.setattr(main, "settings", replace(main.settings, storage_backend="s3", s3_endpoint_url="http://127.0.0.1:9",
                                                  s3_bucket="b", s3_access_key_id="a", s3_secret_access_key="b",
                                                  s3_max_attempts=1, s3_connect_timeout=0.5))
    r = client.get("/api/health/ready")
    assert r.status_code == 503 and r.json()["checks"]["object_storage"] == "unavailable"


def test_api_answers_503_not_500_when_the_database_is_unreachable(scenario):
    from backend.app.database import get_db
    from backend.app.main import app
    from sqlalchemy.orm import sessionmaker

    client, token, _ = scenario
    headers = token("finance@northstar.demo")
    dead = create_engine("postgresql+psycopg://x:y@127.0.0.1:9/z", connect_args={"connect_timeout": 1})
    factory = sessionmaker(dead)

    def broken_db():
        with factory() as db:
            yield db
    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = broken_db
    try:
        r = client.get("/api/documents", headers=headers)
    finally:
        app.dependency_overrides[get_db] = previous
        dead.dispose()
    assert r.status_code == 503 and "temporarily unavailable" in r.json()["detail"]


# ----------------------------------------------------------------------------- tracing

def test_trace_context_crosses_the_queue_boundary(scenario):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from backend.app import main
    from backend.app.jobs import queue
    from backend.app.jobs.runner import run_inline
    from backend.app.observability import tracing

    from .conftest import api_factories

    client, token, factory = scenario
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    old = tracing.tracer
    tracing.tracer = tracer
    try:
        with tracer.start_as_current_span("http.request /api/documents/{id}/reindex") as parent:
            with factory() as db:
                job = queue.enqueue(db, tenant_id="northstar", user_id="u1", job_type="reindex", document_id="d_fin")
                db.commit()
            trace_id = parent.get_span_context().trace_id
        assert job.trace_context and "traceparent" in job.trace_context
        _, worker_factory = api_factories(factory)
        from backend.app.embeddings import get_embedder
        outcome = run_inline(job.id, cfg=main.settings, sessions=worker_factory, embedder_factory=get_embedder)
        assert outcome.status == "completed"
    finally:
        tracing.tracer = old
    spans = {s.name: s for s in exporter.get_finished_spans()}
    executed = spans["job.execute"]
    assert executed.context.trace_id == trace_id  # same trace as the enqueuing request
    assert executed.parent is not None and executed.attributes["job.type"] == "reindex"
    assert spans["job.stage.embedding"].context.trace_id == trace_id
