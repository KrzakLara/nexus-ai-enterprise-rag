"""PDF / DOCX / TXT parsing, chunking and page references, and upload -> retrieval integration.

Parser tests are pure Python. Upload tests use the `scenario` fixture, i.e. they run on SQLite and
again on PostgreSQL (with real embedding-on-upload) when TEST_DATABASE_URL is set.
"""
import re

import pytest
from sqlalchemy import select

from backend.app.models import Chunk, ChunkEmbedding
from backend.app.parsers import extract_contract_fields, extract_pages, split_page

from .fixtures import KESTREL_FACT, KESTREL_PAGES, LONG_CLAUSES, make_docx, make_pdf

# ------------------------------------------------------------------------------------- parsers


def test_pdf_text_layer_is_extracted_page_by_page():
    pages = extract_pages("agreement.pdf", make_pdf(KESTREL_PAGES))
    assert len(pages) == 3
    assert "Counterparty: Kestrel Freight d.o.o." in pages[0]
    assert all(clause in pages[1] for clause in LONG_CLAUSES)
    assert KESTREL_FACT in pages[2] and KESTREL_FACT not in pages[0] + pages[1]


def test_pdf_contract_fields_carry_the_correct_page():
    fields = extract_contract_fields(extract_pages("a.pdf", make_pdf(KESTREL_PAGES)))
    assert fields["expiration_date"]["value"] == "2028-06-30"
    assert fields["notice_period"] == {"value": "60 days", "page": 1, "evidence": "Notice period: 60 days",
                                       "method": "regex", "status": "needs_review"}


def test_pdf_with_blank_pages_keeps_page_numbering():
    pages = extract_pages("gap.pdf", make_pdf([["First page."], [], ["Third page text."]]))
    assert [p.strip() for p in pages] == ["First page.", "", "Third page text."]


def test_pdf_without_any_text_layer_is_rejected_as_needing_ocr():
    with pytest.raises(ValueError, match="No selectable text"):
        extract_pages("scan.pdf", make_pdf([[], []]))


def test_pdf_page_limit():
    with pytest.raises(ValueError, match="max 100 pages"):
        extract_pages("big.pdf", make_pdf([["x"]] * 101))


@pytest.mark.parametrize("filename, data", [
    ("broken.pdf", b"%PDF-1.4\nthis is not really a PDF"),
    ("broken.pdf", make_pdf(KESTREL_PAGES)[:200]),          # truncated download
    ("broken.docx", b"PK\x03\x04 truncated zip archive"),
    ("text.docx", b"plain text renamed to .docx"),
    ("latin1.txt", "Café notice period".encode("cp1252")),  # not UTF-8
])
def test_malformed_files_raise(filename, data):
    with pytest.raises(Exception):  # noqa: B017 - the API maps any parser exception to HTTP 422
        extract_pages(filename, data)


def test_docx_paragraphs_heading_and_table_rows_are_extracted():
    data = make_docx(["Notice period is 45 days.", "   ", "Payment within 30 days."],
                     table=[["Field", "Value"], ["Annual value", "EUR 12,500"]], heading="Atlas terms")
    [page] = extract_pages("terms.docx", data)  # DOCX has no fixed pages: one logical page
    assert page.splitlines() == ["Atlas terms", "Notice period is 45 days.", "Payment within 30 days.",
                                 "Field | Value", "Annual value | EUR 12,500"]


def test_empty_docx_is_rejected():
    with pytest.raises(ValueError, match="No selectable text"):
        extract_pages("empty.docx", make_docx([]))


def test_txt_and_md_with_utf8_bom_and_croatian_text():
    data = "﻿Naknada za korištenje privatnog automobila je 0,30 EUR/km.".encode("utf-8")
    assert extract_pages("putni.txt", data) == ["Naknada za korištenje privatnog automobila je 0,30 EUR/km."]
    assert extract_pages("notes.MD", b"# Title\nbody") == ["# Title\nbody"]


@pytest.mark.parametrize("filename", ["sheet.xlsx", "slides.pptx", "archive.zip", "noextension"])
def test_unsupported_formats_are_rejected(filename):
    with pytest.raises(ValueError, match="Supported"):
        extract_pages(filename, b"anything")


# ------------------------------------------------------------------------------------- chunking

