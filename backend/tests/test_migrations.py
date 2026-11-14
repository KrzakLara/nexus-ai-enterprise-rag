"""Alembic migrations: fresh install, v0.1 (create_all) upgrade path, and model/schema parity."""
import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from backend.app.database import Base
from backend.app.migrate import (
    BASELINE_REVISION,
    alembic_config,
    current_revision,
    head_revision,
    include_object,
    migrate,
)

from .conftest import PG_URL, recreate_pg_database


def _fresh_url(kind: str, tmp_path) -> str:
    if kind == "sqlite":
        return f"sqlite:///{(tmp_path / 'migrations.db').as_posix()}"
    if not PG_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    base = PG_URL.rsplit("/", 1)[0]
    return recreate_pg_database(f"{base}/nexus_migrations_test")


KINDS = ["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)]


@pytest.mark.parametrize("kind", KINDS)
def test_fresh_database_upgrades_to_head_and_matches_models(kind, tmp_path):
    url = _fresh_url(kind, tmp_path)
    migrate(url)
    engine = create_engine(url)
    try:
        assert current_revision(engine) == head_revision()
        with engine.connect() as conn:
            ctx = MigrationContext.configure(
                conn, opts={"compare_type": True, "include_object": include_object})
            diff = compare_metadata(ctx, Base.metadata)
        assert diff == [], diff
    finally:
        engine.dispose()


@pytest.mark.parametrize("kind", KINDS)
def test_unversioned_v01_database_is_stamped_and_upgraded_without_data_loss(kind, tmp_path):
    url = _fresh_url(kind, tmp_path)
    # Recreate a v0.1 database: baseline tables with data, but no alembic_version table
    command.upgrade(alembic_config(url), BASELINE_REVISION)
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE alembic_version"))
        conn.execute(text(
            "INSERT INTO users (id, tenant_id, email, password_hash, role, groups) "
            "VALUES ('u1', 't1', 'a@t1.demo', 'x', 'member', '[\"hr\"]')"))
    assert current_revision(engine) is None
    action = migrate(url)
    try:
        assert action.startswith("stamped legacy v0.1 schema")
        assert current_revision(engine) == head_revision()
        assert "chunk_embeddings" in inspect(engine).get_table_names()
        with engine.connect() as conn:
            assert conn.execute(text("SELECT email FROM users")).scalar() == "a@t1.demo"
    finally:
        engine.dispose()


def test_downgrade_to_baseline_and_back(tmp_path):
    url = f"sqlite:///{(tmp_path / 'roundtrip.db').as_posix()}"
    cfg = alembic_config(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, BASELINE_REVISION)
    engine = create_engine(url)
    assert "chunk_embeddings" not in inspect(engine).get_table_names()
    engine.dispose()
    command.upgrade(cfg, "head")
