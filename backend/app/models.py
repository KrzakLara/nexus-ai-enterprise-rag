from __future__ import annotations

from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base

# Fixed by the schema (vector(384)) and by intfloat/multilingual-e5-small.
EMBEDDING_DIM = 384


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(40), index=True)
    email: Mapped[str] = mapped_column(String(150), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(30), default="member")
    groups: Mapped[list[str]] = mapped_column(JSON, default=list)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(40), index=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(String(200))
    filename: Mapped[str] = mapped_column(String(255))
    storage_key: Mapped[str] = mapped_column(String(100))
    sha256: Mapped[str] = mapped_column(String(64))
    page_count: Mapped[int] = mapped_column(default=0)
    extracted_fields: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DocumentGrant(Base):
    __tablename__ = "document_grants"
    __table_args__ = (
        UniqueConstraint("document_id", "group_name", name="uq_document_grants_document_group"),
    )
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    group_name: Mapped[str] = mapped_column(String(60), index=True)


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "page_number", "position", name="uq_chunks_document_page_position"),
    )
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    page_number: Mapped[int] = mapped_column()
    position: Mapped[int] = mapped_column()
    content: Mapped[str] = mapped_column(Text)


class ChunkEmbedding(Base):
    """One persisted embedding per (chunk, model). Stored as pgvector vector(384) on PostgreSQL.

    SQLite (lightweight tests only) stores the vector as JSON; similarity search is PostgreSQL-only.
    """

    __tablename__ = "chunk_embeddings"
    __table_args__ = (
        CheckConstraint(f"embedding_dim = {EMBEDDING_DIM}", name="ck_chunk_embeddings_dim"),
        Index("ix_chunk_embeddings_model", "model_name"),
    )
    chunk_id: Mapped[str] = mapped_column(
        ForeignKey("chunks.id", ondelete="CASCADE"), primary_key=True
    )
    model_name: Mapped[str] = mapped_column(String(200), primary_key=True)
    model_revision: Mapped[str] = mapped_column(String(64))
    embedding_dim: Mapped[int] = mapped_column(Integer)
    normalized: Mapped[bool] = mapped_column(Boolean, default=True)
    # sha256 of the exact text that was embedded (without the "passage: " prefix):
    # makes re-runs idempotent and detects stale vectors after content changes.
    content_sha256: Mapped[str] = mapped_column(String(64))
    embedding: Mapped[list[float]] = mapped_column(
        Vector(EMBEDDING_DIM).with_variant(JSON(), "sqlite")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(40), index=True)
    tenant_id: Mapped[str] = mapped_column(String(40), index=True)
    action: Mapped[str] = mapped_column(String(50))
    document_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


Index("ix_documents_tenant_owner", Document.tenant_id, Document.owner_id)
