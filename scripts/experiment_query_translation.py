"""Phase 2d EXPERIMENT: local query translation (MarianMT) for cross-language retrieval.

    uv sync --locked --extra dev --extra mt
    EVAL_DATABASE_URL=postgresql+psycopg://nexus:demo_local_only@127.0.0.1:5433/nexus_mt_eval \
        python scripts/experiment_query_translation.py            # DEV split only (default)

Not used by the API. The question is translated into the other corpus language (HR <-> EN) and
the translation is searched as an ADDITIONAL permission-scoped branch; the original query's
branches are always kept, so exact identifiers and the original intent are never replaced.
Every branch is the production fts_search / semantic_search (ACL inside the SQL), fused with
the production reciprocal_rank_fusion.

Adoption rule, fixed BEFORE measuring (dev split): a variant is a candidate only if
  (a) dev cross_language nDCG@5 improves by >= 0.10 over its baseline, AND
  (b) dev nDCG@5 over all answerable questions drops by no more than 0.01, AND
  (c) zero authorization leaks.
Only a candidate is ever run on the test split (--split test --variants <name>).
Results: eval/reports/query-translation-<split>.json.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path
from statistics import median
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
from sqlalchemy.orm import sessionmaker

from backend.app.embeddings import Embedder
from backend.app.evidence import extract_identifiers
from backend.app.fusion import reciprocal_rank_fusion
from backend.app.models import User
from backend.app.retrieval import fts_search, semantic_search
from backend.evaluation import runner
from backend.evaluation.corpus import EVAL_DIR, load_corpus
from backend.evaluation.dataset import load_frozen
from backend.evaluation.metrics import aggregate, score_case
from backend.evaluation.safety import assert_disposable_database

MT_MODELS = {"hr->en": "Helsinki-NLP/opus-mt-sla-en", "en->hr": "Helsinki-NLP/opus-mt-en-sla"}
CATEGORIES = ("cross_language", "distractor", "exact_identifier", "croatian", "paraphrase",
              "keyword", "numeric_date", "multi_document")
CANDIDATES, TOP_K = 50, 10
_HR_CHARS = re.compile(r"[čćšđžČĆŠĐŽ]")
_HR_WORDS = frozenset("""je su li da se na za od do koji koja koje što kada koliko koliki kolika
kako gdje mora moramo smije ima imati može prema nakon prije ugovor ugovora dana godišnje""".split())
_EN_WORDS = frozenset("""the is are what which when how many much does do of for to in on with
must can after before within agreement days per""".split())

VARIANTS = {
    "semantic": ("semantic", False),           # baseline: production semantic
    "semantic+translated": ("semantic", True),  # RRF(sem(q), sem(t))
    "hybrid": ("hybrid", False),               # baseline: production hybrid
    "hybrid+translated_semantic": ("hybrid", "sem"),   # RRF(lex(q), sem(q), sem(t))
    "hybrid+translated_both": ("hybrid", "both"),      # RRF(lex(q), lex(t), sem(q), sem(t))
}


def detect_language(text: str) -> str:
    """Tiny deterministic HR/EN detector (diacritics + function words). Runtime-only signal: the
    dataset's language labels are used solely to REPORT its accuracy, never as an input."""
    words = re.findall(r"\w+", text.lower())
    hr = sum(w in _HR_WORDS for w in words) + 2 * len(_HR_CHARS.findall(text))
    en = sum(w in _EN_WORDS for w in words)
    return "hr" if hr > en else "en"


class Translator:
    def __init__(self):
        from huggingface_hub import snapshot_download
        from transformers import MarianMTModel, MarianTokenizer

        start = perf_counter()
        self.models, self.revisions, params = {}, {}, 0
        for direction, name in MT_MODELS.items():
            path = snapshot_download(name, allow_patterns=["*.json", "*.spm", "*.bin", "*.safetensors"])
            self.revisions[name] = Path(path).name
            model = MarianMTModel.from_pretrained(path).eval()
            params += sum(p.numel() for p in model.parameters())
            self.models[direction] = (MarianTokenizer.from_pretrained(path), model)
        self.load_ms = round((perf_counter() - start) * 1000)
        self.parameters, self.weights_mb = params, round(params * 4 / 2**20, 1)

    def translate(self, text: str, source_lang: str) -> str:
        import torch

        direction = "hr->en" if source_lang == "hr" else "en->hr"
        tok, model = self.models[direction]
        src = text if direction == "hr->en" else ">>hrv<< " + text  # multi-target model: Croatian
        with torch.no_grad():
            out = model.generate(**tok([src], return_tensors="pt"), num_beams=4, max_length=128)
        return tok.batch_decode(out, skip_special_tokens=True)[0]


