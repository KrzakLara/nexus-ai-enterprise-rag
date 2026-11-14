"""Evidence-sufficiency policy: unit tests (no database) + calibration drift guard."""
import json
from pathlib import Path

import pytest

from backend.app import main
from backend.app.config import settings
from backend.app.evidence import (
    EvidencePolicy,
    assess,
    decide,
    extract_identifiers,
    identifier_in,
)
from backend.app.retrieval import Hit, RetrievalResult

ROOT = Path(__file__).resolve().parents[2]


def hit(cid: str, content: str, distance: float | None = None) -> Hit:
    return Hit("d", "T", cid, 1, content, 0.0, distance=distance)


def result(semantic: list[Hit], lexical: list[Hit] | None = None) -> RetrievalResult:
    branches = {"semantic": semantic} | ({"lexical": lexical} if lexical is not None else {})
    return RetrievalResult(mode="hybrid", hits=semantic or (lexical or []), branch_hits=branches)


def test_identifier_extraction_requires_hyphen_and_digit():
    ids = extract_identifiers("NS-SUP-2026-041, silver-ibis-73, KESTREL-9, line-stop, three-way, Q3")
    assert ids == ["NS-SUP-2026-041", "SILVER-IBIS-73", "KESTREL-9"]


def test_identifier_matching_is_exact_token_not_prefix_or_suffix():
    assert identifier_in("NS-SUP-2026-041", "Agreement NS-SUP-2026-041 between")
    assert identifier_in("ns-sup-2026-041", "(NS-SUP-2026-041).")
    assert not identifier_in("NS-SUP-2026-041", "Amendment ID: NS-SUP-2026-041-A1")
    assert not identifier_in("NS-SUP-2026-041", "Agreement ID: OC-SUP-2026-041")


POLICY = EvidencePolicy(min_semantic_similarity=0.82, require_identifier_match=True)


def test_missing_identifier_abstains_even_with_high_similarity():
    r = result([hit("c1", "Agreement OC-SUP-2026-041 ...", distance=0.05)])
    d = assess("What does NS-SUP-2026-041 say?", r, POLICY)
    assert not d.sufficient and d.reasons == ["identifier_not_found"]
    assert d.signals["identifiers"] == ["NS-SUP-2026-041"] and d.signals["identifiers_found"] == []


def test_found_identifier_is_sufficient_even_with_low_similarity():
    r = result([hit("c1", "Agreement NS-SUP-2026-041 renews", distance=0.4)])
    d = assess("notice period NS-SUP-2026-041", r, POLICY)
    assert d.sufficient and d.reasons == ["identifier_match"]


def test_identifier_found_only_in_lexical_branch_counts():
    r = result([hit("s1", "unrelated", 0.3)], [hit("l1", "see NS-IT-2026-017")])
    assert assess("NS-IT-2026-017 RTO", r, POLICY).sufficient


@pytest.mark.parametrize("distance, sufficient", [(0.17, True), (0.18, True), (0.181, False), (0.3, False)])
def test_similarity_threshold_is_inclusive(distance, sufficient):
    d = assess("how long are contracts kept", result([hit("c", "text", distance)]), POLICY)
    assert d.sufficient is sufficient
    assert d.signals["semantic_top_similarity"] == round(1 - distance, 4)


def test_no_candidates_disabled_policy_and_agreement():
    assert decide({"identifiers": []}, False, POLICY).reasons == ["no_candidates"]
    off = EvidencePolicy(enabled=False)
    assert assess("x y z", result([hit("c", "t", 0.9)]), off).sufficient
    agree = EvidencePolicy(min_semantic_similarity=0.95, accept_on_branch_agreement=True)
    r = result([hit("c1", "t", 0.3)], [hit("c9", "t"), hit("c1", "t")])
    d = assess("question", r, agree)
    assert d.sufficient and d.reasons == ["branch_agreement"] and d.signals["branch_agreement"]


def test_rrf_score_is_not_a_signal():
    """A huge RRF score on an irrelevant chunk must not make evidence sufficient."""
    h = hit("c", "unrelated", distance=0.5)
    h.rrf_score = 0.0328
    assert not assess("what is the parking policy", result([h]), POLICY).sufficient


def test_extraction_defaults_match_dev_calibration():
    from backend.app.extractive import ExtractionConfig

    cal = json.loads((ROOT / "eval" / "calibration.json").read_text(encoding="utf-8"))
    assert ExtractionConfig().__dict__ == cal["extraction"]


