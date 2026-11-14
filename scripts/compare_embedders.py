"""Phase 2d embedding-model comparison for semantic/hybrid retrieval (DEV split by default).

    python scripts/compare_embedders.py                    # dev split: baseline vs candidates
    python scripts/compare_embedders.py --split test --models <model>   # one-off confirmation

Each model gets a fresh, re-indexed evaluation database (same corpus, same chunker, real
persisted vectors). The frozen test split is NOT used unless explicitly requested. Results go to
eval/reports/embedders-<split>.json. Model weights stay in the Hugging Face cache (not committed).
"""
import argparse
import json
import os
import sys
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.embeddings import Embedder
from backend.evaluation import runner
from backend.evaluation.corpus import EVAL_DIR, load_corpus
from backend.evaluation.dataset import load_frozen
from backend.evaluation.metrics import aggregate
from backend.evaluation.safety import assert_disposable_database

BASELINE = "intfloat/multilingual-e5-small"
CANDIDATES = ["sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"]
FOCUS = ("cross_language", "distractor", "croatian", "paraphrase", "exact_identifier")


def evaluate_model(model: str, url: str, corpus, cases) -> dict:
    start = perf_counter()
    embedder = Embedder(model)
    embedder.embed_query("cold start probe")
    load_ms = (perf_counter() - start) * 1000
    engine, info = runner.prepare_database(url, corpus, embedder)
    by_id = {c.id: c for c in cases}
    out = {"model": model, "revision": info["model_revision"], "model_load_ms": round(load_ms),
           "corpus_embedding_ms": info["corpus_embedding_ms"], **embedder.resource_profile(), "modes": {}}
    from sqlalchemy.orm import sessionmaker
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        for mode in ("semantic", "hybrid"):
            runner.run_case(db, corpus, cases[0], mode, embedder, with_signals=False)  # warm-up
            runs = [runner.run_case(db, corpus, c, mode, embedder, with_signals=False) for c in cases]
            ans = [r for r in runs if r["metrics"] is not None]
            out["modes"][mode] = {
                "all": aggregate([r["metrics"] for r in ans]),
                "by_category": {cat: aggregate([r["metrics"] for r in ans if by_id[r["id"]].category == cat])
                                for cat in FOCUS},
                "latency": runner.latency_summary(runs),
                "leaked_chunks": sum(len(r["leaked_chunk_ids"]) for r in runs),
            }
    engine.dispose()
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--split", choices=("dev", "test"), default="dev")
    parser.add_argument("--models", nargs="*", default=[BASELINE, *CANDIDATES])
    parser.add_argument("--database-url", default=os.getenv("EVAL_DATABASE_URL", ""))
    args = parser.parse_args()
    assert_disposable_database(args.database_url)
    corpus = load_corpus()
    cases = [c for c in load_frozen(corpus) if c.split == args.split]
    results = [evaluate_model(m, args.database_url, corpus, cases) for m in args.models]
    path = EVAL_DIR / "reports" / f"embedders-{args.split}.json"
    path.write_text(json.dumps({"split": args.split, "results": results}, indent=2), encoding="utf-8")
    for r in results:
        print(f"\n{r['model']} @ {r['revision'][:10]}  params={r['parameters']:,} weights={r['weights_mb']} MB "
              f"load={r['model_load_ms']} ms corpus_embed={r['corpus_embedding_ms']} ms")
        for mode, m in r["modes"].items():
            cats = " ".join(f"{c}={m['by_category'][c].get('ndcg@5', '-')}" for c in FOCUS)
            print(f"  {mode:<8} nDCG@5={m['all']['ndcg@5']} R@5={m['all']['recall@5']} "
                  f"p50={m['latency']['wall_ms']['p50']}ms leaks={m['leaked_chunks']} | {cats}")
    print(f"\nwritten {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
