"""Evaluation runner: real PostgreSQL + pgvector, persisted e5 embeddings, the production retrieval
code, and the production /api/ask endpoint (through FastAPI's TestClient).

Protocol
  1. A dedicated database (name must end in _eval or _test) is DROPPED, re-created, migrated with
     Alembic, seeded from eval/corpus and embedded with the real model. Stored rows are verified
     against the corpus (chunk text, embedding count, vector_dims = 384).
  2. Retrieval benchmark: every case x {bm25, lexical, semantic, hybrid}, top_k = 10, warm process.
  3. Ablation (hybrid weights / candidates / k): selected on the DEV split only, reported on TEST.
  4. Evidence sufficiency: rules/thresholds selected on DEV signals only, reported on TEST.
  5. End-to-end /api/ask: citation integrity, answer facts, abstention, authorization.
  6. Security: every returned or candidate chunk is checked against an independent ACL oracle.
No number in the reports is typed by hand; everything is computed here.
"""
from __future__ import annotations

import json
import platform
import re
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from statistics import mean
from time import perf_counter

import numpy as np
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from backend.app.embeddings import Embedder
from backend.app.evidence import EvidencePolicy, compute_signals, decide
from backend.app.indexing import embed_pending_chunks
from backend.app.migrate import migrate
from backend.app.models import Chunk, DocumentGrant, User
from backend.app.retrieval import retrieve

from .corpus import Corpus, seed_corpus
from .dataset import FROZEN_PATH, EvalCase
from .safety import assert_disposable_database
from .metrics import METRICS, aggregate, bootstrap_ci, paired_bootstrap_diff, percentile, score_case

MODES = ("bm25", "lexical", "semantic", "hybrid")
DEFAULT_HYBRID = {"candidates": 50, "rrf_k": 60, "weights": {"lexical": 1.0, "semantic": 1.0}}
CITATION = re.compile(r"\[(S\d+)\]")
QUOTE_LINE = re.compile(r"^\[(S\d+)\] .*?, page \d+: (.+)$", re.M)


# --------------------------------------------------------------------------------- database

def recreate_database(url: str) -> str:
    parsed = assert_disposable_database(url)  # explicit, local, *_eval/*_test, not the app DB
    admin = create_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{parsed.database}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{parsed.database}"'))
    admin.dispose()
    return parsed.render_as_string(hide_password=False)


def prepare_database(url: str, corpus: Corpus, embedder: Embedder) -> tuple[Engine, dict]:
    url = recreate_database(url)
    migrate(url)
    engine = create_engine(url)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        seed_corpus(db, corpus)
        start = perf_counter()
        stats = embed_pending_chunks(db, embedder, batch_size=32)
        embed_ms = (perf_counter() - start) * 1000
        db.commit()
        n_chunks = db.query(Chunk).count()
        dims = db.execute(text("SELECT DISTINCT vector_dims(embedding) FROM chunk_embeddings")).scalars().all()
        n_emb = db.execute(text("SELECT count(*) FROM chunk_embeddings WHERE model_name = :m"),
                           {"m": embedder.model_name}).scalar()
        stored = {c.id: c.content for c in db.query(Chunk)}
        tsv = db.execute(text("SELECT count(*) FROM chunks WHERE content_tsv IS NOT NULL")).scalar()
    expected = {c.id: c.content for c in corpus.chunks}
    if stored != expected:
        raise SystemExit("Stored chunks differ from the corpus chunker output")
    if n_chunks != len(corpus.chunks) or n_emb != n_chunks or dims != [384] or tsv != n_chunks:
        raise SystemExit(f"Index verification failed: chunks={n_chunks} embeddings={n_emb} "
                         f"dims={dims} tsvector={tsv}")
    return engine, {
        "database": make_url(url).database, "chunks": n_chunks, "embeddings": n_emb,
        "vector_dims": dims[0], "tsvector_rows": tsv, "embedded_now": stats.embedded,
        "corpus_embedding_ms": round(embed_ms, 1), "model": embedder.model_name,
        "model_revision": embedder.revision,
    }


class CachedQueryEmbedder:
    """Memoizes query vectors for the ablation grid only (identical vectors, faster). Latency is
    never measured through this wrapper."""

    def __init__(self, inner: Embedder):
        self.inner, self.model_name, self._cache = inner, inner.model_name, {}

    def embed_query(self, text_: str) -> np.ndarray:
        if text_ not in self._cache:
            self._cache[text_] = self.inner.embed_query(text_)
        return self._cache[text_]


