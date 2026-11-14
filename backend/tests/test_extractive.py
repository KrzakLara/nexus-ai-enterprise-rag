"""Query-aware extractive answers (lexical scorer, no model) + database-target safety guard."""
from dataclasses import replace

import numpy as np
import pytest

from backend.app import rag
from backend.app.extractive import (
    ExtractionConfig,
    content_terms,
    render_extractive_answer,
    select_evidence,
    split_sentences,
)
from backend.app.retrieval import Hit
from backend.evaluation.safety import UnsafeDatabaseTarget, assert_disposable_database

ATLAS = ("ATLAS INDUSTRIAL SUPPLY AGREEMENT\nAgreement ID: NS-SUP-2026-041\n"
         "1. Scope of supply\nAtlas supplies precision steel fasteners for the plants in Zagreb and Graz. "
         "Orders are confirmed within two business days.\n2. Commercial terms\n"
         "The estimated annual contract value is EUR 184,000. Prices are fixed for the first twelve months. "
         "Invoices are payable within 45 days of receipt of a correct invoice.")
HR_DOC = ("Članak 2. Trajanje godišnjeg odmora\nRadnik ima pravo na godišnji odmor od najmanje 20 radnih dana "
          "u kalendarskoj godini. Roditelj djeteta mlađeg od sedam godina ostvaruje dva dodatna dana.")
LEX = ExtractionConfig(w_semantic=0.0, w_lexical=1.0, w_exact=0.0, sentences_per_source=1)


def hit(cid: str, content: str, title: str = "Doc") -> Hit:
    return Hit("d-" + cid, title, cid, 1, content, 1.0)


def test_sentence_split_keeps_numbers_dates_and_identifiers_intact():
    parts = split_sentences(ATLAS + " Ugovor istječe 31. ožujka 2027. godine. Cijena je EUR 1.60 po litri.")
    assert "Agreement ID: NS-SUP-2026-041" in parts
    assert "The estimated annual contract value is EUR 184,000." in parts
    assert any("31. ožujka 2027. godine" in p for p in parts)
    assert any("EUR 1.60 po litri" in p for p in parts)


WRAPPED = ("Agreement ID: NS-IT-2026-017\nNotice period: 60 days before the end date\n"
           "Department: Finance and Legal\n\n"
           "Cygnus Cloud Services provides cloud hosting and backup services. The contract\n"
           "includes a 99.9% monthly availability target. Service levels require\n"
           "98.5% on-time delivery.\n1. Scope of supply\n2. Commercial terms")


def test_hard_wrapped_prose_is_joined_but_structured_lines_stay_separate():
    parts = split_sentences(WRAPPED)
    assert "The contract\nincludes a 99.9% monthly availability target." in parts
    assert "Service levels require\n98.5% on-time delivery." in parts
    for line in ("Agreement ID: NS-IT-2026-017", "Notice period: 60 days before the end date",
                 "Department: Finance and Legal", "Scope of supply", "Commercial terms"):
        assert line in parts  # numbered headings stay separate (not merged into the previous line)
    assert all(p in WRAPPED for p in parts)  # still verbatim substrings


def test_joined_quote_is_rendered_on_one_line_and_cited():
    [s] = select_evidence("What availability does the cloud contract include?",
                          [hit("c1", WRAPPED, "Cygnus")], None, LEX)
    assert "99.9%" in s.sentence and s.sentence in WRAPPED
    text = render_extractive_answer([s])
    assert "[S1] Cygnus, page 1: The contract includes a 99.9% monthly availability target." in text


def test_fact_late_in_the_chunk_is_selected_not_the_first_330_characters():
    question = "How many days do we have to pay an invoice?"
    assert ATLAS.find("45 days") > 330  # the old offline preview could never show this fact
    [snippet] = select_evidence(question, [hit("c1", ATLAS)], None, LEX)
    assert "45 days" in snippet.sentence and snippet.sentence in ATLAS


def test_croatian_inflection_is_matched_by_prefix_stemming():
    assert content_terms("godišnjeg odmora") & content_terms("godišnji odmor")
    # inflected question forms (godišnjeg odmora) match the base forms in the text (godišnji odmor)
    [snippet] = select_evidence("Koliko godišnjeg odmora ima radnik?", [hit("c", HR_DOC)], None, LEX)
    assert "20 radnih dana" in snippet.sentence and snippet.score > 0.5
    [snippet] = select_evidence("Što dobiva roditelj djeteta?", [hit("c", HR_DOC)], None, LEX)
    assert "dva dodatna dana" in snippet.sentence


