"""Phase 4A: configuration fail-closed rules, strict JWT validation, OIDC Authorization Code + PKCE
through the backend (BFF), server-side sessions, CSRF, identity mapping and session lifecycle.

Uses the in-process fake IdP (real RSA / EC signatures). The `scenario` fixture runs every API test
on SQLite and on PostgreSQL (runtime role with row-level security).
"""
import threading
import time
from dataclasses import replace
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest

from backend.app.config import Settings, validate_settings
from backend.app.identity import oidc as oidc_mod
from backend.app.identity.mapping import MappingError, map_claims
from backend.app.identity.oidc import JWKSCache, TokenValidationError, validate_jwt
from backend.app.models import AuthSession, User, UserIdentity

from .oidc_fakes import CLIENT_ID, ISSUER, FakeIdP, ec_key, oidc_settings, rsa_key

N = "/tenants/northstar/groups/"


# ----------------------------------------------------------------------------- configuration

def _errors(**kw):
    return validate_settings(replace(Settings(), **kw))


def test_demo_mode_is_refused_in_production_like_environments():
    for env in ("staging", "production"):
        errors = _errors(app_env=env, auth_mode="demo", demo_mode=True)
        assert any("AUTH_MODE=demo" in e for e in errors)
        assert any("DEMO_MODE=true" in e for e in errors)


def test_incomplete_oidc_configuration_fails_closed():
    errors = _errors(auth_mode="oidc")
    for name in ("OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "OIDC_REDIRECT_URI",
                 "SESSION_ENCRYPTION_KEY", "OIDC_ALLOWED_TENANTS"):
        assert any(name in e for e in errors), name


def test_oidc_rejects_symmetric_or_none_algorithms_and_plain_http():
    base = oidc_settings(Settings(), oidc_redirect_uri="https://nexus.example/api/auth/oidc/callback")
    assert validate_settings(base) == []
    assert any("asymmetric" in e for e in validate_settings(replace(base, oidc_allowed_algs=("HS256",))))
    assert any("asymmetric" in e for e in validate_settings(replace(base, oidc_allowed_algs=("none",))))
    http = replace(base, oidc_issuer="http://idp.local/realms/x")
    assert any("https" in e for e in validate_settings(http))
    assert validate_settings(replace(http, oidc_allow_insecure_http=True)) == []  # local Keycloak only
    assert any("OIDC_ALLOW_INSECURE_HTTP" in e for e in validate_settings(
        replace(http, oidc_allow_insecure_http=True, app_env="production")))


def test_production_requires_secure_cookies_s3_queue_and_postgres():
    prod = oidc_settings(Settings(), app_env="production", demo_mode=False, session_cookie_secure=False,
                         frontend_origins=("https://nexus.example",))
    errors = validate_settings(prod)
    assert any("SESSION_COOKIE_SECURE" in e for e in errors)
    assert any("STORAGE_BACKEND=s3" in e for e in errors)
    assert any("PROCESSING_MODE=queue" in e for e in errors)
    assert any("PostgreSQL" in e for e in errors)
    ok = replace(prod, session_cookie_secure=True, storage_backend="s3", processing_mode="queue",
                 database_url="postgresql+psycopg://nexus_app:x@db/nexus", oidc_issuer="https://idp/realms/n",
                 oidc_redirect_uri="https://nexus.example/api/auth/oidc/callback")
    assert validate_settings(ok) == []


def test_demo_session_key_and_wildcard_origins_are_rejected():
    assert any("SESSION_ENCRYPTION_KEY" in e for e in validate_settings(
        oidc_settings(Settings(), session_encryption_key="not-base64-32-bytes")))
    assert any("FRONTEND_ORIGINS" in e for e in _errors(frontend_origins=("*",)))


# ----------------------------------------------------------------------------- token validation

@pytest.fixture
def idp():
    return FakeIdP()


def _cache(idp, ttl=300, min_refresh=0):
    def fetch():
        idp.jwks_requests += 1
        return {"keys": [jwk for _, jwk in idp.keys.values()]}
    return JWKSCache(fetch, ttl, min_refresh)


