"""OpenID Connect relying party (confidential client used by the backend-for-frontend).

Standards: OpenID Connect Core 1.0 (Authorization Code Flow), RFC 7636 (PKCE, S256),
OpenID Connect Discovery 1.0, RFC 7517 (JWKS), OpenID Connect Back-Channel Logout 1.0 and
RP-Initiated Logout 1.0.

Token validation never trusts an unverified payload. Order of checks:
  1. the header `alg` must be in the explicit allowlist (asymmetric only; `none` / HS* refused)
  2. a `kid` is required; the key comes from the IdP's JWKS (cached; an unknown kid triggers one
     rate-limited refetch, which is how key rotation is picked up)
  3. the JWK must fit the algorithm (kty, optional `alg` / `use`)
  4. signature, `iss` (exact), `aud`, `exp` / `nbf` / `iat` (with bounded clock skew)
  5. token-type specific checks: `nonce` and `azp` for ID tokens, `typ` for access tokens,
     `events` / no `nonce` for logout tokens
"""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlencode

import httpx
import jwt
from jwt import PyJWK

from ..config import Settings, settings as default_settings
from .crypto import constant_time_equal

BACKCHANNEL_LOGOUT_EVENT = "http://schemas.openid.net/event/backchannel-logout"
_KTY_FOR_ALG = {"RS": "RSA", "PS": "RSA", "ES": "EC"}