# -------------------------------------------------------------------------------- retrieval

def run_case(db: Session, corpus: Corpus, case: EvalCase, mode: str, embedder, *,
             top_k: int = 10, hybrid: dict = DEFAULT_HYBRID, with_signals: bool = True) -> dict:
    user = db.get(User, case.persona)
    start = perf_counter()
    result = retrieve(db, user, case.question, mode, embedder, top_k=top_k,
                      candidates=hybrid["candidates"], weights=dict(hybrid["weights"]),
                      rrf_k=hybrid["rrf_k"])
    wall_ms = (perf_counter() - start) * 1000
    ranked = [h.chunk_id for h in result.hits]
    candidates = set(ranked) | {h.chunk_id for hs in result.branch_hits.values() for h in hs}
    authorized = corpus.authorized_chunk_ids(case.persona)
    return {
        "id": case.id, "mode": mode, "split": case.split, "ranked": ranked,
        "metrics": score_case(ranked, case.relevance) if case.answerable else None,
        "wall_ms": round(wall_ms, 2), "timings": result.timings_ms,
        "leaked_chunk_ids": sorted(candidates - authorized),
        "forbidden_evidence_hits": sorted(candidates & set(case.unauthorized_relevant_chunk_ids)),
        "has_hits": bool(ranked),
        "signals": _signals(case.question, result, embedder)
        if with_signals and mode in ("semantic", "hybrid") else None,
    }


def _signals(question: str, result, embedder) -> dict:
    """Same computation as the API: sentence evidence from the top-5 hits it would answer from."""
    from backend.app.extractive import select_evidence

    api_view = replace(result, hits=result.hits[:5])
    snippets = select_evidence(question, api_view.hits, embedder)
    return compute_signals(question, api_view, snippets=snippets)


def group_metrics(runs: list[dict], cases: dict[str, EvalCase], key: Callable[[EvalCase], str]) -> dict:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in runs:
        if r["metrics"] is not None:
            groups[key(cases[r["id"]])].append(r["metrics"])
    return {g: aggregate(v) for g, v in sorted(groups.items())}


def latency_summary(runs: list[dict]) -> dict:
    out = {"queries": len(runs)}
    walls = [r["wall_ms"] for r in runs]
    out["wall_ms"] = {"p50": round(percentile(walls, 50), 2), "p95": round(percentile(walls, 95), 2),
                      "mean": round(mean(walls), 2)}
    components: dict[str, list[float]] = defaultdict(list)
    for r in runs:
        for k, v in r["timings"].items():
            if k != "total":
                components[k].append(v)
    for k, values in sorted(components.items()):
        out[f"{k}_ms"] = {"p50": round(percentile(values, 50), 2),
                          "p95": round(percentile(values, 95), 2)}
    return out


def benchmark(engine: Engine, corpus: Corpus, cases: list[EvalCase], embedder: Embedder) -> dict:
    factory = sessionmaker(engine, expire_on_commit=False)
    by_id = {c.id: c for c in cases}
    report: dict = {"modes": {}, "cold": {}}
    with factory() as db:
        probe = cases[0]
        for mode in MODES:  # first query per mode in this process (DB caches cold for that path)
            report["cold"][mode] = run_case(db, corpus, probe, mode, embedder)["wall_ms"]
        for mode in MODES:  # warm-up, not measured
            run_case(db, corpus, probe, mode, embedder)
        all_runs = []
        for mode in MODES:
            runs = [run_case(db, corpus, c, mode, embedder) for c in cases]
            all_runs.extend(runs)
            answerable = [r for r in runs if r["metrics"] is not None]
            report["modes"][mode] = {
                "all": aggregate([r["metrics"] for r in answerable]),
                "dev": aggregate([r["metrics"] for r in answerable if r["split"] == "dev"]),
                "test": aggregate([r["metrics"] for r in answerable if r["split"] == "test"]),
                "by_category": group_metrics(runs, by_id, lambda c: c.category),
                "by_language": group_metrics(runs, by_id, lambda c: c.language),
                "by_persona": group_metrics(runs, by_id, lambda c: c.persona),
                "by_tenant": group_metrics(runs, by_id, lambda c: c.tenant),
                "unanswerable_with_results": sum(1 for r in runs if not by_id[r["id"]].answerable and r["has_hits"]),
                "unanswerable_total": sum(1 for c in cases if not c.answerable),
                "latency": latency_summary(runs),
                "leaked_chunks": sum(len(r["leaked_chunk_ids"]) for r in runs),
            }
    report["significance_test_split"] = significance(all_runs)
    return {"report": report, "runs": all_runs}


