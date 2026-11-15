"""Phase 4C / 4D: durable jobs and the document lifecycle through the real API and a real worker
loop (backend.app.worker.Worker), on SQLite and on PostgreSQL (runtime roles, RLS enforced).

Failure injection is done in-process (monkeypatched storage / parser / embedder / lease clock); the
Docker drill scripts/resilience_drill.py repeats the crash and outage scenarios with real
processes and containers.
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from backend.app.embeddings import EmbeddingError
from backend.app.jobs import queue
from backend.app.models import (AuditEvent, Chunk, ChunkEmbedding, Document, DocumentGrant, DocumentProcessing,
                                DocumentVersion, Extraction, ProcessingJob, ReviewEvent, User)
from backend.app.storage import StorageUnavailable
from backend.app.worker import Worker

from .conftest import api_factories
from .fixtures import KESTREL_FACT, KESTREL_PAGES, make_pdf

ALL_MODES = ("bm25",)


def _set(monkeypatch, **values):
    from backend.app import main

    cfg = replace(main.settings, **values)
    monkeypatch.setattr(main, "settings", cfg)
    return cfg


@pytest.fixture
def queued(scenario, monkeypatch):
    """The scenario in PROCESSING_MODE=queue plus a worker bound to the worker role."""
    client, token, factory = scenario
    # Long back-off: a retry is never due by itself during a test; run_due() makes it due
    # explicitly (a 10 ms back-off made the drain loop re-claim retries under load: flaky).
    cfg = _set(monkeypatch, processing_mode="queue", job_backoff_base_seconds=30, job_backoff_max_seconds=60)
    _, worker_factory = api_factories(factory)
    worker = Worker(worker_factory, cfg, worker_id="test-worker")
    return client, token, factory, worker


def upload(client, headers, name="kestrel.pdf", data=None, groups="finance", **kw):
    return client.post("/api/documents", headers=headers, data={"groups": groups},
                       files={"file": (name, data if data is not None else make_pdf(KESTREL_PAGES), "application/pdf")}, **kw)


def drain(worker, limit=10):
    statuses = []
    for _ in range(limit):
        status = worker.run_once()
        if status is None:
            break
        statuses.append(status)
    return statuses


def ask(client, headers, question="What rebate does agreement KR-2026-77 grant?"):
    return client.post("/api/ask", headers=headers, json={"question": question, "mode": "bm25"}).json()


def run_due(factory):
    """Make retry back-off due now (instead of sleeping in tests), on the database clock."""
    with factory() as db:
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("UPDATE processing_jobs SET run_after = now() - interval '1 second' "
                            "WHERE status = 'retrying'"))
        else:
            db.execute(text("UPDATE processing_jobs SET run_after = :t WHERE status = 'retrying'"),
                       {"t": datetime.now(timezone.utc) - timedelta(seconds=1)})
        db.commit()


# ----------------------------------------------------------------------------- queue mode

def test_queue_mode_upload_returns_promptly_and_a_worker_completes_it(queued):
    client, token, factory, worker = queued
    finance = token("finance@northstar.demo")
    r = upload(client, finance)
    assert r.status_code == 202, r.text
    body = r.json()
    doc_id, job_id = body["id"], body["job"]["id"]
    assert body["lifecycle_state"] == "uploaded" and body["job"]["status"] == "queued"
    assert client.get(f"/api/jobs/{job_id}", headers=finance).json()["status"] == "queued"
    listed = {d["id"]: d for d in client.get("/api/documents", headers=finance).json()}
    assert listed[doc_id]["lifecycle_state"] == "uploaded" and listed[doc_id]["processing"]["status"] == "queued"
    assert KESTREL_FACT not in ask(client, finance)["answer"]  # not indexed yet
    assert drain(worker) == ["completed"]
    job = client.get(f"/api/jobs/{job_id}", headers=finance).json()
    assert job["status"] == "completed" and job["stage"] == "completed" and job["attempts"] == 1
    assert job["result"]["pages"] == 3 and job["result"]["checkpoint"] == "done"
    detail = client.get(f"/api/documents/{doc_id}", headers=finance).json()
    assert detail["lifecycle_state"] == "ready" and detail["page_count"] == 3
    assert KESTREL_FACT in ask(client, finance)["answer"]
    # other users still cannot see the document nor its job
    for outsider in ("hr@northstar.demo", "finance@otherco.demo"):
        assert client.get(f"/api/jobs/{job_id}", headers=token(outsider)).status_code == 404


def test_idempotency_key_prevents_duplicate_documents_and_jobs(queued):
    client, token, factory, worker = queued
    finance = token("finance@northstar.demo")
    keyed = {**finance, "Idempotency-Key": "upload-kestrel-0001"}
    first = upload(client, keyed)
    again = upload(client, keyed)
    assert first.status_code == 202 and again.status_code == 200 and again.json()["deduplicated"]
    assert again.json()["id"] == first.json()["id"]
    with factory() as db:
        assert db.query(Document).count() == 4 and db.query(ProcessingJob).count() == 1
    assert upload(client, {**finance, "Idempotency-Key": "bad key!"}).status_code == 400


def test_duplicate_reindex_requests_share_one_job(queued):
    client, token, factory, worker = queued
    rev = token("reviewer@northstar.demo")
    a = client.post("/api/documents/d_fin/reindex", headers=rev, json={})
    b = client.post("/api/documents/d_fin/reindex", headers=rev, json={})
    assert a.status_code == b.status_code == 202
    assert b.json()["deduplicated"] and a.json()["job"]["id"] == b.json()["job"]["id"]
    assert client.post("/api/documents/d_fin/reindex", headers=token("hr@northstar.demo"), json={}).status_code == 404


def test_concurrent_duplicate_submissions_create_exactly_one_job(queued):
    """Found by the resilience drill: "unfinished job? else enqueue" raced when two requests for
    one document arrived together. Advisory locks make check-and-enqueue atomic (PostgreSQL)."""
    import threading
    import time

    from backend.app import lifecycle, main
    from backend.app.database import set_security_context, user_context

    _, _, factory, _ = queued
    if factory.kw["bind"].dialect.name != "postgresql":
        pytest.skip("concurrent sessions need PostgreSQL (SQLite serializes writers itself)")
    api_factory, _ = api_factories(factory)
    barrier, results, errors = threading.Barrier(2), [], []

    def submit():
        try:
            with api_factory() as db:
                set_security_context(db, user_context("northstar", "u4"))
                user, doc = db.get(User, "u4"), db.get(Document, "d_fin")
                barrier.wait(10)
                job, created = lifecycle.submit(db, user, "reindex", doc, {"force": True}, main.settings)
                time.sleep(0.3)  # keep the transaction open: the other submission must wait, not race
                db.commit()
                results.append((job.id, created))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
    threads = [threading.Thread(target=submit) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors, errors
    assert sorted(created for _, created in results) == [False, True]
    assert len({job_id for job_id, _ in results}) == 1
    with factory() as db:
        jobs = db.scalars(select(ProcessingJob).where(ProcessingJob.document_id == "d_fin",
                                                      ProcessingJob.job_type == "reindex")).all()
    assert len(jobs) == 1


def test_transient_storage_failure_retries_with_backoff_then_succeeds(queued, monkeypatch):
    client, token, factory, worker = queued
    finance = token("finance@northstar.demo")
    job_id = upload(client, finance).json()["job"]["id"]
    from backend.app.jobs import handlers

    real = handlers.store_for

    class Down:
        def get(self, key):
            raise StorageUnavailable("object storage unreachable (EndpointConnectionError)")
    monkeypatch.setattr(handlers, "store_for", lambda backend, cfg: Down())
    assert drain(worker) == ["retrying"]
    job = client.get(f"/api/jobs/{job_id}", headers=finance).json()
    assert job["status"] == "retrying" and job["error_code"] == "storage_unavailable" and job["attempts"] == 1
    # exponential back-off (attempt 1): between 50 % and 100 % of the 30 s base
    delay = (datetime.fromisoformat(job["run_after"]) - datetime.fromisoformat(job["updated_at"])).total_seconds()
    assert 14 <= delay <= 31, delay
    monkeypatch.setattr(handlers, "store_for", real)
    run_due(factory)
    assert drain(worker) == ["completed"]
    final = client.get(f"/api/jobs/{job_id}", headers=finance).json()
    assert final["status"] == "completed" and final["attempts"] == 2 and final["error_code"] is None


def test_permanent_parse_failure_dead_letters_and_can_be_retried(queued, monkeypatch):
    client, token, factory, worker = queued
    finance = token("finance@northstar.demo")
    broken = b"%PDF-1.4\nthis is not really a PDF"
    body = upload(client, finance, name="broken.pdf", data=broken).json()
    assert drain(worker) == ["failed"]
    job = client.get(f"/api/jobs/{body['job']['id']}", headers=finance).json()
    assert job["status"] == "failed" and job["error_code"] == "parse_failed" and job["retry_available"]
    assert "Document could not be parsed" in job["error_message"] and "not really" not in job["error_message"]
    doc = client.get(f"/api/documents/{body['id']}", headers=finance).json()
    assert doc["lifecycle_state"] == "failed" and doc["processing"]["error_code"] == "parse_failed"
    assert client.get("/api/jobs", headers=finance, params={"status": "failed"}).json()[0]["id"] == job["id"]
    # the dead-lettered job is retried on request (still fails: it is a genuinely broken file)
    assert client.post(f"/api/jobs/{job['id']}/retry", headers=token("hr@northstar.demo")).status_code == 404
    retried = client.post(f"/api/jobs/{job['id']}/retry", headers=finance)
    assert retried.status_code == 202 and retried.json()["status"] == "queued"
    assert drain(worker) == ["failed"]


def test_max_attempts_moves_a_job_to_failed(queued, monkeypatch):
    client, token, factory, worker = queued
    finance = token("finance@northstar.demo")
    job_id = upload(client, finance).json()["job"]["id"]
    with factory() as db:
        db.get(ProcessingJob, job_id).max_attempts = 2
        db.commit()
    from backend.app.jobs import handlers

    monkeypatch.setattr(handlers, "store_for", lambda b, c: type("D", (), {"get": lambda s, k: (_ for _ in ()).throw(
        StorageUnavailable("down"))})())
    assert drain(worker) == ["retrying"]
    run_due(factory)
    assert drain(worker) == ["failed"]
    assert client.get(f"/api/jobs/{job_id}", headers=finance).json()["status"] == "failed"


def test_embedding_outage_keeps_the_document_keyword_searchable_and_retries(queued, monkeypatch):
    client, token, factory, worker = queued
    if factory.kw["bind"].dialect.name != "postgresql":
        pytest.skip("embeddings are PostgreSQL-only")
    finance = token("finance@northstar.demo")
    body = upload(client, finance).json()

    class Broken:
        model_name = "intfloat/multilingual-e5-small"
        revision = "x"

        def embed_passages(self, texts):
            raise EmbeddingError("model files missing")
    worker.embedder_factory = lambda: Broken()
    assert drain(worker) == ["retrying"]
    job = client.get(f"/api/jobs/{body['job']['id']}", headers=finance).json()
    assert job["error_code"] == "embedding_unavailable" and job["result"]["checkpoint"] == "indexed"
    assert KESTREL_FACT in ask(client, finance)["answer"]  # chunks are committed: keyword search works
    from backend.app.embeddings import get_embedder
    worker.embedder_factory = get_embedder
    run_due(factory)
    assert drain(worker) == ["completed"]
    with factory() as db:
        n_chunks = db.query(Chunk).filter(Chunk.document_id == body["id"]).count()
        n_vectors = db.query(ChunkEmbedding).join(Chunk, Chunk.id == ChunkEmbedding.chunk_id)\
            .filter(Chunk.document_id == body["id"]).count()
        events = db.scalars(select(ReviewEvent.action).where(ReviewEvent.document_id == body["id"])).all()
    assert n_chunks == n_vectors > 0
    assert events == ["process"]  # the retry resumed at the checkpoint: no duplicate extraction


def test_crashed_worker_lease_is_recovered_and_the_stale_worker_is_fenced(queued):
    client, token, factory, worker = queued
    finance = token("finance@northstar.demo")
    job_id = upload(client, finance).json()["job"]["id"]
    _, worker_factory = api_factories(factory)
    with worker_factory() as db:
        crashed = queue.claim(db, "worker-that-dies", 30)  # claimed ... then the process dies
    assert crashed.id == job_id and crashed.attempts == 1
    with factory() as db:  # time passes beyond the lease
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("UPDATE processing_jobs SET locked_until = now() - interval '1 second' WHERE id = :i"),
                       {"i": job_id})
        else:
            db.execute(text("UPDATE processing_jobs SET locked_until = :t WHERE id = :i"),
                       {"t": datetime.now(timezone.utc) - timedelta(seconds=1), "i": job_id})
        db.commit()
    assert drain(worker) == ["completed"]
    job = client.get(f"/api/jobs/{job_id}", headers=finance).json()
    assert job["status"] == "completed" and job["attempts"] == 2
    # the zombie wakes up and tries to write: refused by the fencing token (and by RLS on PostgreSQL)
    with worker_factory() as db:
        with pytest.raises(queue.LeaseLost):
            queue.lock(db, crashed)
    with factory() as db:
        assert db.query(Chunk).filter(Chunk.document_id == job["document_id"]).count() > 0
        assert db.query(Document).count() == 4  # no duplicate document


def test_document_deleted_while_queued_cancels_processing(queued):
    client, token, factory, worker = queued
    finance = token("finance@northstar.demo")
    body = upload(client, finance).json()
    assert client.delete(f"/api/documents/{body['id']}", headers=finance).status_code == 202
    statuses = drain(worker)
    assert sorted(statuses) == ["cancelled", "completed"]  # ingest cancelled, purge completed
    ingest = client.get(f"/api/jobs/{body['job']['id']}", headers=finance).json()
    assert ingest["status"] == "cancelled" and ingest["error_code"] == "document_deleted"
    with factory() as db:
        assert db.query(Chunk).filter(Chunk.document_id == body["id"]).count() == 0
        assert db.get(Document, body["id"]).lifecycle_state == "purged"


def test_permission_revoked_between_submission_and_execution_fails_the_job(queued):
    client, token, factory, worker = queued
    reviewer = token("reviewer@northstar.demo")  # u4, sees d_fin through the 'finance' grant
    r = client.post("/api/documents/d_fin/process", headers=reviewer, json={})
    assert r.status_code == 202
    with factory() as db:
        db.get(User, "u4").groups = ["legal"]  # removed from 'finance' before the worker runs
        db.commit()
    assert drain(worker) == ["failed"]
    job = client.get(f"/api/jobs/{r.json()['job']['id']}", headers=reviewer).json()
    assert job["status"] == "failed" and job["error_code"] == "authorization_revoked"
    with factory() as db:
        assert db.query(Extraction).filter(Extraction.document_id == "d_fin").count() == 0


def test_cancel_queued_job_and_purge_jobs_cannot_be_cancelled(queued):
    client, token, factory, worker = queued
    finance = token("finance@northstar.demo")
    body = upload(client, finance).json()
    cancelled = client.post(f"/api/jobs/{body['job']['id']}/cancel", headers=finance)
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert client.post(f"/api/jobs/{body['job']['id']}/cancel", headers=finance).status_code == 409
    purge = client.delete(f"/api/documents/{body['id']}", headers=finance).json()["purge_job"]
    assert client.post(f"/api/jobs/{purge['id']}/cancel", headers=finance).status_code == 409
    assert drain(worker) == ["completed"]


def test_worker_reports_heartbeats_for_readiness(queued):
    client, token, factory, worker = queued
    worker.beat(force=True)
    from backend.app.models import WorkerHeartbeat
    with factory() as db:
        beat = db.get(WorkerHeartbeat, "test-worker")
        assert beat is not None and beat.version
    ready = client.get("/api/health/ready").json()
    assert ready["checks"]["workers"] == "ok"


def test_backoff_is_exponential_bounded_and_jittered():
    import random

    rng = random.Random(7)
    delays = [queue.backoff_seconds(a, 5, 300, rng) for a in range(1, 9)]
    assert all(2.5 <= delays[0] <= 5 for _ in [0])
    assert all(0.5 * min(300, 5 * 2 ** (a - 1)) <= d <= min(300, 5 * 2 ** (a - 1))
               for a, d in zip(range(1, 9), delays))
    assert max(delays) <= 300


# ----------------------------------------------------------------------------- lifecycle (inline)

def test_new_version_replaces_the_index_and_keeps_old_versions_downloadable(scenario):
    client, token, factory = scenario
    finance = token("finance@northstar.demo")
    v1 = make_pdf([["Contract KR-2026-77 version one.", "Rebate: 7 percent."]])
    doc_id = upload(client, finance, data=v1).json()["id"]
    old_chunks = {c["chunk_id"] for c in ask(client, finance, "KR-2026-77 rebate")["sources"]}
    v2 = make_pdf([["Contract KR-2026-77 version two.", "Rebate: 9 percent."]])
    r = client.post(f"/api/documents/{doc_id}/versions", headers=finance,
                    files={"file": ("kestrel-v2.pdf", v2, "application/pdf")})
    assert r.status_code == 202 and r.json()["version"] == 2 and r.json()["job"]["status"] == "completed"
    versions = client.get(f"/api/documents/{doc_id}/versions", headers=finance).json()
    assert versions["current_version"] == 2 and [v["state"] for v in versions["versions"]] == ["active", "superseded"]
    answer = ask(client, finance, "KR-2026-77 rebate")
    assert "9 percent" in answer["answer"] and "7 percent" not in answer["answer"]
    assert not old_chunks & {s["chunk_id"] for s in answer["sources"]}  # old chunk ids are gone
    assert client.get(f"/api/documents/{doc_id}/versions/1/source", headers=finance).content == v1
    assert client.get(f"/api/documents/{doc_id}/source", headers=finance).content == v2
    # only the owner / an admin may add versions; group members may not
    rev = token("reviewer@northstar.demo")
    assert client.post(f"/api/documents/{doc_id}/versions", headers=rev,
                       files={"file": ("x.pdf", v2, "application/pdf")}).status_code == 403


def test_delete_removes_the_document_from_every_read_path_immediately_and_purges_it(scenario):
    client, token, factory = scenario
    finance, admin = token("finance@northstar.demo"), token("admin@northstar.demo")
    doc_id = upload(client, finance).json()["id"]
    first = ask(client, finance)
    cited = first["sources"][0]["chunk_id"]
    assert first["sources"][0]["document_id"] == doc_id
    eid = client.get(f"/api/documents/{doc_id}/extraction", headers=finance).json()["id"]
    assert client.delete(f"/api/documents/{doc_id}", headers=token("hr@northstar.demo")).status_code == 404
    assert client.delete(f"/api/documents/{doc_id}", headers=token("reviewer@northstar.demo")).status_code == 403
    gone = client.delete(f"/api/documents/{doc_id}", headers=finance)
    assert gone.status_code == 202 and gone.json()["purge_job"]["status"] == "completed"
    for headers in (finance, admin):
        assert doc_id not in {d["id"] for d in client.get("/api/documents", headers=headers).json()}
        for path in (f"/api/documents/{doc_id}", f"/api/documents/{doc_id}/source", f"/api/documents/{doc_id}/versions",
                     f"/api/documents/{doc_id}/extraction", f"/api/documents/{doc_id}/structure",
                     f"/api/extractions/{eid}/history", f"/api/documents/{doc_id}/jobs"):
            assert client.get(path, headers=headers).status_code == 404, path
        answer = ask(client, headers)
        assert cited not in {s["chunk_id"] for s in answer["sources"]} and KESTREL_FACT not in answer["answer"]
        assert client.post("/api/intelligence/compare", headers=headers,
                           json={"document_ids": [doc_id, "d_fin"]}).status_code == 404
        assert doc_id not in client.get("/api/intelligence/documents", headers=headers).text
    with factory() as db:
        doc = db.get(Document, doc_id)
        assert (doc.lifecycle_state, doc.title, doc.filename, doc.page_count) == ("purged", "[deleted]", "[deleted]", 0)
        assert doc.deleted_by == "u1" and doc.purged_at is not None and doc.sha256  # tombstone for audit
        for model in (Chunk, DocumentProcessing, Extraction, DocumentGrant):
            assert db.query(model).filter(model.document_id == doc_id).count() == 0, model.__name__
        assert db.query(ReviewEvent).filter(ReviewEvent.document_id == doc_id).count() == 0
        assert db.query(ChunkEmbedding).filter(ChunkEmbedding.chunk_id == cited).count() == 0
        version = db.scalar(select(DocumentVersion).where(DocumentVersion.document_id == doc_id))
        assert version.state == "purged" and version.object_deleted_at and version.original_filename == "[purged]"
        actions = {a for a, in db.execute(select(AuditEvent.action).where(AuditEvent.document_id == doc_id))}
    assert {"upload", "document_delete", "document_purged"} <= actions  # audit history is retained
    from backend.app import main
    assert not (main.settings.storage_dir / version.storage_key).exists()  # original removed


def test_purge_refuses_a_live_document(scenario):
    client, token, factory = scenario
    _, worker_factory = api_factories(factory)
    with factory() as db:
        job = queue.enqueue(db, tenant_id="northstar", user_id="u1", job_type="purge", document_id="d_fin")
        db.commit()
    from backend.app import main
    from backend.app.jobs.runner import run_inline
    outcome = run_inline(job.id, cfg=main.settings, sessions=worker_factory, embedder_factory=lambda: None)
    assert outcome.status == "failed" and outcome.error_code == "not_deleted"
    with factory() as db:
        assert db.query(Chunk).filter(Chunk.document_id == "d_fin").count() == 1


def test_retention_expiry_soft_deletes_and_purges(scenario, monkeypatch):
    client, token, factory = scenario
    if factory.kw["bind"].dialect.name != "postgresql":
        pytest.skip("the retention sweep is a PostgreSQL function")
    finance = token("finance@northstar.demo")
    doc_id = client.post("/api/documents", headers=finance, data={"groups": "finance", "retention_days": "30"},
                         files={"file": ("short.txt", b"Retention test KR-RET-1.", "text/plain")}).json()["id"]
    with factory() as db:
        assert db.get(Document, doc_id).retention_until is not None
        db.execute(text("UPDATE documents SET retention_until = now() - interval '1 minute' WHERE id = :i"), {"i": doc_id})
        db.commit()
    _, worker_factory = api_factories(factory)
    from backend.app import main
    w = Worker(worker_factory, replace(main.settings, retention_sweep_seconds=0), worker_id="retention")
    w.sweep()
    assert client.get(f"/api/documents/{doc_id}", headers=finance).status_code == 404  # immediately invisible
    assert drain(w) == ["completed"]
    with factory() as db:
        doc = db.get(Document, doc_id)
        assert doc.lifecycle_state == "purged" and doc.deleted_by == "system:retention"
        assert db.scalar(select(AuditEvent.action).where(AuditEvent.document_id == doc_id,
                                                         AuditEvent.action == "document_expired"))


def test_reindex_rebuilds_embeddings(scenario, embedder):
    client, token, factory = scenario
    if factory.kw["bind"].dialect.name != "postgresql":
        pytest.skip("embeddings are PostgreSQL-only")
    finance = token("finance@northstar.demo")
    doc_id = upload(client, finance).json()["id"]
    with factory() as db:
        before = db.query(ChunkEmbedding).join(Chunk, Chunk.id == ChunkEmbedding.chunk_id)\
            .filter(Chunk.document_id == doc_id).count()
    r = client.post(f"/api/documents/{doc_id}/reindex", headers=finance, json={"force": True})
    assert r.status_code == 202 and r.json()["job"]["status"] == "completed"
    assert r.json()["job"]["result"]["removed"] == before == r.json()["job"]["result"]["embedded_now"]


def test_upload_rolls_back_the_object_when_the_database_transaction_fails(scenario, monkeypatch):
    client, token, factory = scenario
    from backend.app import lifecycle, main

    def boom(*args, **kwargs):
        raise RuntimeError("database write failed")
    monkeypatch.setattr(lifecycle, "create_document", boom)
    with pytest.raises(RuntimeError):
        upload(client, token("finance@northstar.demo"))
    leftovers = [p for p in main.settings.storage_dir.rglob("original")]
    assert leftovers == []  # the stored object was deleted again


def test_session_expiring_during_a_review_does_not_lose_the_review(scenario):
    client, token, factory = scenario
    login = client.post("/api/auth/login", json={"email": "reviewer@northstar.demo", "password": "TestPass123!"}).json()
    csrf = login["csrf_token"]
    doc_id = upload(client, {"Authorization": "Bearer " + login["access_token"]}).json()["id"]
    extraction = client.get(f"/api/documents/{doc_id}/extraction").json()
    from backend.app.models import AuthSession
    with factory() as db:
        for row in db.query(AuthSession):
            row.absolute_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    body = {"expected_version": extraction["version"], "corrections": [{"name": "currency", "value": "EUR"}]}
    expired = client.patch(f"/api/extractions/{extraction['id']}/fields", json=body, headers={"X-CSRF-Token": csrf})
    assert expired.status_code == 401 and expired.json()["detail"]["code"] == "session_expired"
    with factory() as db:
        assert db.get(Extraction, extraction["id"]).version == extraction["version"]  # nothing half-applied
    again = client.post("/api/auth/login", json={"email": "reviewer@northstar.demo", "password": "TestPass123!"}).json()
    saved = client.patch(f"/api/extractions/{extraction['id']}/fields", json=body,
                         headers={"X-CSRF-Token": again["csrf_token"]})
    assert saved.status_code == 200 and saved.json()["version"] == extraction["version"] + 1
    client.cookies.clear()
