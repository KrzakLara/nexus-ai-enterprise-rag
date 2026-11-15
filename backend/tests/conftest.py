"""Shared fixtures.

* SQLite (in-memory) runs everywhere, no services or API keys required.
* PostgreSQL + pgvector runs when TEST_DATABASE_URL points at a database whose name ends with
  "_test" (it is DROPPED and re-created by the test session). Example:
      TEST_DATABASE_URL=postgresql+psycopg://nexus:demo_local_only@127.0.0.1:5433/nexus_test
  Without it, every "postgres" parametrization is reported as SKIPPED.

Phase 4: on PostgreSQL the API under test connects as the RUNTIME role `nexus_app` and inline jobs
run as `nexus_worker` (both NOSUPERUSER, NOBYPASSRLS), so every Phase 1-3 API test also runs with
row-level security enforced. Seeding and assertions keep using the admin (migration) engine.
"""
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.auth import hash_password
from backend.app.database import Base, get_db
from backend.app.db_roles import provision_roles, runtime_url
from backend.app.lifecycle import get_worker_sessions
from backend.app.main import app
from backend.app.migrate import migrate
from backend.app.models import Chunk, Document, DocumentGrant, User

PG_URL = os.getenv("TEST_DATABASE_URL", "")
TABLES = ("chunk_embeddings, audit_events, chunks, document_grants, document_versions, processing_jobs, "
          "auth_sessions, user_identities, auth_login_states, worker_heartbeats, documents, users")
RUNTIME_PASSWORD = "test-runtime-only-1"
RUNTIME = {}  # admin engine id -> (app engine, worker engine)


def recreate_pg_database(url: str) -> str:
    """DROP + CREATE the (must be *_test) database named in `url`; returns the URL string."""
    from backend.evaluation.safety import assert_disposable_database

    parsed = assert_disposable_database(url)
    if not (parsed.database or "").endswith("_test"):
        raise RuntimeError("Refusing to drop a database whose name does not end with '_test'")
    admin = create_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{parsed.database}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{parsed.database}"'))
    admin.dispose()
    return parsed.render_as_string(hide_password=False)


def runtime_engines(admin_engine, url: str):
    """(app engine, worker engine) connecting as nexus_app / nexus_worker to the same database."""
    key = id(admin_engine)
    if key not in RUNTIME:
        provision_roles(admin_engine, RUNTIME_PASSWORD, RUNTIME_PASSWORD)
        RUNTIME[key] = (create_engine(runtime_url(url, "nexus_app", RUNTIME_PASSWORD)),
                        create_engine(runtime_url(url, "nexus_worker", RUNTIME_PASSWORD)))
    return RUNTIME[key]


@pytest.fixture(scope="session")
def pg_url():
    if not PG_URL:
        pytest.skip("TEST_DATABASE_URL not set (PostgreSQL + pgvector integration tests)")
    # One test session per disposable database: a second concurrent pytest run would TRUNCATE
    # tables under the first one's open transactions and both would block. Fail fast instead.
    from sqlalchemy.engine import make_url

    target = make_url(PG_URL)
    guard = create_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
    conn = guard.connect()
    if not conn.execute(text("SELECT pg_try_advisory_lock(hashtext(:k))"),
                        {"k": f"nexus-pytest:{target.database}"}).scalar():
        conn.close()
        guard.dispose()
        pytest.exit(f"another test session is already using {target.database!r}; refusing to run concurrently",
                    returncode=3)
    url = recreate_pg_database(PG_URL)
    migrate(url)  # the real Alembic migrations, not create_all()
    yield url
    conn.close()  # releases the session-level advisory lock
    guard.dispose()


@pytest.fixture(scope="session")
def pg_engine(pg_url):
    engine = create_engine(pg_url)
    runtime_engines(engine, pg_url)
    yield engine
    for app_engine, worker_engine in RUNTIME.values():
        app_engine.dispose()
        worker_engine.dispose()
    engine.dispose()


@pytest.fixture(scope="session")
def app_engine(pg_engine):
    return RUNTIME[id(pg_engine)][0]


