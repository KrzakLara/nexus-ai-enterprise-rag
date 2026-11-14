-- Enables pgvector on first container start. Alembic migration 0002 also runs
-- CREATE EXTENSION IF NOT EXISTS vector, so databases created elsewhere work too.
CREATE EXTENSION IF NOT EXISTS vector;
