"""Classification, offline rules extraction, schemas, validation and evidence grounding.

The extraction regression test runs the real parser + rules on the DEV-split corpus documents that
need no OCR and compares against the frozen ground truth (test-split documents are only measured by
scripts/evaluate_docintel.py, so the rules are not tuned on them)."""
import json
from decimal import Decimal
from pathlib import Path

import pydantic
import pytest

from backend.app.docintel import grounding, rules
from backend.app.docintel.classify import classify
from backend.app.docintel.parsing import parse_document
from backend.app.docintel.schemas import Evidence, ExtractedField, TableRef, typed
from backend.app.docintel.validation import validate
from backend.evaluation.docintel_eval import ITEM_COLS, SCALAR
from backend.evaluation.docintel_metrics import equal

CORPUS = Path(__file__).resolve().parents[2] / "eval" / "docintel"
LABELS = {r["id"]: r for r in map(json.loads, (CORPUS / "labels.jsonl").read_text(encoding="utf-8").splitlines())}
DEV_TEXT = [k for k, v in LABELS.items() if v["split"] == "dev" and not v["scanned_pages"]]


def parsed(doc_id):
    path = CORPUS / LABELS[doc_id]["file"]
    return parse_document(path.name, path.read_bytes())


@pytest.mark.parametrize("doc_id", DEV_TEXT)
def test_classification_and_rules_extraction_match_ground_truth(doc_id):
    lab = LABELS[doc_id]
    doc = parsed(doc_id)
    result = classify(doc)
    assert result["type"] == lab["doc_type"], result
    fields = rules.extract(doc, doc_id, lab["doc_type"])
    if lab["doc_type"] == "unknown":
        assert fields == []
        return
    pred = {f.name: f.value for f in fields}
    for name, vtype in SCALAR[lab["doc_type"]].items():
        assert equal(vtype, pred.get(name), lab["fields"][name]), (name, pred.get(name), lab["fields"][name])
    assert sorted(v for k, v in pred.items() if k.startswith("parties[")) == sorted(lab["parties"])
    for i, item in enumerate(lab["line_items"]):
        for col, vtype in ITEM_COLS.items():
            assert equal(vtype, pred.get(f"line_items[{i}].{col}"), item[col]), (i, col)
    for f in fields:  # every value is grounded in its own document
        g, problems = grounding.ground(f, doc, doc_id)
        assert problems == [] and g in ("exact", "normalized"), (f.name, f.value, problems)
    typed(lab["doc_type"], fields)  # the typed Pydantic schema accepts the output


def test_unknown_documents_are_not_forced_into_a_class():
    for doc_id in ("di-o01", "di-o02"):
        result = classify(parsed(doc_id))
        assert result["type"] == "unknown" and result["decision"] in ("insufficient_evidence", "ambiguous")
        assert "not probabilities" in result["note"]


def test_conflicting_values_inside_one_document_are_flagged_not_resolved():
    doc = parsed("di-c10")  # two different 'End date' lines
    [exp] = [f for f in rules.extract(doc, "di-c10", "contract") if f.name == "expiration_date"]
    assert exp.value == "2028-06-30"
    assert [i.code for i in exp.issues] == ["conflicting_values_in_document"] and exp.issues[0].actual == "2029-06-30"


def test_jurisdiction_is_canonical_and_the_wording_stays_as_evidence():
    en = {f.name: f for f in rules.extract(parsed("di-c01"), "di-c01", "contract")}["governing_law"]
    hr = {f.name: f for f in rules.extract(parsed("di-c02"), "di-c02", "contract")}["governing_law"]
    assert en.value == hr.value == "Croatia"
    assert "Republic of Croatia" in en.evidence.text and "Republike Hrvatske" in hr.evidence.text
    assert grounding.ground(en, parsed("di-c01"), "di-c01") == ("exact", [])      # 'Croatia' is in the text
    assert grounding.ground(hr, parsed("di-c02"), "di-c02") == ("normalized", [])  # via the alias table


@pytest.mark.parametrize("doc_id, period", [("di-c01", "annual"), ("di-c02", "total")])
def test_value_period_comes_from_the_value_label(doc_id, period):
    f = {x.name: x for x in rules.extract(parsed(doc_id), doc_id, "contract")}
    assert f["value_period"].value == period and f["value_period"].evidence == f["contract_value"].evidence
    assert grounding.ground(f["value_period"], parsed(doc_id), doc_id)[0] in ("exact", "normalized")


