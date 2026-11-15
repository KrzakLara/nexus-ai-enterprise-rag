"""Authentication (Phase 4A): OIDC relying party, server-side sessions, identity mapping.

    oidc.py           discovery, JWKS cache, token endpoint, strict token validation
    mapping.py        validated claims -> tenant, groups, role (server-side allowlist)
    provisioning.py   (issuer, subject) -> local user, attribute synchronisation
    sessions.py       BFF sessions in HttpOnly cookies, CSRF, expiry, revocation
    dependencies.py   `current_user` for every protected route (cookie, demo JWT, OIDC bearer)
    routes.py         /api/auth/* endpoints
"""
