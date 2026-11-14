"""Unit tests for Reciprocal Rank Fusion (pure function, no database)."""
import itertools

import pytest

from backend.app.fusion import reciprocal_rank_fusion


def scores(results):
    return {r.chunk_id: r.rrf_score for r in results}


def test_formula_matches_sum_of_weight_over_k_plus_rank():
    fused = reciprocal_rank_fusion({"lexical": ["a", "b", "c"], "semantic": ["c", "a"]}, k=60)
    assert [r.chunk_id for r in fused] == ["a", "c", "b"]
    s = scores(fused)
    assert s["a"] == pytest.approx(1 / 61 + 1 / 62)
    assert s["c"] == pytest.approx(1 / 63 + 1 / 61)
    assert s["b"] == pytest.approx(1 / 62)
    a = fused[0]
    assert a.ranks == {"lexical": 1, "semantic": 2}
    assert fused[2].ranks == {"lexical": 2}  # absent from semantic: no semantic rank at all


def test_custom_k_and_weights():
    fused = reciprocal_rank_fusion(
        {"lexical": ["x", "y"], "semantic": ["y", "x"]},
        weights={"lexical": 2.0, "semantic": 0.5}, k=10,
    )
    s = scores(fused)
    assert s["x"] == pytest.approx(2.0 / 11 + 0.5 / 12)
    assert s["y"] == pytest.approx(2.0 / 12 + 0.5 / 11)
    assert fused[0].chunk_id == "x"


def test_zero_weight_branch_keeps_ranks_but_adds_nothing():
    fused = reciprocal_rank_fusion({"lexical": ["a", "b"], "semantic": ["b", "c"]},
                                   weights={"lexical": 1.0, "semantic": 0.0})
    assert [r.chunk_id for r in fused[:2]] == ["a", "b"]
    assert scores(fused)["c"] == 0.0 and fused[-1].ranks == {"semantic": 2}


def test_deduplicates_within_a_branch_by_chunk_id():
    fused = reciprocal_rank_fusion({"lexical": ["a", "a", "b"]})
    assert scores(fused) == pytest.approx({"a": 1 / 61, "b": 1 / 62})
    assert {r.chunk_id: r.ranks["lexical"] for r in fused} == {"a": 1, "b": 2}


def test_ties_are_broken_deterministically_independent_of_input_order():
    branches = {"lexical": ["q", "p"], "semantic": ["p", "q"], }
    expected = None
    for order in itertools.permutations(branches):
        fused = reciprocal_rank_fusion({name: branches[name] for name in order})
        ids = [r.chunk_id for r in fused]
        expected = expected or ids
        assert ids == expected == ["p", "q"]  # equal score, equal best rank -> chunk id asc
    # Exactly equal scores (61/61 == 62/62 == 1.0) but "z" has the better best-rank -> it wins
    fused = reciprocal_rank_fusion({"lexical": ["z"], "semantic": ["s0", "a"]},
                                   weights={"lexical": 61.0, "semantic": 62.0})
    s = scores(fused)
    assert s["z"] == s["a"] == 1.0
    assert [r.chunk_id for r in fused] == ["s0", "z", "a"]


def test_top_k_and_empty_inputs():
    assert reciprocal_rank_fusion({"lexical": [], "semantic": []}) == []
    assert reciprocal_rank_fusion({}) == []
    fused = reciprocal_rank_fusion({"lexical": list("abcdef")}, top_k=2)
    assert [r.chunk_id for r in fused] == ["a", "b"]
    only_semantic = reciprocal_rank_fusion({"lexical": [], "semantic": ["s1", "s2"]})
    assert [(r.chunk_id, r.ranks) for r in only_semantic] == [("s1", {"semantic": 1}),
                                                             ("s2", {"semantic": 2})]


def test_score_is_bounded_rank_statistic_not_probability():
    fused = reciprocal_rank_fusion({"lexical": ["a"], "semantic": ["a"]})
    assert fused[0].rrf_score == pytest.approx(2 / 61)  # maximum possible with k=60, two branches


@pytest.mark.parametrize("kwargs, message", [
    ({"k": 0}, "positive"),
    ({"weights": {"lexical": -1.0, "semantic": 1.0}}, "non-negative"),
    ({"weights": {"lexical": 0.0, "semantic": 0.0}}, "positive weight"),
    ({"weights": {"lexical": 1.0, "rerank": 1.0}}, "unknown"),
])
def test_invalid_parameters_are_rejected(kwargs, message):
    with pytest.raises(ValueError, match=message):
        reciprocal_rank_fusion({"lexical": ["a"], "semantic": ["b"]}, **kwargs)
