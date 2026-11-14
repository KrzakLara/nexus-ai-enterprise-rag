"""Information-retrieval metrics (standard definitions, chunk-level, graded relevance).

Notation: `ranked` is the ordered list of returned chunk IDs (rank 1 first, deduplicated),
`relevance` maps relevant chunk ID -> grade (1 = supporting, 2 = primary). Grade > 0 is
"relevant" for the binary metrics.

* Recall@k     = |rel ∩ top-k| / |rel|
* Precision@k  = |rel ∩ top-k| / k       (divides by k even if fewer than k results were returned)
* HitRate@k    = 1 if |rel ∩ top-k| > 0 else 0
* MRR@k        = 1 / rank of the first relevant result within top-k, else 0
* nDCG@k       = DCG@k / IDCG@k, DCG = Σ_i (2^grade_i - 1) / log2(i + 1)

Relevance-based metrics are UNDEFINED for questions with no authorized relevant chunk
(unanswerable / authorization cases); callers must exclude those cases explicitly instead of
scoring them as 0 or 1. They are evaluated separately (abstention, security).
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from statistics import mean


class UndefinedMetric(ValueError):
    """Raised when a relevance metric is requested for a case without relevant items."""


def _dedup(ranked: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    return [r for r in ranked if not (r in seen or seen.add(r))]


def _check(relevance: Mapping[str, int], k: int) -> None:
    if k <= 0:
        raise ValueError("k must be positive")
    if not any(g > 0 for g in relevance.values()):
        raise UndefinedMetric("no relevant items: metric is undefined for this case")


def recall_at_k(ranked: Sequence[str], relevance: Mapping[str, int], k: int) -> float:
    _check(relevance, k)
    rel = {d for d, g in relevance.items() if g > 0}
    return len(rel & set(_dedup(ranked)[:k])) / len(rel)


def precision_at_k(ranked: Sequence[str], relevance: Mapping[str, int], k: int) -> float:
    _check(relevance, k)
    rel = {d for d, g in relevance.items() if g > 0}
    return len(rel & set(_dedup(ranked)[:k])) / k


def hit_rate_at_k(ranked: Sequence[str], relevance: Mapping[str, int], k: int) -> float:
    _check(relevance, k)
    return 1.0 if any(relevance.get(d, 0) > 0 for d in _dedup(ranked)[:k]) else 0.0


def mrr_at_k(ranked: Sequence[str], relevance: Mapping[str, int], k: int) -> float:
    _check(relevance, k)
    for i, d in enumerate(_dedup(ranked)[:k], 1):
        if relevance.get(d, 0) > 0:
            return 1.0 / i
    return 0.0


def ndcg_at_k(ranked: Sequence[str], relevance: Mapping[str, int], k: int) -> float:
    _check(relevance, k)
    def dcg(grades: Sequence[int]) -> float:
        return sum((2 ** g - 1) / math.log2(i + 1) for i, g in enumerate(grades, 1))
    gains = [relevance.get(d, 0) for d in _dedup(ranked)[:k]]
    ideal = sorted((g for g in relevance.values() if g > 0), reverse=True)[:k]
    return dcg(gains) / dcg(ideal)


METRICS = {
    "recall@1": lambda r, rel: recall_at_k(r, rel, 1),
    "recall@5": lambda r, rel: recall_at_k(r, rel, 5),
    "recall@10": lambda r, rel: recall_at_k(r, rel, 10),
    "precision@5": lambda r, rel: precision_at_k(r, rel, 5),
    "hit_rate@5": lambda r, rel: hit_rate_at_k(r, rel, 5),
    "mrr@10": lambda r, rel: mrr_at_k(r, rel, 10),
    "ndcg@5": lambda r, rel: ndcg_at_k(r, rel, 5),
}


def score_case(ranked: Sequence[str], relevance: Mapping[str, int]) -> dict[str, float]:
    return {name: fn(ranked, relevance) for name, fn in METRICS.items()}


def aggregate(per_case: Sequence[Mapping[str, float]]) -> dict[str, float | int]:
    """Macro-average over cases (each question weighs the same)."""
    if not per_case:
        return {"n": 0}
    return {"n": len(per_case), **{m: round(mean(c[m] for c in per_case), 4) for m in METRICS}}


def percentile(values: Sequence[float], p: float) -> float:
    """Nearest-rank percentile (p in (0, 100]); deterministic and dependency-free."""
    if not values:
        raise ValueError("no values")
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


def bootstrap_ci(values: Sequence[float], n_resamples: int = 2000, seed: int = 0,
                 alpha: float = 0.05) -> tuple[float, float]:
    """Percentile bootstrap CI of the mean (fixed seed -> reproducible)."""
    import random

    if not values:
        raise ValueError("no values")
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(n_resamples))
    lo = means[int((alpha / 2) * n_resamples)]
    hi = means[min(n_resamples - 1, int((1 - alpha / 2) * n_resamples))]
    return round(lo, 4), round(hi, 4)


def paired_bootstrap_diff(a: Sequence[float], b: Sequence[float], **kwargs) -> dict:
    """Mean of (a - b) over paired cases with a bootstrap CI; 'significant' if the CI excludes 0."""
    if len(a) != len(b):
        raise ValueError("paired samples must have equal length")
    diffs = [x - y for x, y in zip(a, b)]
    lo, hi = bootstrap_ci(diffs, **kwargs)
    return {"mean_diff": round(sum(diffs) / len(diffs), 4), "ci95": [lo, hi],
            "ci_excludes_zero": lo > 0 or hi < 0}
