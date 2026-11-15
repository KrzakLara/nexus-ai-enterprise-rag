"""Reconcile object storage with the database (report by default).

    python scripts/storage_reconcile.py                       # report only
    python scripts/storage_reconcile.py --delete-orphans      # also delete orphaned objects

Orphans: objects under tenants/ without a document_versions row (e.g. the upload's object PUT
succeeded but its database transaction did not commit and the best-effort cleanup failed). Only
objects older than --min-age-minutes (default 60) are considered, so in-flight uploads are never
touched. Missing objects: version rows that should have an object (not purged) but do not; these
are integrity problems to investigate (restore from backup), never deleted automatically.
Needs the admin / migration database connection.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.config import settings
from backend.app.models import DocumentVersion
from backend.app.storage import default_store, store_for


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--delete-orphans", action="store_true")
    parser.add_argument("--min-age-minutes", type=int, default=60)
    args = parser.parse_args()
    sessions = sessionmaker(create_engine(os.getenv("MIGRATION_DATABASE_URL") or settings.database_url))
    with sessions() as db:
        versions = db.execute(select(DocumentVersion.storage_backend, DocumentVersion.storage_key,
                                     DocumentVersion.object_deleted_at)).all()
    known = {key for backend, key, _ in versions if backend == settings.storage_backend}
    expected = [(backend, key) for backend, key, deleted in versions if deleted is None]
    store = default_store(settings)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=args.min_age_minutes)
    orphans = [o for o in store.list_keys("tenants/") if o.key not in known
               and (o.last_modified is None or o.last_modified < cutoff)]
    missing = [{"backend": b, "key": k} for b, k in expected if store_for(b, settings).head(k) is None]
    deleted = 0
    if args.delete_orphans:
        for o in orphans:
            store.delete(o.key)
            deleted += 1
    print(json.dumps({"objects_checked_backend": settings.storage_backend, "orphans": [o.key for o in orphans],
                      "orphans_deleted": deleted, "missing_objects": missing}, indent=2))
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
