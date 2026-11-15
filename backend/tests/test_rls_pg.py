"""Phase 4B: PostgreSQL row-level security, verified with direct SQL under the runtime roles.

Every test connects as nexus_app / nexus_worker (NOSUPERUSER, NOBYPASSRLS) to the migrated test
database; seeding uses the admin role. The last section removes the APPLICATION ACL on purpose
(mutation test) and shows that the database still returns nothing unauthorized, while the same
mutation leaks on SQLite (no RLS), i.e. the test is sensitive.
"""
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from backend.app.database import SecurityContext, set_security_context, user_context
from backend.app.jobs import queue
from backend.app.models import (AuditEvent, ChunkEmbedding, Document, DocumentProcessing,
                                Extraction, ExtractionField, ProcessingJob, ReviewEvent, User)

from .conftest import RUNTIME_PASSWORD, make_client, seed, truncate

pytestmark = [pytest.mark.postgres, pytest.mark.rls]
PROTECTED = {"users", "user_identities", "auth_sessions", "documents", "document_grants", "document_versions",
             "chunks", "chunk_embeddings", "document_processing", "document_tables", "extractions",
             "extraction_fields", "review_events", "audit_events", "processing_jobs"}
UNPROTECTED = {"alembic_version", "auth_login_states", "worker_heartbeats"}
DIM = 384


def vec(seed_value: float) -> list[float]:
    v = [seed_value] + [0.0] * (DIM - 1)
    return v


@pytest.fixture
def world(pg_engine, app_engine, worker_engine):
    """Two tenants; northstar has an hr doc, a finance doc and an hr-only extraction; otherco has
    a secret. Every derived table has rows for every document."""
    truncate(pg_engine)
    admin = sessionmaker(pg_engine, expire_on_commit=False)
    seed(admin)  # u0 hr, u1 finance, u2 admin, u3 otherco finance, u4 northstar reviewer, u5 otherco reviewer
    now = datetime.now(timezone.utc)
    with admin() as db:
        for doc_id, tenant in (("d_hr", "northstar"), ("d_fin", "northstar"), ("d_other", "otherco")):
            db.add(ChunkEmbedding(chunk_id=f"{doc_id}_c0", model_name="test-model", model_revision="t",
                                  embedding_dim=DIM, normalized=True, content_sha256="0" * 64,
                                  embedding=vec(1.0 if doc_id == "d_other" else 0.5)))
            db.add(DocumentProcessing(document_id=doc_id, status="processed", doc_format="txt", parser="t",
                                      page_count=1, ocr_pages=[], structure={}, attempts=1, updated_at=now))
            ext = Extraction(id=f"e_{doc_id}", document_id=doc_id, tenant_id=tenant, doc_type="contract",
                             schema_version="1", provider="rules", status="needs_review", version=1, issues=[],
                             created_at=now, updated_at=now)
            db.add(ext)
            db.flush()
            db.add(ExtractionField(extraction_id=ext.id, name="counterparty", position=0, value_type="string",
                                   value=f"secret value of {doc_id}", method="rules", grounding="exact", issues=[]))
            db.add(ReviewEvent(extraction_id=ext.id, document_id=doc_id, tenant_id=tenant, user_id="u2",
                               action="process", extraction_version=1, created_at=now))
            db.add(AuditEvent(user_id="u3" if tenant == "otherco" else "u2", tenant_id=tenant, action="upload",
                              document_id=doc_id, created_at=now))
            db.add(ProcessingJob(id=str(uuid4()), tenant_id=tenant, user_id="u3" if tenant == "otherco" else "u2",
                                 document_id=doc_id, job_type="ingest", status="completed", payload={},
                                 priority=100, attempts=1, max_attempts=5, run_after=now,
                                 cancel_requested=False, created_at=now, updated_at=now))
        db.commit()
    return admin, sessionmaker(app_engine, expire_on_commit=False), sessionmaker(worker_engine, expire_on_commit=False)


