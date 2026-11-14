"""PostgreSQL full-text search: generated chunks.content_tsv ('simple' config) + GIN index.

`simple` = lowercase + split on the default parser, no stemming and no stop words. It treats
English and Croatian identically (diacritics preserved, e.g. 'istječe'), keeps business
identifiers (SILVER-IBIS-73 -> 'silver-ibis' <-> 'silver' <-> 'ibis' <-> '73'), but does NOT
match inflected forms ('ugovor' vs 'ugovora') or unaccented spellings ('istjece').

The column is GENERATED ... STORED, so every insert/update of chunks.content keeps it in sync and
existing rows are populated by the ALTER itself. PostgreSQL only: SQLite has no tsvector, so on
SQLite this revision is a no-op (lexical FTS mode is refused there with HTTP 409).

Revision ID: 0003_chunk_fulltext_search
Revises: 0002_pgvector_embeddings
Create Date: 2026-11-11
"""
from alembic import op

revision = "0003_chunk_fulltext_search"
down_revision = "0002_pgvector_embeddings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        "ALTER TABLE chunks ADD COLUMN content_tsv tsvector "
        "GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, content)) STORED"
    )
    op.execute("CREATE INDEX ix_chunks_content_tsv ON chunks USING gin (content_tsv)")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP INDEX IF EXISTS ix_chunks_content_tsv")
    op.execute("ALTER TABLE chunks DROP COLUMN IF EXISTS content_tsv")
