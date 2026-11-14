"""Document-intelligence API: processing, persistence, human review lifecycle, authorization and
audit, on SQLite and PostgreSQL (scenario fixture), with real parsing of corpus files."""
import json
from pathlib import Path

from sqlalchemy import func, select

from backend.app.models import AuditEvent, Extraction, ExtractionField, ReviewEvent

CORPUS = Path(__file__).resolve().parents[2] / "eval" / "docintel"
LABELS = {r["id"]: r for r in map(json.loads, (CORPUS / "labels.jsonl").read_text(encoding="utf-8").splitlines())}


def upload(client, headers, doc_id, groups="finance"):
    path = CORPUS / LABELS[doc_id]["file"]
    r = client.post("/api/documents", headers=headers, files={"file": (path.name, path.read_bytes(), "x")},
                    data={"groups": groups})
    assert r.status_code == 201, r.text
    return r.json()


def test_upload_parses_classifies_extracts_and_persists(scenario):
    client, token, factory = scenario
    fin = token("finance@northstar.demo")
    body = upload(client, fin, "di-i01")
    assert body["processing"]["processing_status"] == "processed" and body["processing"]["doc_type"] == "invoice"
    doc_id = body["id"]
    st = client.get(f"/api/documents/{doc_id}/processing", headers=fin).json()
    assert st["format"] == "pdf" and st["classification"]["type"] == "invoice" and st["attempts"] == 1
    structure = client.get(f"/api/documents/{doc_id}/structure", headers=fin).json()
    [table] = [b["table"] for p in structure["pages"] for b in p["blocks"] if b["table"]]
    assert table["n_cols"] == 5 and table["source"] == "pdf_ruling"
    ext = client.get(f"/api/documents/{doc_id}/extraction", headers=fin).json()
    fields = {f["name"]: f for f in ext["fields"]}
    assert fields["total_amount"]["value"] == LABELS["di-i01"]["fields"]["total_amount"]
    ev = fields["line_items[1].quantity"]["evidence"]
    assert ev["page"] == 1 and ev["table"]["col"] == 1 and ev["bbox"] is not None
    assert ext["status"] == "pending" and ext["can_review"] is False and "no field carries a probability" in ext["note"]
    # table rows are also searchable by the RAG pipeline (BM25 here), with the page citation
    answer = client.post("/api/ask", headers=fin, json={"question": "Warehouse handling", "mode": "bm25"}).json()
    assert answer["sources"][0]["document_id"] == doc_id and "Warehouse handling" in answer["sources"][0]["excerpt"]
    library = {d["id"]: d for d in client.get("/api/intelligence/documents", headers=fin).json()}
    assert library[doc_id]["doc_type"] == "invoice" and library[doc_id]["review_status"] == "awaiting_review"
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(ExtractionField)
                         .where(ExtractionField.extraction_id == ext["id"])) == len(ext["fields"])