def significance(runs: list[dict], metrics: tuple[str, ...] = ("ndcg@5", "recall@5")) -> dict:
    """Bootstrap CIs per mode and paired hybrid-vs-other differences on the TEST split."""
    per_mode = {m: {r["id"]: r["metrics"] for r in runs
                    if r["mode"] == m and r["split"] == "test" and r["metrics"] is not None}
                for m in MODES}
    ids = sorted(per_mode["hybrid"])
    if not ids:  # e.g. a subset without answerable test cases: nothing to estimate
        return {"n_cases": 0}
    out: dict = {"n_cases": len(ids), "ci95": {}, "hybrid_minus": {}}
    for metric in metrics:
        out["ci95"][metric] = {m: bootstrap_ci([per_mode[m][i][metric] for i in ids]) for m in MODES}
        out["hybrid_minus"][metric] = {
            m: paired_bootstrap_diff([per_mode["hybrid"][i][metric] for i in ids],
                                     [per_mode[m][i][metric] for i in ids])
            for m in MODES if m != "hybrid"}
    return out


# --------------------------------------------------------------------------------- ablation

def ablation_grid() -> list[dict]:
    grid = []
    for wl, ws in ((1.0, 1.0), (1.0, 2.0), (2.0, 1.0), (1.0, 3.0), (3.0, 1.0)):
        for cand in (10, 20, 50, 100):
            for k in (10, 30, 60, 100):
                grid.append({"candidates": cand, "rrf_k": k, "weights": {"lexical": wl, "semantic": ws}})
    return grid


def _hybrid_scores(db, corpus, cases, embedder, cfg) -> dict:
    metrics = [run_case(db, corpus, c, "hybrid", embedder, hybrid=cfg, with_signals=False)["metrics"]
               for c in cases if c.answerable]
    return aggregate(metrics)


def ablation(engine: Engine, corpus: Corpus, cases: list[EvalCase], embedder: Embedder) -> dict:
    """Select the configuration on DEV only; then report default vs selected on TEST."""
    cached = CachedQueryEmbedder(embedder)
    dev = [c for c in cases if c.split == "dev"]
    test = [c for c in cases if c.split == "test"]
    factory = sessionmaker(engine, expire_on_commit=False)
    rows = []
    with factory() as db:
        for cfg in ablation_grid():
            rows.append({"config": cfg, "dev": _hybrid_scores(db, corpus, dev, cached, cfg)})
        default_dev = next(r for r in rows if r["config"] == DEFAULT_HYBRID)["dev"]
        best = max(rows, key=lambda r: (r["dev"]["ndcg@5"], r["dev"]["mrr@10"],
                                        r["config"] == DEFAULT_HYBRID))
        selected = best["config"]
        default_test = _hybrid_scores(db, corpus, test, cached, DEFAULT_HYBRID)
        selected_test = _hybrid_scores(db, corpus, test, cached, selected)
    return {
        "selection_metric": "dev nDCG@5 (tie-break: dev MRR@10, then the default config)",
        "grid_size": len(rows),
        "default": {"config": DEFAULT_HYBRID, "dev": default_dev, "test": default_test},
        "selected": {"config": selected, "dev": best["dev"], "test": selected_test},
        "test_delta_ndcg@5": round(selected_test["ndcg@5"] - default_test["ndcg@5"], 4),
        "test_delta_recall@5": round(selected_test["recall@5"] - default_test["recall@5"], 4),
        "rows": rows,
    }


# ----------------------------------------------------------------------- evidence sufficiency