def test_value_period_validation():
    base = {"contract_id": "C-1", "parties[0]": "A", "effective_date": "2026-01-01", "contract_value": "10.00",
            "currency": "EUR"}
    assert [i.code for i in validate("contract", base)] == ["value_period_missing"]
    assert codes(validate("contract", base | {"value_period": "weekly"})) == ["invalid_value_period"]
    assert validate("contract", base | {"value_period": "monthly"}) == []


def test_missing_fields_stay_missing():
    fields = {f.name: f.value for f in rules.extract(parsed("di-c04"), "di-c04", "contract")}
    for absent in ("contract_value", "currency", "payment_terms", "notice_period", "auto_renewal"):
        assert fields.get(absent) is None


# ------------------------------------------------------------------------------- schemas

def test_schema_rejects_non_canonical_values():
    with pytest.raises(pydantic.ValidationError):
        ExtractedField(name="invoice_date", value_type="date", value="31.03.2026.", method="t")
    with pytest.raises(pydantic.ValidationError):
        ExtractedField(name="total_amount", value_type="money", value="1.234,56", method="t")
    with pytest.raises(pydantic.ValidationError):
        ExtractedField(name="currency", value_type="currency", value="EURO", method="t")
    ok = ExtractedField(name="total_amount", value_type="money", value="1234.56", method="t")
    assert ok.grounding == "no_evidence"


def test_typed_invoice_keeps_line_items_together():
    f = lambda n, vt, v: ExtractedField(name=n, value_type=vt, value=v, method="t")  # noqa: E731
    inv = typed("invoice", [f("invoice_number", "text", "INV-1"), f("line_items[1].amount", "money", "20.00"),
                            f("line_items[0].amount", "money", "10.00"), f("line_items[0].quantity", "decimal", "2")])
    assert [i.amount.value for i in inv.line_items] == ["10.00", "20.00"]
    assert inv.line_items[1].quantity.value is None and inv.total_amount.value is None


# ---------------------------------------------------------------------------- validation

def invoice(**overrides):
    values = {"invoice_number": "I-1", "supplier_name": "S", "invoice_date": "2026-03-01", "due_date": "2026-03-31",
              "currency": "EUR", "line_items[0].quantity": "3", "line_items[0].unit_price": "10.00",
              "line_items[0].tax_rate": "25", "line_items[0].amount": "30.00", "line_items[1].quantity": "1",
              "line_items[1].unit_price": "20.00", "line_items[1].tax_rate": "5", "line_items[1].amount": "20.00",
              "subtotal": "50.00", "tax_amount": "8.50", "total_amount": "58.50", "iban": "HR1210010051863000160"}
    values.update(overrides)
    return values


def codes(issues):
    return sorted(i.code for i in issues if i.severity == "error")


def test_consistent_invoice_has_no_errors():
    assert codes(validate("invoice", invoice())) == []


@pytest.mark.parametrize("overrides, expected", [
    ({"line_items[0].amount": "31.00", "subtotal": "51.00", "tax_amount": "8.75", "total_amount": "59.75"},
     ["line_amount_mismatch"]),
    ({"subtotal": "60.00", "total_amount": "68.50"}, ["subtotal_mismatch"]),
    ({"tax_amount": "9.50", "total_amount": "59.50"}, ["tax_mismatch"]),
    ({"total_amount": "60.00"}, ["total_mismatch"]),
    ({"due_date": "2026-02-01"}, ["due_before_invoice_date"]),
    ({"iban": "HR1210010051863000161"}, ["iban_checksum"]),
    ({"invoice_number": None}, ["required_missing"]),
])
def test_invoice_rules_report_field_level_errors_without_correcting(overrides, expected):
    values = invoice(**overrides)
    before = dict(values)
    issues = validate("invoice", values)
    assert codes(issues) == expected
    assert values == before  # nothing was "fixed"
    mismatch = next(i for i in issues if i.code == expected[0])
    assert mismatch.field and mismatch.message


