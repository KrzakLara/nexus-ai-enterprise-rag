"""pgvector extension, persistent chunk embeddings (vector(384)) and integrity constraints.

No ANN (HNSW/IVFFlat) index on purpose: v0.2 uses exact, permission-scoped cosine search.

Revision ID: 0002_pgvector_embeddings
Revises: 0001_v01_baseline
Create Date: 2026-11-11
"""
import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0002_pgvector_embeddings"
down_revision = "0001_v01_baseline"
branch_labels = None
depends_on = None

EMBEDDING_DIM = 384


def upgrade() -> None:
    is_pg = op.get_bind().dialect.name == "postgresql"
    if is_pg:
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # Remove exact duplicates (never produced by v0.1 code paths, but enforce before constraining).
    op.execute(
        "DELETE FROM document_grants WHERE id NOT IN "
        "(SELECT MIN(id) FROM document_grants GROUP BY document_id, group_name)"
    )
    with op.batch_alter_table("document_grants") as batch:
        batch.create_unique_constraint(
            "uq_document_grants_document_group", ["document_id", "group_name"]
        )
    with op.batch_alter_table("chunks") as batch:
        batch.create_unique_constraint(
            "uq_chunks_document_page_position", ["document_id", "page_number", "position"]
        )

    op.create_table(
        "chunk_embeddings",
        sa.Column(
            "chunk_id", sa.String(40),
            sa.ForeignKey("chunks.id", ondelete="CASCADE"), primary_key=True,
        ),
        sa.Column("model_name", sa.String(200), primary_key=True),
        sa.Column("model_revision", sa.String(64), nullable=False),
        sa.Column("embedding_dim", sa.Integer(), nullable=False),
        sa.Column("normalized", sa.Boolean(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column(
            "embedding", Vector(EMBEDDING_DIM) if is_pg else sa.JSON(), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"embedding_dim = {EMBEDDING_DIM}", name="ck_chunk_embeddings_dim"),
    )
    op.create_index("ix_chunk_embeddings_model", "chunk_embeddings", ["model_name"])


def downgrade() -> None:
    op.drop_index("ix_chunk_embeddings_model", table_name="chunk_embeddings")
    op.drop_table("chunk_embeddings")
    with op.batch_alter_table("chunks") as batch:
        batch.drop_constraint("uq_chunks_document_page_position", type_="unique")
    with op.batch_alter_table("document_grants") as batch:
        batch.drop_constraint("uq_document_grants_document_group", type_="unique")
    # The vector extension is left installed (it may be used by other schemas).