def abstention_metrics(decisions: list[tuple[EvalCase, bool, dict]]) -> dict:
    """decisions: (case, answered, run). Positive class = 'answer' (answerable)."""
    tp = sum(1 for c, a, _ in decisions if c.answerable and a)
    fn = sum(1 for c, a, _ in decisions if c.answerable and not a)
    fp = sum(1 for c, a, _ in decisions if not c.answerable and a)
    tn = sum(1 for c, a, _ in decisions if not c.answerable and not a)
    n_ans, n_una = tp + fn, fp + tn
    ar = tp / n_ans if n_ans else 0.0
    ud = tn / n_una if n_una else 0.0
    with_rel = sum(1 for c, a, r in decisions if c.answerable and a and r["metrics"]["hit_rate@5"] > 0)
    # "harmful" false abstention: relevant evidence WAS in the top 5 and the system still refused.
    # Abstaining when retrieval missed the evidence is the desired behaviour, not a loss.
    harmful = sum(1 for c, a, r in decisions if c.answerable and not a and r["metrics"]["hit_rate@5"] > 0)
    return {
        "n_answerable": n_ans, "n_unanswerable": n_una,
        "confusion": {"answered_answerable": tp, "abstained_answerable": fn,
                      "answered_unanswerable": fp, "abstained_unanswerable": tn},
        "answerability_precision": round(tp / (tp + fp), 4) if tp + fp else None,
        "answerable_recall": round(ar, 4),
        "unanswerable_detection": round(ud, 4),
        "false_abstention_rate": round(1 - ar, 4) if n_ans else None,
        "false_answerability_rate": round(1 - ud, 4) if n_una else None,
        "balanced_accuracy": round((ar + ud) / 2, 4),
        "answered_with_relevant_in_top5": round(with_rel / n_ans, 4) if n_ans else None,
        "harmful_false_abstentions": harmful,
        "abstentions_after_retrieval_miss": fn - harmful,
    }


def _decisions(runs, cases, policy):
    return [(cases[r["id"]], decide(r["signals"], r["has_hits"], policy).sufficient, r) for r in runs]


GRID = [round(0.75 + 0.005 * i, 3) for i in range(37)]          # 0.750 .. 0.930
COVERAGE = [0.34, 0.5, 0.6, 0.67, 0.75, 0.8, 1.0]
FAMILY_ORDER = ["chunk", "sentence", "chunk_or_sentence", "sentence_or_coverage",
                "chunk_or_coverage"]  # simpler families first (tie-break)


def policy_candidates() -> list[tuple[str, EvidencePolicy]]:
    out = []
    for t in GRID:
        out.append(("chunk", EvidencePolicy(min_semantic_similarity=t)))
        out.append(("sentence", EvidencePolicy(min_semantic_similarity=None, min_sentence_similarity=t)))
        for c in COVERAGE:
            out.append(("sentence_or_coverage", EvidencePolicy(
                min_semantic_similarity=None, min_sentence_similarity=t, min_term_coverage=c)))
            out.append(("chunk_or_coverage", EvidencePolicy(min_semantic_similarity=t, min_term_coverage=c)))
        for t2 in GRID[::2]:
            out.append(("chunk_or_sentence", EvidencePolicy(min_semantic_similarity=t,
                                                            min_sentence_similarity=t2)))
    return out