def _validate(idp, token, cache=None, audience=CLIENT_ID, algs=("RS256", "ES256"), leeway=0):
    return validate_jwt(token, jwks=cache or _cache(idp), issuer=ISSUER, audience=audience,
                        allowed_algs=algs, leeway=leeway)


def test_valid_rs256_and_es256_tokens_are_accepted(idp):
    assert _validate(idp, idp.sign(idp.claims("alice", [])))["sub"] == "alice"
    idp.keys["e1"] = ec_key("e1")
    assert _validate(idp, idp.sign(idp.claims("bob", []), kid="e1", alg="ES256"))["sub"] == "bob"


@pytest.mark.parametrize("mutate, code", [
    (lambda c: c.update(iss="https://evil.test/realms/nexus"), "invalid_issuer"),
    (lambda c: c.update(aud="another-client"), "invalid_audience"),
    (lambda c: c.update(exp=int(time.time()) - 60), "expired"),
    (lambda c: c.update(nbf=int(time.time()) + 600), "not_yet_valid"),
    (lambda c: c.update(iat=int(time.time()) + 600), "not_yet_valid"),
    (lambda c: c.pop("exp"), "missing_claim"),
])
def test_claim_violations_are_rejected(idp, mutate, code):
    claims = idp.claims("alice", [])
    mutate(claims)
    with pytest.raises(TokenValidationError) as err:
        _validate(idp, idp.sign(claims))
    assert err.value.code == code


def test_signature_from_a_foreign_key_with_a_known_kid_is_rejected(idp):
    forger, _ = rsa_key("k1")  # attacker reuses the IdP's kid with their own key
    token = jwt.encode(idp.claims("alice", []), forger, algorithm="RS256", headers={"kid": "k1"})
    with pytest.raises(TokenValidationError) as err:
        _validate(idp, token)
    assert err.value.code == "invalid_signature"


def test_tampered_payload_is_rejected(idp):
    token = idp.sign(idp.claims("alice", [N + "finance"]))
    header, payload, sig = token.split(".")
    forged = jwt.utils.base64url_encode(
        jwt.utils.base64url_decode(payload).replace(b'"alice"', b'"admin"')).decode()
    with pytest.raises(TokenValidationError) as err:
        _validate(idp, f"{header}.{forged}.{sig}")
    assert err.value.code == "invalid_signature"


def test_alg_none_and_hs256_key_confusion_are_rejected(idp):
    claims = idp.claims("alice", [])
    unsigned = jwt.encode(claims, None, algorithm="none", headers={"kid": "k1"})
    with pytest.raises(TokenValidationError) as err:
        _validate(idp, unsigned)
    assert err.value.code == "alg_not_allowed"
    # HS256 signed with the PUBLIC key bytes (classic algorithm-confusion attack)
    public_pem = idp.keys["k1"][0].public_key().public_bytes(
        __import__("cryptography").hazmat.primitives.serialization.Encoding.PEM,
        __import__("cryptography").hazmat.primitives.serialization.PublicFormat.SubjectPublicKeyInfo)
    import hmac
    import hashlib
    header = jwt.utils.base64url_encode(b'{"alg":"HS256","kid":"k1","typ":"JWT"}').decode()
    body = jwt.utils.base64url_encode(__import__("json").dumps(claims).encode()).decode()
    sig = jwt.utils.base64url_encode(hmac.new(public_pem, f"{header}.{body}".encode(), hashlib.sha256).digest()).decode()
    with pytest.raises(TokenValidationError) as err:
        _validate(idp, f"{header}.{body}.{sig}", algs=("RS256", "HS256"))  # even if misconfigured
    assert err.value.code == "alg_not_allowed"


