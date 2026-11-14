"""Ground-truth dataset integrity (no database): the CI gate for malformed evaluation data."""
import json
from collections import Counter

import pytest

from backend.evaluation.corpus import load_corpus
from backend.evaluation.dataset import (
    FROZEN_PATH,
    QUESTIONS_PATH,
    DatasetError,
    build_cases,
    load_frozen,
    read_jsonl,
)


@pytest.fixture(scope="module")
def corpus():
    return load_corpus()


def test_frozen_dataset_is_valid_and_matches_a_fresh_rebuild(corpus):
    cases = load_frozen(corpus)
    assert len(cases) >= 80
    assert len(corpus.documents) >= 25 and len(corpus.chunks) > len(corpus.documents)
    assert {c.split for c in cases} == {"dev", "test"}
    assert not {c.id for c in cases if c.split == "dev"} & {c.id for c in cases if c.split == "test"}
    cats = Counter(c.category for c in cases)
    assert cats["unanswerable"] >= 8 and cats["authorization"] >= 6 and cats["cross_language"] >= 8
    assert {c.language for c in cases} == {"en", "hr"}
    assert any(len(c.relevance) > 1 for c in cases)  # multi-chunk / multi-document cases exist


def test_labels_point_at_real_authorized_chunks_containing_the_evidence(corpus):
    chunks = corpus.chunk_by_id
    for case in load_frozen(corpus):
        allowed = corpus.authorized_chunk_ids(case.persona)
        for cid in case.relevance:
            assert cid in chunks and cid in allowed, (case.id, cid)
            assert any(ev in chunks[cid].content for ev in case.expected_evidence), (case.id, cid)
        for cid in case.unauthorized_relevant_chunk_ids:
            assert cid not in allowed, (case.id, cid)
        if case.answerable:
            assert case.relevance and not case.unauthorized_relevant_chunk_ids
        else:
            assert not case.relevance


def _broken(mutate):
    rows = read_jsonl(QUESTIONS_PATH)
    mutate(rows)
    return rows


@pytest.mark.parametrize("mutate, message", [
    (lambda r: r[0]["evidence"][0].update(text="this sentence is not in the document"), "not found verbatim"),
    (lambda r: r[0].update(persona="ns_hr"), "may not read"),  # finance doc asked by HR, answerable
    (lambda r: r[1].update(id=r[0]["id"]), "duplicate id"),
    (lambda r: r[0].update(category="vibes"), "unknown category"),
    (lambda r: r[0]["evidence"][0].update(doc="no-such-doc"), "unknown document"),
    (lambda r: r[0].update(expected_facts=["EUR 999,999"]), "expected fact"),
    (lambda r: r[82].update(answerable=True), "no authorized relevant chunk"),
    (lambda r: r[0].pop("question"), "missing field"),
])
def test_malformed_ground_truth_is_rejected(corpus, mutate, message):
    with pytest.raises(DatasetError, match=message):
        build_cases(_broken(mutate), corpus)


def test_hand_edited_frozen_file_is_detected(corpus, tmp_path):
    rows = read_jsonl(FROZEN_PATH)
    rows[0]["relevance"] = {"ns-sup-2026-041:p1:c0": 2}  # sneak in a label not backed by evidence
    edited = tmp_path / "retrieval.jsonl"
    edited.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    with pytest.raises(DatasetError, match="stale or was edited"):
        load_frozen(corpus, path=edited)
