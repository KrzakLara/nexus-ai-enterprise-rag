from __future__ import annotations

from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
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
    # Deferred: never loaded with the user row. On PostgreSQL the runtime role has no SELECT
    # privilege on this column at all; demo login verifies it through nexus_rls.demo_credentials().
    # OIDC users store the unusable marker "!".
    password_hash: Mapped[str] = mapped_column(String(200), deferred=True)
    role: Mapped[str] = mapped_column(String(30), default="member")
    groups: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Phase 4: account state and identity source. Disabled users are denied by the API and by the
    # row-level-security helper functions.
    status: Mapped[str] = mapped_column(String(20), default="active", server_default="active")
    auth_source: Mapped[str] = mapped_column(String(20), default="local", server_default="local")
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=utcnow)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(40), index=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(String(200))
    filename: Mapped[str] = mapped_column(String(255))
    # Server-generated object key of the CURRENT version (never derived from the user's filename).
    storage_key: Mapped[str] = mapped_column(String(300))
    sha256: Mapped[str] = mapped_column(String(64))
    page_count: Mapped[int] = mapped_column(default=0)
    extracted_fields: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Phase 4 lifecycle (docs/object-storage-lifecycle.md):
    #   uploaded -> processing -> ready | failed ; deleted (soft, invisible) -> purged (tombstone)
    lifecycle_state: Mapped[str] = mapped_column(String(20), default="uploaded", server_default="ready")
    storage_backend: Mapped[str] = mapped_column(String(10), default="local", server_default="local")
    content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    current_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[str | None] = mapped_column(String(60), nullable=True)
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retention_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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
Index("ix_documents_retention_until", Document.retention_until)


# ------------------------------------------------------------ Phase 3: document intelligence

class DocumentProcessing(Base):
    """One row per document: parse result (normalized structure), classification, status."""

    __tablename__ = "document_processing"
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(20))  # parsed | processed | failed
    doc_format: Mapped[str] = mapped_column(String(10))
    parser: Mapped[str] = mapped_column(String(200))
    page_count: Mapped[int] = mapped_column(Integer)
    ocr_pages: Mapped[list] = mapped_column(JSON, default=list)
    structure: Mapped[dict] = mapped_column(JSON)
    classification: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DocumentTable(Base):
    """Tables with row/column structure and merged cells (also searchable as chunk text)."""

    __tablename__ = "document_tables"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)  # "<document_id>:<table id>"
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    table_key: Mapped[str] = mapped_column(String(20))
    page_number: Mapped[int] = mapped_column(Integer)
    n_rows: Mapped[int] = mapped_column(Integer)
    n_cols: Mapped[int] = mapped_column(Integer)
    header_rows: Mapped[int] = mapped_column(Integer)
    cells: Mapped[list] = mapped_column(JSON)
    bbox: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source: Mapped[str] = mapped_column(String(20))


