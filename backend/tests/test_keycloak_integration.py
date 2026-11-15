"""Phase 4A against a REAL Keycloak (realm docker/keycloak/nexus-realm.json).

Runs when TEST_KEYCLOAK_URL is set (e.g. http://localhost:8080), together with the admin
credentials TEST_KEYCLOAK_ADMIN / TEST_KEYCLOAK_ADMIN_PASSWORD (bootstrap admin of the disposable
container). Nothing here is mocked: discovery, JWKS, the login page, the authorization code with
PKCE, refresh tokens, key rotation and session revocation are Keycloak's own.

    docker run -d --name nexus-p4-keycloak -p 127.0.0.1:8080:8080 \\
      -e KC_BOOTSTRAP_ADMIN_USERNAME=kc-test-admin -e KC_BOOTSTRAP_ADMIN_PASSWORD=kc-test-admin-secret \\
      -e KC_HOSTNAME=http://localhost:8080 -e KC_HOSTNAME_BACKCHANNEL_DYNAMIC=true \\
      -v ./docker/keycloak/nexus-realm.json:/opt/keycloak/data/import/nexus-realm.json:ro \\
      quay.io/keycloak/keycloak:26.4.2 start-dev --import-realm
"""
import html
import os
import re
import time
from dataclasses import replace
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from backend.app.identity import oidc as oidc_mod
from backend.app.identity.mapping import map_claims
from backend.app.identity.oidc import OIDCClient
from backend.app.models import AuthSession, UserIdentity

from .oidc_fakes import SESSION_KEY

KC = os.getenv("TEST_KEYCLOAK_URL", "").rstrip("/")
pytestmark = [pytest.mark.keycloak, pytest.mark.skipif(not KC, reason="TEST_KEYCLOAK_URL not set (real Keycloak)")]
ISSUER = f"{KC}/realms/nexus"
PASSWORD = "DemoPass123!"  # synthetic local-only demo credential from the realm import


def kc_settings(base, **overrides):
    values = dict(auth_mode="oidc", oidc_issuer=ISSUER, oidc_discovery_url="", oidc_client_id="nexus-bff",
                  oidc_client_secret="nexus-bff-local-dev-secret-change-me",
                  oidc_redirect_uri="http://localhost:8000/api/auth/oidc/callback",
                  oidc_post_logout_redirect_uri="http://localhost:5173/", oidc_allowed_algs=("RS256",),
                  oidc_allowed_tenants=("northstar", "otherco"), oidc_link_verified_email=True,
                  oidc_allow_insecure_http=True, session_encryption_key=SESSION_KEY,
                  frontend_origins=("http://localhost:5173", "http://testserver"),
                  oidc_jwks_min_refresh_seconds=0)
    values.update(overrides)
    return replace(base, **values)


def admin_token() -> str:
    r = httpx.post(f"{KC}/realms/master/protocol/openid-connect/token", data={
        "grant_type": "password", "client_id": "admin-cli",
        "username": os.getenv("TEST_KEYCLOAK_ADMIN", "kc-test-admin"),
        "password": os.getenv("TEST_KEYCLOAK_ADMIN_PASSWORD", "kc-test-admin-secret")}, timeout=10)
    r.raise_for_status()
    return r.json()["access_token"]


def admin(method: str, path: str, **kwargs) -> httpx.Response:
    r = httpx.request(method, f"{KC}/admin/realms/nexus{path}", timeout=10,
                      headers={"Authorization": f"Bearer {admin_token()}"}, **kwargs)
    r.raise_for_status()
    return r


def user_id(email: str) -> str:
    return admin("GET", "/users", params={"email": email, "exact": "true"}).json()[0]["id"]


def group_id(path: str) -> str:
    return admin("GET", "/group-by-path" + path).json()["id"]


def password_grant(email: str) -> dict:
    r = httpx.post(f"{ISSUER}/protocol/openid-connect/token", timeout=10, data={
        "grant_type": "password", "client_id": "nexus-loadtest", "client_secret": "nexus-loadtest-local-dev-secret",
        "username": email, "password": PASSWORD, "scope": "openid"})
    r.raise_for_status()
    return r.json()


def browser_login(client, email: str, return_to: str = "/"):
    """BFF login exactly as a browser does it: API -> Keycloak login form -> API callback."""
    start = client.get("/api/auth/oidc/login", params={"return_to": return_to}, follow_redirects=False)
    assert start.status_code == 302, start.text
    with httpx.Client(follow_redirects=False, timeout=15) as browser:
        page = browser.get(start.headers["location"])
        assert page.status_code == 200, page.text[:300]
        action = html.unescape(re.search(r'<form[^>]+id="kc-form-login"[^>]+action="([^"]+)"', page.text).group(1))
        # Keycloak's cookies are `Secure`; browsers send them to http://localhost (a secure
        # context), Python's cookie jar does not, so they are forwarded explicitly.
        cookies = "; ".join(c.split(";", 1)[0] for c in page.headers.get_list("set-cookie"))
        posted = browser.post(action, data={"username": email, "password": PASSWORD, "credentialId": ""},
                              headers={"Cookie": cookies})
        assert posted.status_code == 302, posted.text[:300]
        target = urlsplit(posted.headers["location"])
    assert f"{target.scheme}://{target.netloc}{target.path}" == "http://localhost:8000/api/auth/oidc/callback"
    query = {k: v[0] for k, v in parse_qs(target.query).items()}
    return client.get("/api/auth/oidc/callback", params=query, follow_redirects=False)