def test_review_lifecycle_with_versions_corrections_and_audit(scenario):
    client, token, factory = scenario
    fin, rev = token("finance@northstar.demo"), token("reviewer@northstar.demo")
    doc_id = upload(client, fin, "di-i04")["id"]  # invalid: subtotal != sum of lines
    ext = client.get(f"/api/documents/{doc_id}/extraction", headers=rev).json()
    eid, v = ext["id"], ext["version"]
    assert ext["status"] == "needs_review" and ext["can_review"] is True
    sub = next(f for f in ext["fields"] if f["name"] == "subtotal")
    assert sub["state"] == "invalid" and sub["issues"][0]["code"] == "subtotal_mismatch"

    # a member may read but not review
    assert client.patch(f"/api/extractions/{eid}/fields", headers=fin, json={
        "expected_version": v, "corrections": [{"name": "currency", "value": "EUR"}]}).status_code == 403
    # stale version -> 409 with the current version
    stale = client.patch(f"/api/extractions/{eid}/fields", headers=rev, json={
        "expected_version": v - 1, "corrections": [{"name": "currency", "value": "EUR"}]})
    assert stale.status_code == 409 and stale.json()["detail"]["current_version"] == v
    # invalid values / evidence are refused
    assert client.patch(f"/api/extractions/{eid}/fields", headers=rev, json={
        "expected_version": v, "corrections": [{"name": "due_date", "value": "not a date"}]}).status_code == 422
    assert client.patch(f"/api/extractions/{eid}/fields", headers=rev, json={
        "expected_version": v, "corrections": [{"name": "due_date", "value": "2026-06-30",
                                                "evidence_quote": "text that is nowhere"}]}).status_code == 422
    assert client.patch(f"/api/extractions/{eid}/fields", headers=rev, json={
        "expected_version": v, "corrections": [{"name": "ceo_bonus", "value": "1"}]}).status_code == 422
    # approval is blocked by the validation error
    blocked = client.post(f"/api/extractions/{eid}/approve", headers=rev, json={"expected_version": v})
    assert blocked.status_code == 409 and blocked.json()["detail"]["errors"][0]["code"] == "subtotal_mismatch"
    # a human-written correction is normalized; the original value and evidence are preserved
    fixed = client.patch(f"/api/extractions/{eid}/fields", headers=rev, json={
        "expected_version": v, "corrections": [{"name": "subtotal", "value": "2.080,00 EUR",
                                                "comment": "sum of the line amounts"}]}).json()
    f = next(x for x in fixed["fields"] if x["name"] == "subtotal")  # 10 x 120 + 16 x 55 = 2080
    assert f["value"] == "2080.00" and f["corrected"] is True and f["extracted_value"] == "2180.00"
    assert f["extracted_evidence"]["text"].startswith("Osnovica") and f["grounding"] == "reviewer_asserted"
    assert fixed["version"] == v + 1
    # total = printed subtotal + VAT no longer adds up -> still an error; reviewer rejects instead
    reject_no_reason = client.post(f"/api/extractions/{eid}/reject", headers=rev,
                                   json={"expected_version": fixed["version"]})
    assert reject_no_reason.status_code == 422
    rejected = client.post(f"/api/extractions/{eid}/reject", headers=rev, json={
        "expected_version": fixed["version"], "comment": "printed subtotal is wrong; ask supplier for a new invoice"}).json()
    assert rejected["status"] == "rejected" and rejected["reviewed_by"] == "u4"
    # final: no more edits, approvals or silent re-processing
    assert client.patch(f"/api/extractions/{eid}/fields", headers=rev, json={
        "expected_version": rejected["version"], "corrections": [{"name": "currency", "value": "EUR"}]}).status_code == 409
    assert client.post(f"/api/documents/{doc_id}/process", headers=rev, json={}).status_code == 409
    history = client.get(f"/api/extractions/{eid}/history", headers=fin).json()["events"]
    assert [e["action"] for e in history] == ["process", "correct", "reject"]
    assert history[1]["field"] == "subtotal" and history[1]["old_value"] == "2180.00" and history[1]["user_id"] == "u4"
    with factory() as db:
        actions = set(db.scalars(select(AuditEvent.action).where(AuditEvent.document_id == doc_id)))
    assert {"upload", "extraction_process", "extraction_correct", "extraction_reject"} <= actions
    # forced re-processing is explicit and recorded; it starts a fresh review
    again = client.post(f"/api/documents/{doc_id}/process", headers=rev, json={"force": True}).json()
    assert again["status"] == "needs_review" and again["id"] == eid
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Extraction).where(Extraction.document_id == doc_id)) == 1
        last = db.scalars(select(ReviewEvent).where(ReviewEvent.extraction_id == eid).order_by(ReviewEvent.id.desc())).first()
    assert last.action == "reprocess" and "forced from rejected" in last.comment


