"""Local sentence embeddings with intfloat/multilingual-e5-small (no API key, runs on CPU).

E5 models are trained with asymmetric prefixes: documents are encoded as "passage: ..." and
search requests as "query: ...". Vectors are L2-normalized so cosine distance == 1 - dot product.

There is deliberately NO fallback: if the model cannot be loaded or returns unexpected output,
an EmbeddingError is raised instead of producing placeholder vectors.
"""
from __future__ import annotations

import threading
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path

import numpy as np

from .config import settings
from .models import EMBEDDING_DIM

QUERY_PREFIX = "query: "
PASSAGE_PREFIX = "passage: "
# Models trained WITHOUT asymmetric prefixes (prefixing would hurt them). Everything else is
# treated as E5-style. Used for the Phase 2d embedding comparison; the default model is E5.
NO_PREFIX_MODELS = {"sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"}


class EmbeddingError(RuntimeError):
    """The embedding model is unavailable or produced invalid vectors."""


def _resolve_revision(model_name: str, requested: str | None) -> str:
    """Return the Hugging Face commit hash of the loaded snapshot (offline, from the HF cache)."""
    if requested:
        return requested[:64]
    try:
        from huggingface_hub import try_to_load_from_cache

        cached = try_to_load_from_cache(model_name, "config.json")
        if isinstance(cached, str):
            parts = Path(cached).parts
            if "snapshots" in parts:
                return parts[parts.index("snapshots") + 1][:64]
    except Exception:  # noqa: BLE001 - metadata lookup must not hide a loaded model
        pass
    if Path(model_name).exists():
        return "local-path"
    return "unknown"


class Embedder:
    def __init__(
        self,
        model_name: str = settings.embedding_model,
        revision: str | None = settings.embedding_model_revision,
        device: str = settings.embedding_device,
        batch_size: int = settings.embedding_batch_size,
    ) -> None:
        self.model_name = model_name
        self.requested_revision = revision
        self.device = device
        self.batch_size = batch_size
        self.dimension = EMBEDDING_DIM
        prefixed = model_name not in NO_PREFIX_MODELS
        self.query_prefix = QUERY_PREFIX if prefixed else ""
        self.passage_prefix = PASSAGE_PREFIX if prefixed else ""
        self._model = None
        self._revision: str | None = None
        self._lock = threading.Lock()

    def _load(self):
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is None:
                try:
                    from sentence_transformers import SentenceTransformer

                    model = SentenceTransformer(
                        self.model_name, device=self.device, revision=self.requested_revision
                    )
                except Exception as exc:
                    raise EmbeddingError(
                        f"Could not load embedding model '{self.model_name}': {exc}"
                    ) from exc
                get_dim = getattr(model, "get_embedding_dimension", None) or getattr(
                    model, "get_sentence_embedding_dimension"
                )  # renamed in sentence-transformers 5.x
                dim = get_dim()
                if dim != EMBEDDING_DIM:
                    raise EmbeddingError(
                        f"Model '{self.model_name}' produces {dim}-d vectors; schema requires "
                        f"{EMBEDDING_DIM}-d (vector({EMBEDDING_DIM}))."
                    )
                self._revision = _resolve_revision(self.model_name, self.requested_revision)
                self._model = model
        return self._model

    def resource_profile(self) -> dict:
        """Parameter count / in-memory weight size of the loaded model (for cost reporting)."""
        model = self._load()
        params = list(model.parameters())
        return {"parameters": sum(p.numel() for p in params),
                "weights_mb": round(sum(p.numel() * p.element_size() for p in params) / 2**20, 1)}

    @property
    def revision(self) -> str:
        self._load()
        assert self._revision is not None
        return self._revision

    def _encode(self, texts: list[str]) -> np.ndarray:
        model = self._load()
        try:
            vectors = model.encode(
                texts,
                batch_size=self.batch_size,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
        except Exception as exc:
            raise EmbeddingError(f"Embedding generation failed: {exc}") from exc
        return validate_vectors(np.asarray(vectors, dtype=np.float32), expected=len(texts))

    def embed_passages(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.empty((0, EMBEDDING_DIM), dtype=np.float32)
        if any(not t or not t.strip() for t in texts):
            raise EmbeddingError("Cannot embed an empty passage")
        return self._encode([self.passage_prefix + t for t in texts])

    def embed_query(self, text: str) -> np.ndarray:
        if not text or not text.strip():
            raise EmbeddingError("Cannot embed an empty query")
        return self._encode([self.query_prefix + text.strip()])[0]


def validate_vectors(vectors: np.ndarray, expected: int) -> np.ndarray:
    if vectors.ndim != 2 or vectors.shape != (expected, EMBEDDING_DIM):
        raise EmbeddingError(
            f"Expected embeddings of shape ({expected}, {EMBEDDING_DIM}), got {vectors.shape}"
        )
    if not np.isfinite(vectors).all():
        raise EmbeddingError("Embedding contains NaN or infinite values")
    norms = np.linalg.norm(vectors, axis=1)
    if not np.allclose(norms, 1.0, atol=1e-3):
        raise EmbeddingError("Embeddings are not L2-normalized")
    return vectors


@lru_cache(maxsize=1)
def _default_embedder() -> Embedder:
    return Embedder()


def get_embedder() -> Embedder:
    """FastAPI dependency / process-wide singleton (model is loaded lazily on first use)."""
    return _default_embedder()
