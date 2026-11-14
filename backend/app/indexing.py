"""Idempotent chunk-embedding indexing (used by upload, seeding and the backfill script)."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

import numpy as np
from sqlalchemy import and_, delete, select
from sqlalchemy.orm import Session

from .models import EMBEDDING_DIM, Chunk, ChunkEmbedding


class PassageEmbedder(Protocol):
    model_name: str

    @property
    def revision(self) -> str: ...

    def embed_passages(self, texts: list[str]) -> np.ndarray: ...


@dataclass
class IndexStats:
    candidates: int = 0
    embedded: int = 0
    skipped_up_to_date: int = 0


def content_hash(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def _pending_chunks_query(model_name: str, document_ids: list[str] | None = None):
    """Chunks that have no embedding for `model_name`, or whose stored hash is stale."""
    q = (
        select(Chunk, ChunkEmbedding.content_sha256)
        .outerjoin(
            ChunkEmbedding,
            and_(ChunkEmbedding.chunk_id == Chunk.id, ChunkEmbedding.model_name == model_name),
        )
        .order_by(Chunk.document_id, Chunk.page_number, Chunk.position)
    )
    if document_ids is not None:
        q = q.where(Chunk.document_id.in_(document_ids))
    return q


def embed_pending_chunks(
    db: Session,
    embedder: PassageEmbedder,
    document_ids: list[str] | None = None,
    batch_size: int = 64,
    dry_run: bool = False,
) -> IndexStats:
    """Embed every chunk lacking an up-to-date vector for this model. Safe to re-run.

    Caller controls the transaction; this function only flushes. Raises EmbeddingError from the
    embedder unchanged - nothing is written for a failed batch.
    """
    stats = IndexStats()
    pending: list[Chunk] = []
    for chunk, stored_hash in db.execute(_pending_chunks_query(embedder.model_name, document_ids)):
        stats.candidates += 1
        if stored_hash is not None and stored_hash == content_hash(chunk.content):
            stats.skipped_up_to_date += 1
        else:
            pending.append(chunk)
    if dry_run or not pending:
        return stats

    revision = embedder.revision
    for start in range(0, len(pending), batch_size):
        batch = pending[start:start + batch_size]
        vectors = embedder.embed_passages([c.content for c in batch])
        if vectors.shape != (len(batch), EMBEDDING_DIM):
            raise ValueError(f"Embedder returned shape {vectors.shape} for {len(batch)} chunks")
        ids = [c.id for c in batch]
        db.execute(
            delete(ChunkEmbedding).where(
                ChunkEmbedding.chunk_id.in_(ids), ChunkEmbedding.model_name == embedder.model_name
            )
        )
        db.add_all(
            ChunkEmbedding(
                chunk_id=chunk.id,
                model_name=embedder.model_name,
                model_revision=revision,
                embedding_dim=EMBEDDING_DIM,
                normalized=True,
                content_sha256=content_hash(chunk.content),
                embedding=[float(x) for x in vector],
            )
            for chunk, vector in zip(batch, vectors, strict=True)
        )
        db.flush()
        stats.embedded += len(batch)
    return stats


def embedding_coverage(db: Session, model_name: str) -> tuple[int, int]:
    """(chunks with an embedding for model_name, total chunks)."""
    total = db.query(Chunk).count()
    covered = (
        db.query(ChunkEmbedding)
        .join(Chunk, Chunk.id == ChunkEmbedding.chunk_id)
        .where(ChunkEmbedding.model_name == model_name)
        .count()
    )
    return covered, total


__all__ = ["IndexStats", "content_hash", "embed_pending_chunks", "embedding_coverage"]
