"""Small, audited cryptographic helpers for the browser-session layer.

* random tokens: `secrets.token_urlsafe` (256 bit by default)
* stored token references: SHA-256 hex (the raw session token / state never touches the database)
* encryption at rest: AES-256-GCM with a random 96-bit nonce, key from SESSION_ENCRYPTION_KEY,
  associated data binds a ciphertext to its purpose (a refresh token cannot be decrypted as a
  PKCE verifier and vice versa)
* PKCE (RFC 7636): S256 code challenge
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def pkce_pair() -> tuple[str, str]:
    """(code_verifier, S256 code_challenge). Verifier: 86 characters from the unreserved set."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class TokenCipher:
    PREFIX = "v1."

    def __init__(self, key: bytes):
        if len(key) != 32:
            raise ValueError("AES-256-GCM needs a 32-byte key")
        self._aead = AESGCM(key)

    def encrypt(self, plaintext: str, purpose: str) -> str:
        nonce = os.urandom(12)
        ct = self._aead.encrypt(nonce, plaintext.encode("utf-8"), purpose.encode("utf-8"))
        return self.PREFIX + base64.urlsafe_b64encode(nonce + ct).decode("ascii")

    def decrypt(self, token: str, purpose: str) -> str:
        if not token.startswith(self.PREFIX):
            raise ValueError("unknown ciphertext format")
        raw = base64.urlsafe_b64decode(token[len(self.PREFIX):].encode("ascii"))
        return self._aead.decrypt(raw[:12], raw[12:], purpose.encode("utf-8")).decode("utf-8")