def calibrate(bench_runs: list[dict], cases: list[EvalCase],
              previous_policy: EvidencePolicy | None = None) -> dict:
    """Pick the evidence policy per mode on DEV signals only; evaluate the pick on TEST.

    Objective: maximise dev balanced accuracy; tie-breaks: fewer false abstentions, simpler family,
    then higher (more conservative) thresholds. The test split is only read after selection."""
    by_id = {c.id: c for c in cases}
    candidates = policy_candidates()
    out = {"objective": "max dev balanced accuracy; tie-break fewer false abstentions, "
                        "simpler family, higher thresholds",
           "candidates_evaluated": len(candidates), "modes": {}}
    for mode in ("semantic", "hybrid"):
        runs = [r for r in bench_runs if r["mode"] == mode]
        dev = [r for r in runs if r["split"] == "dev"]
        test = [r for r in runs if r["split"] == "test"]
        best_by_family: dict[str, tuple] = {}
        for family, pol in candidates:
            m = abstention_metrics(_decisions(dev, by_id, pol))
            thresholds = [v or 0.0 for v in (pol.min_semantic_similarity, pol.min_sentence_similarity,
                                             pol.min_term_coverage)]
            key = (m["balanced_accuracy"], -m["false_abstention_rate"], -FAMILY_ORDER.index(family),
                   *thresholds)
            if family not in best_by_family or key > best_by_family[family][0]:
                best_by_family[family] = (key, pol, m)
        chosen_family = max(best_by_family, key=lambda f: best_by_family[f][0])
        chosen = best_by_family[chosen_family][1]
        # TEST is evaluated only below, after the choice is fixed.
        families = {f: {"policy": pol.describe(), "dev": m,
                        "test": abstention_metrics(_decisions(test, by_id, pol))}
                    for f, (key, pol, m) in best_by_family.items()}
        cross = [r for r in test if by_id[r["id"]].category == "cross_language"]
        entry = {
            "baselines": {"no_policy": {
                split: abstention_metrics(_decisions(rr, by_id, EvidencePolicy(enabled=False)))
                for split, rr in (("dev", dev), ("test", test))}},
            "families": families,
            "selected": {"family": chosen_family, "policy": chosen.describe(),
                         "dev": families[chosen_family]["dev"], "test": families[chosen_family]["test"]},
            "selected_test_by_language": {
                lang: abstention_metrics(_decisions(
                    [r for r in test if by_id[r["id"]].language == lang], by_id, chosen))
                for lang in ("en", "hr")},
            "selected_test_cross_language": abstention_metrics(_decisions(cross, by_id, chosen)),
            "similarity_distribution_dev": _similarity_distribution(dev, by_id),
        }
        if previous_policy is not None:
            entry["previous_policy"] = {
                "policy": previous_policy.describe(),
                "dev": abstention_metrics(_decisions(dev, by_id, previous_policy)),
                "test": abstention_metrics(_decisions(test, by_id, previous_policy)),
                "test_cross_language": abstention_metrics(_decisions(cross, by_id, previous_policy)),
            }
        out["modes"][mode] = entry
    return out


def _similarity_distribution(runs, by_id) -> dict:
    def stats(values):
        if not values:
            return None
        return {"n": len(values), "min": round(min(values), 4), "p50": round(percentile(values, 50), 4),
                "max": round(max(values), 4)}
    sims = defaultdict(list)
    for r in runs:
        label = "answerable" if by_id[r["id"]].answerable else "unanswerable"
        for signal in ("semantic_top_similarity", "sentence_top_similarity", "term_coverage"):
            v = r["signals"].get(signal)
            if v is not None:
                sims[f"{signal}:{label}"].append(v)
    return {k: stats(v) for k, v in sorted(sims.items())}


# ----------------------------------------------------------- end-to-end API: citations, security

def _norm(s: str) -> str:
    return " ".join(s.split()).lower()


def _ws(s: str) -> str:
    return " ".join(s.split())


