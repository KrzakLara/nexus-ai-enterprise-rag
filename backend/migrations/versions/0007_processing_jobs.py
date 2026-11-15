"""Phase 4C durable background processing: `processing_jobs` (PostgreSQL-backed queue) and
`worker_heartbeats`.

New tables only. Existing documents are already processed and get no jobs.

Revision ID: 0007_processing_jobs
Revises: 0006_storage_lifecycle
Create Date: 2026-11-15
"""
import sqlalchemy as sa
from alembic import op

revision = "0007_processing_jobs"
down_revision = "0006_storage_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "processing_jobs",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("user_id", sa.String(40), nullable=False),
        sa.Column("document_id", sa.String(40), sa.ForeignKey("documents.id", ondelete="CASCADE"),
                  nullable=True),
        sa.Column("document_version", sa.Integer(), nullable=True),
        sa.Column("job_type", sa.String(30), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("stage", sa.String(30), nullable=True),
        sa.Column("priority", sa.SmallInteger(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_by", sa.String(120), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_token", sa.String(64), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.String(200), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(40), nullable=True),
        sa.Column("error_message", sa.String(500), nullable=True),
        sa.Column("trace_context", sa.JSON(), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_processing_jobs_idempotency"),
    )
    op.create_index("ix_processing_jobs_claim", "processing_jobs", ["status", "run_after", "priority"])
    op.create_index("ix_processing_jobs_tenant_created", "processing_jobs", ["tenant_id", "created_at"])
    op.create_index("ix_processing_jobs_document_id", "processing_jobs", ["document_id"])

    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.String(120), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.String(40), nullable=False),
        sa.Column("current_job_id", sa.String(40), nullable=True),
        sa.Column("jobs_completed", sa.Integer(), nullable=False),
        sa.Column("jobs_failed", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("worker_heartbeats")
    op.drop_table("processing_jobs")
