"""Multi-document intelligence: comparison, structured queries, question routing and the security
of every cross-document operation (SQLite and PostgreSQL)."""
import json
from pathlib import Path

import pytest
from sqlalchemy import delete

from backend.app.docintel.compare import interpret
from backend.app.models import DocumentGrant

CORPUS = Path(__file__).resolve().parents[2] / "eval" / "docintel"
LABELS = {r["id"]: r for r in map(json.loads, (CORPUS / "labels.jsonl").read_text(encoding="utf-8").splitlines())}


@pytest.fixture
def docs(scenario):
    client, token, factory = scenario
    fin = token("finance@northstar.demo")
    ids = {}
    for lid in ("di-c01", "di-c09", "di-c03", "di-i01", "di-i10", "di-i04"):
        path = CORPUS / LABELS[lid]["file"]
        r = client.post("/api/documents", headers=fin, files={"file": (path.name, path.read_bytes(), "x")},
                        data={"groups": "finance"})
        ids[lid] = r.json()["id"]
    path = CORPUS / LABELS["di-c15"]["file"]  # another tenant's contract
    r = client.post("/api/documents", headers=token("finance@otherco.demo"),
                    files={"file": (path.name, path.read_bytes(), "x")}, data={"groups": "finance"})
    ids["di-c15"] = r.json()["id"]
    return client, token, factory, ids


def test_compare_two_contracts_with_citations(docs):
    client, token, _, ids = docs
    r = client.post("/api/intelligence/compare", headers=token("finance@northstar.demo"),
                    json={"document_ids": [ids["di-c01"], ids["di-c03"]], "fields": ["payment_terms", "notice_period",
                                                                                        "expiration_date", "obligations"]})
    assert r.status_code == 200
    by = {f["field"]: f for f in r.json()["fields"]}
    pay = {d["document_id"]: d["values"][0]["value"] for d in by["payment_terms"]["documents"]}
    assert pay == {ids["di-c01"]: "P45D", ids["di-c03"]: "P30D"}
    assert by["payment_terms"]["analysis"]["differs"] is True
    assert by["payment_terms"]["analysis"]["lowest"] == {"document_id": ids["di-c03"], "value": "P30D"}
    # P90D vs P3M are compared in days (90 vs ~90): the ordering is deterministic, not an LLM opinion
    assert by["expiration_date"]["analysis"]["highest"]["document_id"] == ids["di-c03"]
    for f in by.values():
        for d in f["documents"]:
            for v in d["values"]:
                assert v["evidence"]["page"] >= 1 and v["evidence"]["text"]


def test_equivalent_jurisdictions_and_different_value_periods(scenario):
    client, token, _ = scenario
    fin = token("finance@northstar.demo")
    ids = []
    for lid in ("di-c01", "di-c02"):  # English 'Republic of Croatia' / Croatian 'Republike Hrvatske'
        path = CORPUS / LABELS[lid]["file"]
        ids.append(client.post("/api/documents", headers=fin, files={"file": (path.name, path.read_bytes(), "x")},
                               data={"groups": "finance"}).json()["id"])
    by = {f["field"]: f for f in client.post("/api/intelligence/compare", headers=fin,
                                              json={"document_ids": ids}).json()["fields"]}
    assert by["governing_law"]["analysis"]["all_equal"] is True          # no false conflict
    value = by["contract_value"]["analysis"]                              # annual vs total
    assert value["comparable"] is False and "lowest" not in value and "highest" not in value
    assert sorted(value["value_periods"].values()) == ["annual", "total"]


def test_structured_queries(docs):
    client, token, _, ids = docs
    h = token("finance@northstar.demo")
    q = lambda **b: client.post("/api/intelligence/query", headers=h, json=b).json()  # noqa: E731
    exp = q(type="expiring_within", days=90, as_of="2026-12-01")
    assert [m["document_id"] for m in exp["matches"]] == [ids["di-c01"]] and exp["matches"][0]["days_left"] == 44
    assert exp["window_end"] == "2027-03-01"
    renew = {m["document_id"] for m in q(type="auto_renewal")["matches"]}
    assert renew == {ids["di-c01"], ids["di-c09"], ids["di-c03"]}
    invalid = q(type="invalid_invoices")
    assert [m["document_id"] for m in invalid["matches"]] == [ids["di-i04"]]
    assert invalid["matches"][0]["issues"][0]["code"] == "subtotal_mismatch"
    conflicts = q(type="conflicts")["matches"]
    kinds = {(c["kind"], c["field"]) for c in conflicts}
    assert ("same_contract_id", "notice_period") in kinds and ("same_contract_id", "expiration_date") in kinds
    assert ("same_supplier", "iban") in kinds and ("same_supplier", "supplier_tax_id") not in kinds
    atlas = next(c for c in conflicts if c["field"] == "iban")
    assert {v["document_id"] for v in atlas["values"]} == {ids["di-i01"], ids["di-i10"]}