class Extraction(Base):
    """The current structured extraction of a document (one per document; re-processing is an
    idempotent upsert). `version` is the optimistic-concurrency token for review actions."""

    __tablename__ = "extractions"
    __table_args__ = (UniqueConstraint("document_id", name="uq_extractions_document"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    tenant_id: Mapped[str] = mapped_column(String(40), index=True)
    doc_type: Mapped[str] = mapped_column(String(20))
    schema_version: Mapped[str] = mapped_column(String(10))
    provider: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(20))  # pending | needs_review | approved | rejected
    version: Mapped[int] = mapped_column(Integer, default=1)
    issues: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reviewed_by: Mapped[str | None] = mapped_column(String(40), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_comment: Mapped[str | None] = mapped_column(Text, nullable=True)


class ExtractionField(Base):
    """One extracted value with its original evidence. A reviewer correction is stored NEXT to the
    original (corrected_*), so the source provenance of the extracted value is never overwritten."""

    __tablename__ = "extraction_fields"
    __table_args__ = (UniqueConstraint("extraction_id", "name", name="uq_extraction_fields_name"),)
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    extraction_id: Mapped[str] = mapped_column(
        ForeignKey("extractions.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(80))
    position: Mapped[int] = mapped_column(Integer)
    value_type: Mapped[str] = mapped_column(String(20))
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    method: Mapped[str] = mapped_column(String(80))
    grounding: Mapped[str] = mapped_column(String(30))
    issues: Mapped[list] = mapped_column(JSON, default=list)
    corrected_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    corrected_evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    corrected_grounding: Mapped[str | None] = mapped_column(String(30), nullable=True)
    corrected_by: Mapped[str | None] = mapped_column(String(40), nullable=True)
    corrected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReviewEvent(Base):
    """Append-only review history (processing, corrections, approvals, rejections)."""

    __tablename__ = "review_events"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    extraction_id: Mapped[str] = mapped_column(
        ForeignKey("extractions.id", ondelete="CASCADE"), index=True
    )
    document_id: Mapped[str] = mapped_column(String(40), index=True)
    tenant_id: Mapped[str] = mapped_column(String(40), index=True)
    user_id: Mapped[str] = mapped_column(String(40))
    action: Mapped[str] = mapped_column(String(30))
    field_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    extraction_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ------------------------------------------------------------ Phase 4: enterprise hardening

class DocumentVersion(Base):
    """One stored original per document version (object storage metadata). The document row holds
    the CURRENT version; older versions stay downloadable until the document is purged."""

    __tablename__ = "document_versions"
    __table_args__ = (UniqueConstraint("document_id", "version", name="uq_document_versions_version"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    tenant_id: Mapped[str] = mapped_column(String(40), index=True)
    version: Mapped[int] = mapped_column(Integer)
    storage_backend: Mapped[str] = mapped_column(String(10))
    storage_key: Mapped[str] = mapped_column(String(300))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    # stored -> active | failed ; active -> superseded ; any -> purged (object deleted)
    state: Mapped[str] = mapped_column(String(20))
    created_by: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    object_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProcessingJob(Base):
    """Durable background job (PostgreSQL-backed queue, claimed with FOR UPDATE SKIP LOCKED).

    The job row is inserted in the SAME transaction as the document change that requires it, so
    there is no window in which a committed upload has no job (a transactional outbox whose outbox
    table is the queue itself). `payload` holds identifiers and options only, never document text.
    """

    __tablename__ = "processing_jobs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_processing_jobs_idempotency"),
        Index("ix_processing_jobs_claim", "status", "run_after", "priority"),
        Index("ix_processing_jobs_tenant_created", "tenant_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(40))
    user_id: Mapped[str] = mapped_column(String(40))
    document_id: Mapped[str | None] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=True, index=True)
    document_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    job_type: Mapped[str] = mapped_column(String(30))  # ingest | reprocess | reindex | purge
    # queued | running | retrying | completed | failed | cancelled
    status: Mapped[str] = mapped_column(String(20))
    stage: Mapped[str | None] = mapped_column(String(30), nullable=True)
    priority: Mapped[int] = mapped_column(SmallInteger, default=100)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    locked_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    trace_context: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkerHeartbeat(Base):
    """Liveness of background workers (no tenant data)."""

    __tablename__ = "worker_heartbeats"
    worker_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    version: Mapped[str] = mapped_column(String(40))
    current_job_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    jobs_completed: Mapped[int] = mapped_column(Integer, default=0)
    jobs_failed: Mapped[int] = mapped_column(Integer, default=0)


class UserIdentity(Base):
    """Binding of an external OIDC identity (issuer + subject) to exactly one local user. The
    user's tenant is fixed at the first login; a later token asserting another tenant is refused."""

    __tablename__ = "user_identities"
    __table_args__ = (UniqueConstraint("issuer", "subject", name="uq_user_identities_issuer_subject"),)
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    tenant_id: Mapped[str] = mapped_column(String(40), index=True)
    issuer: Mapped[str] = mapped_column(String(300))
    subject: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(150), nullable=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuthSession(Base):
    """Server-side browser session (BFF). The cookie holds a random 256-bit token; only its
    SHA-256 is stored. Refresh and ID tokens are AES-GCM encrypted at rest."""

    __tablename__ = "auth_sessions"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    tenant_id: Mapped[str] = mapped_column(String(40))
    auth_mode: Mapped[str] = mapped_column(String(10))  # demo | oidc
    csrf_token_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revalidate_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    issuer: Mapped[str | None] = mapped_column(String(300), nullable=True)
    idp_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    idp_session_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    refresh_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    id_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)


class AuthLoginState(Base):
    """Pending OIDC authorization request (state, nonce, PKCE verifier). Single use, short-lived,
    pre-authentication and tenant-less, therefore intentionally outside row-level security."""

    __tablename__ = "auth_login_states"
    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    nonce: Mapped[str] = mapped_column(String(128))
    code_verifier_enc: Mapped[str] = mapped_column(Text)
    browser_binding_hash: Mapped[str] = mapped_column(String(64))
    return_to: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
