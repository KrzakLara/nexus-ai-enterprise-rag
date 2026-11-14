"""The CI regression gates must fail on leaks, broken citations and retrieval regressions."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("evaluate_script", ROOT / "scripts" / "evaluate.py")
evaluate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluate)


def passing_result() -> dict:
    floors = json.loads((ROOT / "eval" / "ci_thresholds.json").read_text(encoding="utf-8"))
    modes = {m: {"test": {"ndcg@5": 0.9, "recall@5": 0.9}} for m in ("bm25", *floors["retrieval_min"])}
    per_mode = {m: {"citations": {"total": 10, "valid": 10, "source_mapping_errors": 0,
                                  "extractive_quotes": 10, "quotes_not_verbatim_in_cited_chunk": 0},
                    "answer_quality": {"unsupported_fact_answers": 0, "answer_fact_recall": 0.99},
                    "abstention": {"unanswerable_abstained": 9, "unanswerable_total": 9}}
                for m in modes}
    return {"security": {"total_violations": 0}, "benchmark": {"modes": modes},
            "end_to_end": {"per_mode": per_mode}}


def test_clean_result_passes():
    assert evaluate.gates(passing_result()) == []


@pytest.mark.parametrize("break_it, expected", [
    (lambda r: r["security"].update(total_violations=1), "authorization violations"),
    (lambda r: r["end_to_end"]["per_mode"]["hybrid"]["citations"].update(valid=9), "invalid citations"),
    (lambda r: r["end_to_end"]["per_mode"]["lexical"]["citations"].update(source_mapping_errors=2), "mapping"),
    (lambda r: r["benchmark"]["modes"]["hybrid"]["test"].update({"ndcg@5": 0.5}), "hybrid ndcg@5"),
    (lambda r: r["benchmark"]["modes"]["semantic"]["test"].update({"recall@5": 0.1}), "semantic recall@5"),
    (lambda r: r["end_to_end"]["per_mode"]["hybrid"]["abstention"].update(unanswerable_abstained=1),
     "unanswerable abstention"),
    (lambda r: r["end_to_end"]["per_mode"]["semantic"]["citations"].update(quotes_not_verbatim_in_cited_chunk=1),
     "not verbatim"),
    (lambda r: r["end_to_end"]["per_mode"]["bm25"]["answer_quality"].update(unsupported_fact_answers=1),
     "unsupported fact"),
    (lambda r: r["end_to_end"]["per_mode"]["hybrid"]["answer_quality"].update(answer_fact_recall=0.1),
     "answer fact recall"),
])
def test_each_gate_fails_when_breached(break_it, expected):
    result = copy.deepcopy(passing_result())
    break_it(result)
    failures = evaluate.gates(result)
    assert failures and any(expected in f for f in failures), failures


def test_missing_answer_fact_recall_threshold_fails_instead_of_skipping(tmp_path, monkeypatch):
    cfg = json.loads((ROOT / "eval" / "ci_thresholds.json").read_text(encoding="utf-8"))
    del cfg["hybrid_min_answer_fact_recall"]
    path = tmp_path / "ci_thresholds.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(evaluate, "THRESHOLDS", path)
    assert any("hybrid_min_answer_fact_recall is missing" in f for f in evaluate.gates(passing_result()))


def test_answer_fact_recall_floor_rejects_the_phase2c_extractor():
    """The Phase 2c first-330-characters extractor scored 0.3077 on this exact metric (--ci, test)."""
    floors = json.loads((ROOT / "eval" / "ci_thresholds.json").read_text(encoding="utf-8"))
    assert 0.3077 < floors["hybrid_min_answer_fact_recall"] <= floors["measured_test_split"]["hybrid_answer_fact_recall"]
    result = passing_result()
    result["end_to_end"]["per_mode"]["hybrid"]["answer_quality"]["answer_fact_recall"] = 0.3077
    assert any("answer fact recall" in f for f in evaluate.gates(result))


def test_published_benchmark_numbers_are_generated_not_hand_edited():
    """docs/benchmark-results.md and the README section must equal a render of the stored report."""
    from backend.evaluation import report

    stored = json.loads((ROOT / "eval" / "reports" / "latest.json").read_text(encoding="utf-8"))
    rendered = report.render(stored)
    assert (ROOT / "docs" / "benchmark-results.md").read_text(encoding="utf-8") == rendered
    assert (ROOT / "eval" / "reports" / "latest.md").read_text(encoding="utf-8") == rendered
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("<!-- BENCHMARK:START -->\n", 1)[1].split("\n<!-- BENCHMARK:END -->", 1)[0]
    assert section == report.readme_section(stored)
    assert stored["security"]["total_violations"] == 0
