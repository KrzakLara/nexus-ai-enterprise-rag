"""`current_user`: the single authentication dependency of every protected route.

Accepted credentials, depending on AUTH_MODE:

    demo   session cookie (created by POST /api/auth/login)  |  Bearer <HS256 demo JWT>
    oidc   session cookie (created by the OIDC callback)      |  Bearer <IdP access token>
           (aud must contain OIDC_API_AUDIENCE; demo JWTs fail the asymmetric-alg allowlist)

After authentication the database session gets the user's row-level-security context. A
successful login proves identity only: document access is still decided per document by the
ACL and by PostgreSQL RLS.

Error bodies: {"detail": {"code": <fixed category>, "message": ...}}; 401 = not / no longer
authenticated, 403 = authenticated but refused (CSRF, origin, disabled account, no tenant).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from ..config import Settings
from ..database import get_db, set_security_context, user_context
from ..models import AuthSession, User
from ..observability.logging import bind
from ..observability.metrics import AUTH_FAILURES
from ..runtime import get_settings
from . import demo, sessions
from .mapping import MappingError, map_claims
from .oidc import OIDCError, get_oidc_client
from .provisioning import IdentityRejected, provision, resolve_identity, sync_attributes

log = logging.getLogger("nexus.auth")
bearer = HTTPBearer(auto_error=False)


@dataclass
class Principal:
    user: User
    method: str  # session | demo_bearer | oidc_bearer
    session: AuthSession | None = None
    session_token: str | None = None


class AuthFailure(HTTPException):
    def __init__(self, status_code: int, code: str, message: str):
        headers = {"WWW-Authenticate": "Bearer"} if status_code == 401 else None
        super().__init__(status_code, {"code": code, "message": message}, headers=headers)
        self.code = code


MESSAGES = {
    "not_authenticated": "Authentication required",
    "invalid_token": "Invalid or expired access token",
    "unknown_account": "Unknown account",
    "account_disabled": "This account is disabled",
    "session_invalid": "Invalid session; please sign in again",
    "session_expired": "Your session has expired; please sign in again",
    "session_revoked": "Your session was ended; please sign in again",
    "session_mode_mismatch": "This session was created in another authentication mode",
    "revalidation_unavailable": "The identity provider could not confirm your session; sign in again",
    "csrf_failed": "Missing or invalid CSRF token",
    "origin_not_allowed": "Request origin is not allowed",
    "bearer_disabled": "Bearer tokens are not accepted",
    "no_tenant": "Your account is not a member of any workspace",
}


def fail(method: str, code: str, status_code: int = status.HTTP_401_UNAUTHORIZED,
         message: str | None = None) -> AuthFailure:
    AUTH_FAILURES.labels(method, code).inc()
    log.warning("auth_failed", extra={"event": "auth_failed", "method": method, "reason": code,
                                      "status": status_code})
    return AuthFailure(status_code, code, message or MESSAGES.get(code, "Authentication failed"))


def _active(user: User | None, method: str, db: Session, row: AuthSession | None = None) -> User:
    if user is None:
        raise fail(method, "unknown_account")
    if user.status != "active":
        if row is not None:
            sessions.revoke(db, row, "account_disabled")
            db.commit()
        raise fail(method, "account_disabled", status.HTTP_403_FORBIDDEN)
    return user


def _demo_bearer(token: str, db: Session, cfg: Settings) -> Principal:
    try:
        payload = demo.decode_token(token, cfg)
    except jwt.InvalidTokenError:
        raise fail("demo_bearer", "invalid_token") from None
    set_security_context(db, user_context(payload["tenant"], payload["sub"]))
    user = db.get(User, payload["sub"])
    if user is None or user.tenant_id != payload["tenant"]:
        raise fail("demo_bearer", "unknown_account")
    return Principal(_active(user, "demo_bearer", db), "demo_bearer")


def _oidc_bearer(token: str, db: Session, cfg: Settings) -> Principal:
    try:
        claims = get_oidc_client(cfg).validate_access_token(token)
        mapped = map_claims(claims, cfg)
    except OIDCError as exc:
        raise fail("oidc_bearer", exc.code if exc.code in MESSAGES else "invalid_token") from None
    except MappingError as exc:
        raise fail("oidc_bearer", exc.code, status.HTTP_403_FORBIDDEN, str(exc)) from None
    found = resolve_identity(db, mapped.issuer, mapped.subject)
    try:
        if found is None:  # first API call of this identity: same provisioning rules as login
            user = provision(db, mapped, link_verified_email=cfg.oidc_link_verified_email,
                             jit=cfg.oidc_jit_provisioning)
            db.commit()
        else:
            user_id, tenant_id, state = found
            if tenant_id != mapped.tenant_id:
                raise IdentityRejected("tenant_conflict", "this identity belongs to a different tenant")
            set_security_context(db, user_context(tenant_id, user_id))
            user = _active(db.get(User, user_id), "oidc_bearer", db)
            if sync_attributes(user, mapped):  # IdP group / role change: write only on change
                db.commit()
    except IdentityRejected as exc:
        db.rollback()
        raise fail("oidc_bearer", exc.code, status.HTTP_403_FORBIDDEN, str(exc)) from None
    return Principal(_active(user, "oidc_bearer", db), "oidc_bearer")


def _revalidate(db: Session, row: AuthSession, user: User, cfg: Settings) -> None:
    """Refresh-token grant against the IdP; re-map tenant / groups / role from the new token."""
    now = sessions.now_utc()
    refresh_token = sessions.decrypt_refresh_token(row, cfg)
    client = get_oidc_client(cfg)
    try:
        if not refresh_token:
            raise OIDCError("invalid_grant", "session has no refresh token")
        tokens = client.refresh(refresh_token)
        id_token = tokens.get("id_token")
        claims = client.validate_id_token(id_token, nonce=None) if id_token else \
            client.validate_access_token(tokens["access_token"])
        mapped = map_claims(claims, cfg)
    except OIDCError as exc:
        if exc.code in ("idp_unavailable", "idp_invalid_response", "token_request_failed"):
            grace_end = sessions._aware(row.revalidate_after) + timedelta(
                seconds=cfg.session_revalidate_grace_seconds)
            if now < grace_end:
                log.warning("session_revalidation_deferred", extra={
                    "event": "session_revalidation_deferred", "reason": exc.code})
                return
            raise fail("session", "revalidation_unavailable") from None
        sessions.revoke(db, row, "idp_session_ended")
        db.commit()
        raise fail("session", "session_revoked") from None
    except MappingError as exc:
        sessions.revoke(db, row, "access_removed")
        db.commit()
        raise fail("session", exc.code, status.HTTP_403_FORBIDDEN, str(exc)) from None
    if mapped.subject != row.idp_subject or mapped.tenant_id != user.tenant_id:
        sessions.revoke(db, row, "identity_changed")
        db.commit()
        raise fail("session", "session_revoked")
    sync_attributes(user, mapped)
    cipher = sessions.cipher(cfg)
    row.refresh_token_enc = cipher.encrypt(tokens.get("refresh_token") or refresh_token, "refresh_token")
    if id_token:
        row.id_token_enc = cipher.encrypt(id_token, "id_token")
    row.revalidate_after = now + timedelta(seconds=cfg.session_revalidate_seconds)
    db.commit()


def _session(request: Request, token: str, db: Session, cfg: Settings) -> Principal:
    row = sessions.lookup(db, token)
    if row is None:
        raise fail("session", "session_invalid")
    reason = sessions.invalid_reason(row)
    if reason:
        raise fail("session", reason)
    sessions.bind_user_context(db, row)
    if row.auth_mode != cfg.auth_mode:
        sessions.revoke(db, row, "mode_mismatch")
        db.commit()
        raise fail("session", "session_mode_mismatch")
    if request.method in sessions.UNSAFE_METHODS:
        own_origin = f"{request.url.scheme}://{request.url.netloc}"
        if not sessions.origin_allowed(request.headers.get("origin"), cfg, own_origin):
            raise fail("session", "origin_not_allowed", status.HTTP_403_FORBIDDEN)
        if not sessions.csrf_valid(row, request.headers.get("x-csrf-token")):
            raise fail("session", "csrf_failed", status.HTTP_403_FORBIDDEN)
    user = db.get(User, row.user_id)
    if user is None or user.tenant_id != row.tenant_id:
        sessions.revoke(db, row, "account_missing")
        db.commit()
        raise fail("session", "session_invalid")
    _active(user, "session", db, row)
    if row.auth_mode == "oidc" and row.revalidate_after is not None \
            and sessions.now_utc() >= sessions._aware(row.revalidate_after):
        _revalidate(db, row, user, cfg)
    before = row.last_seen_at
    sessions.touch(db, row, cfg)
    if row.last_seen_at is not before:
        db.commit()
    return Principal(user, "session", row, token)


def authenticate(request: Request, credentials: HTTPAuthorizationCredentials | None, db: Session,
                 cfg: Settings) -> Principal:
    if credentials is not None:
        if cfg.demo_auth:
            return _demo_bearer(credentials.credentials, db, cfg)
        if cfg.oidc_auth and cfg.oidc_accept_bearer:
            return _oidc_bearer(credentials.credentials, db, cfg)
        raise fail("bearer", "bearer_disabled")
    token = request.cookies.get(cfg.session_cookie_name)
    if token:
        return _session(request, token, db, cfg)
    raise fail("anonymous", "not_authenticated")


def current_principal(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
                      db: Session = Depends(get_db)) -> Principal:
    cfg = get_settings()
    principal = authenticate(request, credentials, db, cfg)
    user = principal.user
    # the user's RLS context for every later transaction of this request
    set_security_context(db, user_context(user.tenant_id, user.id))
    request.state.principal = principal
    bind(tenant_id=user.tenant_id, user_id=user.id, auth=principal.method)
    return principal


def current_user(principal: Principal = Depends(current_principal)) -> User:
    return principal.user