def test_app_defaults_match_dev_calibration():
    """Guard against drift between eval/calibration.json (selected on the dev split) and the app."""
    cal = json.loads((ROOT / "eval" / "calibration.json").read_text(encoding="utf-8"))
    assert cal["selected_on"] == "dev split only"
    assert settings.evidence_min_similarity == cal["policy"]["min_semantic_similarity"]
    assert settings.evidence_require_identifier == cal["policy"]["require_identifier_match"]
    assert settings.evidence_accept_agreement == cal["policy"]["accept_on_branch_agreement"]
    assert settings.evidence_min_sentence_similarity == cal["policy"]["min_sentence_similarity"]
    assert settings.evidence_min_term_coverage == cal["policy"]["min_term_coverage"]
    assert main.EVIDENCE_POLICY.describe() == cal["policy"]


def _env_example() -> dict[str, str]:
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines if line.strip() and not line.startswith("#"))


def test_env_example_evidence_policy_matches_dev_calibration(monkeypatch):
    """Configuration drift guard: .env.example, parsed with the app's OWN parsing rules
    (Settings re-instantiated from those values), must yield exactly the calibrated policy.
    "none" disables a rule; a forgotten old value such as 0.82 would silently OR-in the
    Phase 2c chunk rule and make the policy more permissive than the one measured."""
    from backend.app import config

    env = _env_example()
    evidence_vars = {k for k in env if k.startswith("EVIDENCE_")}
    assert evidence_vars == {"EVIDENCE_POLICY_ENABLED", "EVIDENCE_MIN_SIMILARITY",
                             "EVIDENCE_MIN_SENTENCE_SIMILARITY", "EVIDENCE_MIN_TERM_COVERAGE",
                             "EVIDENCE_REQUIRE_IDENTIFIER", "EVIDENCE_ACCEPT_AGREEMENT"}
    for key in evidence_vars:
        monkeypatch.setenv(key, env[key])
    import importlib

    fresh = importlib.reload(config).Settings()  # dataclass defaults are evaluated at import
    try:
        cal = json.loads((ROOT / "eval" / "calibration.json").read_text(encoding="utf-8"))["policy"]
        assert fresh.evidence_policy_enabled == cal["enabled"]
        assert fresh.evidence_min_similarity == cal["min_semantic_similarity"]
        assert fresh.evidence_min_sentence_similarity == cal["min_sentence_similarity"]
        assert fresh.evidence_min_term_coverage == cal["min_term_coverage"]
        assert fresh.evidence_require_identifier == cal["require_identifier_match"]
        assert fresh.evidence_accept_agreement == cal["accept_on_branch_agreement"]
    finally:
        for key in evidence_vars:
            monkeypatch.delenv(key)
        importlib.reload(config)


@pytest.mark.parametrize("raw, expected", [("none", None), ("", None), ("OFF", None),
                                           ("0.835", 0.835), ("0", 0.0)])
def test_disabled_threshold_means_rule_off_not_zero(raw, expected, monkeypatch):
    from backend.app.config import _opt_float

    monkeypatch.setenv("NEXUS_TEST_THRESHOLD", raw)
    assert _opt_float("NEXUS_TEST_THRESHOLD", "0.5") == expected
    # A disabled (None) chunk rule must not accept anything on its own
    h = hit("c1", "Unrelated text.", distance=0.01)  # chunk cosine 0.99
    off = EvidencePolicy(min_semantic_similarity=None, min_sentence_similarity=0.9)
    d = assess("parking rules", result([h]), off, snippets=[])
    assert not d.sufficient and d.reasons == ["evidence_similarity_below_threshold"]


def test_sentence_similarity_rule_accepts_when_chunk_similarity_is_low():
    from backend.app.extractive import Snippet

    h = hit("c1", "Long contract text. The notice period is 90 days.", distance=0.25)  # chunk cos 0.75
    snip = Snippet(1, h, "The notice period is 90 days.", 1.0, semantic_similarity=0.88)
    pol = EvidencePolicy(min_semantic_similarity=0.82, min_sentence_similarity=0.85)
    d = assess("notice period", result([h]), pol, snippets=[snip])
    assert d.sufficient and d.reasons == ["sentence_similarity_above_threshold"]
    assert d.signals["sentence_top_similarity"] == 0.88
    strict = EvidencePolicy(min_semantic_similarity=0.82, min_sentence_similarity=0.90)
    assert not assess("notice period", result([h]), strict, snippets=[snip]).sufficient


def test_term_coverage_rule_and_identifier_rule_still_has_priority():
    h = hit("c1", "Invoices are payable within 45 days of receipt.", distance=0.4)
    pol = EvidencePolicy(min_semantic_similarity=0.9, min_term_coverage=0.6)
    d = assess("invoices payable days", result([h]), pol)
    assert d.sufficient and d.reasons == ["question_terms_covered"] and d.signals["term_coverage"] == 1.0
    assert not assess("parking garage rules", result([h]), pol).sufficient
    # a forbidden / unknown identifier is refused even when every other signal is perfect
    d = assess("payable days NS-SUP-2026-099", result([h]), pol)
    assert not d.sufficient and d.reasons == ["identifier_not_found"]