def test_quotes_are_verbatim_per_source_and_never_merged():
    hits = [hit("c1", ATLAS, "Atlas"), hit("c2", HR_DOC, "Leave policy")]
    snippets = select_evidence("annual value and vacation days", hits, None,
                               replace(LEX, sentences_per_source=2))
    assert {s.source_index for s in snippets} == {1, 2}
    for s in snippets:
        assert s.sentence in hits[s.source_index - 1].content  # verbatim from ITS OWN source
    text = render_extractive_answer(snippets)
    for line in text.splitlines():
        if line.startswith("[S"):
            sid, quote = int(line[2]), line.split(": ", 1)[1]
            assert quote in hits[sid - 1].content


def test_only_given_authorized_hits_can_be_quoted():
    """The extractor has no database access: it can only quote the hits it receives."""
    secret = "Codename KESTREL-9: 23 positions in Graz are affected."
    snippets = select_evidence("What is KESTREL-9?", [hit("c1", ATLAS)], None, LEX)
    assert all("KESTREL" not in s.sentence for s in snippets)
    assert secret not in render_extractive_answer(snippets)
    assert select_evidence("anything", [], None, LEX) == []


def test_max_sources_and_sentences_bound_the_answer_length():
    many = [hit(f"c{i}", ATLAS) for i in range(6)]
    snippets = select_evidence("annual contract value", many, None, ExtractionConfig(w_semantic=0.0))
    assert max(s.source_index for s in snippets) <= 3 and len(snippets) <= 3 * 2
    assert len(render_extractive_answer(snippets)) < 6 * len(ATLAS) / 2


class FakeEmbedder:
    """Deterministic test double: vectors depend only on whether the text mentions 'invoice'."""

    def _vec(self, text):
        v = np.array([1.0, 0.0]) if "invoice" in text.lower() else np.array([0.0, 1.0])
        return v / np.linalg.norm(v)

    def embed_query(self, text):
        return self._vec(text)

    def embed_passages(self, texts):
        return np.stack([self._vec(t) for t in texts])


def test_semantic_scorer_is_used_when_an_embedder_is_given():
    cfg = ExtractionConfig(w_semantic=1.0, w_lexical=0.0, w_exact=0.0, sentences_per_source=1)
    [s] = select_evidence("When must the bill (invoice) be settled?", [hit("c", ATLAS)], FakeEmbedder(), cfg)
    assert "Invoices are payable" in s.sentence and s.semantic_similarity == pytest.approx(1.0)


def test_rag_offline_answer_cites_and_quotes(monkeypatch):
    monkeypatch.setattr(rag, "settings", replace(rag.settings, openai_api_key=""))
    text, mode = rag.answer("How many days to pay an invoice?", [hit("c1", ATLAS, "Atlas")])
    assert mode == "extractive" and "[S1] Atlas, page 1:" in text and "45 days" in text
    assert rag.answer("x y z", [])[1] == "no_evidence"


@pytest.mark.parametrize("url, app_url, message", [
    ("", None, "explicit"),
    ("sqlite:///eval_test.db", None, "PostgreSQL"),
    ("postgresql+psycopg://u:p@127.0.0.1:5433/postgres", None, "reserved"),
    ("postgresql+psycopg://u:p@127.0.0.1:5433/nexus", None, "_eval or _test"),
    ("postgresql+psycopg://u:p@127.0.0.1:5433/Nexus_Eval", None, "_eval or _test"),
    ("postgresql+psycopg://u:p@127.0.0.1:5433/nexus_eval",
     "postgresql+psycopg://u:p@127.0.0.1:5433/nexus_eval", "application database"),
    ("postgresql+psycopg://u:p@db.prod.example.com:5432/nexus_eval", None, "remote host"),
])
def test_destructive_operations_reject_unsafe_targets(url, app_url, message, monkeypatch):
    monkeypatch.delenv("EVAL_ALLOW_REMOTE_DB", raising=False)
    with pytest.raises(UnsafeDatabaseTarget, match=message):
        assert_disposable_database(url, app_database_url=app_url or "sqlite:///./data/nexus.db")


def test_disposable_local_database_is_accepted():
    url = assert_disposable_database("postgresql+psycopg://u:p@127.0.0.1:5433/nexus_eval",
                                     app_database_url="postgresql+psycopg://u:p@127.0.0.1:5433/nexus")
    assert url.database == "nexus_eval"
