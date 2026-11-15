"""Verify a restored database + bucket against the original (run inside the api container by
scripts/backup_restore_drill.py; admin URLs and storage credentials come from the environment).

    ORIGINAL_DATABASE_URL, RESTORED_DATABASE_URL   admin connections (read-only use)
    RESTORE_BUCKET                                 bucket with the restored objects
    S3_* (root credentials for the drill)          endpoint / keys
Prints one JSON object. Checks: every non-purged document version's object exists in the restored
bucket with the recorded SHA-256; rebuilt embeddings (after the drill deleted them) equal the
original vectors (cosine distance ~0, CPU inference is deterministic).
"""
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from sqlalchemy import create_engine, text

from backend.app.config import settings
from backend.app.storage import S3ObjectStore, sha256_hex


def vectors(url: str) -> dict[str, np.ndarray]:
    engine = create_engine(url)
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT chunk_id, embedding::text FROM chunk_embeddings")).all()
    engine.dispose()
    return {cid: np.array(json.loads(vec)) for cid, vec in rows}


def main() -> int:
    restored = os.environ["RESTORED_DATABASE_URL"]
    mode = sys.argv[1] if len(sys.argv) > 1 else "objects"
    if mode == "objects":
        store = S3ObjectStore(replace(settings, s3_bucket=os.environ["RESTORE_BUCKET"]))
        engine = create_engine(restored)
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT document_id, version, storage_key, sha256 FROM document_versions "
                                     "WHERE storage_backend = 's3' AND object_deleted_at IS NULL")).all()
        engine.dispose()
        bad = []
        for doc_id, version, key, digest in rows:
            try:
                if sha256_hex(store.get(key)) != digest:
                    bad.append({"document_id": doc_id, "version": version, "problem": "checksum"})
            except Exception as exc:  # noqa: BLE001
                bad.append({"document_id": doc_id, "version": version, "problem": type(exc).__name__})
        print(json.dumps({"objects_checked": len(rows), "objects_bad": bad}))
        return 1 if bad or not rows else 0
    original = vectors(os.environ["ORIGINAL_DATABASE_URL"])
    rebuilt = vectors(restored)
    common = sorted(set(original) & set(rebuilt))
    distances = [float(1 - np.dot(original[c], rebuilt[c]) / (np.linalg.norm(original[c]) * np.linalg.norm(rebuilt[c])))
                 for c in common]
    print(json.dumps({"original_vectors": len(original), "rebuilt_vectors": len(rebuilt), "compared": len(common),
                      "max_cosine_distance": max(distances) if distances else None}))
    return 0 if common and len(common) == len(original) and max(distances) < 1e-4 else 1


if __name__ == "__main__":
    raise SystemExit(main())
