"""Phase 3 document intelligence: parse results, tables, extractions, fields, review history.

New tables only; no existing table or column changes, so Phase 1/2 data, document IDs, chunk IDs and
citations are untouched. Downgrade drops exactly these tables.

Revision ID: 0004_document_intelligence
Revises: 0003_chunk_fulltext_search
Create Date: 2026-11-14
"""
import sqlalchemy as sa
from alembic import op

revision = "0004_document_intelligence"
down_revision = "0003_chunk_fulltext_search"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "document_processing",
        sa.Column("document_id", sa.String(40), sa.ForeignKey("documents.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("doc_format", sa.String(10), nullable=False),
        sa.Column("parser", sa.String(200), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("ocr_pages", sa.JSON(), nullable=False),
        sa.Column("structure", sa.JSON(), nullable=False),
        sa.Column("classification", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "document_tables",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("document_id", sa.String(40), sa.ForeignKey("documents.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("table_key", sa.String(20), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("n_rows", sa.Integer(), nullable=False),
        sa.Column("n_cols", sa.Integer(), nullable=False),
        sa.Column("header_rows", sa.Integer(), nullable=False),
        sa.Column("cells", sa.JSON(), nullable=False),
        sa.Column("bbox", sa.JSON(), nullable=True),
        sa.Column("source", sa.String(20), nullable=False),
    )
    op.create_index("ix_document_tables_document_id", "document_tables", ["document_id"])
    op.create_table(
        "extractions",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("document_id", sa.String(40), sa.ForeignKey("documents.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("doc_type", sa.String(20), nullable=False),
        sa.Column("schema_version", sa.String(10), nullable=False),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("issues", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_by", sa.String(40), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_comment", sa.Text(), nullable=True),
        sa.UniqueConstraint("document_id", name="uq_extractions_document"),
    )
    op.create_index("ix_extractions_tenant_id", "extractions", ["tenant_id"])
    op.create_table(
        "extraction_fields",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("extraction_id", sa.String(40), sa.ForeignKey("extractions.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("value_type", sa.String(20), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("raw", sa.Text(), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("method", sa.String(80), nullable=False),
        sa.Column("grounding", sa.String(30), nullable=False),
        sa.Column("issues", sa.JSON(), nullable=False),
        sa.Column("corrected_value", sa.Text(), nullable=True),
        sa.Column("corrected_evidence", sa.JSON(), nullable=True),
        sa.Column("corrected_grounding", sa.String(30), nullable=True),
        sa.Column("corrected_by", sa.String(40), nullable=True),
        sa.Column("corrected_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("extraction_id", "name", name="uq_extraction_fields_name"),
    )
    op.create_index("ix_extraction_fields_extraction_id", "extraction_fields", ["extraction_id"])
    op.create_table(
        "review_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("extraction_id", sa.String(40), sa.ForeignKey("extractions.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("document_id", sa.String(40), nullable=False),
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("user_id", sa.String(40), nullable=False),
        sa.Column("action", sa.String(30), nullable=False),
        sa.Column("field_name", sa.String(80), nullable=True),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("extraction_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("extraction_id", "document_id", "tenant_id"):
        op.create_index(f"ix_review_events_{column}", "review_events", [column])


def downgrade() -> None:
    op.drop_table("review_events")
    op.drop_table("extraction_fields")
    op.drop_table("extractions")
    op.drop_table("document_tables")
    op.drop_table("document_processing")