def test_missing_kid_malformed_and_wrong_key_type_are_rejected(idp):
    claims = idp.claims("alice", [])
    no_kid = jwt.encode(claims, idp.keys["k1"][0], algorithm="RS256")
    with pytest.raises(TokenValidationError) as err:
        _validate(idp, no_kid)
    assert err.value.code == "missing_kid"
    with pytest.raises(TokenValidationError) as err:
        _validate(idp, "not.a.jwt.at.all")
    assert err.value.code == "malformed"
    idp.keys["e1"] = ec_key("e1")
    with pytest.raises(TokenValidationError) as err:  # RS256 header pointing at an EC key
        _validate(idp, jwt.encode(claims, idp.keys["k1"][0], algorithm="RS256", headers={"kid": "e1"}))
    assert err.value.code == "key_mismatch"


def test_key_rotation_is_picked_up_and_unknown_kids_are_rate_limited(idp):
    cache = _cache(idp, ttl=300, min_refresh=3600)
    assert _validate(idp, idp.sign(idp.claims("a", [])), cache)["sub"] == "a"
    assert idp.jwks_requests == 1
    # random kids do not cause refetch storms
    for _ in range(5):
        with pytest.raises(TokenValidationError):
            _validate(idp, jwt.encode(idp.claims("a", []), idp.keys["k1"][0], algorithm="RS256",
                                      headers={"kid": "random-kid"}), cache)
    assert idp.jwks_requests == 1
    # the IdP rotates to a new key: one refetch once the minimum interval allows it
    idp.keys["k2"] = rsa_key("k2")
    idp.signing_kid = "k2"
    rotated = idp.sign(idp.claims("b", []))
    cache._min_refresh = 0
    assert _validate(idp, rotated, cache)["sub"] == "b"
    assert idp.jwks_requests == 2
    # the old key is removed at the IdP: after the cache TTL, old-key tokens fail
    old = idp.sign(idp.claims("c", []), kid="k1")
    del idp.keys["k1"]
    cache._ttl = -1
    with pytest.raises(TokenValidationError) as err:
        _validate(idp, old, cache)
    assert err.value.code == "unknown_key"


def test_jwks_outage_keeps_known_keys_usable_but_never_admits_unknown_ones(idp):
    """Stale-while-revalidate: an IdP outage during a JWKS refresh must not reject every valid
    token (found under load: Keycloak saturated, JWKS TTL expired -> all bearer requests 401)."""
    state = {"down": False}

    def fetch():
        if state["down"]:
            raise oidc_mod.OIDCError("idp_unavailable", "IdP down")
        idp.jwks_requests += 1
        return {"keys": [jwk for _, jwk in idp.keys.values()]}
    cache = JWKSCache(fetch, ttl_seconds=0, min_refresh_seconds=0, stale_max_seconds=3600)
    assert _validate(idp, idp.sign(idp.claims("a", [])), cache)["sub"] == "a"
    state["down"] = True
    time.sleep(0.01)  # TTL expired: a refresh is attempted and fails
    assert _validate(idp, idp.sign(idp.claims("b", [])), cache)["sub"] == "b"
    assert cache.stale_uses == 1
    idp.keys["k9"] = rsa_key("k9")
    with pytest.raises(oidc_mod.OIDCError) as err:  # a key we never saw cannot be trusted while down
        _validate(idp, idp.sign(idp.claims("c", []), kid="k9"), cache)
    assert err.value.code == "idp_unavailable"


