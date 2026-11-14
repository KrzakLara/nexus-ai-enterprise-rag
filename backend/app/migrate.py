"""Schema management via Alembic (replaces Base.metadata.create_all for real databases).

Upgrade path for v0.1 databases: v0.1 created its tables with create_all() and has no
`alembic_version` table. If exactly the v0.1 tables are present, the database is stamped at the
baseline revision and then upgraded - no table is dropped or re-created.
"""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine

from .config import settings

ROOT = Path(__file__).resolve().parents[2]
BASELINE_REVISION = "0001_v01_baseline"
V01_TABLES = {"users", "documents", "document_grants", "chunks", "audit_events"}
# PostgreSQL-only, database-generated objects that are intentionally NOT mapped in the ORM
# (SQLite unit tests use create_all and have no tsvector). Excluded from autogenerate diffs.
UNMAPPED_DB_OBJECTS = {("column", "content_tsv"), ("index", "ix_chunks_content_tsv")}


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    return (type_, name) not in UNMAPPED_DB_OBJECTS


class SchemaNotReady(RuntimeError):
    pass


def alembic_config(database_url: str | None = None) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "backend" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", (database_url or settings.database_url).replace("%", "%%"))
    return cfg


def head_revision(cfg: Config | None = None) -> str:
    return ScriptDirectory.from_config(cfg or alembic_config()).get_current_head()


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def migrate(database_url: str | None = None) -> str:
    """Bring the database to the latest revision. Returns a short description of what happened."""
    url = database_url or settings.database_url
    cfg = alembic_config(url)
    engine = create_engine(url)
    try:
        tables = set(inspect(engine).get_table_names())
        revision = current_revision(engine)
    finally:
        engine.dispose()
    action = "upgraded"
    if revision is None and tables & V01_TABLES:
        missing = V01_TABLES - tables
        if missing:
            raise SchemaNotReady(
                f"Unversioned database with a partial v0.1 schema (missing {sorted(missing)}); "
                "refusing to guess. Inspect it manually."
            )
        command.stamp(cfg, BASELINE_REVISION)
        action = "stamped legacy v0.1 schema, then upgraded"
    command.upgrade(cfg, "head")
    return action


def assert_schema_current(engine: Engine) -> None:
    current, head = current_revision(engine), head_revision()
    if current != head:
        raise SchemaNotReady(
            f"Database schema is at revision {current!r}, expected {head!r}. "
            "Run: python scripts/migrate.py"
        )