@pytest.mark.parametrize("question, intent, params", [
    ("Which supplier contracts expire within 90 days?", "expiring_within", {"days": 90}),
    ("Koji ugovori ističu u sljedećih 60 dana?", "expiring_within", {"days": 60}),
    ("Which agreements have automatic renewal clauses?", "auto_renewal", {}),
    ("Which invoices have totals that fail validation?", "invalid_invoices", {}),
    ("Which documents contain conflicting supplier information?", "conflicts", {}),
    ("Compare the payment terms of these two contracts.", "compare_payment_terms", {}),
    ("Usporedi uvjete plaćanja", "compare_payment_terms", {}),
])
def test_question_router(question, intent, params):
    assert interpret(question) == (intent, params)


def test_ask_endpoint_and_unsupported_questions(docs):
    client, token, _, ids = docs
    h = token("finance@northstar.demo")
    r = client.post("/api/intelligence/ask", headers=h, json={"question": "Which contracts expire within 90 days?",
                                                               "as_of": "2026-12-01"}).json()
    assert r["interpreted_as"] == "expiring_within" and "no LLM" in r["method"]
    assert client.post("/api/intelligence/ask", headers=h,
                       json={"question": "What is the meaning of life?"}).status_code == 422
    assert client.post("/api/intelligence/ask", headers=h,
                       json={"question": "Compare payment terms"}).status_code == 422  # no documents selected


def test_cross_document_security(docs):
    client, token, factory, ids = docs
    fin, hr = token("finance@northstar.demo"), token("hr@northstar.demo")
    mine, foreign, missing = ids["di-c01"], ids["di-c15"], "00000000-0000-0000-0000-000000000000"
    responses = []
    for other in (foreign, missing, "d_hr", "d_other"):  # other tenant, non-existent, other group, other tenant
        r = client.post("/api/intelligence/compare", headers=fin, json={"document_ids": [mine, other]})
        assert r.status_code == 404
        responses.append(r.json())
        assert other not in r.text and "Otherco" not in r.text
    assert all(x == responses[0] for x in responses)  # forbidden == non-existent: nothing to infer
    # a same-tenant user without the group sees none of these documents in any query
    for qtype in ("expiring_within", "auto_renewal", "invalid_invoices", "conflicts"):
        body = client.post("/api/intelligence/query", headers=hr, json={"type": qtype, "days": 3650,
                                                                         "as_of": "2026-01-01"}).text
        assert not any(i in body for i in ids.values())
    # Otherco's reviewer cannot pull Northstar documents in through document_ids
    oc = token("reviewer@otherco.demo")
    assert client.post("/api/intelligence/query", headers=oc,
                       json={"type": "auto_renewal", "document_ids": [mine]}).status_code == 404
    # the reviewer sees di-c09 only through the 'finance' grant: revoking it removes the document
    # from comparisons and queries immediately
    rev = token("reviewer@northstar.demo")
    pair = {"document_ids": [mine, ids["di-c09"]]}
    assert client.post("/api/intelligence/compare", headers=rev, json=pair).status_code == 200
    with factory() as db:
        db.execute(delete(DocumentGrant).where(DocumentGrant.document_id == ids["di-c09"]))
        db.commit()
    assert client.post("/api/intelligence/compare", headers=rev, json=pair).status_code == 404
    conflicts = client.post("/api/intelligence/query", headers=rev, json={"type": "conflicts"}).json()["matches"]
    assert not any(c["kind"] == "same_contract_id" for c in conflicts)
    # ...while the uploader (owner rule) and the tenant admin still see it
    for keeps in (fin, token("admin@northstar.demo")):
        assert client.post("/api/intelligence/compare", headers=keeps, json=pair).status_code == 200


def test_rejected_extractions_are_excluded(docs):
    client, token, _, ids = docs
    rev = token("reviewer@northstar.demo")
    ext = client.get(f"/api/documents/{ids['di-i04']}/extraction", headers=rev).json()
    client.post(f"/api/extractions/{ext['id']}/reject", headers=rev,
                json={"expected_version": ext["version"], "comment": "invalid totals"})
    q = client.post("/api/intelligence/query", headers=rev, json={"type": "invalid_invoices"}).json()
    assert q["matches"] == []
    cmp = client.post("/api/intelligence/compare", headers=rev,
                      json={"document_ids": [ids["di-i01"], ids["di-i04"]]}).json()
    assert [d["document_id"] for d in cmp["excluded_rejected"]] == [ids["di-i04"]]
    assert [d["document_id"] for d in cmp["documents"]] == [ids["di-i01"]]
