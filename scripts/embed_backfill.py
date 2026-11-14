"""Generate missing or stale chunk embeddings with the local model (idempotent).

Usage:
    python scripts/embed_backfill.py            # embed everything that is missing/stale
    python scripts/embed_backfill.py --dry-run  # only report what would be embedded
"""
import argparse
from pathlib import Path
import sys
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.database import SessionLocal
from backend.app.embeddings import EmbeddingError, get_embedder
from backend.app.indexing import embed_pending_chunks, embedding_coverage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=64)
    args = parser.parse_args()
    embedder = get_embedder()
    start = perf_counter()
    with SessionLocal() as db:
        try:
            stats = embed_pending_chunks(db, embedder, batch_size=args.batch_size,
                                         dry_run=args.dry_run)
        except EmbeddingError as exc:
            db.rollback()
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        if args.dry_run:
            db.rollback()
        else:
            db.commit()
        covered, total = embedding_coverage(db, embedder.model_name)
    pending = stats.candidates - stats.skipped_up_to_date
    print(
        f"model={embedder.model_name} chunks={stats.candidates} up_to_date={stats.skipped_up_to_date} "
        + (f"would_embed={pending}" if args.dry_run else f"embedded={stats.embedded}")
        + f" coverage={covered}/{total} elapsed={perf_counter() - start:.1f}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
