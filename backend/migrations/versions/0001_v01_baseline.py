"""v0.1 baseline schema (identical to what Base.metadata.create_all produced in v0.1).

Existing v0.1 databases created by create_all() are detected by backend.app.migrate and stamped
at this revision instead of being re-created.

Revision ID: 0001_v01_baseline
Revises:
Create Date: 2026-11-11
"""
import sqlalchemy as sa
from alembic import op

revision = "0001_v01_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("email", sa.String(150), nullable=False),
        sa.Column("password_hash", sa.String(200), nullable=False),
        sa.Column("role", sa.String(30), nullable=False),
        sa.Column("groups", sa.JSON(), nullable=False),
        sa.UniqueConstraint("email"),
    )
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])

    op.create_table(
        "documents",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("owner_id", sa.String(40), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("storage_key", sa.String(100), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("extracted_fields", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_documents_tenant_id", "documents", ["tenant_id"])
    op.create_index("ix_documents_tenant_owner", "documents", ["tenant_id", "owner_id"])

    op.create_table(
        "document_grants",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "document_id", sa.String(40),
            sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("group_name", sa.String(60), nullable=False),
    )
    op.create_index("ix_document_grants_document_id", "document_grants", ["document_id"])
    op.create_index("ix_document_grants_group_name", "document_grants", ["group_name"])

    op.create_table(
        "chunks",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column(
            "document_id", sa.String(40),
            sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
    )
    op.create_index("ix_chunks_document_id", "chunks", ["document_id"])

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(40), nullable=False),
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("document_id", sa.String(40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_events_user_id", "audit_events", ["user_id"])
    op.create_index("ix_audit_events_tenant_id", "audit_events", ["tenant_id"])


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("chunks")
    op.drop_table("document_grants")
    op.drop_table("documents")
    op.drop_table("users")