def test_chunks_respect_size_overlap_and_lose_no_text():
    text = extract_pages("a.pdf", make_pdf(KESTREL_PAGES))[1]
    chunks = split_page(text)
    assert len(chunks) >= 3 and all(len(c) <= 1000 for c in chunks)
    for clause in LONG_CLAUSES:  # every clause survives intact in at least one chunk
        assert any(clause in c for c in chunks), clause
    for left, right in zip(chunks, chunks[1:]):  # consecutive chunks overlap
        tail = left[-60:]
        assert tail[:30] in right, (tail, right[:200])
    # a chunk END prefers a line / sentence boundary (the overlap START may fall mid-word)
    assert all(re.search(r"[.\n]$", c) for c in chunks[:-1]), [c[-20:] for c in chunks]


def test_short_page_is_a_single_chunk_and_whitespace_is_normalized():
    assert split_page("Notice\tperiod   is 45 days.") == ["Notice period is 45 days."]
    assert split_page("   \t ") == []


# --------------------------------------------------------------------- upload -> index -> retrieve

def _upload(client, headers, filename, data, groups="finance"):
    return client.post("/api/documents", headers=headers,
                       files={"file": (filename, data, "application/octet-stream")},
                       data={"groups": groups})


def test_pdf_upload_chunks_with_page_numbers_and_is_retrievable(scenario):
    client, token, factory = scenario
    headers = token("finance@northstar.demo")
    response = _upload(client, headers, "kestrel.pdf", make_pdf(KESTREL_PAGES))
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["pages"] == 3 and body["fields_extracted"] == 4
    postgres = factory.kw["bind"].dialect.name == "postgresql"
    with factory() as db:
        chunks = db.scalars(select(Chunk).where(Chunk.document_id == body["id"])
                            .order_by(Chunk.page_number, Chunk.position)).all()
        embedded = db.scalars(select(ChunkEmbedding.chunk_id)).all()
    assert sorted({c.page_number for c in chunks}) == [1, 2, 3]
    assert [c.position for c in chunks if c.page_number == 2] == list(range(len([
        c for c in chunks if c.page_number == 2])))
    assert len([c for c in chunks if c.page_number == 2]) >= 3  # long page was split
    [fact_chunk] = [c for c in chunks if KESTREL_FACT in c.content]
    assert fact_chunk.page_number == 3
    if postgres:  # embedding-on-upload: every chunk has a persisted vector
        assert body["embedding_status"] == f"embedded:{len(chunks)}"
        assert set(embedded) == {c.id for c in chunks}
    else:
        assert body["embedding_status"] == "skipped_requires_postgresql"

    answer = client.post("/api/ask", headers=headers, json={
        "question": "What rebate does agreement KR-2026-77 grant?", "mode": "bm25"}).json()
    top = answer["sources"][0]
    assert top["document_id"] == body["id"] and top["page"] == 3 and top["chunk_id"] == fact_chunk.id
    assert "[S1] kestrel, page 3:" in answer["answer"] and "7 percent" in answer["answer"]


def test_docx_upload_is_retrievable_with_table_content(scenario):
    client, token, _ = scenario
    headers = token("finance@northstar.demo")
    data = make_docx(["Orion maintenance contract."], table=[["Service", "Response time"],
                                                             ["Orion hotline", "4 hours"]])
    response = _upload(client, headers, "orion.docx", data)
    assert response.status_code == 201, response.text
    answer = client.post("/api/ask", headers=headers, json={
        "question": "Orion hotline response time", "mode": "bm25"}).json()
    assert answer["sources"][0]["document_id"] == response.json()["id"]
    assert "Orion hotline | 4 hours" in answer["sources"][0]["excerpt"]


@pytest.mark.parametrize("filename, data, status, detail", [
    ("empty.pdf", b"", 400, "empty"),
    ("broken.pdf", b"%PDF-1.4\nnot a pdf", 422, "could not be parsed"),
    ("scan.pdf", make_pdf([[]]), 422, "No selectable text"),
    ("broken.docx", b"PK\x03\x04 junk", 422, "could not be parsed"),
    ("sheet.xlsx", b"x", 415, "Supported"),
])
def test_upload_rejects_bad_files_and_stores_nothing(scenario, filename, data, status, detail):
    client, token, factory = scenario
    response = _upload(client, token("finance@northstar.demo"), filename, data)
    assert response.status_code == status and detail in response.json()["detail"]
    with factory() as db:  # nothing was indexed (only the three seeded chunks exist)
        assert db.query(Chunk).count() == 3


def test_upload_over_the_size_limit_is_413(scenario, monkeypatch):
    from dataclasses import replace

    from backend.app import main

    client, token, _ = scenario
    monkeypatch.setattr(main, "settings", replace(main.settings, max_upload_bytes=1024))
    response = _upload(client, token("finance@northstar.demo"), "big.txt", b"a" * 1025)
    assert response.status_code == 413 and "exceeds" in response.json()["detail"]
