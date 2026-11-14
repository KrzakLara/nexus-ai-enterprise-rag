"""Phase 3 document-intelligence evaluation (end to end through the API, real OCR, PostgreSQL).

    python scripts/evaluate_docintel.py --database-url postgresql+psycopg://...@127.0.0.1:5433/nexus_docintel_eval
    python scripts/evaluate_docintel.py --ci        # enforce eval/docintel/ci_thresholds.json (exit 1 on breach)

Needs Tesseract (eng + hrv): it is installed in the API Docker image and in CI. The database must be a
disposable local *_eval / *_test database; it is dropped and re-created.
Full run writes eval/docintel/reports/latest.json + docs/extraction-evaluation-results.md;
--ci writes eval/docintel/reports/ci.json (not committed).
"""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.docintel.ocr import ocr_available  # noqa: E402
from backend.evaluation import docintel_eval, docintel_report  # noqa: E402

REPORTS = ROOT / "eval" / "docintel" / "reports"
THRESHOLDS = ROOT / "eval" / "docintel" / "ci_thresholds.json"
DOC = ROOT / "docs" / "extraction-evaluation-results.md"


def gates(result: dict, thresholds: dict) -> list[str]:
    failures = []
    if result["security"]["total_violations"] != 0:
        failures.append(f"authorization violations: {result['security']['total_violations']} (must be 0)")
    if result["meta"]["uploads_ok"] != result["meta"]["documents"]:
        failures.append(f"only {result['meta']['uploads_ok']}/{result['meta']['documents']} documents processed")
    checks = {
        "field_f1_all": result["extraction"]["groups"]["all"]["f1"],
        "field_f1_test": result["extraction"]["groups"]["split:test"]["f1"],
        "evidence_support_rate": result["evidence"]["evidence_support_rate"],
        "evidence_reference_valid_rate": result["evidence"]["evidence_reference_valid_rate"],
        "line_item_cell_accuracy": result["tables"]["cell_association_accuracy"],
        "numeric_reconciliation_accuracy": result["validation"]["numeric_reconciliation_accuracy"],
        "classification_accuracy": result["classification"]["accuracy"],
        "ocr_key_token_recall": result["ocr"]["mean_key_token_recall"],
        "approval_consistency": result["review"]["approval_consistency"],
    }
    for key, minimum in thresholds["min"].items():
        if key not in checks:
            failures.append(f"unknown threshold {key}")
        elif checks[key] is None or checks[key] < minimum:
            failures.append(f"{key} {checks[key]} < {minimum}")
    for key, maximum in thresholds["max"].items():
        value = {"ocr_mean_cer": result["ocr"]["mean_cer"],
                 "validation_false_accept_rate": result["review"]["validation_false_accept_rate"]}[key]
        if value is None or value > maximum:
            failures.append(f"{key} {value} > {maximum}")
    if result["review"]["optimistic_lock_conflict_detected"] is not True:
        failures.append("stale concurrent review edit was not refused")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database-url", default=os.getenv("DOCINTEL_EVAL_DATABASE_URL", ""))
    parser.add_argument("--ci", action="store_true")
    args = parser.parse_args()
    if not ocr_available():
        print("Tesseract (eng+hrv) is required: run inside the API Docker image or install tesseract-ocr")
        return 2
    result = docintel_eval.run(args.database_url, ci=args.ci)
    REPORTS.mkdir(parents=True, exist_ok=True)
    name = "ci" if args.ci else "latest"
    (REPORTS / f"{name}.json").write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str) + "\n",
                                          encoding="utf-8")
    markdown = docintel_report.render(result)
    if not args.ci:
        DOC.write_text(markdown, encoding="utf-8")
    print(markdown)
    if args.ci:
        failures = gates(result, json.loads(THRESHOLDS.read_text(encoding="utf-8")))
        if failures:
            print("DOCINTEL CI GATE FAILED:\n  " + "\n  ".join(failures))
            return 1
        print("docintel CI gates passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
