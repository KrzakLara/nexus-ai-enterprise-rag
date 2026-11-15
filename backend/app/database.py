"""Engines, sessions and the per-transaction PostgreSQL security context (row-level security).

Every PostgreSQL transaction opened by a Session starts with ONE statement that sets all
security settings transaction-locally (`set_config(name, value, is_local => true)`):

    app.tenant_id, app.user_id     authenticated API user
    app.job_id, app.lease_token    background job whose lease the worker holds
    app.session_hash               SHA-256 of a presented session cookie (session lookup only)
    app.context                    anonymous | session_lookup | user | worker (diagnostics)

The values live only until COMMIT / ROLLBACK, so a pooled connection can never carry one
request's identity into the next request: the next transaction on that connection starts by
overwriting every key (empty = no access). The context is stored on the Session object itself
(`session.info`), never in a global or thread-local, so concurrent requests in FastAPI's thread
pool cannot see each other's context. A context change inside an open transaction is applied
immediately as well.
"""
from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


@dataclass(frozen=True)
class SecurityContext:
    tenant_id: str | None = None
    user_id: str | None = None
    kind: str = "anonymous"  # anonymous | session_lookup | user | worker
    job_id: str | None = None
    lease_token: str | None = None
    session_hash: str | None = None

    def params(self) -> dict[str, str]:
        return {"tenant": self.tenant_id or "", "user": self.user_id or "", "job": self.job_id or "",
                "lease": self.lease_token or "", "session": self.session_hash or "", "kind": self.kind}


ANONYMOUS = SecurityContext()
CONTEXT_KEY = "nexus.security_context"
_SET_CONTEXT = text(
    "SELECT set_config('app.tenant_id', :tenant, true), set_config('app.user_id', :user, true), "
    "set_config('app.job_id', :job, true), set_config('app.lease_token', :lease, true), "
    "set_config('app.session_hash', :session, true), set_config('app.context', :kind, true)"
)


def user_context(tenant_id: str, user_id: str) -> SecurityContext:
    return SecurityContext(tenant_id=tenant_id, user_id=user_id, kind="user")


def _apply(connection, ctx: SecurityContext) -> None:
    connection.execute(_SET_CONTEXT, ctx.params())


@event.listens_for(Session, "after_begin")
def _set_security_context(session: Session, transaction, connection) -> None:
    if connection.dialect.name != "postgresql":
        return
    _apply(connection, session.info.get(CONTEXT_KEY, ANONYMOUS))


def set_security_context(session: Session, ctx: SecurityContext) -> None:
    """Set the context for this session: the open transaction (if any) and every later one."""
    session.info[CONTEXT_KEY] = ctx
    if session.in_transaction() and session.get_bind().dialect.name == "postgresql":
        _apply(session.connection(), ctx)


def security_context(session: Session) -> SecurityContext:
    return session.info.get(CONTEXT_KEY, ANONYMOUS)


def make_engine(database_url: str, role_label: str = "api") -> Engine:
    if database_url.startswith("sqlite"):
        if database_url.startswith("sqlite:///") and ":memory:" not in database_url:
            Path(database_url.replace("sqlite:///", "")).parent.mkdir(parents=True, exist_ok=True)
        return create_engine(database_url, connect_args={"check_same_thread": False}, pool_pre_ping=True)
    connect_args = {}
    if database_url.startswith("postgresql"):
        connect_args = {
            "connect_timeout": settings.db_connect_timeout,
            "application_name": f"nexus-{role_label}",
            # server-side guard against runaway statements (per connection)
            "options": f"-c statement_timeout={settings.db_statement_timeout_ms}",
        }
    return create_engine(
        database_url, pool_pre_ping=True, pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow, pool_timeout=settings.db_pool_timeout,
        pool_recycle=1800, connect_args=connect_args,
    )


engine = make_engine(settings.database_url, "api")
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
_worker_engine: Engine | None = None


def worker_engine() -> Engine:
    """Engine of the worker role (WORKER_DATABASE_URL). Without it: the API engine."""
    global _worker_engine
    if not settings.worker_database_url:
        return engine
    if _worker_engine is None:
        _worker_engine = make_engine(settings.worker_database_url, "worker")
    return _worker_engine


_PROBE_ENGINES: dict[str, Engine] = {}


def probe_engine(bind: Engine) -> Engine:
    """Engine for health probes.

    PostgreSQL: a NEW connection per probe (NullPool) with a short connect timeout and statement
    timeout. A pooled connection to a server that is frozen but still accepts TCP blocks
    indefinitely (libpq has no client-side query timeout); a new connection fails after
    connect_timeout instead. SQLite: the bind itself (local file / in-memory test database).
    """
    if bind.dialect.name != "postgresql":
        return bind
    key = bind.url.render_as_string(hide_password=False)
    probe = _PROBE_ENGINES.get(key)
    if probe is None:
        probe = create_engine(bind.url, poolclass=NullPool, connect_args={
            "connect_timeout": 2, "application_name": "nexus-probe", "options": "-c statement_timeout=2000"})
        _PROBE_ENGINES[key] = probe
    return probe


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as db:
        yield db


class PrivilegedDatabaseRole(RuntimeError):
    pass


def runtime_role(engine_: Engine) -> dict | None:
    """Attributes of the role the engine connects as (PostgreSQL only)."""
    if engine_.dialect.name != "postgresql":
        return None
    with engine_.connect() as conn:
        row = conn.execute(text(
            "SELECT current_user AS name, r.rolsuper AS superuser, r.rolbypassrls AS bypassrls "
            "FROM pg_roles r WHERE r.rolname = current_user")).mappings().one()
    return dict(row)


def assert_runtime_role(engine_: Engine, required: bool) -> dict | None:
    """Fail closed if the runtime role could bypass row-level security."""
    info = runtime_role(engine_)
    if info and required and (info["superuser"] or info["bypassrls"]):
        raise PrivilegedDatabaseRole(
            f"The runtime database role {info['name']!r} is a superuser or has BYPASSRLS, so "
            "row-level security would not apply. Connect as nexus_app / nexus_worker "
            "(scripts/provision_db_roles.py) or set DB_REQUIRE_RLS_ROLE=false for local demos.")
    return info
