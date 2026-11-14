"""Unit tests for the IR metrics (hand-computed expected values, no database)."""
import math

import pytest

from backend.evaluation.metrics import (
    UndefinedMetric,
    aggregate,
    hit_rate_at_k,
    mrr_at_k,
    ndcg_at_k,
    percentile,
    precision_at_k,
    recall_at_k,
    score_case,
)

REL = {"a": 2, "b": 1}


def test_recall_precision_hit():
    ranked = ["x", "a", "y", "z", "q", "b"]
    assert recall_at_k(ranked, REL, 1) == 0.0
    assert recall_at_k(ranked, REL, 5) == 0.5
    assert recall_at_k(ranked, REL, 10) == 1.0
    assert precision_at_k(ranked, REL, 5) == pytest.approx(1 / 5)
    assert precision_at_k(["a"], REL, 5) == pytest.approx(1 / 5)  # divides by k, not by |returned|
    assert hit_rate_at_k(ranked, REL, 1) == 0.0 and hit_rate_at_k(ranked, REL, 5) == 1.0


def test_mrr_uses_first_relevant_rank_within_cutoff():
    assert mrr_at_k(["x", "y", "b", "a"], REL, 10) == pytest.approx(1 / 3)
    assert mrr_at_k(["x", "y", "z", "a"], REL, 3) == 0.0
    assert mrr_at_k([f"n{i}" for i in range(10)] + ["a"], REL, 10) == 0.0


def test_ndcg_graded_hand_computed():
    # ranked: b (g1) at 1, a (g2) at 3 -> DCG = (2^1-1)/log2(2) + (2^2-1)/log2(4) = 1 + 1.5
    # ideal:  a (g2) at 1, b (g1) at 2 -> IDCG = 3/1 + 1/log2(3)
    expected = (1 + 1.5) / (3 + 1 / math.log2(3))
    assert ndcg_at_k(["b", "x", "a"], REL, 5) == pytest.approx(expected)
    assert ndcg_at_k(["a", "b"], REL, 5) == pytest.approx(1.0)
    assert ndcg_at_k(["x", "y"], REL, 5) == 0.0


def test_duplicates_in_ranking_are_ignored():
    assert recall_at_k(["a", "a", "a"], REL, 2) == 0.5
    assert precision_at_k(["a", "a"], REL, 2) == 0.5


def test_undefined_for_cases_without_relevant_items():
    for fn in (recall_at_k, precision_at_k, hit_rate_at_k, mrr_at_k, ndcg_at_k):
        with pytest.raises(UndefinedMetric):
            fn(["a"], {}, 5)
    with pytest.raises(ValueError):
        recall_at_k(["a"], REL, 0)


def test_score_case_and_macro_aggregate():
    s1 = score_case(["a", "b"], REL)
    s2 = score_case(["x"], REL)
    assert s1["recall@1"] == 0.5 and s1["ndcg@5"] == pytest.approx(1.0) and s2["mrr@10"] == 0.0
    agg = aggregate([s1, s2])
    assert agg["n"] == 2 and agg["hit_rate@5"] == 0.5 and agg["recall@5"] == 0.5
    assert aggregate([]) == {"n": 0}


def test_percentile_nearest_rank():
    values = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    assert percentile(values, 50) == 50 and percentile(values, 95) == 100
    assert percentile([5], 95) == 5
