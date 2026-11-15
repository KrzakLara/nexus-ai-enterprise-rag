"""Map validated OIDC claims to a tenant, application groups and an application role.

Tenant authority is NOT taken from an arbitrary claim value:
  * only the configured group claim of a signature-validated token from the configured issuer is
    read (the IdP administrator controls group membership, the user cannot edit it);
  * only group paths under OIDC_TENANT_PREFIX are considered:
        /tenants/<tenant>/groups/<group>     application group (document grants)
        /tenants/<tenant>/roles/<role>       member | reviewer | admin
  * <tenant> must be in the server-side allowlist OIDC_ALLOWED_TENANTS;
  * an identity whose groups span two tenants is refused (ambiguous), one without any mapped tenant
    is authenticated but NOT authorized (403);
  * once bound, an identity's tenant can never change (identity/provisioning.py refuses a token
    that asserts a different tenant).
Authentication never implies document access: documents still require the ACL / RLS rule.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import Settings

ROLE_RANK = {"member": 0, "reviewer": 1, "admin": 2}
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,59}$")
TENANT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
MAX_GROUPS = 50


class MappingError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class MappedIdentity:
    issuer: str
    subject: str
    email: str | None
    email_verified: bool
    display_name: str | None
    tenant_id: str
    groups: tuple[str, ...]
    role: str
    session_id: str | None  # IdP session id (`sid`), used by back-channel logout


def map_claims(claims: dict, cfg: Settings) -> MappedIdentity:
    raw = claims.get(cfg.oidc_group_claim) or []
    if not isinstance(raw, list) or not all(isinstance(g, str) for g in raw):
        raise MappingError("invalid_group_claim", "group claim must be a list of strings")
    prefix = cfg.oidc_tenant_prefix.rstrip("/") + "/"
    allowed = set(cfg.oidc_allowed_tenants)
    tenants: set[str] = set()
    groups: set[str] = set()
    role = "member"
    for path in raw[:500]:
        if not path.startswith(prefix):
            continue  # IdP groups outside the tenant tree are ignored
        parts = path[len(prefix):].split("/")
        if len(parts) != 3 or parts[1] not in ("groups", "roles"):
            continue
        tenant, kind, name = parts
        if not TENANT.match(tenant) or tenant not in allowed:
            raise MappingError("tenant_not_allowed", "the IdP asserted a tenant that is not configured")
        tenants.add(tenant)
        if kind == "groups":
            if not NAME.match(name):
                raise MappingError("invalid_group", "group name outside the allowed character set")
            groups.add(name)
        elif name in ROLE_RANK:
            if ROLE_RANK[name] > ROLE_RANK[role]:
                role = name
        else:
            raise MappingError("invalid_role", "unknown application role")
    if not tenants:
        raise MappingError("no_tenant", "the identity is not a member of any configured tenant")
    if len(tenants) > 1:
        raise MappingError("ambiguous_tenant", "the identity is a member of more than one tenant")
    if len(groups) > MAX_GROUPS:
        raise MappingError("too_many_groups", "too many application groups")
    email = claims.get("email")
    email = email.strip().lower()[:150] if isinstance(email, str) and email.strip() else None
    name = claims.get("name") or claims.get("preferred_username")
    return MappedIdentity(
        issuer=cfg.oidc_issuer, subject=claims["sub"], email=email,
        email_verified=claims.get("email_verified") is True,
        display_name=name[:200] if isinstance(name, str) else None,
        tenant_id=next(iter(tenants)), groups=tuple(sorted(groups)), role=role,
        session_id=claims.get("sid") if isinstance(claims.get("sid"), str) else None,
    )
