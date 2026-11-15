"""In-process OpenID Provider for unit tests: real RSA / EC keys, signed JWTs, discovery, JWKS,
token endpoint with PKCE verification, refresh tokens and back-channel logout tokens. Served
through httpx.MockTransport, so the production OIDCClient code paths run unchanged.

Integration tests against a real Keycloak live in test_keycloak_integration.py.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import uuid
from dataclasses import dataclass, field
from dataclasses import replace as dc_replace
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

from backend.app.identity.oidc import OIDCClient

ISSUER = "https://idp.test/realms/nexus"
CLIENT_ID = "nexus-bff"
SESSION_KEY = base64.urlsafe_b64encode(b"k" * 32).decode()


def rsa_key(kid: str, alg: str = "RS256"):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid=kid, use="sig", alg=alg)
    return key, jwk


def ec_key(kid: str):
    key = ec.generate_private_key(ec.SECP256R1())
    jwk = json.loads(ECAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid=kid, use="sig", alg="ES256")
    return key, jwk


@dataclass
class FakeIdP:
    issuer: str = ISSUER
    client_id: str = CLIENT_ID
    keys: dict = field(default_factory=dict)          # kid -> (private key, jwk)
    signing_kid: str = "k1"
    codes: dict = field(default_factory=dict)         # code -> (claims, nonce, challenge)
    refresh: dict = field(default_factory=dict)       # refresh token -> claims
    jwks_requests: int = 0
    token_requests: list = field(default_factory=list)
    down: bool = False
    revoked_refresh: set = field(default_factory=set)

    def __post_init__(self):
        if not self.keys:
            self.keys["k1"] = rsa_key("k1")

    # ------------------------------------------------------------------ signing
    def sign(self, claims: dict, kid: str | None = None, alg: str = "RS256", headers: dict | None = None) -> str:
        kid = kid or self.signing_kid
        key = self.keys[kid][0]
        return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid, **(headers or {})})

    def claims(self, sub: str, groups: list[str], email: str | None = None, **extra) -> dict:
        now = int(time.time())
        base = {"iss": self.issuer, "sub": sub, "aud": self.client_id, "azp": self.client_id,
                "iat": now, "exp": now + 300, "typ": "ID", "email": email or f"{sub}@idp.test",
                "email_verified": True, "name": sub.title(), "groups": groups, "sid": f"sid-{sub}"}
        base.update(extra)
        return base

    def access_token(self, sub: str, groups: list[str], **extra) -> str:
        c = self.claims(sub, groups, **extra)
        c.update(aud=["nexus-api", "account"], typ="Bearer")
        c.update(extra)
        return self.sign(c)

    def logout_token(self, sid: str | None = None, sub: str | None = None, **extra) -> str:
        now = int(time.time())
        c = {"iss": self.issuer, "aud": self.client_id, "iat": now, "exp": now + 120, "jti": uuid.uuid4().hex,
             "events": {"http://schemas.openid.net/event/backchannel-logout": {}}}
        if sid:
            c["sid"] = sid
        if sub:
            c["sub"] = sub
        c.update(extra)
        return self.sign(c)

    # ------------------------------------------------------------------ authorization
    def authorize(self, authorization_url: str, sub: str, groups: list[str], email: str | None = None) -> tuple[str, str]:
        """What the browser + login page would do: returns (code, state)."""
        q = {k: v[0] for k, v in parse_qs(urlsplit(authorization_url).query).items()}
        assert q["response_type"] == "code" and q["client_id"] == self.client_id
        assert q["code_challenge_method"] == "S256" and len(q["code_challenge"]) == 43
        code = secrets.token_urlsafe(16)
        self.codes[code] = (self.claims(sub, groups, email=email), q["nonce"], q["code_challenge"])
        return code, q["state"]

    # ------------------------------------------------------------------ HTTP
    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("IdP down", request=request)
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={
                "issuer": self.issuer, "authorization_endpoint": f"{self.issuer}/protocol/openid-connect/auth",
                "token_endpoint": f"{self.issuer}/protocol/openid-connect/token",
                "jwks_uri": f"{self.issuer}/protocol/openid-connect/certs",
                "end_session_endpoint": f"{self.issuer}/protocol/openid-connect/logout",
                "code_challenge_methods_supported": ["plain", "S256"]})
        if path.endswith("/certs"):
            self.jwks_requests += 1
            return httpx.Response(200, json={"keys": [jwk for _, jwk in self.keys.values()]})
        if path.endswith("/token"):
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_requests.append(form)
            auth = request.headers.get("authorization", "")
            expected = "Basic " + base64.b64encode(f"{self.client_id}:secret".encode()).decode()
            if auth != expected:
                return httpx.Response(401, json={"error": "invalid_client"})
            if form.get("grant_type") == "authorization_code":
                entry = self.codes.pop(form.get("code", ""), None)
                if entry is None:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                claims, nonce, challenge = entry
                verifier = form.get("code_verifier", "")
                digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                if digest != challenge:
                    return httpx.Response(400, json={"error": "invalid_grant", "error_description": "PKCE"})
                return self._tokens(claims, nonce)
            if form.get("grant_type") == "refresh_token":
                token = form.get("refresh_token", "")
                if token in self.revoked_refresh or token not in self.refresh:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                claims = self.refresh.pop(token)
                return self._tokens(claims, None)
            return httpx.Response(400, json={"error": "unsupported_grant_type"})
        return httpx.Response(404)

    def _tokens(self, claims: dict, nonce: str | None) -> httpx.Response:
        now = int(time.time())
        id_claims = dict(claims, iat=now, exp=now + 300)
        if nonce:
            id_claims["nonce"] = nonce
        refresh_token = secrets.token_urlsafe(24)
        self.refresh[refresh_token] = claims
        return httpx.Response(200, json={
            "access_token": self.access_token(claims["sub"], claims["groups"]), "token_type": "Bearer",
            "expires_in": 300, "refresh_token": refresh_token, "id_token": self.sign(id_claims)})

    def client(self, cfg) -> OIDCClient:
        transport = httpx.MockTransport(self.handler)
        return OIDCClient(cfg, http_factory=lambda: httpx.Client(transport=transport))


def oidc_settings(base, **overrides):
    """Settings for AUTH_MODE=oidc against the fake IdP (local environment)."""
    values = dict(auth_mode="oidc", oidc_issuer=ISSUER, oidc_discovery_url="", oidc_client_id=CLIENT_ID,
                  oidc_client_secret="secret", oidc_redirect_uri="http://testserver/api/auth/oidc/callback",
                  oidc_post_logout_redirect_uri="http://testserver/", oidc_allowed_algs=("RS256", "ES256"),
                  oidc_allowed_tenants=("northstar", "otherco"), oidc_link_verified_email=True,
                  session_encryption_key=SESSION_KEY, frontend_origins=("http://testserver",),
                  oidc_jwks_min_refresh_seconds=0)
    values.update(overrides)
    return dc_replace(base, **values)
