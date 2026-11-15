# Authentication: OpenID Connect and browser sessions (Phase 4A)

NEXUS AI has two strictly separated authentication modes.

| | `AUTH_MODE=demo` | `AUTH_MODE=oidc` |
|---|---|---|
| Allowed in | `APP_ENV=local / development / test` only | every environment (required in `staging` / `production`) |
| Login | synthetic personas, shared demo password | Authorization Code Flow + PKCE (S256) through the backend |
| Browser credential | HttpOnly session cookie | HttpOnly session cookie |
| API clients | HS256 demo JWT (`POST /api/auth/login` returns one) | IdP access token, `aud` must contain `OIDC_API_AUDIENCE` |
| Disabled | OIDC routes return 404 | demo login returns 404, demo JWTs fail the algorithm allowlist |

Configuration is validated at startup (`backend/app/config.py::validate_settings`) and the API and
the worker **refuse to start** on any error. Examples: demo mode or `DEMO_MODE=true` in
`staging/production`; OIDC without issuer, client, client secret, redirect URI, session key or
tenant allowlist; `OIDC_ALLOWED_ALGS` containing `HS*` or `none`; plain-http IdP URLs outside a
local IdP; insecure cookies, local storage, inline processing or a non-PostgreSQL database in
production-like environments; a published local-development session key in production.
`backend/tests/test_auth_oidc.py` verifies each rule.

## Flow (backend-for-frontend)

```mermaid
sequenceDiagram
  autonumber
  participant B as Browser (React SPA)
  participant V as Vite / reverse proxy (same origin)
  participant A as NEXUS API (BFF)
  participant K as IdP (Keycloak / any OIDC provider)
  B->>V: GET /api/auth/oidc/login?return_to=/library
  V->>A: (proxied)
  A->>A: state, nonce, PKCE verifier (S256); store hash(state) + encrypted verifier,<br/>set HttpOnly login-binding cookie
  A-->>B: 302 authorization_endpoint?response_type=code&code_challenge=…&state&nonce
  B->>K: login form (credentials never reach NEXUS)
  K-->>B: 302 redirect_uri?code&state
  B->>A: GET /api/auth/oidc/callback?code&state (+ binding cookie)
  A->>A: consume state (single use), verify binding cookie, expiry
  A->>K: POST token_endpoint (code, code_verifier, client secret)
  K-->>A: id_token, access_token, refresh_token
  A->>A: validate ID token (JWKS, alg allowlist, iss, aud, azp, exp/nbf/iat, nonce, typ)<br/>map claims -> tenant / groups / role; provision or link user
  A-->>B: 302 return_to + Set-Cookie session (HttpOnly, SameSite=Lax, Secure in prod, __Host-)
  B->>A: GET /api/auth/session (cookie) -> user + CSRF token
  B->>A: POST /api/... (cookie + X-CSRF-Token)
```

Tokens stay on the server: the refresh and ID tokens are stored AES-256-GCM encrypted
(`SESSION_ENCRYPTION_KEY`, associated data binds each ciphertext to its purpose); the cookie holds a
random 256-bit token whose SHA-256 is the only thing in the database. Nothing is stored in
`localStorage`.

## Token validation

`backend/app/identity/oidc.py::validate_jwt` never reads an unverified payload:

1. header `alg` must be in `OIDC_ALLOWED_ALGS` (asymmetric only; `none` and `HS*` are refused even if
   misconfigured, which also blocks the classic HS256-with-public-key confusion attack);
2. `kid` is required; the key comes from the IdP JWKS (discovery `jwks_uri`). The cache refreshes
   after `OIDC_JWKS_CACHE_SECONDS`; an **unknown kid** triggers at most one refetch per
   `OIDC_JWKS_MIN_REFRESH_SECONDS`, which is how key rotation is picked up without letting random
   kids turn the API into an IdP traffic amplifier;
3. the JWK must match the algorithm (`kty`, optional `alg` / `use=sig`);
4. signature, exact `iss`, `aud`, `exp`, `nbf`, `iat` (±`OIDC_CLOCK_SKEW_SECONDS`, default 30 s);
5. ID tokens: `nonce` (constant-time compare), `azp` (required when there are several audiences),
   Keycloak `typ=ID`; access tokens: `aud ∋ OIDC_API_AUDIENCE`, `typ=Bearer` (stops an ID token
   being replayed as an access token); logout tokens: `events` contains the back-channel logout
   event, `sid` or `sub`, no `nonce`.

The discovery document's `issuer` must equal `OIDC_ISSUER` exactly and the IdP must support PKCE
S256. When the API reaches the IdP through an internal address (Docker), set `OIDC_DISCOVERY_URL`
(the issuer stays the public URL).

## Identity, tenant and role mapping

Tenant authority is not taken from arbitrary claims (`backend/app/identity/mapping.py`):

* only the configured group claim (`OIDC_GROUP_CLAIM`, default `groups`, full paths) of a validated
  token from the configured issuer is read;
* only paths below `OIDC_TENANT_PREFIX` count: `/tenants/<tenant>/groups/<group>` (document grants)
  and `/tenants/<tenant>/roles/<member|reviewer|admin>`;
* `<tenant>` must be in `OIDC_ALLOWED_TENANTS`; group names are restricted to `[a-z0-9_-]`;
* no mapped tenant → authenticated but **not authorized** (no session, `auth_error=no_tenant`);
  groups in two tenants → refused (`ambiguous_tenant`);