@pytest.fixture(scope="session")
def worker_engine(pg_engine):
    return RUNTIME[id(pg_engine)][1]


@pytest.fixture(scope="session")
def embedder():
    from backend.app.embeddings import Embedder

    return Embedder()  # real intfloat/multilingual-e5-small, loaded lazily on first use


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
def db_engine(request):
    if request.param == "sqlite":
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(engine)  # lightweight unit tests only; real DBs use Alembic
        yield engine
        engine.dispose()
    else:
        engine = request.getfixturevalue("pg_engine")
        truncate(engine)
        yield engine


def truncate(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))


PEOPLE = [
    ("hr@northstar.demo", "northstar", "member", ["hr"]),
    ("finance@northstar.demo", "northstar", "member", ["finance"]),
    ("admin@northstar.demo", "northstar", "admin", ["hr", "finance"]),
    ("finance@otherco.demo", "otherco", "member", ["finance"]),
    ("reviewer@northstar.demo", "northstar", "reviewer", ["finance"]),  # u4: Phase 3 review rights
    ("reviewer@otherco.demo", "otherco", "reviewer", ["finance"]),      # u5
]
DOCS = [
    ("d_hr", "northstar", "u0", "Compensation policy", ["hr"],
     "Salary increase budget is EUR 310,000 for next year."),
    ("d_fin", "northstar", "u1", "Atlas agreement", ["finance"],
     "Atlas supplier annual value is EUR 184,000, notice period is 90 days."),
    ("d_other", "otherco", "u3", "Secret budget", ["finance"],
     "Otherco confidential codename SILVER-IBIS-73. No other tenant may access."),
]


def seed(factory, docs=DOCS, people=PEOPLE):
    pwd = hash_password("TestPass123!")
    with factory() as db:
        for i, (email, tenant, role, groups) in enumerate(people):
            db.add(User(id=f"u{i}", email=email, tenant_id=tenant, role=role,
                        groups=groups, password_hash=pwd))
        db.flush()
        for doc_id, tenant, owner, title, groups, body in docs:
            db.add(Document(id=doc_id, tenant_id=tenant, owner_id=owner,
                            title=title, filename=doc_id + ".txt", storage_key=doc_id + ".txt",
                            sha256="0" * 64, page_count=1, extracted_fields={},
                            lifecycle_state="ready"))
            db.flush()
            db.add(Chunk(id=doc_id + "_c0", document_id=doc_id,
                         page_number=1, position=0, content=body))
            for group in groups:
                db.add(DocumentGrant(document_id=doc_id, group_name=group))
        db.commit()


def api_factories(factory):
    """(API session factory, inline-worker session factory) for a test's admin factory: on
    PostgreSQL the runtime roles (RLS enforced), on SQLite the same factory."""
    bind = factory.kw["bind"]
    if bind.dialect.name == "postgresql":
        for key, (app_e, worker_e) in RUNTIME.items():
            if app_e.url.database == bind.url.database:
                return (sessionmaker(app_e, expire_on_commit=False),
                        sessionmaker(worker_e, expire_on_commit=False))
    return factory, factory


def make_client(factory):
    api_factory, worker_factory = api_factories(factory)

    def override_db():
        with api_factory() as db:
            yield db
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_worker_sessions] = lambda: worker_factory
    client = TestClient(app)  # no lifespan: never touches the normal file-backed database

    def token(email: str):
        response = client.post("/api/auth/login", json={"email": email, "password": "TestPass123!"})
        assert response.status_code == 200, response.text
        client.cookies.clear()  # bearer-token clients; cookie sessions are tested separately
        return {"Authorization": "Bearer " + response.json()["access_token"]}
    return client, token


@pytest.fixture
def scenario(db_engine, tmp_path, monkeypatch):
    """Two tenants, three documents; runs once on SQLite and once on PostgreSQL."""
    from dataclasses import replace

    from backend.app import main

    monkeypatch.setattr(main, "settings", replace(main.settings, storage_dir=tmp_path))
    factory = sessionmaker(db_engine, expire_on_commit=False)
    seed(factory)
    client, token = make_client(factory)
    yield client, token, factory
    app.dependency_overrides.clear()
