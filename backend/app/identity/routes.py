"""/api/auth/* : demo login (demo mode only), OIDC Authorization Code + PKCE through the backend,
session restoration, logout (local + RP-initiated) and OIDC back-channel logout."""
from __future__ import annotations

import logging
from datetime import timedelta
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session

from ..config import Settings
from ..database import ANONYMOUS, get_db, set_security_context
from ..models import AuditEvent, AuthLoginState, AuthSession
from ..observability.metrics import AUTH_LOGINS
from ..runtime import get_settings
from . import demo, sessions
from .crypto import pkce_pair, random_token, sha256_hex
from .dependencies import Principal, current_principal, fail
from .mapping import MappingError, map_claims
from .oidc import OIDCError, get_oidc_client
from .provisioning import IdentityRejected, provision

log = logging.getLogger("nexus.auth")
router = APIRouter(prefix="/api/auth", tags=["authentication"])


class LoginIn(BaseModel):
    email: str = Field(max_length=150)
    password: str = Field(max_length=200)


def user_view(user) -> dict:
    return {"email": user.email, "groups": user.groups, "role": user.role, "tenant": user.tenant_id,
            "display_name": user.display_name}


def _set_session_cookie(response: Response, token: str, cfg: Settings) -> None:
    response.set_cookie(cfg.session_cookie_name, token, max_age=cfg.session_absolute_timeout_seconds,
                        httponly=True, secure=cfg.session_cookie_secure,
                        samesite=cfg.session_cookie_samesite, path="/")


def _clear_session_cookie(response: Response, cfg: Settings) -> None:
    response.delete_cookie(cfg.session_cookie_name, path="/", secure=cfg.session_cookie_secure,
                           httponly=True, samesite=cfg.session_cookie_samesite)


def _audit(db: Session, user, action: str) -> None:
    db.add(AuditEvent(user_id=user.id, tenant_id=user.tenant_id, action=action, document_id=None))


def safe_return_to(value: str | None, cfg: Settings) -> str:
    """Relative path on the frontend, or an absolute URL whose origin is a configured frontend
    origin. Anything else (//evil, \\evil, javascript:, other hosts) falls back to the default."""
    default = cfg.frontend_default_path or "/"
    if not value or len(value) > 500 or any(c in value for c in "\r\n\t\\"):
        return default
    if value.startswith("/") and not value.startswith("//"):
        return value
    parts = urlsplit(value)
    if parts.scheme in ("http", "https") and f"{parts.scheme}://{parts.netloc}" in cfg.frontend_origins:
        return value
    return default


def _frontend_url(path_or_url: str, cfg: Settings, **params) -> str:
    base = path_or_url if "://" in path_or_url else f"{cfg.frontend_origins[0]}{path_or_url}"
    if params:
        base += ("&" if "?" in base else "?") + urlencode(params)
    return base


# ----------------------------------------------------------------------------- configuration

@router.get("/config")
def auth_config():
    cfg = get_settings()
    return {"auth_mode": cfg.auth_mode,
            "login_url": "/api/auth/oidc/login" if cfg.oidc_auth else None,
            "password_login": cfg.demo_auth,
            "csrf_header": "X-CSRF-Token",
            "session_idle_timeout_seconds": cfg.session_idle_timeout_seconds}


# ----------------------------------------------------------------------------- demo login

@router.post("/login")
def demo_login(payload: LoginIn, response: Response, db: Session = Depends(get_db)):
    cfg = get_settings()
    if not cfg.demo_auth:
        raise HTTPException(404, "Password login is disabled (AUTH_MODE=oidc)")
    user = demo.check_credentials(db, payload.email, payload.password)
    if user is None:
        raise fail("demo_password", "invalid_credentials", message="Invalid credentials")
    issued = sessions.create_session(db, user, cfg, auth_mode="demo")
    _audit(db, user, "login")
    db.commit()
    AUTH_LOGINS.labels("demo").inc()
    _set_session_cookie(response, issued.token, cfg)
    return {
        # Bearer token kept for API clients and v0.3 scripts; the browser uses the cookie session.
        "access_token": demo.make_token(user, cfg), "token_type": "bearer",
        "csrf_token": issued.csrf_token,
        "expires_at": issued.row.absolute_expires_at.isoformat(),
        "user": user_view(user),
    }


# ----------------------------------------------------------------------------- session

