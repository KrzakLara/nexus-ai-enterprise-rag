"""Application settings (environment variables) and fail-closed security validation.

Two authentication modes are strictly separated (Phase 4A):

    AUTH_MODE=demo   synthetic demo personas, password login, HS256 demo JWT. Allowed ONLY when
                     APP_ENV is local / development / test. Never in staging or production.
    AUTH_MODE=oidc   OpenID Connect Authorization Code Flow + PKCE through the backend (BFF),
                     server-side sessions in HttpOnly cookies, RS256/PS256/ES256 token validation.

`validate_settings()` returns every configuration error; `assert_secure_configuration()` raises
on any of them, and the API and the worker call it at startup. Incomplete security configuration
therefore stops the process instead of silently falling back to demo credentials.

`DEMO_MODE` is the legacy (v0.1-v0.3) switch. It only chooses the DEFAULT of AUTH_MODE; it can
never enable demo authentication in a production-like environment.
"""
from __future__ import annotations

import base64
import binascii
import os
import re
from dataclasses import dataclass
from pathlib import Path


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _opt_float(name: str, default: str) -> float | None:
    """Float setting where 'none' (or empty) disables the corresponding rule."""
    value = os.getenv(name, default).strip().lower()
    return None if value in {"", "none", "off"} else float(value)


def _opt_int(name: str, default: str = "") -> int | None:
    value = os.getenv(name, default).strip().lower()
    return None if value in {"", "none", "off"} else int(value)


def _csv(name: str, default: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in os.getenv(name, default).split(",") if v.strip())


LOCAL_ENVS = frozenset({"local", "development", "test"})
PRODUCTION_LIKE_ENVS = frozenset({"staging", "production"})
KNOWN_ENVS = LOCAL_ENVS | PRODUCTION_LIKE_ENVS
# Asymmetric algorithms only. HS* (shared secret) and "none" are never accepted for OIDC tokens.
ASYMMETRIC_ALGS = frozenset({"RS256", "RS384", "RS512", "PS256", "PS384", "PS512",
                             "ES256", "ES384", "ES512"})
# Defaults that exist only for the local demo. Any of them in a production-like environment, or as
# the OIDC session key, is a configuration error.
DEMO_DEFAULT_SECRETS = frozenset({
    "local-demo-secret-not-for-production", "change-this-local-demo-secret",
    "nexus-local-demo-rotate-this-secret",
    "bG9jYWwtb25seS1kZXYta2V5LWRvLW5vdC1yZXVzZSE=",  # compose.oidc.yaml local-only session key
})
TENANT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

_APP_ENV = os.getenv("APP_ENV", "local").strip().lower()
_DEMO_MODE = _flag("DEMO_MODE", "true")
_PRODUCTION_LIKE = _APP_ENV in PRODUCTION_LIKE_ENVS


