"""Idempotency/staleness logic of the indexing service (SQLite + a recording test double).

The double below exists ONLY to observe which chunks get embedded; the production code path has
no fake-embedding fallback (see test_embeddings.py). Real vectors are tested in test_pgvector.py.
"""
import numpy as np
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.database import Base
from backend.app.embeddings import EmbeddingError
from backend.app.indexing import content_hash, embed_pending_chunks, embedding_coverage
from backend.app.models import Chunk, ChunkEmbedding

from .conftest import seed


class RecordingEmbedder:
    model_name = "test/recording"
    revision = "rev-test"

    def __init__(self, fail: bool = False):
        self.calls: list[list[str]] = []
        self.fail = fail

    def embed_passages(self, texts):
        if self.fail:
            raise EmbeddingError("model unavailable")
        self.calls.append(list(texts))
        v = np.ones((len(texts), 384), dtype=np.float32)
        return v / np.linalg.norm(v, axis=1, keepdims=True)


@pytest.fixture
def factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    f = sessionmaker(engine, expire_on_commit=False)
    seed(f)
    yield f
    engine.dispose()


def test_backfill_embeds_all_then_is_idempotent(factory):
    e = RecordingEmbedder()
    with factory() as db:
        first = embed_pending_chunks(db, e, batch_size=2)
        db.commit()
        assert (first.candidates, first.embedded) == (3, 3)
        assert [len(c) for c in e.calls] == [2, 1]  # batched
        second = embed_pending_chunks(db, e)
        db.commit()
        assert (second.embedded, second.skipped_up_to_date) == (0, 3)
        assert embedding_coverage(db, e.model_name) == (3, 3)


def test_metadata_is_persisted(factory):
    e = RecordingEmbedder()
    with factory() as db:
        embed_pending_chunks(db, e)
        db.commit()
        row = db.scalar(select(ChunkEmbedding).where(ChunkEmbedding.chunk_id == "d_fin_c0"))
        chunk = db.get(Chunk, "d_fin_c0")
        assert row.model_name == "test/recording" and row.model_revision == "rev-test"
        assert row.embedding_dim == 384 and row.normalized is True
        assert row.content_sha256 == content_hash(chunk.content)
        assert len(row.embedding) == 384


def test_changed_content_is_re_embedded_once(factory):
    e = RecordingEmbedder()
    with factory() as db:
        embed_pending_chunks(db, e)
        db.get(Chunk, "d_hr_c0").content = "Updated compensation text."
        db.commit()
        stats = embed_pending_chunks(db, e)
        db.commit()
        assert stats.embedded == 1 and e.calls[-1] == ["Updated compensation text."]
        assert db.query(ChunkEmbedding).count() == 3  # replaced, not duplicated


def test_scoped_to_document_and_dry_run_writes_nothing(factory):
    e = RecordingEmbedder()
    with factory() as db:
        dry = embed_pending_chunks(db, e, dry_run=True)
        assert dry.candidates == 3 and dry.embedded == 0 and not e.calls
        scoped = embed_pending_chunks(db, e, document_ids=["d_fin"])
        assert scoped.embedded == 1 and e.calls == [[db.get(Chunk, "d_fin_c0").content]]


def test_model_failure_propagates_and_writes_nothing(factory):
    with factory() as db:
        with pytest.raises(EmbeddingError):
            embed_pending_chunks(db, RecordingEmbedder(fail=True))
        db.rollback()
        assert db.query(ChunkEmbedding).count() == 0