def end_to_end(engine: Engine, corpus: Corpus, cases: list[EvalCase], embedder: Embedder,
               policy: EvidencePolicy) -> dict:
    from fastapi.testclient import TestClient

    from backend.app import main
    from backend.app.database import get_db
    from backend.app.embeddings import get_embedder

    factory = sessionmaker(engine, expire_on_commit=False)

    def override_db():
        with factory() as db:
            yield db

    original_policy = main.EVIDENCE_POLICY
    main.app.dependency_overrides[get_db] = override_db
    main.app.dependency_overrides[get_embedder] = lambda: embedder
    main.EVIDENCE_POLICY = policy
    client = TestClient(main.app)
    tokens = {}

    def headers(persona: str) -> dict:
        if persona not in tokens:
            r = client.post("/api/auth/login", json={"email": corpus.personas[persona].email,
                                                       "password": corpus.password})
            tokens[persona] = {"Authorization": "Bearer " + r.json()["access_token"]}
        return tokens[persona]

    chunk_text = {c.id: c.content for c in corpus.chunks}
    chunk_doc = {c.id: (c.document_id, c.page_number) for c in corpus.chunks}
    per_mode = {}
    security = {"responses": 0, "unauthorized_sources": 0, "unauthorized_citations": 0,
                "forbidden_evidence_in_output": 0}
    try:
        for mode in MODES:
            cit_total = cit_valid = mapping_errors = 0
            fact_cases = retr_fact = ans_fact = unsupported = answered_without_citation = 0
            quotes = non_verbatim_quotes = 0
            abst_una = n_una = false_abst = n_ans = 0
            for case in cases:
                body = client.post("/api/ask", headers=headers(case.persona), json={
                    "question": case.question, "mode": mode, "top_k": 5}).json()
                security["responses"] += 1
                authorized = corpus.authorized_chunk_ids(case.persona)
                sources = {s["id"]: s for s in body["sources"]}
                for s in body["sources"]:
                    if s["chunk_id"] not in authorized:
                        security["unauthorized_sources"] += 1
                    if (s["chunk_id"] not in chunk_text
                            or chunk_doc[s["chunk_id"]] != (s["document_id"], s["page"])
                            or s["excerpt"] != chunk_text[s["chunk_id"]][:800]):
                        mapping_errors += 1
                output = _norm(body["answer"] + " " + " ".join(s["excerpt"] for s in body["sources"]))
                if case.category == "authorization" and any(_norm(e) in output for e in case.expected_evidence):
                    security["forbidden_evidence_in_output"] += 1
                cited = CITATION.findall(body["answer"])
                cit_total += len(cited)
                for sid in cited:
                    if sid in sources:
                        cit_valid += 1
                        if sources[sid]["chunk_id"] not in authorized:
                            security["unauthorized_citations"] += 1
                if body["mode"] in ("extractive", "llm") and not cited:
                    answered_without_citation += 1
                if body["mode"] == "extractive":
                    for sid, quote in QUOTE_LINE.findall(body["answer"]):
                        quotes += 1
                        src = sources.get(sid)
                        # verbatim up to whitespace: a quote joined across a hard line break is
                        # rendered on one line; words, case and punctuation must match exactly
                        if src is None or _ws(quote) not in _ws(chunk_text.get(src["chunk_id"], "")):
                            non_verbatim_quotes += 1
                abstained = body["mode"] == "no_evidence"
                if case.answerable:
                    n_ans += 1
                    false_abst += abstained
                else:
                    n_una += 1
                    abst_una += abstained
                if case.answerable and case.expected_facts:
                    fact_cases += 1
                    returned = " ".join(chunk_text[s["chunk_id"]] for s in body["sources"]
                                        if s["chunk_id"] in chunk_text)
                    retr_fact += all(f in returned for f in case.expected_facts)
                    answer_n = _norm(body["answer"])
                    has_facts = all(_norm(f) in answer_n for f in case.expected_facts)
                    ans_fact += has_facts
                    cited_text = _norm(" ".join(chunk_text.get(sources[sid]["chunk_id"], "")
                                                for sid in cited if sid in sources))
                    if has_facts and not all(_norm(f) in cited_text for f in case.expected_facts):
                        unsupported += 1
            per_mode[mode] = {
                "citations": {"total": cit_total, "valid": cit_valid,
                              "validity_rate": round(cit_valid / cit_total, 4) if cit_total else None,
                              "source_mapping_errors": mapping_errors,
                              "answers_without_citation": answered_without_citation,
                              "extractive_quotes": quotes,
                              "quotes_not_verbatim_in_cited_chunk": non_verbatim_quotes},
                "answer_quality": {
                    "cases_with_expected_facts": fact_cases,
                    "retrieval_fact_recall@5": round(retr_fact / fact_cases, 4) if fact_cases else None,
                    "answer_fact_recall": round(ans_fact / fact_cases, 4) if fact_cases else None,
                    "unsupported_fact_answers": unsupported,
                    "generator": "extractive (no LLM key)" if not main.settings.openai_api_key else "llm",
                },
                "abstention": {"unanswerable_abstained": abst_una, "unanswerable_total": n_una,
                               "answerable_abstained": false_abst, "answerable_total": n_ans},
            }
        security["llm_context"] = _llm_context_check(client, headers, corpus, cases)
        security["permission_change"] = _permission_change_check(factory, client, headers, corpus)
    finally:
        main.app.dependency_overrides.clear()
        main.EVIDENCE_POLICY = original_policy
    return {"policy": policy.describe(), "per_mode": per_mode, "security": security}


