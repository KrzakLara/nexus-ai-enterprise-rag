"""Give the runtime roles nexus_app / nexus_worker LOGIN and a password (after migrations).

Usage (admin connection in DATABASE_URL or MIGRATION_DATABASE_URL):
    DB_APP_PASSWORD=... DB_WORKER_PASSWORD=... python scripts/provision_db_roles.py

The API and the workers then connect as those roles, which are subject to row-level security:
    DATABASE_URL=postgresql+psycopg://nexus_app:<pw>@db:5432/nexus
    WORKER_DATABASE_URL=postgresql+psycopg://nexus_worker:<pw>@db:5432/nexus
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import create_engine

from backend.app.db_roles import RoleProvisioningError, provision_roles


def main() -> int:
    url = os.getenv("MIGRATION_DATABASE_URL") or os.getenv("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        print("Row-level security roles exist only on PostgreSQL; nothing to do.")
        return 0
    app_pw, worker_pw = os.getenv("DB_APP_PASSWORD", ""), os.getenv("DB_WORKER_PASSWORD", "")
    if not app_pw or not worker_pw:
        print("DB_APP_PASSWORD and DB_WORKER_PASSWORD are required", file=sys.stderr)
        return 2
    engine = create_engine(url)
    try:
        provision_roles(engine, app_pw, worker_pw)
    except RoleProvisioningError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        engine.dispose()
    print("Runtime roles nexus_app and nexus_worker can log in (NOSUPERUSER, NOBYPASSRLS).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