def run_variant(db, case, variant, embedder, translation):
    mode, extra = VARIANTS[variant]
    user = db.get(User, case.persona)
    q, t = case.question, translation
    branches = {"semantic": [h.chunk_id for h in semantic_search(db, user, q, embedder, CANDIDATES)]}
    if mode == "hybrid":
        branches["lexical"] = [h.chunk_id for h in fts_search(db, user, q, CANDIDATES)]
    if extra in (True, "sem", "both"):
        branches["semantic_translated"] = [h.chunk_id for h in semantic_search(db, user, t, embedder, CANDIDATES)]
    if extra == "both":
        branches["lexical_translated"] = [h.chunk_id for h in fts_search(db, user, t, CANDIDATES)]
    if mode == "semantic" and not extra:
        ranked = branches["semantic"][:TOP_K]
    else:
        ranked = [r.chunk_id for r in reciprocal_rank_fusion(branches, top_k=TOP_K)]
    candidates = {c for ids in branches.values() for c in ids}
    return ranked, candidates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--split", choices=("dev", "test"), default="dev")
    parser.add_argument("--variants", nargs="*", default=list(VARIANTS))
    parser.add_argument("--database-url", default=os.getenv("EVAL_DATABASE_URL", ""))
    args = parser.parse_args()
    assert_disposable_database(args.database_url)
    corpus = load_corpus()
    cases = [c for c in load_frozen(corpus) if c.split == args.split]
    embedder = Embedder()
    translator = Translator()
    engine, info = runner.prepare_database(args.database_url, corpus, embedder)

    translations, mt_ms, det_ok, id_kept, id_total = {}, [], 0, 0, 0
    for c in cases:
        lang = detect_language(c.question)
        det_ok += lang == c.language
        start = perf_counter()
        translations[c.id] = translator.translate(c.question, lang)
        mt_ms.append((perf_counter() - start) * 1000)
        for ident in extract_identifiers(c.question):
            id_total += 1
            id_kept += ident.lower() in translations[c.id].lower()

    factory = sessionmaker(engine, expire_on_commit=False)
    out = {"split": args.split, "embedding_model": info["model"], "embedding_revision": info["model_revision"],
           "mt_models": translator.revisions, "mt_load_ms": translator.load_ms,
           "mt_parameters": translator.parameters, "mt_weights_mb_fp32": translator.weights_mb,
           "translation_ms": {"p50": round(median(mt_ms), 1), "p95": round(float(np.percentile(mt_ms, 95)), 1)},
           "language_detection_accuracy": round(det_ok / len(cases), 4),
           "identifiers_preserved_by_translation": f"{id_kept}/{id_total}",
           "variants": {}, "translations": {c.id: translations[c.id] for c in cases}}
    with factory() as db:
        for variant in args.variants:
            per_case, leaks, wall = {}, 0, []
            for c in cases:
                start = perf_counter()
                ranked, candidates = run_variant(db, c, variant, embedder, translations[c.id])
                wall.append((perf_counter() - start) * 1000)
                leaks += len(candidates - corpus.authorized_chunk_ids(c.persona))
                if c.answerable:
                    per_case[c.id] = score_case(ranked, c.relevance)
            by_id = {c.id: c for c in cases}
            out["variants"][variant] = {
                "all": aggregate(list(per_case.values())),
                "by_category": {cat: aggregate([m for i, m in per_case.items() if by_id[i].category == cat])
                                for cat in CATEGORIES},
                "retrieval_wall_ms_excluding_translation": {"p50": round(median(wall), 1),
                                                            "p95": round(float(np.percentile(wall, 95)), 1)},
                "leaked_candidate_chunks": leaks,
            }
    engine.dispose()

    def ndcg(v, cat=None):
        m = out["variants"][v]["by_category"][cat] if cat else out["variants"][v]["all"]
        return m.get("ndcg@5")

    for v in out["variants"]:
        base = "semantic" if v.startswith("semantic") else "hybrid"
        if base in out["variants"] and v != base:
            d_cross = ndcg(v, "cross_language") - ndcg(base, "cross_language")
            d_all = ndcg(v) - ndcg(base)
            out["variants"][v]["vs_baseline"] = {
                "delta_ndcg@5_all": round(d_all, 4), "delta_ndcg@5_cross_language": round(d_cross, 4),
                "passes_adoption_rule": bool(d_cross >= 0.10 and d_all >= -0.01
                                             and out["variants"][v]["leaked_candidate_chunks"] == 0)}
    path = EVAL_DIR / "reports" / f"query-translation-{args.split}.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k not in ("variants", "translations")}, indent=1))
    for v, r in out["variants"].items():
        cats = " ".join(f"{c}={r['by_category'][c].get('ndcg@5', '-')}" for c in CATEGORIES[:3])
        print(f"{v:<28} nDCG@5={r['all']['ndcg@5']} R@5={r['all']['recall@5']} {cats} "
              f"p50={r['retrieval_wall_ms_excluding_translation']['p50']}ms leaks={r['leaked_candidate_chunks']} "
              f"{r.get('vs_baseline', '')}")
    print("written", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