@router.get("/session")
def current_session(request: Request, principal: Principal = Depends(current_principal)):
    cfg = get_settings()
    body = {"authenticated": True, "auth_mode": cfg.auth_mode, "method": principal.method,
            "user": user_view(principal.user)}
    if principal.session is not None and principal.session_token:
        row = principal.session
        body |= {"csrf_token": sessions.derive_csrf(principal.session_token, cfg),
                 "expires_at": sessions._aware(row.absolute_expires_at).isoformat(),
                 "idle_expires_at": sessions._aware(row.idle_expires_at).isoformat()}
    return body


@router.post("/logout")
def logout(request: Request, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    cfg = get_settings()
    logout_url = None
    if principal.session is not None:
        row = db.get(AuthSession, principal.session.id)
        if row is not None:
            if row.auth_mode == "oidc":
                try:
                    logout_url = get_oidc_client(cfg).end_session_url(sessions.decrypt_id_token(row, cfg))
                except OIDCError:
                    logout_url = None  # local logout still succeeds
            sessions.revoke(db, row, "logout")
    _audit(db, principal.user, "logout")
    db.commit()
    response = JSONResponse({"logged_out": True, "logout_url": logout_url})
    _clear_session_cookie(response, cfg)
    return response


# ----------------------------------------------------------------------------- OIDC

@router.get("/oidc/login")
def oidc_login(return_to: str | None = None, db: Session = Depends(get_db)):
    cfg = get_settings()
    if not cfg.oidc_auth:
        raise HTTPException(404, "OIDC login is not enabled (AUTH_MODE=demo)")
    client = get_oidc_client(cfg)
    state, nonce, binding = random_token(32), random_token(32), random_token(32)
    verifier, challenge = pkce_pair()
    try:
        url = client.authorization_url(state, nonce, challenge)
    except OIDCError as exc:
        raise fail("oidc_login", exc.code, 503, "The identity provider is unavailable") from None
    now = sessions.now_utc()
    set_security_context(db, ANONYMOUS)
    db.execute(delete(AuthLoginState).where(AuthLoginState.expires_at < now))  # housekeeping
    db.add(AuthLoginState(state_hash=sha256_hex(state), nonce=nonce,
                          code_verifier_enc=sessions.cipher(cfg).encrypt(verifier, "pkce_verifier"),
                          browser_binding_hash=sha256_hex(binding),
                          return_to=safe_return_to(return_to, cfg), created_at=now,
                          expires_at=now + timedelta(seconds=cfg.login_state_ttl_seconds)))
    db.commit()
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(cfg.login_binding_cookie_name, binding, max_age=cfg.login_state_ttl_seconds,
                        httponly=True, secure=cfg.session_cookie_secure, samesite="lax", path="/")
    return response


def _login_error(cfg: Settings, code: str, return_to: str | None = None) -> RedirectResponse:
    response = RedirectResponse(_frontend_url(return_to or cfg.frontend_default_path or "/", cfg,
                                              auth_error=code), status_code=302)
    response.delete_cookie(cfg.login_binding_cookie_name, path="/")
    return response


@router.get("/oidc/callback")
def oidc_callback(request: Request, state: str | None = None, code: str | None = None,
                  error: str | None = None, db: Session = Depends(get_db)):
    cfg = get_settings()
    if not cfg.oidc_auth:
        raise HTTPException(404, "OIDC login is not enabled (AUTH_MODE=demo)")
    if not state or len(state) > 200:
        fail("oidc_callback", "invalid_state", 400)
        return _login_error(cfg, "invalid_state")
    set_security_context(db, ANONYMOUS)
    # single use: the state row is deleted in the same statement that reads it
    pending = db.execute(delete(AuthLoginState).where(AuthLoginState.state_hash == sha256_hex(state))
                         .returning(AuthLoginState.nonce, AuthLoginState.code_verifier_enc,
                                    AuthLoginState.browser_binding_hash, AuthLoginState.return_to,
                                    AuthLoginState.expires_at)).first()
    db.commit()
    if pending is None:
        fail("oidc_callback", "invalid_state", 400)
        return _login_error(cfg, "invalid_state")
    nonce, verifier_enc, binding_hash, return_to, expires_at = pending
    binding = request.cookies.get(cfg.login_binding_cookie_name)
    if not binding or sha256_hex(binding) != binding_hash:  # login-CSRF / injected response
        fail("oidc_callback", "browser_binding_failed", 400)
        return _login_error(cfg, "invalid_state")
    if sessions.now_utc() >= sessions._aware(expires_at):
        fail("oidc_callback", "login_expired", 400)
        return _login_error(cfg, "login_expired", return_to)
    if error or not code:
        fail("oidc_callback", "idp_error", 400)
        return _login_error(cfg, "access_denied" if error == "access_denied" else "login_failed", return_to)
    client = get_oidc_client(cfg)
    try:
        tokens = client.exchange_code(code[:2048], sessions.cipher(cfg).decrypt(verifier_enc, "pkce_verifier"))
        claims = client.validate_id_token(tokens.get("id_token") or "", nonce)
        mapped = map_claims(claims, cfg)
        user = provision(db, mapped, link_verified_email=cfg.oidc_link_verified_email,
                         jit=cfg.oidc_jit_provisioning)
    except OIDCError as exc:
        db.rollback()
        fail("oidc_callback", exc.code, 400)
        return _login_error(cfg, "login_failed", return_to)
    except (MappingError, IdentityRejected) as exc:
        db.rollback()
        fail("oidc_callback", exc.code, 403)
        return _login_error(cfg, "no_tenant" if exc.code == "no_tenant" else "access_denied", return_to)
    issued = sessions.create_session(
        db, user, cfg, auth_mode="oidc", issuer=mapped.issuer, idp_subject=mapped.subject,
        idp_session_id=mapped.session_id, refresh_token=tokens.get("refresh_token"),
        id_token=tokens.get("id_token"))
    _audit(db, user, "login")
    db.commit()
    AUTH_LOGINS.labels("oidc").inc()
    log.info("login", extra={"event": "login", "method": "oidc", "tenant_id": user.tenant_id,
                             "user_id": user.id})
    response = RedirectResponse(_frontend_url(return_to, cfg), status_code=302)
    response.delete_cookie(cfg.login_binding_cookie_name, path="/")
    _set_session_cookie(response, issued.token, cfg)
    return response


@router.post("/oidc/backchannel-logout")
def backchannel_logout(logout_token: str = Form(..., max_length=16384), db: Session = Depends(get_db)):
    """OIDC Back-Channel Logout 1.0: the IdP ends every local session of an IdP session."""
    cfg = get_settings()
    if not cfg.oidc_auth:
        raise HTTPException(404, "OIDC is not enabled")
    try:
        claims = get_oidc_client(cfg).validate_logout_token(logout_token)
    except OIDCError as exc:
        fail("backchannel_logout", exc.code, 400)
        return JSONResponse({"error": "invalid_request"}, status_code=400,
                            headers={"Cache-Control": "no-store"})
    sid, sub = claims.get("sid"), claims.get("sub")
    set_security_context(db, ANONYMOUS)
    if db.get_bind().dialect.name == "postgresql":
        count = db.execute(text("SELECT nexus_rls.revoke_idp_sessions(:i, :sid, :sub, 'backchannel_logout')"),
                           {"i": cfg.oidc_issuer, "sid": sid, "sub": sub}).scalar()
    else:
        cond = (AuthSession.idp_session_id == sid) if sid else (AuthSession.idp_subject == sub)
        count = db.execute(update(AuthSession).where(AuthSession.issuer == cfg.oidc_issuer, cond,
                                                     AuthSession.revoked_at.is_(None))
                           .values(revoked_at=sessions.now_utc(), revoked_reason="backchannel_logout")).rowcount
    db.commit()
    log.info("backchannel_logout", extra={"event": "backchannel_logout", "sessions_revoked": count})
    return Response(status_code=200, headers={"Cache-Control": "no-store"})


@router.get("/sessions")
def my_sessions(principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    """The user's own active sessions (no tokens, no hashes)."""
    rows = db.scalars(select(AuthSession).where(AuthSession.user_id == principal.user.id,
                                                AuthSession.revoked_at.is_(None))
                      .order_by(AuthSession.created_at.desc()).limit(20)).all()
    return [{"id": r.id, "auth_mode": r.auth_mode, "created_at": sessions._aware(r.created_at).isoformat(),
             "last_seen_at": sessions._aware(r.last_seen_at).isoformat(),
             "current": principal.session is not None and r.id == principal.session.id} for r in rows]
