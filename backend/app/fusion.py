"""Reciprocal Rank Fusion (Cormack, Clarke & Büttcher, SIGIR 2009).

    rrf(d) = sum over branches i where d appears of  weight_i / (k + rank_i(d)),  ranks start at 1

Pure function over ranked chunk IDs; it knows nothing about the database or authorization.
Callers must pass only permission-scoped branch results (see retrieval.hybrid_search).

An RRF score is a rank-aggregation statistic. It is NOT a probability, a confidence or a
relevance estimate, and it is not comparable across queries. With k=60 and two equally weighted
branches its maximum is 2/61 ≈ 0.0328 (rank 1 in both branches).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

DEFAULT_RRF_K = 60


@dataclass(frozen=True)
class FusedResult:
    chunk_id: str
    rrf_score: float
    # 1-based rank in each branch where the chunk appeared; absent branches are omitted.
    ranks: Mapping[str, int] = field(default_factory=dict)


def reciprocal_rank_fusion(
    branches: Mapping[str, Sequence[str]],
    weights: Mapping[str, float] | None = None,
    k: int = DEFAULT_RRF_K,
    top_k: int | None = None,
) -> list[FusedResult]:
    """Fuse ranked lists of chunk IDs.

    * Deduplicates by chunk ID (within a branch the first, i.e. best, occurrence wins).
    * Deterministic ordering: higher RRF score, then better (lower) best rank across branches,
      then more branches agreeing, then chunk ID ascending.
    * Branches with weight 0 contribute nothing but their ranks are still reported.
    """
    if k <= 0:
        raise ValueError("RRF constant k must be positive")
    weights = dict(weights) if weights is not None else {name: 1.0 for name in branches}
    unknown = set(weights) - set(branches)
    if unknown:
        raise ValueError(f"Weights given for unknown branches: {sorted(unknown)}")
    if any(w < 0 for w in weights.values()):
        raise ValueError("RRF weights must be non-negative")
    if branches and not any(weights.get(name, 0.0) > 0 for name in branches):
        raise ValueError("At least one branch needs a positive weight")

    scores: dict[str, float] = {}
    ranks: dict[str, dict[str, int]] = {}
    for name, ranked_ids in branches.items():
        weight = weights.get(name, 0.0)
        seen: set[str] = set()
        rank = 0
        for chunk_id in ranked_ids:
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            rank += 1
            ranks.setdefault(chunk_id, {})[name] = rank
            scores[chunk_id] = scores.get(chunk_id, 0.0) + weight / (k + rank)

    fused = [FusedResult(cid, scores[cid], dict(ranks[cid])) for cid in scores]
    fused.sort(key=lambda r: (-r.rrf_score, min(r.ranks.values()), -len(r.ranks), r.chunk_id))
    return fused if top_k is None else fused[:top_k]
