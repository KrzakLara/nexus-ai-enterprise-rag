"""The complete MVP workflow through the real API, on SQLite and on PostgreSQL (scenario fixture):

    login -> upload (PDF) -> parse -> chunk -> (embed on PostgreSQL) -> retrieve -> answer
          -> citation -> source inspection -> original download -> audit trail

plus the permission boundaries of the same document for other users and tenants.
"""
import inspect
import re

from backend.app import main

from .fixtures import KESTREL_FACT, KESTREL_PAGES, make_pdf

QUESTION = "What rebate does agreement KR-2026-77 grant?"


def test_complete_mvp_workflow(scenario):
    client, token, factory = scenario
    postgres = factory.kw["bind"].dialect.name == "postgresql"
    finance = token("finance@northstar.demo")  # login
    assert client.get("/api/me", headers=finance).json()["groups"] == ["finance"]

    pdf = make_pdf(KESTREL_PAGES)
    uploaded = client.post("/api/documents", headers=finance,
                           files={"file": ("kestrel_agreement.pdf", pdf, "application/pdf")},
                           data={"groups": "finance"})
    assert uploaded.status_code == 201, uploaded.text
    doc_id = uploaded.json()["id"]
    listed = {d["id"]: d for d in client.get("/api/documents", headers=finance).json()}
    assert listed[doc_id]["pages"] == 3 and listed[doc_id]["groups"] == ["finance"]
    assert listed[doc_id]["fields"]["counterparty"]["value"] == "Kestrel Freight d.o.o."

    # mode omitted: the deployment default (hybrid on PostgreSQL, bm25 on SQLite)
    body = client.post("/api/ask", headers=finance, json={"question": QUESTION}).json()
    assert body["retrieval"] == ("hybrid" if postgres else "bm25")
    assert body["mode"] == "extractive", body
    if postgres:
        assert body["diagnostics"]["evidence"]["sufficient"] is True
        assert body["diagnostics"]["evidence"]["reasons"] == ["identifier_match"]

    # every [S#] in the answer resolves to a returned source; the fact is quoted from page 3
    cited = set(re.findall(r"\[S(\d+)\]", body["answer"]))
    assert cited and all(1 <= int(i) <= len(body["sources"]) for i in cited)
    top = body["sources"][0]
    assert top["id"] == "S1" and top["document_id"] == doc_id and top["page"] == 3
    assert f"[S1] kestrel_agreement, page 3: {KESTREL_FACT}" in body["answer"]
    assert KESTREL_FACT in top["selected_sentences"]
    assert all(s in top["excerpt"] for s in top["selected_sentences"])  # verbatim

    # source inspection: the cited page holds the quoted text; the original file is unchanged
    detail = client.get(f"/api/documents/{doc_id}", headers=finance).json()
    page3 = " ".join(p["content"] for p in detail["pages"] if p["page"] == 3)
    assert detail["page_count"] == 3 and KESTREL_FACT in page3
    original = client.get(f"/api/documents/{doc_id}/source", headers=finance)
    assert original.status_code == 200 and original.content == pdf

    # the same document is invisible to another group and to another tenant
    for other in ("hr@northstar.demo", "finance@otherco.demo"):
        headers = token(other)
        assert client.get(f"/api/documents/{doc_id}", headers=headers).status_code == 404
        assert client.get(f"/api/documents/{doc_id}/source", headers=headers).status_code == 404
        denied = client.post("/api/ask", headers=headers, json={"question": QUESTION}).json()
        assert doc_id not in {s["document_id"] for s in denied["sources"]}
        assert "7 percent" not in denied["answer"] and denied["mode"] == "no_evidence"

    # audit trail (tenant admin): upload, view, download and ask were recorded
    events = client.get("/api/audit", headers=token("admin@northstar.demo")).json()
    actions = {(e["action"], e["document_id"]) for e in events}
    assert {("upload", doc_id), ("document_view", doc_id), ("source_download", doc_id),
            ("ask", None)} <= actions
    assert not any(e["document_id"] == doc_id and e["action"] == "document_view"
                   and e["user_id"] != "u1" for e in events)  # denied views are not logged as views


def test_upload_route_runs_in_the_threadpool_not_on_the_event_loop():
    # FastAPI executes plain `def` routes in a worker thread; an `async def` route doing parsing and
    # CPU-bound embedding would block every other request while it runs.
    assert not inspect.iscoroutinefunction(main.upload)
    assert not inspect.iscoroutinefunction(main.ask)


def test_default_retrieval_mode_matches_the_database():
    assert main.default_retrieval_mode("postgresql") == "hybrid"
    assert main.default_retrieval_mode("sqlite") == "bm25"


def test_model_warm_up_reports_success_and_failure(monkeypatch):
    from backend.app.embeddings import EmbeddingError

    class Ok:
        def embed_query(self, text):
            self.text = text

    class Broken:
        def embed_query(self, text):
            raise EmbeddingError("model files missing")

    monkeypatch.setattr(main, "get_embedder", Ok)
    assert main.warm_up_embedder() == "loaded"
    monkeypatch.setattr(main, "get_embedder", Broken)
    assert main.warm_up_embedder() == "failed: model files missing"  # logged, never fatal