def test_jwks_refresh_is_single_flight_and_backs_off_after_a_failure(idp):
    now = {"t": 0.0}
    release, in_fetch = threading.Event(), threading.Event()
    state = {"calls": 0, "down": False, "block": False}

    def fetch():
        state["calls"] += 1
        if state["block"]:
            in_fetch.set()
            assert release.wait(5)
        if state["down"]:
            raise oidc_mod.OIDCError("idp_unavailable", "IdP down")
        return {"keys": [jwk for _, jwk in idp.keys.values()]}

    cache = JWKSCache(fetch, ttl_seconds=300, min_refresh_seconds=10, clock=lambda: now["t"])
    k1 = cache.get("k1")
    # the TTL expires and the IdP adds a key; the refresh blocks inside fetch()
    now["t"], state["block"] = 301.0, True
    idp.keys["k2"] = rsa_key("k2")
    out = {}
    refresher = threading.Thread(target=lambda: out.update(a=cache.get("k1")))
    refresher.start()
    assert in_fetch.wait(5)
    assert cache.get("k1") == k1                      # cached key served while the fetch is in flight
    waiter = threading.Thread(target=lambda: out.update(c=cache.get("k2")))
    waiter.start()
    time.sleep(0.1)
    assert "c" not in out                             # a kid the cache lacks waits for that fetch ...
    release.set()
    refresher.join(5)
    waiter.join(5)
    assert out["a"] == k1 and out["c"]["kid"] == "k2" # ... and then gets the new key
    assert state["calls"] == 2                        # one refresh for three concurrent callers
    # the IdP goes down after the next TTL: one failed attempt, then back-off on the cached keys
    state["block"], state["down"] = False, True
    now["t"] = 700.0
    assert cache.get("k1") == k1 and state["calls"] == 3
    now["t"] = 705.0
    assert cache.get("k2")["kid"] == "k2" and state["calls"] == 3   # within back-off: no request
    with pytest.raises(oidc_mod.OIDCError) as err:    # an unseen kid is an IdP problem (503), not 401
        cache.get("never-seen")
    assert err.value.code == "idp_unavailable" and not isinstance(err.value, TokenValidationError)
    now["t"] = 711.0                                  # back-off over: the next call tries again
    assert cache.get("k1") == k1 and state["calls"] == 4
    # beyond stale_max the cached keys are no longer trusted
    now["t"] = 301.0 + 3601
    with pytest.raises(oidc_mod.OIDCError):
        cache.get("k1")


def test_an_unexpected_jwks_fetch_error_does_not_wedge_the_cache(idp):
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("malformed discovery document")  # not an OIDCError
        return {"keys": [jwk for _, jwk in idp.keys.values()]}
    cache = JWKSCache(fetch, ttl_seconds=300, min_refresh_seconds=0)
    with pytest.raises(oidc_mod.OIDCError) as err:
        cache.get("k1")
    assert err.value.code == "idp_invalid_response"
    assert cache.get("k1")["kid"] == "k1" and calls["n"] == 2  # the refresh flag was released


def test_bearer_requests_get_503_not_401_when_the_idp_cannot_be_reached(oidc_app):
    client, _, idp, cfg = oidc_app
    idp.down = True  # no JWKS was ever fetched by this client: nothing to fall back to
    r = client.get("/api/documents", headers={"Authorization": "Bearer " + idp.access_token(
        "kc-finance", [N + "finance"], email="finance@northstar.demo")})
    assert r.status_code == 503 and r.json()["detail"]["code"] == "idp_unavailable"
    assert r.headers["retry-after"] == "5"


def test_id_token_nonce_azp_and_typ_checks(idp):
    cfg = oidc_settings(Settings())
    client = idp.client(cfg)
    good = idp.sign(idp.claims("alice", [], nonce="n1"))
    assert client.validate_id_token(good, "n1")["sub"] == "alice"
    for token, code in ((good, "nonce_mismatch"),
                        (idp.sign(idp.claims("alice", [], nonce="n1", azp="other")), "invalid_azp"),
                        (idp.sign(idp.claims("alice", [], nonce="n1", aud=[CLIENT_ID, "x"], azp=None)), "invalid_azp"),
                        (idp.sign(idp.claims("alice", [], nonce="n1", typ="Bearer")), "wrong_token_type")):
        with pytest.raises(TokenValidationError) as err:
            client.validate_id_token(token, "n1" if code != "nonce_mismatch" else "other-nonce")
        assert err.value.code == code


def test_access_token_requires_api_audience_and_bearer_type(idp):
    client = idp.client(oidc_settings(Settings()))
    assert client.validate_access_token(idp.access_token("alice", []))["sub"] == "alice"
    with pytest.raises(TokenValidationError) as err:  # an ID token replayed as an access token
        client.validate_access_token(idp.sign(idp.claims("alice", [])))
    assert err.value.code == "invalid_audience"
    with pytest.raises(TokenValidationError) as err:
        client.validate_access_token(idp.access_token("alice", [], typ="ID"))
    assert err.value.code == "wrong_token_type"


