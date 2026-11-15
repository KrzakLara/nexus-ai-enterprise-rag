"""Phase 4D object storage and document lifecycle: storage / lifecycle columns on `documents` and
a `document_versions` table.

Existing documents are NOT moved or modified in place: they keep their ids, chunks, citations and
their local original file. They get lifecycle_state='ready', storage_backend='local',
current_version=1 and one `document_versions` row (version 1) that points at the same local
storage key. Copying originals into S3 / MinIO is a separate, explicit, verifiable operation
(scripts/storage_backfill.py, dry-run by default; local files are never deleted by it).

storage_key is widened from 100 to 300 characters (server-generated object keys).

Revision ID: 0006_storage_lifecycle
Revises: 0005_identity_sessions
Create Date: 2026-11-15
"""
import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath

import sqlalchemy as sa
from alembic import op

revision = "0006_storage_lifecycle"
down_revision = "0005_identity_sessions"
branch_labels = None
depends_on = None

CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".txt": "text/plain",
    ".md": "text/markdown",
}


def upgrade() -> None:
    with op.batch_alter_table("documents") as batch:
        batch.alter_column("storage_key", type_=sa.String(300), existing_type=sa.String(100),
                           existing_nullable=False)
        batch.add_column(sa.Column("lifecycle_state", sa.String(20), nullable=False, server_default="ready"))
        batch.add_column(sa.Column("storage_backend", sa.String(10), nullable=False, server_default="local"))
        batch.add_column(sa.Column("content_type", sa.String(100), nullable=True))
        batch.add_column(sa.Column("size_bytes", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("current_version", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("deleted_by", sa.String(60), nullable=True))
        batch.add_column(sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("retention_until", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_documents_retention_until", "documents", ["retention_until"])

    versions = op.create_table(
        "document_versions",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("document_id", sa.String(40), sa.ForeignKey("documents.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("storage_backend", sa.String(10), nullable=False),
        sa.Column("storage_key", sa.String(300), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("content_type", sa.String(100), nullable=True),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("created_by", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("object_deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("document_id", "version", name="uq_document_versions_version"),
    )
    op.create_index("ix_document_versions_document_id", "document_versions", ["document_id"])
    op.create_index("ix_document_versions_tenant_id", "document_versions", ["tenant_id"])

    # Backfill: version 1 for every existing document (deterministic id, same local storage key).
    bind = op.get_bind()
    rows = bind.execute(sa.text(
        "SELECT id, tenant_id, owner_id, filename, storage_key, sha256, created_at FROM documents")).all()
    now = datetime.now(timezone.utc)
    batch_rows = []
    for doc_id, tenant_id, owner_id, filename, storage_key, digest, created_at in rows:
        suffix = PurePosixPath(filename or "").suffix.lower()
        batch_rows.append({
            "id": str(uuid.UUID(hashlib.md5(f"{doc_id}:v1".encode()).hexdigest())),
            "document_id": doc_id, "tenant_id": tenant_id, "version": 1, "storage_backend": "local",
            "storage_key": storage_key, "sha256": digest, "size_bytes": None,
            "content_type": CONTENT_TYPES.get(suffix), "original_filename": filename,
            "state": "active", "created_by": owner_id, "created_at": created_at or now,
            "object_deleted_at": None,
        })
    if batch_rows:
        op.bulk_insert(versions, batch_rows)
        bind.execute(sa.text("UPDATE documents SET content_type = :ct WHERE id = :id"),
                     [{"ct": r["content_type"], "id": r["document_id"]} for r in batch_rows])


def downgrade() -> None:
    op.drop_table("document_versions")
    op.drop_index("ix_documents_retention_until", table_name="documents")
    with op.batch_alter_table("documents") as batch:
        for column in ("retention_until", "purged_at", "deleted_by", "deleted_at", "updated_at",
                       "current_version", "size_bytes", "content_type", "storage_backend",
                       "lifecycle_state"):
            batch.drop_column(column)
        batch.alter_column("storage_key", type_=sa.String(100), existing_type=sa.String(300),
                           existing_nullable=False)
