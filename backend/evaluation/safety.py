"""Guards for every operation that DROPs and re-creates a database (evaluation and test runs)."""
from __future__ import annotations

import os
import re

from sqlalchemy.engine import URL, make_url

from backend.app.config import settings

RESERVED = {"postgres", "template0", "template1"}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "db", "postgres"}  # "db"/"postgres": Compose/CI services
NAME = re.compile(r"^[a-z0-9_]+_(eval|test)$")


class UnsafeDatabaseTarget(SystemExit):
    pass


def _key(url: URL) -> tuple:
    return (url.host or "localhost", url.port or 5432, url.database)


def assert_disposable_database(url_text: str, app_database_url: str | None = None) -> URL:
    """Reject anything that is not an explicitly disposable, local *_eval / *_test database."""
    if not url_text:
        raise UnsafeDatabaseTarget("An explicit evaluation database URL is required "
                                   "(EVAL_DATABASE_URL or --database-url).")
    url = make_url(url_text)
    if url.get_backend_name() != "postgresql":
        raise UnsafeDatabaseTarget("Evaluation requires PostgreSQL + pgvector")
    name = url.database or ""
    if name in RESERVED or name.startswith("template"):
        raise UnsafeDatabaseTarget(f"Refusing to drop reserved database {name!r}")
    if not NAME.match(name):
        raise UnsafeDatabaseTarget(
            f"Refusing to drop {name!r}: disposable databases must be named like 'nexus_eval' "
            "or 'nexus_test' (lowercase, ending in _eval or _test)")
    app_url = app_database_url if app_database_url is not None else settings.database_url
    if app_url and app_url.startswith("postgresql"):
        app = make_url(app_url)
        if _key(app) == _key(url) or (app.database == name):
            raise UnsafeDatabaseTarget(
                f"Refusing to drop {name!r}: it is the application database (DATABASE_URL)")
    if (url.host or "localhost") not in LOCAL_HOSTS and os.getenv("EVAL_ALLOW_REMOTE_DB") != "1":
        raise UnsafeDatabaseTarget(
            f"Refusing to drop a database on remote host {url.host!r}; "
            "set EVAL_ALLOW_REMOTE_DB=1 only for a dedicated, disposable evaluation server")
    return url
