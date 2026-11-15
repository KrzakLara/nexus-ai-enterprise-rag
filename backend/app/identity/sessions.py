"""Server-side browser sessions (backend-for-frontend).

The browser receives one opaque, random 256-bit session token in an HttpOnly cookie
(SameSite=Lax, Secure outside local development, `__Host-` prefix when Secure). No access,
refresh or ID token is ever handed to JavaScript or stored in localStorage. The database stores
only SHA-256(token); refresh / ID tokens are AES-256-GCM encrypted.

Expiry: an idle timeout (sliding, updated at most once a minute) and an absolute timeout. OIDC
sessions are additionally re-validated against the IdP with the refresh token every
SESSION_REVALIDATE_SECONDS: a revoked IdP session, a disabled user or a removed tenant
membership ends the local session at that check. Back-channel logout ends it immediately.

CSRF: cookie-authenticated unsafe requests (POST/PUT/PATCH/DELETE) must send the per-session
X-CSRF-Token (synchronizer token, returned by /api/auth/session to same-origin JavaScript only)
and, when the browser sends an Origin header, an allowed origin. SameSite=Lax is a second layer.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..config import Settings, decode_key
from ..database import SecurityContext, set_security_context
from ..models import AuthSession, User
from .crypto import TokenCipher, constant_time_equal, random_token, sha256_hex

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
TOUCH_INTERVAL = timedelta(seconds=60)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes for timezone-aware columns; treat them as UTC."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


@lru_cache(maxsize=4)
def _cipher_for(raw_key: str) -> TokenCipher:
    key = decode_key(raw_key)
    if key is None:
        raise RuntimeError("SESSION_ENCRYPTION_KEY is not configured")
    return TokenCipher(key)


def cipher(cfg: Settings) -> TokenCipher:
    return _cipher_for(cfg.session_encryption_key)


def derive_csrf(session_token: str, cfg: Settings) -> str:
    """Per-session CSRF token = HMAC(server secret, session token). Stable for the session's
    lifetime (several browser tabs share it), recomputable from the cookie, and useless without
    both the HttpOnly cookie and the server secret."""
    secret = (decode_key(cfg.session_encryption_key) or cfg.jwt_secret.encode("utf-8"))
    digest = hmac.new(hashlib.sha256(b"nexus-csrf-v1" + secret).digest(),
                      session_token.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@dataclass
class IssuedSession:
    token: str          # cookie value (never stored)
    csrf_token: str     # returned to the SPA (never stored in clear)
    row: AuthSession


def create_session(db: Session, user: User, cfg: Settings, *, auth_mode: str, issuer: str | None = None,
                   idp_subject: str | None = None, idp_session_id: str | None = None,
                   refresh_token: str | None = None, id_token: str | None = None,
                   now: datetime | None = None) -> IssuedSession:
    """A NEW random session id on every login (no session fixation)."""
    now = now or now_utc()
    token = random_token(32)
    csrf = derive_csrf(token, cfg)
    row = AuthSession(
        id=str(uuid4()), token_hash=sha256_hex(token), user_id=user.id, tenant_id=user.tenant_id,
        auth_mode=auth_mode, csrf_token_hash=sha256_hex(csrf), created_at=now, last_seen_at=now,
        idle_expires_at=now + timedelta(seconds=cfg.session_idle_timeout_seconds),
        absolute_expires_at=now + timedelta(seconds=cfg.session_absolute_timeout_seconds),
        revalidate_after=(now + timedelta(seconds=cfg.session_revalidate_seconds)) if auth_mode == "oidc" else None,
        issuer=issuer, idp_subject=idp_subject, idp_session_id=idp_session_id,
        refresh_token_enc=cipher(cfg).encrypt(refresh_token, "refresh_token") if refresh_token else None,
        id_token_enc=cipher(cfg).encrypt(id_token, "id_token") if id_token else None,
    )
    set_security_context(db, SecurityContext(tenant_id=user.tenant_id, user_id=user.id, kind="user",
                                             session_hash=row.token_hash))
    db.add(row)
    return IssuedSession(token=token, csrf_token=csrf, row=row)


def lookup(db: Session, token: str) -> AuthSession | None:
    if not token or len(token) > 200:
        return None
    token_hash = sha256_hex(token)
    set_security_context(db, SecurityContext(kind="session_lookup", session_hash=token_hash))
    return db.scalar(select(AuthSession).where(AuthSession.token_hash == token_hash))


def invalid_reason(row: AuthSession, now: datetime | None = None) -> str | None:
    now = now or now_utc()
    if row.revoked_at is not None:
        return "session_revoked"
    if now >= _aware(row.absolute_expires_at) or now >= _aware(row.idle_expires_at):
        return "session_expired"
    return None


def bind_user_context(db: Session, row: AuthSession) -> None:
    set_security_context(db, SecurityContext(tenant_id=row.tenant_id, user_id=row.user_id, kind="user",
                                             session_hash=row.token_hash))


def touch(db: Session, row: AuthSession, cfg: Settings, now: datetime | None = None) -> None:
    now = now or now_utc()
    if now - _aware(row.last_seen_at) < TOUCH_INTERVAL:
        return
    row.last_seen_at = now
    row.idle_expires_at = min(now + timedelta(seconds=cfg.session_idle_timeout_seconds),
                              _aware(row.absolute_expires_at))


def revoke(db: Session, row: AuthSession, reason: str, now: datetime | None = None) -> None:
    if row.revoked_at is None:
        row.revoked_at, row.revoked_reason = now or now_utc(), reason[:40]


def revoke_all_for_user(db: Session, user: User, reason: str) -> int:
    result = db.execute(update(AuthSession).where(AuthSession.user_id == user.id,
                                                  AuthSession.revoked_at.is_(None))
                        .values(revoked_at=now_utc(), revoked_reason=reason[:40]))
    return result.rowcount or 0


def csrf_valid(row: AuthSession, header_value: str | None) -> bool:
    return bool(header_value) and len(header_value) < 200 and constant_time_equal(
        sha256_hex(header_value), row.csrf_token_hash)


def origin_allowed(origin: str | None, cfg: Settings, request_origin: str | None = None) -> bool:
    """Absent Origin (non-browser client, same-origin GET navigations) is decided by the CSRF
    token alone. A present Origin must be a configured frontend origin or the API's own origin."""
    if origin is None:
        return True
    origin = origin.rstrip("/")
    return origin in cfg.frontend_origins or (request_origin is not None and origin == request_origin)


def decrypt_refresh_token(row: AuthSession, cfg: Settings) -> str | None:
    return cipher(cfg).decrypt(row.refresh_token_enc, "refresh_token") if row.refresh_token_enc else None


def decrypt_id_token(row: AuthSession, cfg: Settings) -> str | None:
    return cipher(cfg).decrypt(row.id_token_enc, "id_token") if row.id_token_enc else None


__all__ = ["IssuedSession", "UNSAFE_METHODS", "bind_user_context", "create_session", "csrf_valid",
           "decrypt_id_token", "decrypt_refresh_token", "derive_csrf", "invalid_reason", "lookup",
           "now_utc", "origin_allowed", "revoke", "revoke_all_for_user", "touch"]