def test_logout_token_rules(idp):
    client = idp.client(oidc_settings(Settings()))
    assert client.validate_logout_token(idp.logout_token(sid="s1"))["sid"] == "s1"
    for token in (idp.logout_token(sid="s1", nonce="x"), idp.logout_token(),
                  idp.logout_token(sid="s1", events={"other": {}})):
        with pytest.raises(TokenValidationError):
            client.validate_logout_token(token)


def test_discovery_issuer_mismatch_and_missing_pkce_support(idp):
    client = idp.client(oidc_settings(Settings(), oidc_issuer="https://other-idp.test/realms/nexus"))
    with pytest.raises(oidc_mod.OIDCError) as err:
        client.metadata()
    assert err.value.code == "issuer_mismatch"


# ----------------------------------------------------------------------------- claim mapping

def test_tenant_groups_and_role_are_mapped_from_the_allowlisted_tree():
    cfg = oidc_settings(Settings())
    m = map_claims({"sub": "u", "groups": [N + "finance", N + "legal", "/tenants/northstar/roles/reviewer",
                                           "/unrelated/group"], "email": "U@X.test", "email_verified": True}, cfg)
    assert (m.tenant_id, m.groups, m.role, m.email) == ("northstar", ("finance", "legal"), "reviewer", "u@x.test")


@pytest.mark.parametrize("groups, code", [
    ([], "no_tenant"),
    (["/unrelated/finance"], "no_tenant"),
    ([N + "finance", "/tenants/otherco/groups/finance"], "ambiguous_tenant"),
    (["/tenants/evilcorp/groups/finance"], "tenant_not_allowed"),
    ([N + "Finance Team!"], "invalid_group"),
    (["/tenants/northstar/roles/superuser"], "invalid_role"),
    ("not-a-list", "invalid_group_claim"),
])
def test_unmappable_identities_are_refused(groups, code):
    with pytest.raises(MappingError) as err:
        map_claims({"sub": "u", "groups": groups}, oidc_settings(Settings()))
    assert err.value.code == code


# ----------------------------------------------------------------------------- BFF flow (API)

@pytest.fixture
def oidc_app(scenario, monkeypatch, idp):
    from backend.app import main

    client, token, factory = scenario
    cfg = oidc_settings(main.settings, storage_dir=main.settings.storage_dir)
    monkeypatch.setattr(main, "settings", cfg)
    oidc_mod.set_oidc_client(idp.client(cfg))
    yield client, factory, idp, cfg
    oidc_mod.set_oidc_client(None)
    client.cookies.clear()


def login(client, idp, sub="kc-finance", groups=(N + "finance",), email="finance@northstar.demo", return_to="/workspace"):
    start = client.get("/api/auth/oidc/login", params={"return_to": return_to}, follow_redirects=False)
    assert start.status_code == 302, start.text
    location = start.headers["location"]
    assert location.startswith(f"{ISSUER}/protocol/openid-connect/auth?")
    code, state = idp.authorize(location, sub, list(groups), email=email)
    return client.get("/api/auth/oidc/callback", params={"code": code, "state": state}, follow_redirects=False)


def test_oidc_login_creates_a_server_session_and_maps_to_the_existing_account(oidc_app):
    client, factory, idp, cfg = oidc_app
    done = login(client, idp)
    assert done.status_code == 302 and done.headers["location"] == "http://testserver/workspace"
    cookie = done.headers.get("set-cookie", "")
    assert "nexus_session=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie.replace("Lax", "lax")
    session = client.get("/api/auth/session")
    assert session.status_code == 200, session.text
    body = session.json()
    assert body["user"]["email"] == "finance@northstar.demo" and body["user"]["tenant"] == "northstar"
    assert body["method"] == "session" and body["csrf_token"]
    # the verified e-mail linked the IdP identity to the existing seeded user u1 (owner of d_fin)
    with factory() as db:
        binding = db.query(UserIdentity).one()
        assert (binding.user_id, binding.issuer, binding.subject) == ("u1", ISSUER, "kc-finance")
        row = db.query(AuthSession).one()
        assert row.auth_mode == "oidc" and row.refresh_token_enc.startswith("v1.") and len(row.token_hash) == 64
    # no token reaches the browser: neither in the body nor in a readable cookie
    assert "access_token" not in session.text and "refresh_token" not in session.text
    # authentication is not authorization: the documents are still filtered by the ACL / RLS
    assert [d["id"] for d in client.get("/api/documents").json()] == ["d_fin"]


