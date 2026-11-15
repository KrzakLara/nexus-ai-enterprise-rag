"""Phase 4A identity: account state on users, OIDC identity bindings, server-side sessions and
pending OIDC login states.

Additive only: new nullable / defaulted columns on `users` and three new tables. Existing users
keep their ids, tenants, roles, groups and demo password hashes (status='active',
auth_source='local').

Revision ID: 0005_identity_sessions
Revises: 0004_document_intelligence
Create Date: 2026-11-15
"""
import sqlalchemy as sa
from alembic import op

revision = "0005_identity_sessions"
down_revision = "0004_document_intelligence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("status", sa.String(20), nullable=False, server_default="active"))
    op.add_column("users", sa.Column("auth_source", sa.String(20), nullable=False, server_default="local"))
    op.add_column("users", sa.Column("display_name", sa.String(200), nullable=True))
    op.add_column("users", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "user_identities",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(40), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("issuer", sa.String(300), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("email", sa.String(150), nullable=True),
        sa.Column("email_verified", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("issuer", "subject", name="uq_user_identities_issuer_subject"),
    )
    op.create_index("ix_user_identities_user_id", "user_identities", ["user_id"])
    op.create_index("ix_user_identities_tenant_id", "user_identities", ["tenant_id"])

    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(40), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("auth_mode", sa.String(10), nullable=False),
        sa.Column("csrf_token_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revalidate_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("issuer", sa.String(300), nullable=True),
        sa.Column("idp_subject", sa.String(255), nullable=True),
        sa.Column("idp_session_id", sa.String(255), nullable=True),
        sa.Column("refresh_token_enc", sa.Text(), nullable=True),
        sa.Column("id_token_enc", sa.Text(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(40), nullable=True),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_index("ix_auth_sessions_idp_session_id", "auth_sessions", ["idp_session_id"])

    op.create_table(
        "auth_login_states",
        sa.Column("state_hash", sa.String(64), primary_key=True),
        sa.Column("nonce", sa.String(128), nullable=False),
        sa.Column("code_verifier_enc", sa.Text(), nullable=False),
        sa.Column("browser_binding_hash", sa.String(64), nullable=False),
        sa.Column("return_to", sa.String(500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_auth_login_states_expires_at", "auth_login_states", ["expires_at"])


def downgrade() -> None:
    op.drop_table("auth_login_states")
    op.drop_table("auth_sessions")
    op.drop_table("user_identities")
    with op.batch_alter_table("users") as batch:
        for column in ("created_at", "last_login_at", "display_name", "auth_source", "status"):
            batch.drop_column(column)
