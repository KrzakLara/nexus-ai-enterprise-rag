"""NEXUS AI evaluation: retrieval benchmark, RRF ablation, abstention calibration, citations, security.

    python scripts/evaluate.py                # full benchmark -> eval/reports/, docs/benchmark-results.md, README
    python scripts/evaluate.py --ci           # deterministic CI gate on the test split (no ablation)

The target database must be given explicitly (EVAL_DATABASE_URL or --database-url). It is DROPPED
and re-created, so it must be a local database named *_eval or *_test that is not DATABASE_URL.
    EVAL_DATABASE_URL=postgresql+psycopg://nexus:demo_local_only@127.0.0.1:5433/nexus_eval
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.embeddings import Embedder
from backend.app.evidence import EvidencePolicy
from backend.app.main import EVIDENCE_POLICY
from backend.evaluation import report as rep
from backend.evaluation import runner
from backend.evaluation.corpus import EVAL_DIR, load_corpus
from backend.evaluation.dataset import load_frozen
from backend.evaluation.safety import assert_disposable_database

REPORTS = EVAL_DIR / "reports"
THRESHOLDS = EVAL_DIR / "ci_thresholds.json"


def security_summary(bench_runs, e2e) -> dict:
    s = e2e["security"]
    sec = {
        "retrieval_queries_checked": len(bench_runs),
        "leaked_candidate_chunks (all branches, top-50 candidates)": sum(len(r["leaked_chunk_ids"]) for r in bench_runs),
        "forbidden_evidence_chunks_retrieved": sum(len(r["forbidden_evidence_hits"]) for r in bench_runs),
        "api_responses_checked": s["responses"],
        "unauthorized_sources_in_api_responses": s["unauthorized_sources"],
        "unauthorized_citations": s["unauthorized_citations"],
        "forbidden_evidence_text_in_api_output": s["forbidden_evidence_in_output"],
        "llm_prompts_checked": s["llm_context"]["prompts_checked"],
        "llm_prompts_with_unauthorized_text": s["llm_context"]["prompts_with_unauthorized_text"],
        "permission_change_grant_revoke_immediate": s["permission_change"]["passed"],
    }
    sec["total_violations"] = (sec["leaked_candidate_chunks (all branches, top-50 candidates)"]
                               + sec["forbidden_evidence_chunks_retrieved"]
                               + sec["unauthorized_sources_in_api_responses"]
                               + sec["unauthorized_citations"]
                               + sec["forbidden_evidence_text_in_api_output"]
                               + sec["llm_prompts_with_unauthorized_text"]
                               + (0 if sec["permission_change_grant_revoke_immediate"] else 1))
    return sec


def update_readme(section: str) -> None:
    path = ROOT / "README.md"
    text = path.read_text(encoding="utf-8")
    start, end = "<!-- BENCHMARK:START -->", "<!-- BENCHMARK:END -->"
    if start in text and end in text:
        new = re.sub(re.escape(start) + r".*?" + re.escape(end),
                     lambda _: f"{start}\n{section}\n{end}", text, flags=re.S)
        path.write_text(new, encoding="utf-8")


def publish(result: dict) -> None:
    """Write Markdown report, docs/benchmark-results.md, README section and eval/calibration.json."""
    markdown = rep.render(result)
    (REPORTS / "latest.md").write_text(markdown, encoding="utf-8")
    (ROOT / "docs" / "benchmark-results.md").write_text(markdown, encoding="utf-8")
    update_readme(rep.readme_section(result))
    selected = result["calibration"]["modes"]["hybrid"]["selected"]
    (EVAL_DIR / "calibration.json").write_text(json.dumps({
        "selected_on": "dev split only",
        "family": selected["family"], "policy": selected["policy"],
        "extraction": result["extraction"]["selected"]["config"],
        "dev": selected["dev"], "test": selected["test"],
        "dataset_sha256": result["meta"]["dataset_sha256"],
        "generated_at": result["meta"]["generated_at"],
    }, indent=2), encoding="utf-8")


def gates(result: dict) -> list[str]:
    cfg = json.loads(THRESHOLDS.read_text(encoding="utf-8"))
    failures = []
    if result["security"]["total_violations"] != 0:
        failures.append(f"authorization violations: {result['security']['total_violations']}")
    for mode, e in result["end_to_end"]["per_mode"].items():
        c = e["citations"]
        if c["total"] and c["valid"] != c["total"]:
            failures.append(f"{mode}: invalid citations {c['total'] - c['valid']}")
        if c["source_mapping_errors"]:
            failures.append(f"{mode}: citation source mapping errors {c['source_mapping_errors']}")
        if c.get("quotes_not_verbatim_in_cited_chunk"):
            failures.append(f"{mode}: {c['quotes_not_verbatim_in_cited_chunk']} quotes not verbatim in cited chunk")
        if e["answer_quality"]["unsupported_fact_answers"]:
            failures.append(f"{mode}: unsupported fact answers {e['answer_quality']['unsupported_fact_answers']}")
    for mode, limits in cfg["retrieval_min"].items():
        got = result["benchmark"]["modes"][mode]["test"]
        for metric, minimum in limits.items():
            if got[metric] < minimum:
                failures.append(f"{mode} {metric} {got[metric]:.4f} < {minimum}")
    # End-to-end /api/ask (hybrid, app evidence policy, top_k=5): share of answerable test cases
    # whose answer text contains every expected fact. Abstentions count as misses. Mandatory: a
    # missing threshold is a configuration error, never a silently skipped gate.
    afr_min = cfg.get("hybrid_min_answer_fact_recall")
    afr = result["end_to_end"]["per_mode"]["hybrid"]["answer_quality"]["answer_fact_recall"]
    if afr_min is None:
        failures.append("eval/ci_thresholds.json: hybrid_min_answer_fact_recall is missing")
    elif afr is None or afr < afr_min:
        failures.append(f"hybrid answer fact recall {afr} < {afr_min}")
    hyb = result["end_to_end"]["per_mode"]["hybrid"]["abstention"]
    una_rate = hyb["unanswerable_abstained"] / max(hyb["unanswerable_total"], 1)
    if una_rate < cfg["hybrid_min_unanswerable_abstention"]:
        failures.append(f"hybrid unanswerable abstention {una_rate:.3f} < {cfg['hybrid_min_unanswerable_abstention']}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ci", action="store_true", help="test split only, no ablation, enforce gates")
    parser.add_argument("--render-only", action="store_true",
                        help="regenerate docs/README/calibration from eval/reports/latest.json (no measuring)")
    parser.add_argument("--database-url", default=os.getenv("EVAL_DATABASE_URL", ""),
                        help="explicit disposable PostgreSQL database (*_eval / *_test)")
    args = parser.parse_args()
    if args.render_only:
        publish(json.loads((REPORTS / "latest.json").read_text(encoding="utf-8")))
        print("re-rendered from eval/reports/latest.json")
        return 0

    assert_disposable_database(args.database_url)  # fail fast, before loading the model
    corpus = load_corpus()
    cases = load_frozen(corpus)  # fails if eval/retrieval.jsonl is malformed or stale
    start = perf_counter()
    embedder = Embedder()
    embedder.embed_query("cold start probe")
    cold_ms = (perf_counter() - start) * 1000
    print(f"model loaded + first query: {cold_ms:.0f} ms", flush=True)

    engine, db_info = runner.prepare_database(args.database_url, corpus, embedder)
    print(f"index verified: {db_info}", flush=True)
    selected_cases = [c for c in cases if c.split == "test"] if args.ci else cases
    bench = runner.benchmark(engine, corpus, selected_cases, embedder)
    print("benchmark done", flush=True)
    result = {"meta": runner.metadata(corpus, cases, db_info, cold_ms), "benchmark": bench["report"],
              "mode": "ci" if args.ci else "full"}
    if not args.ci:
        result["ablation"] = runner.ablation(engine, corpus, cases, embedder)
        print("ablation done", flush=True)
        previous = EvidencePolicy(**json.loads(
            (EVAL_DIR / "baselines" / "phase2c-calibration.json").read_text(encoding="utf-8"))["policy"])
        result["calibration"] = runner.calibrate(bench["runs"], cases, previous_policy=previous)
        result["extraction"] = runner.extraction_study(engine, corpus, cases, embedder)
        print("extraction study done", flush=True)
        print("calibration done", flush=True)
    # Full run: end-to-end uses the policy just selected on dev (what ships after the config is
    # updated; test_app_defaults_match_dev_calibration enforces that). CI: the app's configured policy.
    policy = (EVIDENCE_POLICY if args.ci else
              EvidencePolicy(**result["calibration"]["modes"]["hybrid"]["selected"]["policy"]))
    result["end_to_end"] = runner.end_to_end(engine, corpus, selected_cases, embedder, policy)
    result["security"] = security_summary(bench["runs"], result["end_to_end"])
    engine.dispose()

    REPORTS.mkdir(parents=True, exist_ok=True)
    name = "ci" if args.ci else "latest"
    (REPORTS / f"{name}.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    runner.write_jsonl(REPORTS / f"{name}-cases.jsonl",
                       ({k: r[k] for k in ("id", "mode", "split", "ranked", "metrics", "wall_ms",
                                           "leaked_chunk_ids", "signals")} for r in bench["runs"]))
    if not args.ci:
        publish(result)
    print(rep.comparison_table(result, "test"))
    print("security:", json.dumps(result["security"], indent=1))
    if args.ci:
        failures = gates(result)
        if failures:
            print("CI GATE FAILED:\n  " + "\n  ".join(failures))
            return 1
        print("CI gates passed")
    print(f"total {perf_counter() - start:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