def test_callback_rejects_wrong_state_replay_missing_binding_and_pkce_tampering(oidc_app):
    client, _, idp, _ = oidc_app
    start = client.get("/api/auth/oidc/login", follow_redirects=False)
    code, state = idp.authorize(start.headers["location"], "kc-finance", [N + "finance"])
    bad = client.get("/api/auth/oidc/callback", params={"code": code, "state": state + "x"}, follow_redirects=False)
    assert "auth_error=invalid_state" in bad.headers["location"]
    # missing browser-binding cookie (login CSRF / injected authorization response)
    binding = client.cookies.get("nexus_login")
    client.cookies.delete("nexus_login")
    injected = client.get("/api/auth/oidc/callback", params={"code": code, "state": state}, follow_redirects=False)
    assert "auth_error=invalid_state" in injected.headers["location"]
    # the state was consumed by the failed attempt: replaying it with the cookie is refused too
    client.cookies.set("nexus_login", binding)
    replay = client.get("/api/auth/oidc/callback", params={"code": code, "state": state}, follow_redirects=False)
    assert "auth_error=invalid_state" in replay.headers["location"]
    assert client.get("/api/auth/session").status_code == 401
    # PKCE: the IdP refuses a code whose verifier does not match the challenge
    start = client.get("/api/auth/oidc/login", follow_redirects=False)
    code, state = idp.authorize(start.headers["location"], "kc-finance", [N + "finance"])
    claims, nonce, _challenge = idp.codes[code]
    idp.codes[code] = (claims, nonce, "A" * 43)
    tampered = client.get("/api/auth/oidc/callback", params={"code": code, "state": state}, follow_redirects=False)
    assert "auth_error=login_failed" in tampered.headers["location"]


def test_open_redirects_are_neutralised(oidc_app):
    client, _, idp, _ = oidc_app
    for target in ("https://evil.test/x", "//evil.test", "/\\evil.test", "javascript:alert(1)"):
        done = login(client, idp, return_to=target)
        assert done.headers["location"] == "http://testserver/", target
        client.cookies.clear()


def test_identity_without_tenant_or_with_two_tenants_gets_no_session(oidc_app):
    client, factory, idp, _ = oidc_app
    for groups in ([], [N + "finance", "/tenants/otherco/groups/finance"]):
        done = login(client, idp, sub="kc-x", groups=groups, email="x@idp.test")
        assert "auth_error=" in done.headers["location"]
        assert client.get("/api/auth/session").status_code == 401
    with factory() as db:
        assert db.query(AuthSession).count() == 0 and db.query(UserIdentity).count() == 0


def test_bound_identity_cannot_switch_tenant_and_jit_users_start_without_documents(oidc_app):
    client, factory, idp, _ = oidc_app
    assert login(client, idp).status_code == 302
    client.cookies.clear()
    moved = login(client, idp, groups=["/tenants/otherco/groups/finance"])
    assert "auth_error=access_denied" in moved.headers["location"]
    client.cookies.clear()
    # unknown, verified-new user: provisioned just-in-time, sees nothing until granted
    fresh = login(client, idp, sub="kc-new", groups=[N + "legal"], email="new.person@northstar.demo")
    assert fresh.status_code == 302 and "auth_error" not in fresh.headers["location"]
    assert client.get("/api/documents").json() == []
    with factory() as db:
        user = db.query(User).filter_by(email="new.person@northstar.demo").one()
        assert (user.tenant_id, user.groups, user.auth_source, user.password_hash) == ("northstar", ["legal"], "oidc", "!")


