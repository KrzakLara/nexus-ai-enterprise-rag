"""Provision the runtime database roles created (NOLOGIN) by migration 0008.

Run with the migration / admin connection (scripts/provision_db_roles.py, the Compose `migrate`
service, CI). Passwords come from the environment, are validated against a conservative
character set and are never written to the repository or to a migration.
"""
from __future__ import annotations

import re

from sqlalchemy import text
from sqlalchemy.engine import URL, Engine, make_url

APP_ROLE, WORKER_ROLE = "nexus_app", "nexus_worker"
PASSWORD = re.compile(r"^[A-Za-z0-9_@#%+=.,:~-]{12,128}$")


class RoleProvisioningError(RuntimeError):
    pass


def _literal(password: str) -> str:
    if not PASSWORD.match(password or ""):
        raise RoleProvisioningError("runtime role passwords must be 12-128 characters of [A-Za-z0-9_@#%+=.,:~-]")
    return "'" + password.replace("'", "''") + "'"


def provision_roles(admin: Engine, app_password: str, worker_password: str,
                    connection_limit: int = 50) -> None:
    if admin.dialect.name != "postgresql":
        return
    with admin.begin() as conn:
        exists = {r[0] for r in conn.execute(text(
            "SELECT rolname FROM pg_roles WHERE rolname IN ('nexus_app', 'nexus_worker')"))}
        missing = {APP_ROLE, WORKER_ROLE} - exists
        if missing:
            raise RoleProvisioningError(f"roles {sorted(missing)} do not exist; run the migrations first")
        for role, password in ((APP_ROLE, app_password), (WORKER_ROLE, worker_password)):
            conn.exec_driver_sql(
                f"ALTER ROLE {role} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
                f"NOBYPASSRLS CONNECTION LIMIT {int(connection_limit)} PASSWORD {_literal(password)}")
        database = conn.execute(text("SELECT current_database()")).scalar()
        conn.exec_driver_sql(f'GRANT CONNECT ON DATABASE "{database}" TO {APP_ROLE}, {WORKER_ROLE}')


def runtime_url(admin_url: str | URL, role: str, password: str) -> str:
    url = make_url(admin_url) if isinstance(admin_url, str) else admin_url
    return url.set(username=role, password=password).render_as_string(hide_password=False)
