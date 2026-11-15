"""Copy v0.1-v0.3 local originals into the configured S3 / MinIO bucket (safe, resumable).

    python scripts/storage_backfill.py            # DRY RUN (default): report what would be copied
    python scripts/storage_backfill.py --apply    # copy, verify, then switch the database rows

For every document version still on the `local` backend:
  1. read the local file (STORAGE_DIR / storage_key); missing -> reported, skipped
  2. SHA-256 must equal the recorded checksum; mismatch -> reported, skipped (never uploaded)
  3. PUT to tenants/<tenant>/documents/<id>/v<n>/original with a server-side SHA-256 check
  4. read the object back and compare SHA-256 again
  5. only then switch document_versions / documents to storage_backend='s3' (one transaction)
The LOCAL FILE IS NEVER DELETED OR MODIFIED by this script: remove the old document volume
manually after you have verified the migration (docs/operations-runbook.md). Re-running continues
where a previous run stopped (rows already on s3 are skipped). Needs the admin / migration
connection (DATABASE_URL or MIGRATION_DATABASE_URL) and STORAGE_BACKEND=s3 + S3_* settings.
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.config import settings
from backend.app.models import Document, DocumentVersion
from backend.app.storage import UUID, StorageError, object_key, sha256_hex, store_for


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="perform the copy (default: dry run)")
    parser.add_argument("--limit", type=int, default=0, help="stop after N versions (0 = all)")
    args = parser.parse_args()
    if settings.storage_backend != "s3":
        print("STORAGE_BACKEND must be s3 (the copy target)", file=sys.stderr)
        return 2
    url = os.getenv("MIGRATION_DATABASE_URL") or settings.database_url
    sessions = sessionmaker(create_engine(url), expire_on_commit=False)
    local, target = store_for("local", settings), store_for("s3", settings)
    report = {"mode": "apply" if args.apply else "dry_run", "candidates": 0, "copied": 0, "skipped": [],
              "failed": []}
    with sessions() as db:
        rows = db.execute(select(DocumentVersion, Document).join(Document, Document.id == DocumentVersion.document_id)
                          .where(DocumentVersion.storage_backend == "local",
                                 DocumentVersion.object_deleted_at.is_(None),
                                 Document.purged_at.is_(None))
                          .order_by(DocumentVersion.document_id, DocumentVersion.version)).all()
    for version, doc in rows:
        if args.limit and report["candidates"] >= args.limit:
            break
        report["candidates"] += 1
        ref = {"document_id": doc.id, "version": version.version}
        if not UUID.match(doc.id):
            report["skipped"].append(ref | {"reason": "document id is not a UUID (test fixture?)"})
            continue
        try:
            data = local.get(version.storage_key)
        except (StorageError, OSError) as exc:
            report["failed"].append(ref | {"reason": f"local original unavailable ({type(exc).__name__})"})
            continue
        digest = sha256_hex(data)
        if digest != version.sha256:
            report["failed"].append(ref | {"reason": "checksum mismatch: local file differs from the recorded SHA-256"})
            continue
        key = object_key(doc.tenant_id, doc.id, version.version)
        if not args.apply:
            report["copied"] += 1
            continue
        try:
            target.put(key, data, version.content_type or "application/octet-stream", digest,
                       {"document-id": doc.id, "tenant-id": doc.tenant_id, "version": str(version.version),
                        "source": "storage_backfill"})
            if sha256_hex(target.get(key)) != digest:
                raise StorageError("read-back checksum mismatch")
        except StorageError as exc:
            report["failed"].append(ref | {"reason": f"upload failed ({type(exc).__name__})"})
            continue
        with sessions() as db:
            v = db.get(DocumentVersion, version.id)
            v.storage_backend, v.storage_key = "s3", key
            d = db.get(Document, doc.id)
            if d.current_version == version.version:
                d.storage_backend, d.storage_key = "s3", key
                d.size_bytes = d.size_bytes or len(data)
            v.size_bytes = v.size_bytes or len(data)
            db.commit()
        report["copied"] += 1
    print(json.dumps(report, indent=2))
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
