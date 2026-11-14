"""Settings for the local demo. No deployed/SSO mode is implemented yet."""
import os
from dataclasses import dataclass
from pathlib import Path


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).lower() in {"1", "true", "yes"}


def _opt_float(name: str, default: str) -> float | None:
    """Float setting where 'none' (or empty) disables the corresponding rule."""
    value = os.getenv(name, default).strip().lower()
    return None if value in {"", "none", "off"} else float(value)


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./data/nexus.db")
    storage_dir: Path = Path(os.getenv("STORAGE_DIR", "./data/uploads"))
    demo_mode: bool = _flag("DEMO_MODE", "true")
    jwt_secret: str = os.getenv("JWT_SECRET", "local-demo-secret-not-for-production")
    max_upload_bytes: int = int(os.getenv("MAX_UPLOAD_MB", "5")) * 1024 * 1024
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    # Local embedding model (no API key required). Dimension is fixed by the DB schema.
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "intfloat/multilingual-e5-small")
    embedding_model_revision: str | None = os.getenv("EMBEDDING_MODEL_REVISION") or None
    embedding_device: str = os.getenv("EMBEDDING_DEVICE", "cpu")
    embedding_batch_size: int = int(os.getenv("EMBEDDING_BATCH_SIZE", "32"))
    embed_on_upload: bool = _flag("EMBED_ON_UPLOAD", "true")
    # Load the model at API startup (PostgreSQL only) instead of on the first semantic query.
    embedding_warmup: bool = _flag("EMBEDDING_WARMUP", "true")
    # Hybrid retrieval (PostgreSQL FTS + pgvector, fused with Reciprocal Rank Fusion)
    retrieval_candidates: int = int(os.getenv("RETRIEVAL_CANDIDATES", "50"))  # per branch
    rrf_k: int = int(os.getenv("RRF_K", "60"))
    rrf_weight_lexical: float = float(os.getenv("RRF_WEIGHT_LEXICAL", "1.0"))
    rrf_weight_semantic: float = float(os.getenv("RRF_WEIGHT_SEMANTIC", "1.0"))
    # Evidence-sufficiency policy for semantic / hybrid answers (see backend/app/evidence.py).
    # Defaults = policy selected on the DEV split by scripts/evaluate.py (eval/calibration.json;
    # a unit test fails if they drift). Cosine thresholds are heuristics, not probabilities.
    evidence_policy_enabled: bool = _flag("EVIDENCE_POLICY_ENABLED", "true")
    evidence_min_similarity: float | None = _opt_float("EVIDENCE_MIN_SIMILARITY", "none")
    evidence_min_sentence_similarity: float | None = _opt_float("EVIDENCE_MIN_SENTENCE_SIMILARITY", "0.835")
    evidence_min_term_coverage: float | None = _opt_float("EVIDENCE_MIN_TERM_COVERAGE", "none")
    evidence_require_identifier: bool = _flag("EVIDENCE_REQUIRE_IDENTIFIER", "true")
    evidence_accept_agreement: bool = _flag("EVIDENCE_ACCEPT_AGREEMENT", "false")


settings = Settings()