def test_cookie_session_requires_csrf_and_an_allowed_origin_for_unsafe_methods(oidc_app):
    client, _, idp, _ = oidc_app
    login(client, idp)
    csrf = client.get("/api/auth/session").json()["csrf_token"]
    ask = {"question": "Atlas supplier annual value", "mode": "bm25"}
    assert client.post("/api/ask", json=ask).status_code == 403
    assert client.post("/api/ask", json=ask, headers={"X-CSRF-Token": "forged"}).status_code == 403
    hostile = client.post("/api/ask", json=ask, headers={"X-CSRF-Token": csrf, "Origin": "https://evil.test"})
    assert hostile.status_code == 403 and hostile.json()["detail"]["code"] == "origin_not_allowed"
    ok = client.post("/api/ask", json=ask, headers={"X-CSRF-Token": csrf, "Origin": "http://testserver"})
    assert ok.status_code == 200 and ok.json()["sources"][0]["document_id"] == "d_fin"
    assert client.get("/api/auth/session").json()["csrf_token"] == csrf  # stable for the session


def test_logout_revokes_the_session_and_returns_the_rp_initiated_logout_url(oidc_app):
    client, _, idp, _ = oidc_app
    login(client, idp)
    cookie = client.cookies.get("nexus_session")
    csrf = client.get("/api/auth/session").json()["csrf_token"]
    out = client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    assert out.status_code == 200
    url = out.json()["logout_url"]
    q = parse_qs(urlsplit(url).query)
    assert url.startswith(f"{ISSUER}/protocol/openid-connect/logout") and q["id_token_hint"] and \
        q["post_logout_redirect_uri"] == ["http://testserver/"]
    client.cookies.set("nexus_session", cookie)  # a stolen copy of the old cookie
    assert client.get("/api/auth/session").json()["detail"]["code"] == "session_revoked"


def test_backchannel_logout_ends_sessions_of_that_idp_session(oidc_app):
    client, _, idp, _ = oidc_app
    login(client, idp)
    assert client.get("/api/auth/session").status_code == 200
    assert client.post("/api/auth/oidc/backchannel-logout", data={"logout_token": "garbage"}).status_code == 400
    assert client.post("/api/auth/oidc/backchannel-logout",
                       data={"logout_token": idp.logout_token(sid="sid-kc-finance")}).status_code == 200
    assert client.get("/api/auth/session").json()["detail"]["code"] == "session_revoked"


def test_expired_sessions_are_rejected(oidc_app):
    client, factory, idp, _ = oidc_app
    login(client, idp)
    with factory() as db:
        row = db.query(AuthSession).one()
        row.idle_expires_at = row.idle_expires_at - timedelta(days=2)
        db.commit()
    expired = client.get("/api/documents")
    assert expired.status_code == 401 and expired.json()["detail"]["code"] == "session_expired"


def test_revalidation_syncs_group_removal_and_ends_revoked_idp_sessions(oidc_app):
    client, factory, idp, cfg = oidc_app
    login(client, idp, groups=(N + "finance", N + "hr"))
    assert {d["id"] for d in client.get("/api/documents").json()} == {"d_fin", "d_hr"}

    def due():
        with factory() as db:
            row = db.query(AuthSession).one()
            row.revalidate_after = row.created_at - timedelta(seconds=1)
            db.commit()
    # the IdP administrator removes the user from 'hr': applied at the next revalidation
    for token in list(idp.refresh):
        idp.refresh[token]["groups"] = [N + "finance"]
    due()
    assert [d["id"] for d in client.get("/api/documents").json()] == ["d_fin"]
    # the IdP session is revoked (refresh token refused): the local session ends
    idp.revoked_refresh.update(idp.refresh)
    due()
    gone = client.get("/api/documents")
    assert gone.status_code == 401 and gone.json()["detail"]["code"] == "session_revoked"


