"""Evidence-sufficiency policy: decide whether retrieved, AUTHORIZED candidates are good enough to
answer, or whether the system must abstain with `no_evidence`.

Signals (all computed from the permission-scoped candidates only):
  * exact identifiers in the question (e.g. NS-SUP-2026-041, SILVER-IBIS-73) and whether every one
    of them literally occurs in an authorized candidate chunk;
  * the best cosine similarity of the semantic branch;
  * optional agreement: the semantic top-1 chunk is also within the lexical top-N.

None of these is a calibrated probability. RRF scores are deliberately NOT used: they are rank
statistics. Thresholds are calibrated on the development split of the evaluation set
(scripts/evaluate.py) and reported on the held-out test split; see docs/evaluation.md.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from .retrieval import Hit, RetrievalResult

# Letters, then at least one "-segment", and at least one digit somewhere (avoids "line-stop").
_IDENTIFIER = re.compile(r"(?<![\w-])([A-Za-z]{2,}(?:-[A-Za-z0-9]+)+)(?![\w-])")


def extract_identifiers(text: str) -> list[str]:
    found = [m.group(1) for m in _IDENTIFIER.finditer(text)]
    return list(dict.fromkeys(i.upper() for i in found if any(ch.isdigit() for ch in i)))


def identifier_in(identifier: str, content: str) -> bool:
    pattern = rf"(?<![\w-]){re.escape(identifier)}(?![\w-])"
    return re.search(pattern, content, flags=re.IGNORECASE) is not None


@dataclass(frozen=True)
class EvidencePolicy:
    """Rules are OR-ed after the identifier rule; a None threshold disables that rule."""
    enabled: bool = True
    min_semantic_similarity: float | None = 0.80   # best chunk cosine (semantic branch)
    require_identifier_match: bool = True
    accept_on_branch_agreement: bool = False
    agreement_depth: int = 3
    min_sentence_similarity: float | None = None  # best sentence cosine among top hits
    min_term_coverage: float | None = None        # share of question content terms in a top hit

    def describe(self) -> dict:
        return asdict(self)


@dataclass
class EvidenceDecision:
    sufficient: bool
    reasons: list[str]
    signals: dict = field(default_factory=dict)


def candidate_hits(result: RetrievalResult) -> list[Hit]:
    seen: dict[str, Hit] = {}
    for hits in [result.hits, *result.branch_hits.values()]:
        for h in hits:
            seen.setdefault(h.chunk_id, h)
    return list(seen.values())


def compute_signals(question: str, result: RetrievalResult, agreement_depth: int = 3,
                    snippets: list | None = None, coverage_depth: int = 3) -> dict:
    candidates = candidate_hits(result)
    identifiers = extract_identifiers(question)
    found = [i for i in identifiers if any(identifier_in(i, h.content) for h in candidates)]
    semantic = result.branch_hits.get("semantic", [])
    lexical = result.branch_hits.get("lexical", [])
    top_similarity = max((1.0 - h.distance for h in semantic if h.distance is not None),
                         default=None)
    agreement = bool(semantic and lexical and semantic[0].chunk_id in
                     {h.chunk_id for h in lexical[:agreement_depth]})
    from .extractive import content_terms

    q_terms = content_terms(question)
    coverage = max((len(q_terms & content_terms(h.content)) / len(q_terms)
                    for h in result.hits[:coverage_depth]), default=None) if q_terms else None
    sentence_sims = [s.semantic_similarity for s in (snippets or []) if s.semantic_similarity is not None]
    return {
        "sentence_top_similarity": round(max(sentence_sims), 4) if sentence_sims else None,
        "term_coverage": None if coverage is None else round(coverage, 4),
        "identifiers": identifiers,
        "identifiers_found": found,
        "semantic_top_similarity": None if top_similarity is None else round(top_similarity, 4),
        "lexical_candidates": len(lexical),
        "branch_agreement": agreement,
        "candidate_count": len(candidates),
    }


def decide(signals: dict, has_hits: bool, policy: EvidencePolicy) -> EvidenceDecision:
    """Pure decision rule over pre-computed signals (shared by the API and the calibration)."""
    if not policy.enabled:
        return EvidenceDecision(has_hits, ["policy_disabled"], signals)
    if not has_hits:
        return EvidenceDecision(False, ["no_candidates"], signals)
    if policy.require_identifier_match and signals["identifiers"]:
        missing = sorted(set(signals["identifiers"]) - set(signals["identifiers_found"]))
        if missing:
            return EvidenceDecision(False, ["identifier_not_found"], signals | {"missing": missing})
        return EvidenceDecision(True, ["identifier_match"], signals)
    sim = signals["semantic_top_similarity"]
    if policy.min_semantic_similarity is not None and sim is not None:
        if sim >= policy.min_semantic_similarity:
            return EvidenceDecision(True, ["semantic_similarity_above_threshold"], signals)
    ssim = signals.get("sentence_top_similarity")
    if policy.min_sentence_similarity is not None and ssim is not None:
        if ssim >= policy.min_sentence_similarity:
            return EvidenceDecision(True, ["sentence_similarity_above_threshold"], signals)
    cov = signals.get("term_coverage")
    if policy.min_term_coverage is not None and cov is not None and cov >= policy.min_term_coverage:
        return EvidenceDecision(True, ["question_terms_covered"], signals)
    if policy.accept_on_branch_agreement and signals["branch_agreement"]:
        return EvidenceDecision(True, ["branch_agreement"], signals)
    if sim is None:  # no semantic branch (bm25 / lexical modes): nothing to judge with
        return EvidenceDecision(True, ["no_semantic_signal"], signals)
    return EvidenceDecision(False, ["evidence_similarity_below_threshold"], signals)


def assess(question: str, result: RetrievalResult, policy: EvidencePolicy,
           snippets: list | None = None) -> EvidenceDecision:
    signals = compute_signals(question, result, policy.agreement_depth, snippets)
    return decide(signals, bool(result.hits), policy)