def _llm_context_check(client, headers, corpus: Corpus, cases: list[EvalCase]) -> dict:
    """Capture the exact prompt the API would send to an LLM (fake client, no network) and verify
    it only contains authorized chunk text. Evidence policy is disabled here on purpose, so the
    maximum amount of retrieved text reaches the prompt."""
    import openai

    from backend.app import rag

    captured: list[str] = []

    class FakeResponses:
        def create(self, **kwargs):
            captured.append(kwargs["input"])
            return type("R", (), {"output_text": "See [S1]."})()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.responses = FakeResponses()

    real_client, real_settings = openai.OpenAI, rag.settings
    openai.OpenAI = FakeOpenAI
    rag.settings = replace(rag.settings, openai_api_key="sk-eval-fake-not-sent")
    violations = prompts = 0
    unauthorized_text = {c.id: c.content for c in corpus.chunks}
    try:
        for mode in MODES:
            for case in cases:
                captured.clear()
                client.post("/api/ask", headers=headers(case.persona), json={
                    "question": case.question, "mode": mode, "top_k": 10,
                    "apply_evidence_policy": False})
                if not captured:
                    continue
                prompts += 1
                sources_part = captured[0].split("AUTHORIZED SOURCES:", 1)[1]
                allowed = corpus.authorized_chunk_ids(case.persona)
                if any(text_ in sources_part for cid, text_ in unauthorized_text.items()
                       if cid not in allowed):
                    violations += 1
    finally:
        openai.OpenAI, rag.settings = real_client, real_settings
    return {"prompts_checked": prompts, "prompts_with_unauthorized_text": violations}


def _permission_change_check(factory, client, headers, corpus: Corpus) -> dict:
    """Grant -> visible immediately in all PG modes; revoke -> gone immediately."""
    q = "What is the total salary increase budget for 2027?"
    target = "ns-hr-pol-004"
    results = {}

    def visible(mode):
        body = client.post("/api/ask", headers=headers("ns_finance"), json={
            "question": q, "mode": mode, "top_k": 10, "apply_evidence_policy": False}).json()
        return any(s["document_id"] == target for s in body["sources"])

    modes = ("lexical", "semantic", "hybrid")
    results["before_grant"] = {m: visible(m) for m in modes}
    with factory() as db:
        db.add(DocumentGrant(document_id=target, group_name="finance"))
        db.commit()
    results["after_grant"] = {m: visible(m) for m in modes}
    with factory() as db:
        db.execute(text("DELETE FROM document_grants WHERE document_id = :d AND group_name = 'finance'"),
                   {"d": target})
        db.commit()
    results["after_revoke"] = {m: visible(m) for m in modes}
    results["passed"] = (not any(results["before_grant"].values())
                         and all(results["after_grant"].values())
                         and not any(results["after_revoke"].values()))
    return results


# ------------------------------------------------------------------------------- top level

def metadata(corpus: Corpus, cases: list[EvalCase], db_info: dict, cold_model_ms: float) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": sys.version.split()[0], "platform": platform.platform(),
        "dataset_sha256": sha256(FROZEN_PATH.read_bytes()).hexdigest(),
        "corpus": {"documents": len(corpus.documents), "chunks": len(corpus.chunks),
                   "tenants": sorted({d.tenant for d in corpus.documents.values()}),
                   "languages": dict(sorted(_count(d.lang for d in corpus.documents.values()).items()))},
        "cases": {"total": len(cases), "dev": sum(c.split == "dev" for c in cases),
                  "test": sum(c.split == "test" for c in cases),
                  "answerable": sum(c.answerable for c in cases),
                  "unanswerable": sum(not c.answerable for c in cases),
                  "by_category": dict(sorted(_count(c.category for c in cases).items()))},
        "index": db_info,
        "cold_start_model_load_and_first_query_ms": round(cold_model_ms, 1),
        "metrics": list(METRICS),
    }