class OIDCError(Exception):
    """`code` is a fixed, low-cardinality category (metrics / logs / client error codes)."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


class TokenValidationError(OIDCError):
    pass


@dataclass(frozen=True)
class ProviderMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    end_session_endpoint: str | None
    userinfo_endpoint: str | None
    code_challenge_methods: tuple[str, ...]


class JWKSCache:
    """Thread-safe JWKS cache. A token with an unknown `kid` causes at most one refetch per
    `min_refresh_seconds`, so random kids cannot turn the API into an IdP traffic amplifier."""

    def __init__(self, fetch: Callable[[], dict], ttl_seconds: int, min_refresh_seconds: int,
                 clock: Callable[[], float] = time.monotonic):
        self._fetch, self._ttl, self._min_refresh, self._clock = fetch, ttl_seconds, min_refresh_seconds, clock
        self._keys: dict[str, dict] = {}
        self._fetched_at: float | None = None
        self._lock = threading.Lock()
        self.fetch_count = 0

    def _refresh(self) -> None:
        document = self._fetch()
        keys = document.get("keys") if isinstance(document, dict) else None
        if not isinstance(keys, list):
            raise OIDCError("jwks_invalid", "JWKS document has no 'keys' array")
        self._keys = {k["kid"]: k for k in keys if isinstance(k, dict) and isinstance(k.get("kid"), str)}
        self._fetched_at = self._clock()
        self.fetch_count += 1

    def get(self, kid: str) -> dict:
        with self._lock:
            now = self._clock()
            expired = self._fetched_at is None or now - self._fetched_at > self._ttl
            if expired:
                self._refresh()
            elif kid not in self._keys and now - (self._fetched_at or 0) >= self._min_refresh:
                self._refresh()  # key rotation: a new signing key appeared at the IdP
            jwk = self._keys.get(kid)
        if jwk is None:
            raise TokenValidationError("unknown_key", "token signed with a key that is not in the IdP JWKS")
        return jwk


def _key_for(jwk_dict: dict, alg: str) -> PyJWK:
    kty = _KTY_FOR_ALG.get(alg[:2])
    if kty is None or jwk_dict.get("kty") != kty:
        raise TokenValidationError("key_mismatch", f"JWK type {jwk_dict.get('kty')!r} cannot verify {alg}")
    if jwk_dict.get("use") not in (None, "sig"):
        raise TokenValidationError("key_mismatch", "JWK is not a signing key")
    if jwk_dict.get("alg") not in (None, alg):
        raise TokenValidationError("key_mismatch", "JWK algorithm does not match the token")
    try:
        return PyJWK(jwk_dict, algorithm=alg)
    except jwt.PyJWTError as exc:
        raise TokenValidationError("key_mismatch", f"unusable JWK: {type(exc).__name__}") from None


def validate_jwt(token: str, *, jwks: JWKSCache, issuer: str, audience: str | None,
                 allowed_algs: tuple[str, ...], leeway: int, require: tuple[str, ...] = ("exp", "iat"),
                 now: float | None = None) -> dict:
    """Signature + standard claims. Raises TokenValidationError with a fixed `code`."""
    if not isinstance(token, str) or token.count(".") != 2 or len(token) > 16_384:
        raise TokenValidationError("malformed", "not a compact JWS")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise TokenValidationError("malformed", "unreadable JWT header") from None
    alg = header.get("alg")
    if not isinstance(alg, str) or alg not in allowed_algs or alg.lower() == "none" or alg.startswith("HS"):
        raise TokenValidationError("alg_not_allowed", f"signature algorithm {alg!r} is not allowed")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise TokenValidationError("missing_kid", "token header has no key id")
    key = _key_for(jwks.get(kid), alg)
    options = {"require": list(dict.fromkeys(("iss", *require))), "verify_aud": audience is not None}
    try:
        claims = jwt.decode(token, key=key.key, algorithms=[alg], audience=audience, issuer=issuer,
                            leeway=leeway, options=options)
    except jwt.ExpiredSignatureError:
        raise TokenValidationError("expired", "token has expired") from None
    except jwt.ImmatureSignatureError:
        raise TokenValidationError("not_yet_valid", "token is not valid yet (nbf / iat)") from None
    except jwt.InvalidAudienceError:
        raise TokenValidationError("invalid_audience", "token audience does not include this client") from None
    except jwt.InvalidIssuerError:
        raise TokenValidationError("invalid_issuer", "token issuer is not the configured IdP") from None
    except jwt.InvalidSignatureError:
        raise TokenValidationError("invalid_signature", "token signature is invalid") from None
    except jwt.MissingRequiredClaimError as exc:
        raise TokenValidationError("missing_claim", f"required claim missing: {exc.claim}") from None
    except jwt.PyJWTError as exc:
        raise TokenValidationError("invalid_token", f"token rejected: {type(exc).__name__}") from None
    current = time.time() if now is None else now
    iat = claims.get("iat")
    if iat is not None and (not isinstance(iat, (int, float)) or iat > current + leeway):
        raise TokenValidationError("not_yet_valid", "token issued in the future")
    return claims


class OIDCClient:
    def __init__(self, cfg: Settings, http_factory: Callable[[], httpx.Client] | None = None):
        self.cfg = cfg
        self._http_factory = http_factory or (lambda: httpx.Client(
            timeout=cfg.oidc_http_timeout_seconds, follow_redirects=False))
        self._metadata: ProviderMetadata | None = None
        self._metadata_at = 0.0
        self._lock = threading.Lock()
        self.jwks = JWKSCache(self._fetch_jwks, cfg.oidc_jwks_cache_seconds, cfg.oidc_jwks_min_refresh_seconds)

    # ----------------------------------------------------------------- discovery / transport
    def _get_json(self, url: str) -> dict:
        try:
            with self._http_factory() as http:
                response = http.get(url, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise OIDCError("idp_unavailable", f"IdP request failed: {type(exc).__name__}") from None
        if response.status_code != 200:
            raise OIDCError("idp_unavailable", f"IdP answered HTTP {response.status_code}")
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError):
            raise OIDCError("idp_invalid_response", "IdP returned invalid JSON") from None

    def metadata(self) -> ProviderMetadata:
        with self._lock:
            if self._metadata and time.monotonic() - self._metadata_at < 3600:
                return self._metadata
        doc = self._get_json(self.cfg.effective_discovery_url)
        issuer = str(doc.get("issuer", "")).rstrip("/")
        if issuer != self.cfg.oidc_issuer:  # OIDC Discovery 4.3: issuer must match exactly
            raise OIDCError("issuer_mismatch", "discovery document issuer differs from OIDC_ISSUER")
        missing = [k for k in ("authorization_endpoint", "token_endpoint", "jwks_uri") if not doc.get(k)]
        if missing:
            raise OIDCError("idp_invalid_response", f"discovery document lacks {missing}")
        methods = tuple(doc.get("code_challenge_methods_supported") or ())
        if methods and "S256" not in methods:
            raise OIDCError("pkce_unsupported", "the IdP does not support PKCE S256")
        meta = ProviderMetadata(issuer=issuer, authorization_endpoint=doc["authorization_endpoint"],
                                token_endpoint=doc["token_endpoint"], jwks_uri=doc["jwks_uri"],
                                end_session_endpoint=doc.get("end_session_endpoint"),
                                userinfo_endpoint=doc.get("userinfo_endpoint"),
                                code_challenge_methods=methods)
        with self._lock:
            self._metadata, self._metadata_at = meta, time.monotonic()
        return meta

    def _fetch_jwks(self) -> dict:
        return self._get_json(self.metadata().jwks_uri)

    def _token_request(self, form: dict) -> dict:
        meta = self.metadata()
        try:
            with self._http_factory() as http:
                response = http.post(meta.token_endpoint, data=form,
                                     auth=(self.cfg.oidc_client_id, self.cfg.oidc_client_secret),
                                     headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise OIDCError("idp_unavailable", f"token endpoint unreachable: {type(exc).__name__}") from None
        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError):
            body = {}
        if response.status_code != 200:
            error = body.get("error") if isinstance(body, dict) else None
            code = "invalid_grant" if error == "invalid_grant" else "token_request_failed"
            raise OIDCError(code, f"token endpoint returned HTTP {response.status_code} ({error or 'no error code'})")
        if not isinstance(body, dict) or "access_token" not in body:
            raise OIDCError("idp_invalid_response", "token response without access_token")
        if str(body.get("token_type", "")).lower() != "bearer":
            raise OIDCError("idp_invalid_response", "unexpected token_type")
        return body

    # ----------------------------------------------------------------- flows
    def authorization_url(self, state: str, nonce: str, code_challenge: str) -> str:
        meta = self.metadata()
        query = urlencode({
            "response_type": "code", "client_id": self.cfg.oidc_client_id,
            "redirect_uri": self.cfg.oidc_redirect_uri, "scope": self.cfg.oidc_scopes,
            "state": state, "nonce": nonce, "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        })
        separator = "&" if "?" in meta.authorization_endpoint else "?"
        return f"{meta.authorization_endpoint}{separator}{query}"

    def exchange_code(self, code: str, code_verifier: str) -> dict:
        return self._token_request({"grant_type": "authorization_code", "code": code,
                                    "redirect_uri": self.cfg.oidc_redirect_uri,
                                    "code_verifier": code_verifier})

    def refresh(self, refresh_token: str) -> dict:
        return self._token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})

    def end_session_url(self, id_token_hint: str | None) -> str | None:
        endpoint = self.metadata().end_session_endpoint
        if not endpoint:
            return None
        params = {"client_id": self.cfg.oidc_client_id}
        if id_token_hint:
            params["id_token_hint"] = id_token_hint
        if self.cfg.oidc_post_logout_redirect_uri:
            params["post_logout_redirect_uri"] = self.cfg.oidc_post_logout_redirect_uri
        return f"{endpoint}{'&' if '?' in endpoint else '?'}{urlencode(params)}"

    # ----------------------------------------------------------------- token validation
    def _validate(self, token: str, audience: str | None, require: tuple[str, ...]) -> dict:
        return validate_jwt(token, jwks=self.jwks, issuer=self.cfg.oidc_issuer, audience=audience,
                            allowed_algs=self.cfg.oidc_allowed_algs,
                            leeway=self.cfg.oidc_clock_skew_seconds, require=require)

    def validate_id_token(self, id_token: str, nonce: str | None) -> dict:
        claims = self._validate(id_token, self.cfg.oidc_client_id, ("exp", "iat", "sub", "aud"))
        aud = claims.get("aud")
        audiences = aud if isinstance(aud, list) else [aud]
        azp = claims.get("azp")
        if (len(audiences) > 1 or azp is not None) and azp != self.cfg.oidc_client_id:
            raise TokenValidationError("invalid_azp", "ID token authorized party is not this client")
        if nonce is not None:
            token_nonce = claims.get("nonce")
            if not isinstance(token_nonce, str) or not constant_time_equal(token_nonce, nonce):
                raise TokenValidationError("nonce_mismatch", "ID token nonce does not match the login request")
        if claims.get("typ") not in (None, "ID"):
            raise TokenValidationError("wrong_token_type", "not an ID token")
        return _check_subject(claims)

    def validate_access_token(self, token: str) -> dict:
        claims = self._validate(token, self.cfg.oidc_api_audience, ("exp", "iat", "sub", "aud"))
        expected = self.cfg.oidc_access_token_typ
        if expected and claims.get("typ") != expected:
            raise TokenValidationError("wrong_token_type", "not an access token")
        return _check_subject(claims)

    def validate_logout_token(self, token: str) -> dict:
        claims = self._validate(token, self.cfg.oidc_client_id, ("iat", "jti", "aud"))
        events = claims.get("events")
        if not isinstance(events, dict) or BACKCHANNEL_LOGOUT_EVENT not in events:
            raise TokenValidationError("invalid_logout_token", "logout token lacks the back-channel logout event")
        if "nonce" in claims:
            raise TokenValidationError("invalid_logout_token", "logout tokens must not contain a nonce")
        if not claims.get("sid") and not claims.get("sub"):
            raise TokenValidationError("invalid_logout_token", "logout token names neither sid nor sub")
        return claims


def _check_subject(claims: dict) -> dict:
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub or len(sub) > 255:
        raise TokenValidationError("invalid_subject", "token subject missing or too long")
    return claims


@lru_cache(maxsize=4)
def _client_for(cfg: Settings) -> OIDCClient:
    return OIDCClient(cfg)


_override: OIDCClient | None = None


def get_oidc_client(cfg: Settings | None = None) -> OIDCClient:
    if _override is not None:
        return _override
    return _client_for(cfg or default_settings)


def set_oidc_client(client: OIDCClient | None) -> None:
    """Test hook: use a client wired to a local JWKS / a real Keycloak."""
    global _override
    _override = client
