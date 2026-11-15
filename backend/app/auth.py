"""Compatibility facade (v0.1-v0.3 import paths).

Authentication lives in backend.app.identity since Phase 4:
    identity.demo          demo personas, PBKDF2 password hashes, HS256 demo JWT (demo mode only)
    identity.dependencies  `current_user` (session cookie, demo JWT, OIDC access token)
"""
from .config import settings
from .identity.dependencies import current_principal, current_user
from .identity.demo import hash_password, verify_password
from .identity.demo import make_token as _make_token


def make_token(user, cfg=None) -> str:
    return _make_token(user, cfg or settings)


__all__ = ["current_principal", "current_user", "hash_password", "make_token", "verify_password"]