def test_idp_outage_during_revalidation_is_tolerated_only_within_the_grace_period(oidc_app):
    client, factory, idp, _ = oidc_app
    login(client, idp)
    idp.down = True
    with factory() as db:
        row = db.query(AuthSession).one()
        row.revalidate_after = row.created_at - timedelta(seconds=5)
        db.commit()
    assert client.get("/api/documents").status_code == 200  # inside SESSION_REVALIDATE_GRACE_SECONDS
    with factory() as db:
        row = db.query(AuthSession).one()
        row.revalidate_after = row.created_at - timedelta(hours=1)
        db.commit()
    assert client.get("/api/documents").json()["detail"]["code"] == "revalidation_unavailable"


def test_oidc_bearer_access_tokens_and_rejection_of_demo_tokens(oidc_app):
    client, _, idp, _ = oidc_app
    from backend.app.identity import demo
    from backend.app import main

    bearer = {"Authorization": "Bearer " + idp.access_token("kc-finance", [N + "finance"],
                                                            email="finance@northstar.demo")}
    assert [d["id"] for d in client.get("/api/documents", headers=bearer).json()] == ["d_fin"]
    # an HS256 demo token signed with JWT_SECRET is worthless in OIDC mode
    fake_user = User(id="u2", tenant_id="northstar", email="admin@northstar.demo", role="admin", groups=[])
    demo_token = demo.make_token(fake_user, main.settings)
    refused = client.get("/api/documents", headers={"Authorization": f"Bearer {demo_token}"})
    assert refused.status_code == 401
    # demo password login is disabled
    assert client.post("/api/auth/login", json={"email": "admin@northstar.demo",
                                                "password": "TestPass123!"}).status_code == 404
    # an expired access token (beyond the 30 s clock-skew allowance) is refused
    stale = idp.access_token("kc-finance", [N + "finance"], exp=int(time.time()) - 120)
    assert client.get("/api/documents", headers={"Authorization": f"Bearer {stale}"}).status_code == 401


# ----------------------------------------------------------------------------- demo mode

def test_demo_login_issues_a_cookie_session_with_csrf(scenario):
    client, _, factory = scenario
    r = client.post("/api/auth/login", json={"email": "hr@northstar.demo", "password": "TestPass123!"})
    assert r.status_code == 200 and r.json()["csrf_token"]
    assert "HttpOnly" in r.headers["set-cookie"]
    assert [d["id"] for d in client.get("/api/documents").json()] == ["d_hr"]
    ask = {"question": "salary budget", "mode": "bm25"}
    assert client.post("/api/ask", json=ask).status_code == 403  # cookie without CSRF token
    assert client.post("/api/ask", json=ask, headers={"X-CSRF-Token": r.json()["csrf_token"]}).status_code == 200
    # new login = new session id (no fixation)
    first = client.cookies.get("nexus_session")
    client.post("/api/auth/login", json={"email": "hr@northstar.demo", "password": "TestPass123!"})
    assert client.cookies.get("nexus_session") != first
    with factory() as db:
        assert db.query(AuthSession).count() == 2
    client.cookies.clear()


def test_demo_login_failures_do_not_reveal_whether_the_account_exists(scenario):
    client, _, _ = scenario
    wrong = client.post("/api/auth/login", json={"email": "hr@northstar.demo", "password": "nope"})
    unknown = client.post("/api/auth/login", json={"email": "ghost@northstar.demo", "password": "nope"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_disabled_accounts_are_refused_and_their_sessions_end(scenario):
    client, token, factory = scenario
    r = client.post("/api/auth/login", json={"email": "hr@northstar.demo", "password": "TestPass123!"})
    assert r.status_code == 200
    with factory() as db:
        db.get(User, "u0").status = "disabled"
        db.commit()
    assert client.get("/api/documents").status_code == 403
    client.cookies.clear()
    assert client.post("/api/auth/login", json={"email": "hr@northstar.demo",
                                                "password": "TestPass123!"}).status_code == 401