def _count(values: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for v in values:
        out[v] += 1
    return dict(out)


def write_jsonl(path, rows: Iterable[dict]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


# ------------------------------------------------------------------ offline answer extraction

class CachedEmbedder(CachedQueryEmbedder):
    """Query + passage memoization for the extraction study (identical vectors, faster)."""

    def __init__(self, inner: Embedder):
        super().__init__(inner)
        self._passages: dict[str, np.ndarray] = {}

    def embed_passages(self, texts):
        missing = [t for t in dict.fromkeys(texts) if t not in self._passages]
        if missing:
            for t, v in zip(missing, self.inner.embed_passages(missing)):
                self._passages[t] = v
        return np.stack([self._passages[t] for t in texts])


def legacy_first_330(hits) -> str:
    """The Phase 2c offline answer (first 330 characters of each of the top-3 chunks)."""
    return "\n\n".join(f"[S{i}] {h.document_title}: {' '.join(h.content.split())[:330]}"
                       for i, h in enumerate(hits[:3], 1))


def extraction_grid():
    from backend.app.extractive import ExtractionConfig

    grid = []
    for ws in (0.0, 1.0):
        for wl in (0.0, 0.5, 1.0):
            for we in (0.0, 0.5):
                if ws == wl == we == 0:
                    continue
                for sps in (1, 2):
                    grid.append(ExtractionConfig(sentences_per_source=sps, w_semantic=ws,
                                                 w_lexical=wl, w_exact=we))
    return grid


def _facts_in(text_: str, facts) -> bool:
    t = _norm(text_)
    return all(_norm(f) in t for f in facts)


def extraction_study(engine: Engine, corpus: Corpus, cases: list[EvalCase], embedder: Embedder,
                     mode: str = "hybrid") -> dict:
    """Select the extraction config on DEV (answer fact recall), report it on TEST.

    Retrieval is fixed (mode, top_k = 5, no evidence policy) so only the extraction differs."""
    from backend.app.extractive import ExtractionConfig, render_extractive_answer, select_evidence

    cached = CachedEmbedder(embedder)
    factory = sessionmaker(engine, expire_on_commit=False)
    fact_cases = [c for c in cases if c.answerable and c.expected_facts]
    retrieved = {}
    with factory() as db:
        for c in fact_cases:
            res = retrieve(db, db.get(User, c.persona), c.question, mode, cached, top_k=5,
                           candidates=DEFAULT_HYBRID["candidates"], weights=dict(DEFAULT_HYBRID["weights"]),
                           rrf_k=DEFAULT_HYBRID["rrf_k"])
            retrieved[c.id] = res.hits

    def evaluate(make_answer, split):
        rows = [c for c in fact_cases if c.split == split]
        found = ans = cond_den = 0
        chars = []
        verbatim_violations = 0
        for c in rows:
            hits = retrieved[c.id]
            answer_text, snippets = make_answer(c, hits)
            in_retrieval = _facts_in(" ".join(h.content for h in hits), c.expected_facts)
            hit_answer = _facts_in(answer_text, c.expected_facts)
            ans += hit_answer
            cond_den += in_retrieval
            found += hit_answer and in_retrieval
            chars.append(len(answer_text))
            for s in snippets or []:
                if s.sentence not in s.hit.content:
                    verbatim_violations += 1
        n = len(rows)
        return {"n": n, "answer_fact_recall": round(ans / n, 4) if n else None,
                "answer_fact_recall_given_retrieved": round(found / cond_den, 4) if cond_den else None,
                "retrieval_fact_recall@5": round(cond_den / n, 4) if n else None,
                "mean_answer_chars": round(mean(chars), 1) if chars else None,
                "verbatim_violations": verbatim_violations}

    def legacy(c, hits):
        return legacy_first_330(hits), None

    def with_config(cfg):
        def run(c, hits):
            sn = select_evidence(c.question, hits, cached if cfg.w_semantic else None, cfg)
            return render_extractive_answer(sn), sn
        return run

    rows = []
    for cfg in extraction_grid():
        rows.append({"config": cfg.__dict__, "dev": evaluate(with_config(cfg), "dev")})
    best = max(rows, key=lambda r: (r["dev"]["answer_fact_recall"], -r["dev"]["mean_answer_chars"]))
    selected = ExtractionConfig(**best["config"])
    default = ExtractionConfig()
    return {
        "retrieval": {"mode": mode, "top_k": 5, "evidence_policy": "off (isolates extraction)"},
        "selection_metric": "dev answer fact recall; tie-break shorter answers",
        "legacy_first_330": {"dev": evaluate(legacy, "dev"), "test": evaluate(legacy, "test")},
        "selected": {"config": selected.__dict__, "dev": best["dev"],
                     "test": evaluate(with_config(selected), "test")},
        "code_default": {"config": default.__dict__, "dev": evaluate(with_config(default), "dev"),
                         "test": evaluate(with_config(default), "test")},
        "grid_size": len(rows),
        "rows": rows,
    }
