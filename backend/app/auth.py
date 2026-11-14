"""Demo-only password and JWT auth. Replace with OIDC/SSO before real deployment."""
from datetime import datetime, timedelta, timezone
from hashlib import pbkdf2_hmac
from hmac import compare_digest
import os
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session
from .config import settings
from .database import get_db
from .models import User

bearer = HTTPBearer(auto_error=False)


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


def make_token(user: User) -> str:
    return jwt.encode(
        {"sub": user.id, "tenant": user.tenant_id, "iss": "nexus-local-demo",
         "exp": datetime.now(timezone.utc) + timedelta(hours=8)},
        settings.jwt_secret, algorithm="HS256"
    )


def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    if not settings.demo_mode:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "OIDC integration not configured")
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required")
    try:
        payload = jwt.decode(
            credentials.credentials, settings.jwt_secret,
            algorithms=["HS256"], issuer="nexus-local-demo", options={"require": ["sub", "exp", "iss"]}
        )
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid session") from None
    user = db.get(User, payload["sub"])
    if user is None or user.tenant_id != payload.get("tenant"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown account")
    return user
