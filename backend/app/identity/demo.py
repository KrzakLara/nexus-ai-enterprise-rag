"""DEMO authentication only (AUTH_MODE=demo, APP_ENV local / development / test).

Synthetic personas with a shared demo password and an HS256 demo JWT. config.validate_settings
refuses this mode in staging / production, and in AUTH_MODE=oidc every route here is disabled
and demo JWTs are rejected (OIDC tokens must be asymmetrically signed by the IdP).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from hashlib import pbkdf2_hmac
from hmac import compare_digest

import jwt
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..config import Settings
from ..database import set_security_context, user_context
from ..models import User

DEMO_ISSUER = "nexus-local-demo"
_DUMMY_HASH = "00" * 16 + ":" + "00" * 32


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    derived = pbkdf2_hmac("sha256", password.encode(), salt, 300_000)
    return f"{salt.hex()}:{derived.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split(":", 1)
        return compare_digest(hash_password(password, bytes.fromhex(salt)).split(":")[1], digest)
    except (ValueError, TypeError):
        return False


def make_token(user: User, cfg: Settings) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode({"sub": user.id, "tenant": user.tenant_id, "iss": DEMO_ISSUER, "iat": now,
                       "exp": now + timedelta(hours=8)}, cfg.jwt_secret, algorithm="HS256")


def decode_token(token: str, cfg: Settings) -> dict:
    payload = jwt.decode(token, cfg.jwt_secret, algorithms=["HS256"], issuer=DEMO_ISSUER,
                         options={"require": ["sub", "exp", "iss"]})
    if not isinstance(payload.get("sub"), str) or not isinstance(payload.get("tenant"), str):
        raise jwt.InvalidTokenError("demo token without subject / tenant")
    return payload


def check_credentials(db: Session, email: str, password: str) -> User | None:
    """Verify a demo password. The hash is read through nexus_rls.demo_credentials() on
    PostgreSQL (the runtime role has no SELECT privilege on users.password_hash). Unknown e-mail
    addresses still pay one PBKDF2 round (no timing-based account enumeration)."""
    email = email.lower().strip()
    if db.get_bind().dialect.name == "postgresql":
        row = db.execute(text("SELECT user_id, tenant_id, password_hash, status "
                              "FROM nexus_rls.demo_credentials(:e)"), {"e": email}).first()
    else:
        user = db.scalar(select(User).where(User.email == email, User.auth_source == "local"))
        row = (user.id, user.tenant_id, user.password_hash, user.status) if user else None
    if row is None:
        verify_password(password, _DUMMY_HASH)
        return None
    user_id, tenant_id, stored, status = row
    if not verify_password(password, stored or "") or status != "active":
        return None
    set_security_context(db, user_context(tenant_id, user_id))
    return db.get(User, user_id)
