"""Shared fixtures.

* SQLite (in-memory) runs everywhere, no services or API keys required.
* PostgreSQL + pgvector runs when TEST_DATABASE_URL points at a database whose name ends with
  "_test" (it is DROPPED and re-created by the test session). Example:
      TEST_DATABASE_URL=postgresql+psycopg://nexus:demo_local_only@127.0.0.1:5433/nexus_test
  Without it, every "postgres" parametrization is reported as SKIPPED.
"""
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.auth import hash_password
from backend.app.database import Base, get_db
from backend.app.main import app
from backend.app.migrate import migrate
from backend.app.models import Chunk, Document, DocumentGrant, User

PG_URL = os.getenv("TEST_DATABASE_URL", "")
TABLES = "chunk_embeddings, audit_events, chunks, document_grants, documents, users"


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


@pytest.fixture(scope="session")
def pg_engine():
    if not PG_URL:
        pytest.skip("TEST_DATABASE_URL not set (PostgreSQL + pgvector integration tests)")
    url = recreate_pg_database(PG_URL)
    migrate(url)  # the real Alembic migrations, not create_all()
    engine = create_engine(url)
    yield engine
    engine.dispose()


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
        with engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
        yield engine


PEOPLE = [
    ("hr@northstar.demo", "northstar", "member", ["hr"]),
    ("finance@northstar.demo", "northstar", "member", ["finance"]),
    ("admin@northstar.demo", "northstar", "admin", ["hr", "finance"]),
    ("finance@otherco.demo", "otherco", "member", ["finance"]),
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
                            sha256="0" * 64, page_count=1, extracted_fields={}))
            db.flush()
            db.add(Chunk(id=doc_id + "_c0", document_id=doc_id,
                         page_number=1, position=0, content=body))
            for group in groups:
                db.add(DocumentGrant(document_id=doc_id, group_name=group))
        db.commit()


def make_client(factory):
    def override_db():
        with factory() as db:
            yield db
    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)  # no lifespan: never touches the normal file-backed database

    def token(email: str):
        response = client.post("/api/auth/login", json={"email": email, "password": "TestPass123!"})
        assert response.status_code == 200, response.text
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