def test_approval_of_a_valid_invoice_and_override_audit(scenario):
    client, token, _ = scenario
    rev = token("reviewer@northstar.demo")
    doc_id = upload(client, token("finance@northstar.demo"), "di-i01")["id"]
    ext = client.get(f"/api/documents/{doc_id}/extraction", headers=rev).json()
    ok = client.post(f"/api/extractions/{ext['id']}/approve", headers=rev,
                     json={"expected_version": ext["version"], "comment": "checked"}).json()
    assert ok["status"] == "approved" and ok["review_comment"] == "checked"
    assert client.post(f"/api/extractions/{ext['id']}/approve", headers=rev,
                       json={"expected_version": ok["version"]}).status_code == 409   # final
    # an invalid one can only be approved with an explicit, recorded override
    bad_id = upload(client, token("finance@northstar.demo"), "di-i04")["id"]
    bad = client.get(f"/api/documents/{bad_id}/extraction", headers=rev).json()
    over = client.post(f"/api/extractions/{bad['id']}/approve", headers=rev, json={
        "expected_version": bad["version"], "override_reason": "accepted by finance director, ticket 4711"}).json()
    assert over["status"] == "approved"
    hist = client.get(f"/api/extractions/{bad['id']}/history", headers=rev).json()["events"]
    assert "OVERRIDE (1 errors): accepted by finance director" in hist[-1]["comment"]


def test_review_api_enforces_document_acl(scenario):
    client, token, _ = scenario
    fin = token("finance@northstar.demo")
    doc_id = upload(client, fin, "di-i01")["id"]
    eid = client.get(f"/api/documents/{doc_id}/extraction", headers=fin).json()["id"]
    for outsider in ("hr@northstar.demo", "finance@otherco.demo", "reviewer@otherco.demo"):
        h = token(outsider)
        for path in (f"/api/documents/{doc_id}/extraction", f"/api/documents/{doc_id}/structure",
                     f"/api/documents/{doc_id}/processing", f"/api/extractions/{eid}/history",
                     f"/api/extractions/{eid}/fields/total_amount/evidence"):
            assert client.get(path, headers=h).status_code == 404, (outsider, path)
        for path, body in ((f"/api/extractions/{eid}/approve", {"expected_version": 1}),
                           (f"/api/extractions/{eid}/reject", {"expected_version": 1, "comment": "nope"}),
                           (f"/api/extractions/{eid}/validate", None),
                           (f"/api/documents/{doc_id}/process", {})):
            assert client.post(path, headers=h, json=body).status_code == 404, (outsider, path)
        assert client.patch(f"/api/extractions/{eid}/fields", headers=h, json={
            "expected_version": 1, "corrections": [{"name": "currency", "value": "EUR"}]}).status_code == 404
        assert doc_id not in client.get("/api/intelligence/documents", headers=h).text
    # a member of the right group who is not the owner cannot re-process
    assert client.post(f"/api/documents/{doc_id}/process", headers=token("admin@northstar.demo"),
                       json={}).status_code == 200


def test_field_evidence_endpoint_returns_the_page_context(scenario):
    client, token, _ = scenario
    fin = token("finance@northstar.demo")
    doc_id = upload(client, fin, "di-c03")["id"]  # DOCX contract
    eid = client.get(f"/api/documents/{doc_id}/extraction", headers=fin).json()["id"]
    ev = client.get(f"/api/extractions/{eid}/fields/notice_period/evidence", headers=fin).json()
    assert ev["value"] == "P3M" and ev["evidence"]["text"] == "Notice period: 3 months"
    assert ev["evidence"]["bbox"] is None and ev["evidence"]["text"] in ev["page_text"]  # DOCX: no geometry


def test_unknown_document_cannot_be_approved_but_can_be_typed_explicitly(scenario):
    client, token, _ = scenario
    rev = token("reviewer@northstar.demo")
    doc_id = upload(client, token("finance@northstar.demo"), "di-o01")["id"]
    ext = client.get(f"/api/documents/{doc_id}/extraction", headers=rev).json()
    assert ext["doc_type"] == "unknown" and ext["fields"] == [] and ext["issues"][0]["code"] == "document_type_unknown"
    assert client.post(f"/api/extractions/{ext['id']}/approve", headers=rev,
                       json={"expected_version": ext["version"]}).status_code == 409
    typed = client.post(f"/api/documents/{doc_id}/process", headers=rev, json={"doc_type": "contract"}).json()
    assert typed["doc_type"] == "contract" and typed["status"] == "needs_review"   # required fields missing
