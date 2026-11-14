"""End-to-end /api/ask on PostgreSQL + pgvector with the REAL demo documents and the REAL model.

The Croatian Atlas notice question was the documented Phase 2c false abstention (chunk cosine
0.81 < 0.82). It is tested here through the general, dev-calibrated evidence policy and the
query-aware extractor: nothing in the code knows about this question.
"""
import re
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app import main, rag
from backend.app.embeddings import get_embedder
from backend.app.extractive import display
from backend.app.indexing import embed_pending_chunks
from backend.app.main import app
from backend.app.models import Chunk, Document, DocumentGrant
from backend.app.parsers import split_page

from .conftest import make_client, seed

pytestmark = [pytest.mark.postgres, pytest.mark.model]

ROOT = Path(__file__).resolve().parents[2]
TABLES = "chunk_embeddings, audit_events, chunks, document_grants, documents, users"
QUESTION = "Koliko dana unaprijed moramo najaviti raskid ugovora s dobavljačem?"
PEOPLE = [
    ("finance@northstar.demo", "northstar", "member", ["finance", "legal"]),
    ("hr@northstar.demo", "northstar", "member", ["hr"]),
    ("finance@otherco.demo", "otherco", "member", ["finance"]),
]
# (doc id, tenant, owner index, file, groups) -- the same grants as scripts/seed_demo.py
DEMO = [
    ("atlas", "northstar", 0, "atlas_supplier_agreement.txt", ["finance", "legal"]),
    ("cygnus", "northstar", 0, "cygnus_cloud_agreement.txt", ["finance", "legal"]),
    ("comp", "northstar", 1, "employee_compensation_policy.txt", ["hr"]),
    ("travel", "northstar", 0, "pravilnik_putni_troskovi.txt", ["finance", "hr"]),
    ("budget", "otherco", 2, "otherco_confidential_budget.txt", ["finance"]),
]
QUOTE = re.compile(r"^\[(S\d+)\] .*?, page \d+: (.+)$", re.M)


@pytest.fixture
def demo(pg_engine, embedder, monkeypatch):
    with pg_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    factory = sessionmaker(pg_engine, expire_on_commit=False)
    seed(factory, docs=[], people=PEOPLE)
    contents = {}
    with factory() as db:
        for doc_id, tenant, owner, filename, groups in DEMO:
            body = (ROOT / "demo_documents" / filename).read_text(encoding="utf-8")
            db.add(Document(id=doc_id, tenant_id=tenant, owner_id=f"u{owner}", title=filename,
                            filename=filename, storage_key=filename, sha256="0" * 64,
                            page_count=1, extracted_fields={}))
            db.flush()
            db.add_all(DocumentGrant(document_id=doc_id, group_name=g) for g in groups)
            for pos, piece in enumerate(split_page(body)):  # the production chunker
                db.add(Chunk(id=f"{doc_id}_c{pos}", document_id=doc_id, page_number=1,
                             position=pos, content=piece))
                contents[f"{doc_id}_c{pos}"] = piece
        db.commit()
        embed_pending_chunks(db, embedder)
        db.commit()
    monkeypatch.setattr(rag, "settings", replace(rag.settings, openai_api_key=""))  # offline mode
    client, token = make_client(factory)
    app.dependency_overrides[get_embedder] = lambda: embedder
    yield client, token, contents
    app.dependency_overrides.clear()


def ask(client, headers, **body):
    response = client.post("/api/ask", headers=headers, json={"question": QUESTION, "top_k": 5, **body})
    assert response.status_code == 200, response.text
    return response.json()


def test_croatian_atlas_notice_question_is_answered_with_a_verbatim_cited_90_days(demo):
    client, token, contents = demo
    assert main.EVIDENCE_POLICY.min_sentence_similarity is not None  # the shipped default policy
    body = ask(client, token("finance@northstar.demo"), mode="hybrid")

    evidence = body["diagnostics"]["evidence"]
    assert evidence["applied"] and evidence["sufficient"], evidence
    assert evidence["reasons"] == ["sentence_similarity_above_threshold"]
    assert evidence["signals"]["sentence_top_similarity"] >= main.EVIDENCE_POLICY.min_sentence_similarity
    assert body["mode"] == "extractive"

    sources = {s["id"]: s for s in body["sources"]}
    quotes = QUOTE.findall(body["answer"])
    assert quotes, body["answer"]
    for sid, quote in quotes:  # every quote is one of the cited chunk's selected sentences
        assert sid in sources
        selected = sources[sid]["selected_sentences"]
        assert all(s in contents[sources[sid]["chunk_id"]] for s in selected)  # exact substrings
        assert quote in [display(s) for s in selected]  # rendered on one line, words unchanged
    supporting = [(sid, q) for sid, q in quotes if "90 days" in q]
    assert supporting, body["answer"]
    assert all(sources[sid]["document_id"] == "atlas" for sid, _ in supporting)
    # a quoted sentence, not the whole document
    assert all(len(q) < len(contents[sources[sid]["chunk_id"]]) for sid, q in quotes)


def test_same_question_without_access_never_reveals_the_atlas_clause(demo):
    client, token, _ = demo
    for persona in ("hr@northstar.demo", "finance@otherco.demo"):
        body = ask(client, token(persona), mode="hybrid")
        assert all(s["document_id"] != "atlas" for s in body["sources"])
        assert "90 days before" not in body["answer"]
        for s in body["sources"]:
            assert not any("90 days before" in q for q in s["selected_sentences"])


def test_insufficient_evidence_still_abstains_end_to_end(demo):
    client, token, _ = demo
    body = ask(client, token("finance@northstar.demo"), mode="hybrid",
               question="What is the parking policy for visitors at the Graz plant?")
    assert body["mode"] == "no_evidence" and body["sources"] == []
    assert body["diagnostics"]["evidence"]["sufficient"] is False