def test_tax_tolerance_allows_per_line_rounding_but_not_more():
    # 3 lines x 0.333 -> per-line rounding can differ by up to 1 cent per line
    v = {"invoice_number": "I", "supplier_name": "S", "invoice_date": "2026-01-01", "currency": "EUR",
         "total_amount": "3.99"}
    for i in range(3):
        v |= {f"line_items[{i}].amount": "1.11", f"line_items[{i}].tax_rate": "20", f"line_items[{i}].quantity": "1",
              f"line_items[{i}].unit_price": "1.11"}
    v |= {"subtotal": "3.33", "tax_amount": "0.66"}  # exact 0.666 -> 0.67; 0.66 is within 3 cents
    assert "tax_mismatch" not in codes(validate("invoice", v))
    v["tax_amount"] = "0.62"
    assert "tax_mismatch" in codes(validate("invoice", v))


def test_currency_inconsistency_uses_the_evidence_text():
    issues = validate("invoice", invoice(), {"total_amount": "Total due: 58.50 USD"})
    assert "currency_inconsistent" in codes(issues)


def test_contract_rules():
    base = {"contract_id": "C-1", "parties[0]": "A", "effective_date": "2026-01-01"}
    assert codes(validate("contract", base | {"expiration_date": "2025-12-31"})) == ["expiration_not_after_effective"]
    assert codes(validate("contract", base | {"contract_value": "-5.00", "currency": "EUR"})) == ["negative_amount"]
    warn = validate("contract", base | {"notice_period": "P1000D"})
    assert [i.code for i in warn] == ["implausible_notice_period"] and warn[0].severity == "warning"
    assert codes(validate("contract", {"contract_id": "C-1"})) == ["required_missing", "required_missing"]


# ------------------------------------------------------------------------------ grounding

def test_grounding_checks_document_page_text_and_value():
    doc = parsed("di-i01")
    total = LABELS["di-i01"]["fields"]["total_amount"]  # 8031.25, printed '8,031.25 EUR'
    ok = ExtractedField(name="total_amount", value_type="money", value=total, method="t",
                        evidence=Evidence(document_id="d", page=1, text="Total due | 8,031.25 EUR"))
    assert grounding.ground(ok, doc, "d") == ("normalized", [])
    cases = {
        "evidence_wrong_document": ok.model_copy(update={"evidence": ok.evidence.model_copy(update={"document_id": "x"})}),
        "evidence_invalid_page": ok.model_copy(update={"evidence": ok.evidence.model_copy(update={"page": 9})}),
        "evidence_not_in_document": ok.model_copy(update={"evidence": ok.evidence.model_copy(update={"text": "Total due | 1.00 EUR"})}),
        "value_not_supported": ok.model_copy(update={"value": "8031.26"}),
        "missing_evidence": ok.model_copy(update={"evidence": None}),
    }
    for code, field in cases.items():
        result, problems = grounding.ground(field, doc, "d")
        assert result in ("unsupported", "no_evidence") and [p.code for p in problems] == [code], code


def test_table_cell_reference_is_checked_against_the_cell():
    doc = parsed("di-i01")
    [table] = doc.tables()
    row = table.row_lines()[1]
    ev = Evidence(document_id="d", page=1, text=row.text, table=TableRef(table_id=table.id, row=1, col=1))
    good = ExtractedField(name="line_items[0].quantity", value_type="decimal", value="12", method="t", evidence=ev)
    assert grounding.ground(good, doc, "d")[0] == "normalized"
    # 450 is in the row (unit price) but NOT in the quantity cell -> unsupported
    wrong = good.model_copy(update={"value": "450"})
    assert grounding.ground(wrong, doc, "d")[0] == "unsupported"
    bad_ref = good.model_copy(update={"evidence": ev.model_copy(update={"table": TableRef(table_id="t9-9", row=1, col=1)})})
    assert grounding.ground(bad_ref, doc, "d")[1][0].code == "evidence_not_in_document"


def test_numbers_are_not_matched_as_substrings():
    assert grounding.supports("money", "30", "Due 2026-12-30") is None
    assert grounding.supports("money", "30", "within 30 days") == "normalized"
    assert grounding.supports("money", str(Decimal("1234.50")), "Total: 1.234,50 EUR") == "normalized"
