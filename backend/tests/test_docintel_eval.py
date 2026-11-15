"""Phase 3 evaluation: metric functions (hand-computed values), frozen corpus integrity and CI gates."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from backend.evaluation import docintel_metrics as m

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "eval" / "docintel"
spec = importlib.util.spec_from_file_location("evaluate_docintel", ROOT / "scripts" / "evaluate_docintel.py")
evaluate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluate)


def test_slot_scoring_counts_wrong_values_as_fp_and_fn():
    s = m.score_slots([("money", "10.00", "10"), ("text", "Acme", "acme."), ("date", "2026-01-01", "2026-01-02"),
                       ("text", None, "x"), ("text", "y", None), ("text", None, None)])
    assert (s["tp"], s["fp"], s["fn"]) == (2, 2, 2)
    assert s["precision"] == 0.5 and s["recall"] == 0.5 and s["f1"] == 0.5 and s["exact_match"] == 0.5


def test_levenshtein_cer_wer():
    assert m.levenshtein("kitten", "sitting") == 3
    assert m.cer("abcd", "abed") == 0.25 and m.cer("same  text", "same text") == 0.0
    assert m.wer("the total is 10", "the total is 1O") == 0.25
    assert m.key_token_recall("Total 1,234.50 EUR on 2026-03-31", "Total 1,234.50 EUR on 2026-03-3l") == 0.5


def test_set_scores():
    s = m.set_scores({"a", "b"}, {"b", "c"})
    assert (s["tp"], s["fp"], s["fn"], s["exact"]) == (1, 1, 1, False)


def test_frozen_corpus_matches_its_manifest_and_requirements():
    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((DATA / name).read_bytes()).hexdigest() == digest, name
    labels = [json.loads(x) for x in (DATA / "labels.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(labels) >= 30 and len({x["id"] for x in labels}) == len(labels)
    formats = {x["format"] for x in labels}
    assert {"pdf", "scan", "mixed", "docx", "xlsx", "txt", "md"} <= formats
    assert sum(1 for x in labels if x["format"] == "scan") >= 5
    assert {x["language"] for x in labels} == {"en", "hr"} and {x["tenant"] for x in labels} == {"northstar", "otherco"}
    assert {x["split"] for x in labels} == {"dev", "test"}
    assert any(x["expected_errors"] for x in labels) and any(x["expected_warnings"] for x in labels)
    for x in labels:
        if x["doc_type"] == "unknown":
            continue
        for field, value in x["fields"].items():   # every labelled value has a ground-truth page
            if value is not None:
                assert field in x["pages"], (x["id"], field)
        if x["doc_type"] == "invoice":
            assert x["line_items"] and "line_items" in x["pages"]


def test_dataset_v1_1_errata_hold():
    """ERRATA.md A-C: canonical decimals, one jurisdiction name, value_period on every value."""
    import re

    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["dataset_version"] == "1.1" and (DATA / "ERRATA.md").is_file()
    labels = [json.loads(x) for x in (DATA / "labels.jsonl").read_text(encoding="utf-8").splitlines()]
    numeric = re.compile(r"-?\d+(\.\d+)?")
    for x in labels:
        for item in x["line_items"]:
            for col in ("quantity", "unit_price", "tax_rate", "amount"):
                assert numeric.fullmatch(item[col]), (x["id"], col, item[col])           # A: no '4E+1'
        for name in ("contract_value", "subtotal", "tax_amount", "total_amount"):
            v = x["fields"].get(name)
            assert v is None or numeric.fullmatch(v), (x["id"], name, v)
        if x["doc_type"] == "contract":
            assert "republic" not in (x["fields"].get("governing_law") or "").lower()   # B
            has_value = x["fields"].get("contract_value") is not None
            assert (x["fields"].get("value_period") in ("annual", "total", "monthly")) == has_value, x["id"]  # C
    c13 = next(x for x in labels if x["id"] == "di-c13")
    assert (c13["fields"]["contract_value"], c13["fields"]["value_period"]) == ("3600.00", "monthly")
    # the source documents did not change between v1.0 and v1.1
    v10 = json.loads((DATA / "reports" / "archive" / "dataset-v1.0-manifest.json").read_text(encoding="utf-8"))
    docs = {k: v for k, v in v10["files"].items() if k.startswith("documents/")}
    assert len(docs) == 32 and all(manifest["files"][k] == v for k, v in docs.items())


def passing() -> dict:
    return {"security": {"total_violations": 0}, "meta": {"uploads_ok": 32, "documents": 32},
            "extraction": {"groups": {"all": {"f1": 0.99}, "split:test": {"f1": 0.99}}},
            "evidence": {"evidence_support_rate": 0.99, "evidence_reference_valid_rate": 1.0},
            "tables": {"cell_association_accuracy": 0.99}, "validation": {"numeric_reconciliation_accuracy": 0.99},
            "classification": {"accuracy": 1.0}, "ocr": {"mean_key_token_recall": 0.99, "mean_cer": 0.01},
            "review": {"approval_consistency": 1.0, "validation_false_accept_rate": 0.0,
                       "optimistic_lock_conflict_detected": True}}


def thresholds():
    return json.loads((DATA / "ci_thresholds.json").read_text(encoding="utf-8"))


def test_ci_gates_pass_on_a_clean_result():
    assert evaluate.gates(passing(), thresholds()) == []


@pytest.mark.parametrize("path, value, expected", [
    (("security", "total_violations"), 1, "authorization violations"),
    (("meta", "uploads_ok"), 31, "documents processed"),
    (("extraction", "groups", "split:test", "f1"), 0.1, "field_f1_test"),
    (("ocr", "mean_cer"), 0.5, "ocr_mean_cer"),
    (("review", "validation_false_accept_rate"), 0.5, "validation_false_accept_rate"),
    (("review", "optimistic_lock_conflict_detected"), False, "concurrent"),
    (("tables", "cell_association_accuracy"), None, "line_item_cell_accuracy"),
])
def test_each_ci_gate_fails_when_breached(path, value, expected):
    result = copy.deepcopy(passing())
    target = result
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    failures = evaluate.gates(result, thresholds())
    assert failures and any(expected in f for f in failures), failures


def test_thresholds_are_documented_and_below_the_measured_values():
    t = thresholds()
    assert t["_justification"] and set(t["min"]) <= set(t["measured"])
    for key, floor in t["min"].items():
        assert floor <= t["measured"][key], key
    for key, ceiling in t["max"].items():
        assert ceiling >= t["measured"][key], key