@pytest.fixture
def kc_app(scenario, monkeypatch):
    from backend.app import main

    client, _, factory = scenario
    cfg = kc_settings(main.settings, storage_dir=main.settings.storage_dir)
    monkeypatch.setattr(main, "settings", cfg)
    oidc_mod.set_oidc_client(OIDCClient(cfg))
    yield client, factory, cfg
    oidc_mod.set_oidc_client(None)
    client.cookies.clear()


def test_discovery_jwks_and_real_access_tokens(kc_app):
    _, _, cfg = kc_app
    client = OIDCClient(cfg)
    meta = client.metadata()
    assert meta.issuer == ISSUER and "S256" in meta.code_challenge_methods
    tokens = password_grant("reviewer@northstar.demo")
    claims = client.validate_access_token(tokens["access_token"])
    mapped = map_claims(claims, cfg)
    assert (mapped.tenant_id, mapped.role) == ("northstar", "reviewer")
    assert set(mapped.groups) == {"procurement", "finance", "legal"}
    # that ID token was issued to the test client nexus-loadtest: wrong audience for the BFF client
    with pytest.raises(oidc_mod.TokenValidationError) as err:
        client.validate_id_token(tokens["id_token"], nonce=None)
    assert err.value.code == "invalid_audience"


def test_full_browser_login_with_pkce_creates_a_cookie_session(kc_app):
    client, factory, _ = kc_app
    done = browser_login(client, "finance@northstar.demo", return_to="/library")
    assert done.status_code == 302 and done.headers["location"] == "http://localhost:5173/library"
    session = client.get("/api/auth/session").json()
    assert session["user"]["email"] == "finance@northstar.demo" and session["user"]["groups"] == ["finance", "legal"]
    assert [d["id"] for d in client.get("/api/documents").json()] == ["d_fin"]
    with factory() as db:
        binding = db.query(UserIdentity).one()
        assert binding.issuer == ISSUER and binding.user_id == "u1" and binding.email_verified
        assert db.query(AuthSession).one().idp_session_id  # Keycloak's sid, for back-channel logout
    csrf = session["csrf_token"]
    out = client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf}).json()
    assert out["logout_url"].startswith(f"{ISSUER}/protocol/openid-connect/logout?")
    assert client.get("/api/auth/session").status_code == 401


@pytest.mark.parametrize("email, error", [("nogroups@northstar.demo", "no_tenant"),
                                          ("ambiguous@demo.test", "access_denied")])
def test_authenticated_but_unmappable_identities_get_no_session(kc_app, email, error):
    client, factory, _ = kc_app
    done = browser_login(client, email)
    assert f"auth_error={error}" in done.headers["location"]
    assert client.get("/api/auth/session").status_code == 401
    with factory() as db:
        assert db.query(AuthSession).count() == 0


def test_idp_group_removal_and_session_revocation_reach_the_local_session(kc_app):
    client, factory, _ = kc_app
    uid, hr = user_id("admin@northstar.demo"), group_id("/tenants/northstar/groups/hr")
    try:
        browser_login(client, "admin@northstar.demo")
        assert "hr" in client.get("/api/auth/session").json()["user"]["groups"]

        def revalidate_now():
            with factory() as db:
                row = db.query(AuthSession).one()
                row.revalidate_after = row.created_at - timedelta(seconds=1)
                db.commit()
        admin("DELETE", f"/users/{uid}/groups/{hr}")          # IdP admin removes the group
        revalidate_now()
        assert "hr" not in client.get("/api/auth/session").json()["user"]["groups"]
        admin("POST", f"/users/{uid}/logout")                 # IdP admin ends all user sessions
        revalidate_now()
        gone = client.get("/api/auth/session")
        assert gone.status_code == 401 and gone.json()["detail"]["code"] == "session_revoked"
    finally:
        admin("PUT", f"/users/{uid}/groups/{hr}")


def test_signing_key_rotation_at_keycloak_is_picked_up(kc_app):
    _, _, cfg = kc_app
    client = OIDCClient(cfg)
    before = client.validate_access_token(password_grant("hr@northstar.demo")["access_token"])
    realm_id = admin("GET", "").json()["id"]
    created = admin("POST", "/components", json={
        "name": f"rotated-{int(time.time())}", "providerId": "rsa-generated",
        "providerType": "org.keycloak.keys.KeyProvider", "parentId": realm_id,
        "config": {"priority": ["999999"], "enabled": ["true"], "active": ["true"], "keySize": ["2048"],
                   "algorithm": ["RS256"]}})
    component = created.headers["location"].rsplit("/", 1)[-1]
    try:
        fetches = client.jwks.fetch_count
        token = password_grant("hr@northstar.demo")["access_token"]
        new_kid = __import__("jwt").get_unverified_header(token)["kid"]
        assert new_kid not in client.jwks._keys  # signed with the brand-new key
        assert client.validate_access_token(token)["sub"] == before["sub"]
        assert client.jwks.fetch_count == fetches + 1  # exactly one refetch for the unknown kid
    finally:
        admin("DELETE", f"/components/{component}")