@dataclass(frozen=True)
class Settings:
    # ------------------------------------------------------------------ environment
    app_env: str = _APP_ENV
    service_name: str = os.getenv("SERVICE_NAME", "nexus-api")
    service_version: str = "0.4.0"
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./data/nexus.db")
    # Worker / inline-job database URL (role nexus_worker in RLS deployments). Empty: DATABASE_URL.
    worker_database_url: str = os.getenv("WORKER_DATABASE_URL", "")
    storage_dir: Path = Path(os.getenv("STORAGE_DIR", "./data/uploads"))
    demo_mode: bool = _DEMO_MODE  # legacy switch, see module docstring
    jwt_secret: str = os.getenv("JWT_SECRET", "local-demo-secret-not-for-production")
    max_upload_bytes: int = int(os.getenv("MAX_UPLOAD_MB", "5")) * 1024 * 1024
    # Office Open XML (DOCX/XLSX) are ZIP archives: cap the declared uncompressed size and entry
    # count before any parser touches them (zip-bomb protection).
    max_uncompressed_bytes: int = int(os.getenv("MAX_UNCOMPRESSED_MB", "60")) * 1024 * 1024
    max_archive_entries: int = int(os.getenv("MAX_ARCHIVE_ENTRIES", "3000"))
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

    # ------------------------------------------------------------------ database
    db_pool_size: int = int(os.getenv("DB_POOL_SIZE", "5"))
    db_max_overflow: int = int(os.getenv("DB_MAX_OVERFLOW", "10"))
    db_pool_timeout: float = float(os.getenv("DB_POOL_TIMEOUT_SECONDS", "10"))
    db_connect_timeout: int = int(os.getenv("DB_CONNECT_TIMEOUT_SECONDS", "5"))
    db_statement_timeout_ms: int = int(os.getenv("DB_STATEMENT_TIMEOUT_MS", "60000"))
    # Refuse to start when the runtime database role is a superuser or has BYPASSRLS.
    db_require_rls_role: bool = _flag("DB_REQUIRE_RLS_ROLE", "true" if _PRODUCTION_LIKE else "false")

    # ------------------------------------------------------------------ authentication
    auth_mode: str = os.getenv("AUTH_MODE", "demo" if _DEMO_MODE else "oidc").strip().lower()
    frontend_origins: tuple[str, ...] = _csv(
        "FRONTEND_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
    # Where the browser lands after login / logout (path or absolute URL in frontend_origins).
    frontend_default_path: str = os.getenv("FRONTEND_DEFAULT_PATH", "/")
    oidc_issuer: str = os.getenv("OIDC_ISSUER", "").rstrip("/")
    # Discovery document URL. Default: <issuer>/.well-known/openid-configuration. Set it when the
    # backend reaches the IdP through an internal address (e.g. http://keycloak:8080 in Docker).
    oidc_discovery_url: str = os.getenv("OIDC_DISCOVERY_URL", "")
    oidc_client_id: str = os.getenv("OIDC_CLIENT_ID", "")
    oidc_client_secret: str = os.getenv("OIDC_CLIENT_SECRET", "")
    oidc_redirect_uri: str = os.getenv("OIDC_REDIRECT_URI", "")
    oidc_post_logout_redirect_uri: str = os.getenv("OIDC_POST_LOGOUT_REDIRECT_URI", "")
    oidc_scopes: str = os.getenv("OIDC_SCOPES", "openid profile email")
    oidc_allowed_algs: tuple[str, ...] = _csv("OIDC_ALLOWED_ALGS", "RS256")
    oidc_clock_skew_seconds: int = int(os.getenv("OIDC_CLOCK_SKEW_SECONDS", "30"))
    oidc_http_timeout_seconds: float = float(os.getenv("OIDC_HTTP_TIMEOUT_SECONDS", "5"))
    oidc_jwks_cache_seconds: int = int(os.getenv("OIDC_JWKS_CACHE_SECONDS", "300"))
    # An unknown `kid` triggers at most one JWKS refetch per this interval (no refetch storms).
    oidc_jwks_min_refresh_seconds: int = int(os.getenv("OIDC_JWKS_MIN_REFRESH_SECONDS", "10"))
    # Cached keys stay usable this long when the IdP cannot be reached for a refresh
    # (stale-while-revalidate); removed keys are dropped at the next successful refresh.
    oidc_jwks_stale_max_seconds: int = int(os.getenv("OIDC_JWKS_STALE_MAX_SECONDS", "3600"))
    # Bearer access tokens for non-browser API clients (aud must contain oidc_api_audience).
    oidc_accept_bearer: bool = _flag("OIDC_ACCEPT_BEARER", "true")
    oidc_api_audience: str = os.getenv("OIDC_API_AUDIENCE", "nexus-api")
    # Keycloak marks access tokens with typ=Bearer and ID tokens with typ=ID; checking it stops an
    # ID token from being replayed as an access token. Empty disables the check (other IdPs).
    oidc_access_token_typ: str = os.getenv("OIDC_ACCESS_TOKEN_TYP", "Bearer")
    # Tenant / group / role mapping: IdP group paths /tenants/<tenant>/groups/<group> and
    # /tenants/<tenant>/roles/<member|reviewer|admin>. Only tenants listed here can be mapped.
    oidc_group_claim: str = os.getenv("OIDC_GROUP_CLAIM", "groups")
    oidc_tenant_prefix: str = os.getenv("OIDC_TENANT_PREFIX", "/tenants/")
    oidc_allowed_tenants: tuple[str, ...] = _csv("OIDC_ALLOWED_TENANTS", "")
    # Link a first OIDC login to an existing local account with the same e-mail, only when the
    # IdP asserts email_verified=true. Off by default (account-linking is an explicit decision).
    oidc_link_verified_email: bool = _flag("OIDC_LINK_VERIFIED_EMAIL", "false")
    oidc_jit_provisioning: bool = _flag("OIDC_JIT_PROVISIONING", "true")
    # Plain-http IdP URLs are accepted only for local Keycloak in local / test environments.
    oidc_allow_insecure_http: bool = _flag("OIDC_ALLOW_INSECURE_HTTP", "false")

    # ------------------------------------------------------------------ browser sessions
    session_cookie_secure: bool = _flag("SESSION_COOKIE_SECURE", "true" if _PRODUCTION_LIKE else "false")
    session_cookie_samesite: str = os.getenv("SESSION_COOKIE_SAMESITE", "lax").strip().lower()
    session_idle_timeout_seconds: int = int(os.getenv("SESSION_IDLE_TIMEOUT_SECONDS", "1800"))
    session_absolute_timeout_seconds: int = int(os.getenv("SESSION_ABSOLUTE_TIMEOUT_SECONDS", "28800"))
    # OIDC sessions are re-validated against the IdP (refresh-token grant) at this interval; a
    # revoked IdP session or disabled user ends the local session at the next check.
    session_revalidate_seconds: int = int(os.getenv("SESSION_REVALIDATE_SECONDS", "300"))
    session_revalidate_grace_seconds: int = int(os.getenv("SESSION_REVALIDATE_GRACE_SECONDS", "120"))
    # 32 random bytes, base64 (url-safe or standard). Encrypts refresh tokens / PKCE verifiers at
    # rest (AES-256-GCM). Required in OIDC mode.
    session_encryption_key: str = os.getenv("SESSION_ENCRYPTION_KEY", "")
    login_state_ttl_seconds: int = int(os.getenv("LOGIN_STATE_TTL_SECONDS", "600"))

    # ------------------------------------------------------------------ object storage
    storage_backend: str = os.getenv("STORAGE_BACKEND", "local").strip().lower()  # local | s3
    s3_endpoint_url: str = os.getenv("S3_ENDPOINT_URL", "")
    # Endpoint used ONLY to sign presigned download URLs (the address the browser can reach).
    s3_public_endpoint_url: str = os.getenv("S3_PUBLIC_ENDPOINT_URL", "")
    s3_region: str = os.getenv("S3_REGION", "us-east-1")
    s3_bucket: str = os.getenv("S3_BUCKET", "nexus-documents")
    # Empty: the AWS default credential chain (IAM role, profile, AWS_* variables) is used.
    s3_access_key_id: str = os.getenv("S3_ACCESS_KEY_ID", "")
    s3_secret_access_key: str = os.getenv("S3_SECRET_ACCESS_KEY", "")
    s3_force_path_style: bool = _flag("S3_FORCE_PATH_STYLE", "true")
    s3_server_side_encryption: str = os.getenv("S3_SERVER_SIDE_ENCRYPTION", "")  # "" | AES256 | aws:kms
    s3_max_attempts: int = int(os.getenv("S3_MAX_ATTEMPTS", "3"))
    s3_connect_timeout: float = float(os.getenv("S3_CONNECT_TIMEOUT_SECONDS", "3"))
    s3_read_timeout: float = float(os.getenv("S3_READ_TIMEOUT_SECONDS", "30"))
    s3_create_bucket: bool = _flag("S3_CREATE_BUCKET", "false")
    presigned_downloads: bool = _flag("PRESIGNED_DOWNLOADS", "false")
    presigned_ttl_seconds: int = int(os.getenv("PRESIGNED_TTL_SECONDS", "60"))

    # ------------------------------------------------------------------ background processing
    # inline: the API executes the durable job in the request (local demo, tests, evaluation).
    # queue:  the API returns 202 and background workers (python -m backend.app.worker) run it.
    processing_mode: str = os.getenv("PROCESSING_MODE", "inline").strip().lower()
    job_max_attempts: int = int(os.getenv("JOB_MAX_ATTEMPTS", "5"))
    job_backoff_base_seconds: float = float(os.getenv("JOB_BACKOFF_BASE_SECONDS", "5"))
    job_backoff_max_seconds: float = float(os.getenv("JOB_BACKOFF_MAX_SECONDS", "300"))
    job_lease_seconds: int = int(os.getenv("JOB_LEASE_SECONDS", "120"))
    job_heartbeat_seconds: float = float(os.getenv("JOB_HEARTBEAT_SECONDS", "20"))
    worker_poll_seconds: float = float(os.getenv("WORKER_POLL_SECONDS", "1.0"))
    worker_id: str = os.getenv("WORKER_ID", "")
    worker_heartbeat_file: str = os.getenv("WORKER_HEARTBEAT_FILE", "")
    worker_stale_seconds: int = int(os.getenv("WORKER_STALE_SECONDS", "60"))
    # Document lifecycle
    deletion_grace_seconds: int = int(os.getenv("DELETION_GRACE_SECONDS", "0"))
    retention_sweep_seconds: int = int(os.getenv("RETENTION_SWEEP_SECONDS", "300"))
    default_retention_days: int | None = _opt_int("DOCUMENT_DEFAULT_RETENTION_DAYS")
    max_retention_days: int = int(os.getenv("DOCUMENT_MAX_RETENTION_DAYS", "3650"))

    # ------------------------------------------------------------------ observability
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()
    log_format: str = os.getenv("LOG_FORMAT", "json").strip().lower()  # json | text
    metrics_enabled: bool = _flag("METRICS_ENABLED", "true")
    # Internal Prometheus endpoint on a separate port (not published by Docker Compose). 0: off.
    metrics_port: int = int(os.getenv("METRICS_PORT", "0"))
    otel_enabled: bool = _flag("OTEL_ENABLED", "false")
    otel_endpoint: str = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    otel_sample_ratio: float = float(os.getenv("OTEL_TRACES_SAMPLER_RATIO", "1.0"))

    # ------------------------------------------------------------------ derived properties
    @property
    def production_like(self) -> bool:
        return self.app_env in PRODUCTION_LIKE_ENVS

    @property
    def demo_auth(self) -> bool:
        return self.auth_mode == "demo"

    @property
    def oidc_auth(self) -> bool:
        return self.auth_mode == "oidc"

    @property
    def effective_discovery_url(self) -> str:
        return self.oidc_discovery_url or f"{self.oidc_issuer}/.well-known/openid-configuration"

    @property
    def session_cookie_name(self) -> str:
        # The __Host- prefix makes browsers enforce Secure, Path=/ and no Domain attribute.
        return "__Host-nexus_session" if self.session_cookie_secure else "nexus_session"

    @property
    def login_binding_cookie_name(self) -> str:
        return "__Host-nexus_login" if self.session_cookie_secure else "nexus_login"


def decode_key(value: str) -> bytes | None:
    """32-byte AES key from base64 (url-safe or standard, padding optional); None if invalid."""
    if not value:
        return None
    padded = value + "=" * (-len(value) % 4)
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            key = decoder(padded)
        except (binascii.Error, ValueError):
            continue
        if len(key) == 32:
            return key
    return None


def _is_http(url: str) -> bool:
    return url.lower().startswith("http://")


def validate_settings(s: Settings) -> list[str]:
    """Every security-relevant configuration error (empty list = configuration accepted)."""
    errors: list[str] = []
    if s.app_env not in KNOWN_ENVS:
        errors.append(f"APP_ENV must be one of {sorted(KNOWN_ENVS)} (got {s.app_env!r})")
    if s.auth_mode not in {"demo", "oidc"}:
        errors.append(f"AUTH_MODE must be 'demo' or 'oidc' (got {s.auth_mode!r})")
    if s.production_like:
        if s.auth_mode == "demo":
            errors.append("AUTH_MODE=demo (demo personas and passwords) is not allowed in "
                          f"APP_ENV={s.app_env}; configure AUTH_MODE=oidc")
        if s.demo_mode:
            errors.append(f"DEMO_MODE=true is not allowed in APP_ENV={s.app_env}")
        if not s.session_cookie_secure:
            errors.append("SESSION_COOKIE_SECURE must be true outside local environments")
        if s.storage_backend != "s3":
            errors.append("STORAGE_BACKEND=s3 is required outside local environments")
        if s.processing_mode != "queue":
            errors.append("PROCESSING_MODE=queue (background workers) is required outside local "
                          "environments")
        if not s.database_url.startswith("postgresql"):
            errors.append("PostgreSQL is required outside local environments")
        if s.oidc_allow_insecure_http:
            errors.append("OIDC_ALLOW_INSECURE_HTTP is only permitted in local / test environments")
    if s.auth_mode == "demo":
        if not s.jwt_secret:
            errors.append("JWT_SECRET must not be empty in demo mode")
    if s.auth_mode == "oidc":
        required = {"OIDC_ISSUER": s.oidc_issuer, "OIDC_CLIENT_ID": s.oidc_client_id,
                    "OIDC_CLIENT_SECRET": s.oidc_client_secret, "OIDC_REDIRECT_URI": s.oidc_redirect_uri}
        errors += [f"{name} is required when AUTH_MODE=oidc" for name, value in required.items() if not value]
        key = decode_key(s.session_encryption_key)
        if key is None:
            errors.append("SESSION_ENCRYPTION_KEY must be 32 random bytes, base64-encoded "
                          "(python -c \"import secrets,base64;print(base64.urlsafe_b64encode("
                          "secrets.token_bytes(32)).decode())\")")
        elif s.production_like and s.session_encryption_key in DEMO_DEFAULT_SECRETS:
            errors.append("SESSION_ENCRYPTION_KEY must not be a published local-development default")
        bad_algs = [a for a in s.oidc_allowed_algs if a not in ASYMMETRIC_ALGS]
        if bad_algs or not s.oidc_allowed_algs:
            errors.append(f"OIDC_ALLOWED_ALGS may only contain asymmetric algorithms "
                          f"{sorted(ASYMMETRIC_ALGS)} (got {list(s.oidc_allowed_algs)})")
        if not s.oidc_allowed_tenants:
            errors.append("OIDC_ALLOWED_TENANTS must list the tenants the IdP may assert")
        for tenant in s.oidc_allowed_tenants:
            if not TENANT_ID.match(tenant):
                errors.append(f"OIDC_ALLOWED_TENANTS contains an invalid tenant id {tenant!r}")
        insecure_ok = s.oidc_allow_insecure_http and s.app_env in LOCAL_ENVS
        for name, url in (("OIDC_ISSUER", s.oidc_issuer), ("OIDC_DISCOVERY_URL", s.oidc_discovery_url),
                          ("OIDC_REDIRECT_URI", s.oidc_redirect_uri)):
            if url and _is_http(url) and not insecure_ok:
                errors.append(f"{name} must use https (plain http is only accepted for a local IdP "
                              "with OIDC_ALLOW_INSECURE_HTTP=true and APP_ENV=local/test)")
        if s.session_cookie_samesite not in {"lax", "strict"}:
            errors.append("SESSION_COOKIE_SAMESITE must be 'lax' or 'strict' for cookie sessions")
    if s.storage_backend not in {"local", "s3"}:
        errors.append(f"STORAGE_BACKEND must be 'local' or 's3' (got {s.storage_backend!r})")
    if s.storage_backend == "s3" and not s.s3_bucket:
        errors.append("S3_BUCKET is required when STORAGE_BACKEND=s3")
    if s.processing_mode not in {"inline", "queue"}:
        errors.append(f"PROCESSING_MODE must be 'inline' or 'queue' (got {s.processing_mode!r})")
    if s.production_like and s.jwt_secret in DEMO_DEFAULT_SECRETS and s.auth_mode == "demo":
        errors.append("The published demo JWT_SECRET must never be used outside local environments")
    if not s.frontend_origins:
        errors.append("FRONTEND_ORIGINS must list at least one origin")
    for origin in s.frontend_origins:
        if origin == "*" or not re.match(r"^https?://[^/\s]+$", origin):
            errors.append(f"FRONTEND_ORIGINS entries must be explicit origins without paths (got {origin!r})")
        elif s.production_like and _is_http(origin):
            errors.append(f"FRONTEND_ORIGINS must use https outside local environments ({origin})")
    return errors


class InsecureConfiguration(RuntimeError):
    pass


def assert_secure_configuration(s: Settings) -> None:
    errors = validate_settings(s)
    if errors:
        raise InsecureConfiguration("Refusing to start with an insecure or incomplete configuration:\n  - "
                                    + "\n  - ".join(errors))


settings = Settings()