def counts(session_factory, ctx: SecurityContext | None, tables=PROTECTED - {"auth_sessions", "user_identities"}):
    out = {}
    with session_factory() as db:
        if ctx is not None:
            set_security_context(db, ctx)
        for table in sorted(tables):
            out[table] = db.execute(text(f"SELECT count(*) FROM {table}")).scalar()
        db.rollback()
    return out


# ----------------------------------------------------------------------------- schema guarantees

def test_every_tenant_table_has_forced_rls_and_only_allowlisted_tables_are_unprotected(pg_engine):
    with pg_engine.connect() as conn:
        rows = conn.execute(text("SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                                 "WHERE relkind = 'r' AND relnamespace = 'public'::regnamespace")).all()
    tables = {r[0]: (r[1], r[2]) for r in rows}
    assert set(tables) == PROTECTED | UNPROTECTED, "a new table needs RLS or an explicit allowlist entry"
    for name in PROTECTED:
        assert tables[name] == (True, True), name
    for name in UNPROTECTED:
        assert tables[name] == (False, False), name


def test_runtime_roles_are_unprivileged_and_cannot_switch_role(pg_engine, app_engine, worker_engine):
    with pg_engine.connect() as conn:
        roles = {r[0]: r[1:] for r in conn.execute(text(
            "SELECT rolname, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolinherit FROM pg_roles "
            "WHERE rolname IN ('nexus_app', 'nexus_worker', 'nexus_rls_definer')"))}
        members = conn.execute(text(
            "SELECT r.rolname FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid "
            "JOIN pg_roles u ON u.oid = m.member WHERE u.rolname IN ('nexus_app', 'nexus_worker')")).all()
    assert roles["nexus_app"][:4] == (False, False, False, False)
    assert roles["nexus_worker"][:4] == (False, False, False, False)
    assert roles["nexus_rls_definer"][1] is True  # BYPASSRLS, but NOLOGIN (owns the helper functions only)
    assert members == []
    for engine in (app_engine, worker_engine):
        with engine.connect() as conn:
            for target in ("nexus", "nexus_rls_definer", "postgres"):
                with pytest.raises(DBAPIError):
                    conn.execute(text(f"SET ROLE {target}"))
                conn.rollback()
            # row_security=off does not bypass policies, it makes the query fail instead
            conn.execute(text("SET row_security = off"))
            with pytest.raises(DBAPIError):
                conn.execute(text("SELECT count(*) FROM documents"))
            conn.rollback()


def test_force_rls_binds_a_non_superuser_table_owner(pg_engine, world):
    """Demonstrates WHY FORCE is set: a non-superuser owner would otherwise bypass every policy."""
    with pg_engine.connect() as conn:
        conn.execute(text("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_test_owner') "
                          "THEN CREATE ROLE nexus_test_owner NOLOGIN; END IF; END $$"))
        conn.commit()
        conn.execute(text("ALTER TABLE documents OWNER TO nexus_test_owner"))
        conn.execute(text("SET LOCAL ROLE nexus_test_owner"))
        forced = conn.execute(text("SELECT count(*) FROM documents")).scalar()
        conn.execute(text("RESET ROLE"))
        conn.execute(text("ALTER TABLE documents NO FORCE ROW LEVEL SECURITY"))
        conn.execute(text("SET LOCAL ROLE nexus_test_owner"))
        not_forced = conn.execute(text("SELECT count(*) FROM documents")).scalar()
        conn.rollback()  # ownership and FORCE are restored (DDL is transactional)
    assert forced == 0 and not_forced == 3
    with pg_engine.connect() as conn:
        assert conn.execute(text("SELECT relforcerowsecurity FROM pg_class WHERE relname = 'documents'")).scalar()


def test_helper_functions_are_not_public(pg_engine):
    with pg_engine.connect() as conn:
        public = conn.execute(text(
            "SELECT p.proname FROM pg_proc p WHERE p.pronamespace = 'nexus_rls'::regnamespace "
            "AND has_function_privilege('public', p.oid, 'EXECUTE')")).all()
        definer = conn.execute(text(
            "SELECT p.proname, p.prosecdef, array_to_string(p.proconfig, ',') FROM pg_proc p "
            "WHERE p.pronamespace = 'nexus_rls'::regnamespace AND p.prosecdef")).all()
    assert public == []
    assert definer and all("search_path=pg_catalog, public" in (cfg or "") for _, _, cfg in definer)


def test_app_role_cannot_claim_jobs_or_read_password_hashes(world):
    _, app, _ = world
    with app() as db:
        set_security_context(db, user_context("northstar", "u2"))
        with pytest.raises(ProgrammingError, match="permission denied"):
            db.execute(text("SELECT * FROM nexus_rls.claim_job('x', 30, NULL, NULL)"))
        db.rollback()
        set_security_context(db, user_context("northstar", "u2"))
        with pytest.raises(ProgrammingError, match="permission denied"):
            db.execute(text("SELECT password_hash FROM users"))
        db.rollback()


# ----------------------------------------------------------------------------- isolation

def test_no_context_means_no_rows_for_both_runtime_roles(world):
    _, app, worker = world
    assert set(counts(app, None).values()) == {0}
    assert set(counts(worker, None).values()) == {0}


def test_tenant_isolation_across_every_table(world):
    admin, app, _ = world
    finance = counts(app, user_context("northstar", "u1"))       # member of 'finance'
    other = counts(app, user_context("otherco", "u3"))
    tenant_admin = counts(app, user_context("northstar", "u2"))
    # finance sees exactly d_fin and its derived rows
    for table in ("documents", "chunks", "chunk_embeddings", "document_processing", "extractions",
                  "extraction_fields", "review_events", "document_grants"):
        assert finance[table] == 1, table
        assert other[table] == 1, table
        assert tenant_admin[table] == 2, table
    assert finance["users"] == 1 and tenant_admin["users"] == 4  # own row / admin: own tenant
    assert finance["audit_events"] == 0 and tenant_admin["audit_events"] == 2
    assert other["audit_events"] == 1
    # direct probing of the other tenant's rows by id returns nothing
    with app() as db:
        set_security_context(db, user_context("northstar", "u2"))  # even the tenant admin
        for sql in ("SELECT count(*) FROM documents WHERE id = 'd_other'",
                    "SELECT count(*) FROM chunks WHERE document_id = 'd_other'",
                    "SELECT count(*) FROM chunk_embeddings WHERE chunk_id = 'd_other_c0'",
                    "SELECT count(*) FROM extraction_fields WHERE value LIKE '%d_other%'",
                    "SELECT count(*) FROM review_events WHERE tenant_id = 'otherco'",
                    "SELECT count(*) FROM processing_jobs WHERE tenant_id = 'otherco'"):
            assert db.execute(text(sql)).scalar() == 0, sql


def test_cross_tenant_embeddings_are_invisible_even_to_an_unfiltered_nearest_neighbour_query(world):
    _, app, _ = world
    query = "[" + ",".join(str(x) for x in vec(1.0)) + "]"
    with app() as db:
        set_security_context(db, user_context("northstar", "u2"))
        rows = db.execute(text(f"SELECT chunk_id FROM chunk_embeddings ORDER BY embedding <=> '{query}' LIMIT 10")).all()
    assert {r[0] for r in rows} == {"d_hr_c0", "d_fin_c0"}  # the globally nearest d_other vector is absent


def test_forbidden_rows_cannot_be_updated_or_deleted(world):
    admin, app, _ = world
    with app() as db:
        set_security_context(db, user_context("northstar", "u1"))
        assert db.execute(text("UPDATE documents SET title = 'pwned' WHERE id IN ('d_other', 'd_hr')")).rowcount == 0
        assert db.execute(text("UPDATE extractions SET status = 'approved' WHERE id = 'e_d_fin'")).rowcount == 0
        assert db.execute(text("DELETE FROM document_grants WHERE document_id = 'd_hr'")).rowcount == 0
        db.commit()
        for statement in ("DELETE FROM documents", "DELETE FROM chunks", "DELETE FROM chunk_embeddings",
                          "DELETE FROM audit_events", "UPDATE audit_events SET action = 'x'",
                          "DELETE FROM review_events"):
            with pytest.raises(ProgrammingError, match="permission denied"):
                db.execute(text(statement))
            db.rollback()
            set_security_context(db, user_context("northstar", "u1"))
    with admin() as db:
        assert db.get(Document, "d_other").title == "Secret budget" and db.get(Document, "d_hr").title != "pwned"
        assert db.get(Extraction, "e_d_fin").status == "needs_review"


def test_inserts_into_another_tenant_or_as_another_user_are_rejected(world):
    _, app, _ = world
    attempts = [
        "INSERT INTO documents (id, tenant_id, owner_id, title, filename, storage_key, sha256, page_count, "
        "extracted_fields, created_at, lifecycle_state, storage_backend, current_version) VALUES "
        "('x1', 'otherco', 'u1', 't', 'f', 'k', 'h', 0, '{}', now(), 'ready', 'local', 1)",
        "INSERT INTO audit_events (user_id, tenant_id, action, created_at) VALUES ('u2', 'northstar', 'forged', now())",
        "INSERT INTO document_grants (document_id, group_name) VALUES ('d_hr', 'finance')",
        "INSERT INTO processing_jobs (id, tenant_id, user_id, document_id, job_type, status, priority, attempts, "
        "max_attempts, run_after, payload, cancel_requested, created_at, updated_at) VALUES "
        "('j1', 'northstar', 'u1', 'd_hr', 'reindex', 'queued', 1, 0, 5, now(), '{}', false, now(), now())",
    ]
    for sql in attempts:
        with app() as db:
            set_security_context(db, user_context("northstar", "u1"))
            with pytest.raises(ProgrammingError, match="row-level security"):
                db.execute(text(sql))


def test_reviewer_rights_apply_only_to_documents_the_reviewer_can_read(world):
    admin, app, _ = world
    with app() as db:
        set_security_context(db, user_context("northstar", "u4"))  # reviewer, member of 'finance'
        assert db.execute(text("UPDATE extractions SET review_comment = 'ok' WHERE id = 'e_d_fin'")).rowcount == 1
        assert db.execute(text("UPDATE extractions SET review_comment = 'x' WHERE id IN ('e_d_hr', 'e_d_other')")).rowcount == 0
        assert db.execute(text("UPDATE extraction_fields SET corrected_value = 'x' WHERE extraction_id = 'e_d_hr'")).rowcount == 0
        db.commit()
    with app() as db:
        set_security_context(db, user_context("otherco", "u5"))  # the other tenant's reviewer
        assert db.execute(text("UPDATE extractions SET review_comment = 'x' WHERE id = 'e_d_fin'")).rowcount == 0
        db.commit()


def test_group_removal_and_account_disabling_apply_to_the_next_statement(world):
    admin, app, _ = world
    with app() as db:
        # u4 reads d_fin through the 'finance' group grant only (u1 owns it: owners keep access)
        set_security_context(db, user_context("northstar", "u4"))
        assert db.execute(text("SELECT count(*) FROM chunks")).scalar() == 1
        db.commit()
        with admin() as a:
            a.get(User, "u4").groups = ["legal"]
            a.commit()
        assert db.execute(text("SELECT count(*) FROM chunks")).scalar() == 0  # same session, next txn
        db.commit()
        with admin() as a:
            user = a.get(User, "u2")
            user.status = "disabled"
            a.commit()
        set_security_context(db, user_context("northstar", "u2"))
        assert db.execute(text("SELECT count(*) FROM documents")).scalar() == 0


def test_security_context_is_transaction_local_and_never_leaks_through_the_pool(world, pg_engine):
    """One pooled connection, reused by consecutive sessions: the context of one never applies to
    the next, also after an exception, and even if something set a SESSION-level value."""
    url = pg_engine.url.set(username="nexus_app", password=RUNTIME_PASSWORD)
    engine = create_engine(url, poolclass=QueuePool, pool_size=1, max_overflow=0)
    factory = sessionmaker(engine)
    try:
        with factory() as db:
            set_security_context(db, user_context("northstar", "u2"))
            assert db.execute(text("SELECT count(*) FROM documents")).scalar() == 2
            pid = db.execute(text("SELECT pg_backend_pid()")).scalar()
            db.commit()
        with factory() as db:  # same physical connection, no context
            assert db.execute(text("SELECT pg_backend_pid()")).scalar() == pid
            assert db.execute(text("SELECT current_setting('app.tenant_id', true)")).scalar() == ""
            assert db.execute(text("SELECT count(*) FROM documents")).scalar() == 0
        with factory() as db:
            set_security_context(db, user_context("otherco", "u3"))
            with pytest.raises(DBAPIError):
                db.execute(text("SELECT 1 / 0 FROM documents"))
        with factory() as db:  # after the failed transaction
            assert db.execute(text("SELECT count(*) FROM documents")).scalar() == 0
        # a careless SESSION-level SET (not transaction-local) is still overridden per transaction
        with engine.connect() as conn:
            conn.exec_driver_sql("SET app.tenant_id = 'northstar'")
            conn.exec_driver_sql("SET app.user_id = 'u2'")
            conn.commit()
        with factory() as db:
            assert db.execute(text("SELECT count(*) FROM documents")).scalar() == 0
    finally:
        engine.dispose()


def test_concurrent_sessions_in_threads_keep_their_own_context(world):
    import threading

    _, app, _ = world
    results, barrier = {}, threading.Barrier(3)

    def run(name, ctx):
        with app() as db:
            set_security_context(db, ctx)
            barrier.wait()
            results[name] = sorted(r[0] for r in db.execute(text("SELECT id FROM documents")))

    threads = [threading.Thread(target=run, args=a) for a in (
        ("hr", user_context("northstar", "u0")), ("other", user_context("otherco", "u3")),
        ("admin", user_context("northstar", "u2")))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == {"hr": ["d_hr"], "other": ["d_other"], "admin": ["d_fin", "d_hr"]}


# ----------------------------------------------------------------------------- worker scope

def test_worker_role_is_confined_to_the_document_of_its_leased_job(world):
    admin, _, worker = world
    with admin() as db:
        job = queue.enqueue(db, tenant_id="northstar", user_id="u1", job_type="reindex", document_id="d_fin")
        db.commit()
    with worker() as db:
        claimed = queue.claim(db, "test-worker", 60, job_id=job.id)
    assert claimed is not None
    scoped = counts(worker, claimed.context(), tables={"documents", "chunks", "chunk_embeddings", "extractions",
                                                       "extraction_fields", "users", "processing_jobs"})
    assert scoped == {"documents": 1, "chunks": 1, "chunk_embeddings": 1, "extractions": 1,
                      "extraction_fields": 1, "users": 1, "processing_jobs": 1}
    with worker() as db:
        queue.bind(db, claimed)
        assert db.execute(text("SELECT id FROM documents")).scalar() == "d_fin"
        assert db.execute(text("SELECT count(*) FROM chunks WHERE document_id IN ('d_hr', 'd_other')")).scalar() == 0
        assert db.execute(text("UPDATE documents SET title = 'x' WHERE id = 'd_hr'")).rowcount == 0
        db.rollback()
    # a forged lease token opens no document data; the job row itself stays readable by id but
    # cannot be written without the current lease token (fencing)
    forged = SecurityContext(tenant_id="northstar", user_id="u1", kind="worker", job_id=job.id, lease_token="forged")
    forged_counts = counts(worker, forged)
    assert forged_counts.pop("processing_jobs") == 1 and set(forged_counts.values()) == {0}
    with worker() as db:
        set_security_context(db, forged)
        assert db.execute(text("UPDATE processing_jobs SET status = 'completed' WHERE id = :i"),
                          {"i": job.id}).rowcount == 0
        db.rollback()
    wrong_tenant = SecurityContext(tenant_id="otherco", user_id="u1", kind="worker", job_id=job.id,
                                   lease_token=claimed.lease_token)
    assert set(counts(worker, wrong_tenant).values()) == {0}
    # once the lease has expired the scope closes, even with the right token
    with admin() as db:
        db.execute(text("UPDATE processing_jobs SET locked_until = now() - interval '1 second' WHERE id = :i"),
                   {"i": job.id})
        db.commit()
    assert counts(worker, claimed.context(), tables={"documents", "chunks"}) == {"documents": 0, "chunks": 0}


def test_worker_role_has_no_access_to_sessions_identities_or_password_hashes(world):
    _, _, worker = world
    for sql in ("SELECT count(*) FROM auth_sessions", "SELECT count(*) FROM user_identities",
                "SELECT password_hash FROM users", "INSERT INTO users (id, tenant_id, email, password_hash, role, "
                "groups, status, auth_source) VALUES ('x', 'northstar', 'x@x', '!', 'admin', '[]', 'active', 'local')"):
        with worker() as db:
            with pytest.raises(ProgrammingError, match="permission denied"):
                db.execute(text(sql))


def test_claims_are_exclusive_under_concurrency(world):
    import threading

    admin, _, worker = world
    with admin() as db:
        ids = {queue.enqueue(db, tenant_id="northstar", user_id="u1", job_type="reindex", document_id="d_fin").id
               for _ in range(12)}
        db.commit()
    claimed, lock = [], threading.Lock()

    def claimer(n):
        while True:
            with worker() as db:
                job = queue.claim(db, f"w{n}", 60)
            if job is None:
                return
            with lock:
                claimed.append(job.id)

    threads = [threading.Thread(target=claimer, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(claimed) == sorted(ids)  # every job exactly once, no duplicates


# ----------------------------------------------------------------------------- mutation tests

ACL_USERS = ("backend.app.security", "backend.app.retrieval", "backend.app.main", "backend.app.docintel_api",
             "backend.app.docintel.compare", "backend.app.docintel.review", "backend.app.lifecycle_api")


def _remove_application_acl(monkeypatch):
    import importlib

    from sqlalchemy import select

    def unscoped(user):
        return select(Document)  # the application filter is "accidentally" gone
    for name in ACL_USERS:
        monkeypatch.setattr(importlib.import_module(name), "authorized_documents", unscoped)


@pytest.mark.parametrize("kind", ["postgres", "sqlite"])
def test_rls_blocks_access_when_the_application_acl_is_missing(kind, request, monkeypatch, tmp_path):
    from dataclasses import replace

    from backend.app import main
    from backend.app.database import Base

    if kind == "postgres":
        engine = request.getfixturevalue("pg_engine")
        truncate(engine)
    else:
        from sqlalchemy.pool import StaticPool
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "settings", replace(main.settings, storage_dir=tmp_path))
    factory = sessionmaker(engine, expire_on_commit=False)
    seed(factory)
    client, token = make_client(factory)
    try:
        _remove_application_acl(monkeypatch)
        hr = token("hr@northstar.demo")
        listed = {d["id"] for d in client.get("/api/documents", headers=hr).json()}
        answer = client.post("/api/ask", headers=hr, json={"question": "SILVER-IBIS-73 codename", "mode": "bm25"}).json()
        leaked_sources = {s["document_id"] for s in answer["sources"]} - {"d_hr"}
        direct = client.get("/api/documents/d_other", headers=hr).status_code
        if kind == "postgres":
            assert listed == {"d_hr"} and leaked_sources == set() and direct == 404
            assert "SILVER-IBIS-73" not in answer["answer"]
        else:  # without RLS the same bug leaks: the mutation is detected by this test
            assert "d_other" in listed and "d_other" in leaked_sources and direct == 200
    finally:
        from backend.app.main import app
        app.dependency_overrides.clear()
        if kind == "sqlite":
            engine.dispose()


def test_rls_overhead_explain_uses_hashed_subplans(world):
    """The child-table policies use uncorrelated IN (SELECT ...) sub-queries, which PostgreSQL
    evaluates once per statement (hashed SubPlan), not once per row."""
    _, app, _ = world
    with app() as db:
        set_security_context(db, user_context("northstar", "u1"))
        plan = "\n".join(r[0] for r in db.execute(text(
            "EXPLAIN SELECT c.id FROM chunks c JOIN chunk_embeddings e ON e.chunk_id = c.id")))
    assert "hashed SubPlan" in plan or "Hash" in plan
