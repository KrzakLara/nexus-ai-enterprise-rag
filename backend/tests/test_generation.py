"""Answer generation: offline extractive mode and the optional OpenAI path.

The OpenAI client is replaced by a fake that records the request, so these tests make NO network
calls and need no API key. They verify our side of the contract (prompt construction, authorized
context only, citation validation, explicit fallbacks); they do not verify a live model.
"""
from dataclasses import replace

import openai
import pytest

from backend.app import rag
from backend.app.retrieval import Hit

ATLAS = ("Atlas supplier annual value is EUR 184,000. The notice period is 90 days. "
         "Invoices are payable within 45 days.")
CYGNUS = "Cygnus cloud availability is 99.9 percent per month."


def hit(chunk_id, content, title="Atlas", page=1):
    return Hit(chunk_id.split("_")[0], title, chunk_id, page, content, 1.0)


class FakeOpenAI:
    """Stands in for openai.OpenAI. `output` is returned; an Exception instance is raised."""
    output: object = "Notice is 90 days [S1]."
    calls: list[dict] = []
    init_kwargs: list[dict] = []

    def __init__(self, **kwargs):
        FakeOpenAI.init_kwargs.append(kwargs)
        self.responses = self

    def create(self, **kwargs):
        FakeOpenAI.calls.append(kwargs)
        if isinstance(FakeOpenAI.output, Exception):
            raise FakeOpenAI.output
        return type("Response", (), {"output_text": FakeOpenAI.output})()


@pytest.fixture
def llm(monkeypatch):
    FakeOpenAI.calls, FakeOpenAI.init_kwargs, FakeOpenAI.output = [], [], "Notice is 90 days [S1]."
    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(rag, "settings", replace(rag.settings, openai_api_key="sk-test-not-real",
                                                 openai_model="test-model"))
    return FakeOpenAI


def test_prompt_contains_question_and_numbered_sources_only(llm):
    hits = [hit("a_c0", ATLAS, "Atlas", 2), hit("c_c0", CYGNUS, "Cygnus", 1)]
    text, mode = rag.answer("What is the Atlas notice period?", hits)
    assert (text, mode) == ("Notice is 90 days [S1].", "llm")
    [call] = llm.calls
    assert call["model"] == "test-model" and call["max_output_tokens"] == 700
    assert call["instructions"] == rag.SYSTEM_INSTRUCTIONS
    assert "untrusted data" in call["instructions"] and "[S1]" in call["instructions"]
    assert call["input"] == (
        "QUESTION:\nWhat is the Atlas notice period?\n\nAUTHORIZED SOURCES:\n"
        f"[S1] Atlas, page 2:\n{ATLAS}\n\n[S2] Cygnus, page 1:\n{CYGNUS}")
    assert llm.init_kwargs == [{"api_key": "sk-test-not-real", "timeout": 20.0, "max_retries": 1}]


@pytest.mark.parametrize("output", ["Notice is 90 days [S1] and availability 99.9% [S2].",
                                    "[S2] says 99.9 percent; [S1] says 90 days."])
def test_valid_citations_are_accepted(llm, output):
    llm.output = output
    assert rag.answer("q?", [hit("a_c0", ATLAS), hit("c_c0", CYGNUS)]) == (output, "llm")


@pytest.mark.parametrize("output, why", [
    ("I believe it is roughly three months.", "no citation at all"),
    ("Notice is 90 days [S3].", "cites a source that was not supplied"),
    ("Notice is 90 days [S1] [S0].", "S0 is not a valid identifier"),
    ("", "empty model output"),
])
def test_unverifiable_llm_answers_are_discarded_and_replaced_by_verbatim_quotes(llm, output, why):
    llm.output = output
    text, mode = rag.answer("What is the notice period?", [hit("a_c0", ATLAS)])
    assert mode == "citation_validation_failed", why
    assert text.startswith(rag.FALLBACK_NOTICES["citation_validation_failed"])
    if output:
        assert output not in text  # the rejected model text is never shown
    assert "[S1] Atlas, page 1: " in text and "The notice period is 90 days." in text
    assert "no LLM key configured" not in text  # an LLM IS configured; the message must not lie


@pytest.mark.parametrize("error", [TimeoutError("timeout"), ConnectionError("network down"),
                                   RuntimeError("quota exceeded")])
def test_provider_failure_falls_back_explicitly_to_verbatim_evidence(llm, error):
    llm.output = error
    text, mode = rag.answer("What is the notice period?", [hit("a_c0", ATLAS)])
    assert mode == "provider_error"
    assert text.startswith(rag.FALLBACK_NOTICES["provider_error"])
    assert "The notice period is 90 days." in text and "[S1]" in text


def test_no_evidence_never_calls_the_provider(llm):
    assert rag.answer("What is the notice period?", []) == (rag.NO_EVIDENCE, "no_evidence")
    assert llm.calls == []


def test_without_a_key_the_offline_mode_is_extractive_and_makes_no_call(llm, monkeypatch):
    monkeypatch.setattr(rag, "settings", replace(rag.settings, openai_api_key=""))
    text, mode = rag.answer("What is the notice period?", [hit("a_c0", ATLAS)])
    assert mode == "extractive" and "no LLM key configured" in text
    assert "[S1] Atlas, page 1: The notice period is 90 days." in text
    assert llm.calls == []


def test_citation_validator():
    assert rag.citations_valid("a [S1] b [S2]", 2)
    assert not rag.citations_valid("a [S1] b [S3]", 2)
    assert not rag.citations_valid("no markers", 2)
    assert not rag.citations_valid("[S1]", 0)


# ----------------------------------------------- through the API, SQLite and PostgreSQL (scenario)

def test_api_llm_prompt_contains_only_authorized_text_and_sources_match(scenario, llm):
    client, token, _ = scenario
    # finance@northstar may read only "Atlas agreement"; the HR salary chunk (same tenant) and the
    # Otherco secret (other tenant) share query terms but must never reach the prompt.
    question = "Atlas annual value, salary increase budget and confidential codename SILVER-IBIS-73"
    body = client.post("/api/ask", headers=token("finance@northstar.demo"), json={
        "question": question, "mode": "bm25", "top_k": 10}).json()
    assert body["mode"] == "llm" and body["answer"] == "Notice is 90 days [S1]."
    [call] = llm.calls
    sources_part = call["input"].split("AUTHORIZED SOURCES:", 1)[1]
    assert "Atlas supplier annual value" in sources_part
    for forbidden in ("Salary increase budget", "SILVER-IBIS-73. No other tenant"):
        assert forbidden not in sources_part
    assert [s["document_id"] for s in body["sources"]] == ["d_fin"]


def test_api_provider_error_returns_fallback_quotes_and_sources(scenario, llm):
    client, token, _ = scenario
    llm.output = RuntimeError("provider down")
    body = client.post("/api/ask", headers=token("finance@northstar.demo"), json={
        "question": "Atlas notice period", "mode": "bm25"}).json()
    assert body["mode"] == "provider_error"
    assert "notice period is 90 days" in body["answer"] and "[S1]" in body["answer"]
    assert body["sources"][0]["document_id"] == "d_fin"
    assert body["sources"][0]["selected_sentences"]  # the quoted sentence is reported per source