* `(issuer, subject)` is bound to exactly one local user (`user_identities`). A later token that
  maps to a different tenant is refused (`tenant_conflict`); users are never moved between tenants;
* first login: linked to an existing local account with the same e-mail only when
  `OIDC_LINK_VERIFIED_EMAIL=true` **and** the IdP asserts `email_verified=true`; otherwise a new
  user is provisioned just-in-time (`OIDC_JIT_PROVISIONING`), starting with **no documents**; an
  e-mail belonging to an unlinked account is refused (`email_conflict`, prevents account takeover);
* groups / role are re-synchronised at every login and every session re-validation.

**Authentication never implies document access.** After login, every document, chunk, extraction,
comparison and job is still filtered by the application ACL and by PostgreSQL row-level security
([postgresql-rls.md](postgresql-rls.md)).

## Sessions

| Property | Value |
|---|---|
| Cookie | `nexus_session` (`__Host-nexus_session` when Secure), HttpOnly, `SameSite=Lax`, `Path=/`, `Secure` outside local |
| Stored | SHA-256 of the token, user, tenant, mode, timestamps, encrypted refresh / ID token, IdP `sid` |
| Idle / absolute timeout | `SESSION_IDLE_TIMEOUT_SECONDS` (1800) / `SESSION_ABSOLUTE_TIMEOUT_SECONDS` (28800) |
| Re-validation (OIDC) | every `SESSION_REVALIDATE_SECONDS` (300): refresh-token grant, re-map claims. `invalid_grant` (IdP session ended, user disabled) → session revoked; group removal → applied; IdP unreachable → tolerated for `SESSION_REVALIDATE_GRACE_SECONDS`, then 401 |
| Logout | `POST /api/auth/logout` revokes the server session and returns the IdP `end_session_endpoint` URL (RP-initiated logout with `id_token_hint`) |
| Back-channel logout | `POST /api/auth/oidc/backchannel-logout` (validated logout token) revokes every local session of that IdP session (`sid`) or subject |
| Fixation | a new random session id at every login |
| Disabled account | refused at login and on the next request (session revoked) |

**CSRF.** Cookie-authenticated `POST / PUT / PATCH / DELETE` require `X-CSRF-Token` (HMAC of the
session token with a server secret, delivered by `GET /api/auth/session` to same-origin JavaScript)
and, when the browser sends `Origin`, an allowed origin. SameSite=Lax is a second layer. Bearer
requests need no CSRF token (browsers never attach them cross-site).

**Trade-offs.** Server-side sessions cost one indexed lookup per request (measured in
[performance-benchmarks.md](performance-benchmarks.md)) and need the session table, but tokens never
reach JavaScript (XSS cannot exfiltrate them), revocation is immediate and refresh tokens are
rotated server-side. The SPA is served from the same origin as `/api` (Vite proxy in development,
a reverse proxy in production), so the cookie is first-party and no CORS credentials are needed.

## Keycloak (local development IdP)

`compose.oidc.yaml` starts Keycloak 26.4.2 with the realm `docker/keycloak/nexus-realm.json`:
confidential client `nexus-bff` (standard flow, PKCE S256 required, redirect
`http://localhost:5173/api/auth/oidc/callback`, back-channel logout to `http://api:8000/...`),
a **test-only** client `nexus-loadtest` (password grant, used by integration tests and k6), the
group tree `/tenants/{northstar,otherco}/{groups,roles}/…`, a group-membership mapper (full path)
and an audience mapper (`nexus-api`). Users are the synthetic personas (password `DemoPass123!`,
e-mail verified) plus two negative-test identities (`nogroups@northstar.demo` without tenant,
`ambiguous@demo.test` in two tenants). All secrets in the realm file are local-only.

Any standards-compliant provider works by configuration: issue a group claim with full paths (or
map your IdP groups to the `/tenants/...` convention), an audience for API tokens, register the
redirect / post-logout / back-channel URIs and set the `OIDC_*` variables.

## Verification

| Evidence | Where |
|---|---|
| Configuration fail-closed rules, algorithm and http rules | `test_auth_oidc.py` (5 tests) |
| Valid RS256 / ES256; wrong iss, aud, expired, nbf / iat in the future, missing claim, foreign key with known kid, tampered payload, `alg=none`, HS256 key confusion, missing kid, wrong key type, key rotation and refetch rate limiting, nonce, azp, typ, logout tokens, discovery issuer mismatch | `test_auth_oidc.py` (unit, fake IdP with real keys) |
| BFF login, state single use, login-CSRF binding, PKCE tampering, open-redirect neutralisation, no-tenant / ambiguous identities, tenant switch refused, JIT user without documents, CSRF and Origin enforcement, logout + stolen-cookie reuse, back-channel logout, idle expiry, re-validation (group removal, revoked IdP session, outage grace), OIDC bearer tokens, demo token / demo login refused in OIDC mode | `test_auth_oidc.py` (API, SQLite + PostgreSQL with RLS) |
| Real Keycloak: discovery + JWKS, real access tokens and claim mapping, full browser login through the Keycloak login form with PKCE, unmappable identities, group removal and admin logout reaching the local session, signing-key rotation at Keycloak | `test_keycloak_integration.py` |
| Docker: login through the frontend proxy, back-channel logout from Keycloak to the API container | `scripts/e2e_phase4.py` |
