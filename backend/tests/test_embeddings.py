"""Embedding provider tests. `model`-marked tests load the real multilingual-e5-small."""
import re

import numpy as np
import pytest

from backend.app import embeddings
from backend.app.embeddings import Embedder, EmbeddingError, validate_vectors


def _unit(n: int, dim: int = 384) -> np.ndarray:
    v = np.random.default_rng(0).normal(size=(n, dim)).astype(np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


# ---------- no model required ----------

def test_validate_vectors_accepts_normalized_384d():
    assert validate_vectors(_unit(3), expected=3).shape == (3, 384)


@pytest.mark.parametrize("bad", [
    _unit(2, dim=768),                       # wrong dimension
    _unit(3),                                # wrong count (expected 2)
    np.full((2, 384), np.nan, np.float32),   # NaN
    _unit(2) * 3.0,                          # not normalized
])
def test_validate_vectors_rejects_invalid_output(bad):
    with pytest.raises(EmbeddingError):
        validate_vectors(bad, expected=2)


def test_model_with_wrong_dimension_is_rejected(monkeypatch):
    class WrongDim:
        def __init__(self, *args, **kwargs): ...
        def get_sentence_embedding_dimension(self):
            return 768

    import sentence_transformers
    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", WrongDim)
    with pytest.raises(EmbeddingError, match="768-d"):
        Embedder("some/other-model").embed_query("hello")


def test_unloadable_model_raises_explicit_error_not_fake_vectors(monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("no network / model not found")

    import sentence_transformers
    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", broken)
    with pytest.raises(EmbeddingError, match="Could not load"):
        Embedder("missing/model").embed_passages(["text"])


def test_prefixes_are_applied(monkeypatch):
    seen: list[str] = []

    class Recorder:
        def __init__(self, *args, **kwargs): ...
        def get_sentence_embedding_dimension(self):
            return 384
        def encode(self, texts, **kwargs):
            assert kwargs["normalize_embeddings"] is True
            seen.extend(texts)
            return _unit(len(texts))

    import sentence_transformers
    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", Recorder)
    e = Embedder("recorder/model")
    e.embed_query("  when does it expire ")
    e.embed_passages(["Clause 1", "Clause 2"])
    assert seen == ["query: when does it expire", "passage: Clause 1", "passage: Clause 2"]


def test_empty_inputs_are_rejected():
    e = Embedder("never/loaded")
    with pytest.raises(EmbeddingError):
        e.embed_query("   ")
    with pytest.raises(EmbeddingError):
        e.embed_passages(["ok", " "])
    assert e.embed_passages([]).shape == (0, 384)  # nothing to do: model is not even loaded
    assert e._model is None


# ---------- real model ----------

@pytest.mark.model
def test_real_model_produces_normalized_384d_vectors(embedder):
    vectors = embedder.embed_passages(["Ugovor istječe 31. ožujka 2027.", "Notice period: 90 days"])
    assert vectors.shape == (2, 384) and vectors.dtype == np.float32
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-4)
    assert embedder.model_name == "intfloat/multilingual-e5-small"
    assert re.fullmatch(r"[0-9a-f]{40}", embedder.revision), embedder.revision


@pytest.mark.model
def test_query_and_passage_prefixes_change_the_vector(embedder):
    text = "annual value of the supplier agreement"
    q = embedder.embed_query(text)
    p = embedder.embed_passages([text])[0]
    assert not np.allclose(q, p, atol=1e-3)


@pytest.mark.model
def test_batching_is_consistent_with_single_encoding(embedder):
    texts = [f"Clause {i}: the supplier must notify within {i} days." for i in range(40)]
    batched = embedder.embed_passages(texts)
    single = embedder.embed_passages([texts[17]])[0]
    assert np.allclose(batched[17], single, atol=1e-4)


@pytest.mark.model
def test_multilingual_semantic_similarity(embedder):
    query = embedder.embed_query("Kada istječe ugovor s dobavljačem?")  # Croatian question
    passages = embedder.embed_passages([
        "The supplier agreement expires on 31 March 2027 unless renewed.",
        "Employees receive a salary review every February.",
    ])
    sims = passages @ query
    assert sims[0] > sims[1], sims


def test_default_embedder_is_a_lazy_singleton():
    a, b = embeddings.get_embedder(), embeddings.get_embedder()
    assert a is b
