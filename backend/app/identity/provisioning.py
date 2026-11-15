"""Identity-to-user mapping: bind (issuer, subject) to exactly one local user and synchronise the
authorization attributes (tenant-scoped groups and role) from the IdP at login and at every
session re-validation.

Rules
  * an existing binding is authoritative for the tenant: a token mapping to another tenant is
    refused (`tenant_conflict`), the user is never moved between tenants;
  * a first login may link to an existing local account with the same e-mail ONLY when
    OIDC_LINK_VERIFIED_EMAIL=true and the IdP asserts email_verified=true, and only within the
    same tenant;
  * otherwise a new user is provisioned just-in-time (OIDC_JIT_PROVISIONING) with the mapped
    tenant, groups and role; an e-mail that already belongs to an unlinked account is refused;
  * disabled accounts are refused.
Lookups before a tenant is known go through narrow SECURITY DEFINER functions on PostgreSQL
(nexus_rls.resolve_identity / find_user_by_email); every write runs under the user's RLS context.
"""
from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..database import set_security_context, user_context
from ..models import User, UserIdentity
from .mapping import MappedIdentity
from .sessions import now_utc


class IdentityRejected(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _pg(db: Session) -> bool:
    return db.get_bind().dialect.name == "postgresql"


def resolve_identity(db: Session, issuer: str, subject: str) -> tuple[str, str, str] | None:
    if _pg(db):
        row = db.execute(text("SELECT user_id, tenant_id, status FROM nexus_rls.resolve_identity(:i, :s)"),
                         {"i": issuer, "s": subject}).first()
    else:
        row = db.execute(select(UserIdentity.user_id, User.tenant_id, User.status)
                         .join(User, User.id == UserIdentity.user_id)
                         .where(UserIdentity.issuer == issuer, UserIdentity.subject == subject)).first()
    return tuple(row) if row else None


def find_user_by_email(db: Session, email: str) -> tuple[str, str, str] | None:
    if _pg(db):
        row = db.execute(text("SELECT user_id, tenant_id, status FROM nexus_rls.find_user_by_email(:e)"),
                         {"e": email}).first()
    else:
        row = db.execute(select(User.id, User.tenant_id, User.status)
                         .where(User.email == email.strip().lower())).first()
    return tuple(row) if row else None


def _check(found: tuple[str, str, str], identity: MappedIdentity) -> str:
    user_id, tenant_id, status = found
    if tenant_id != identity.tenant_id:
        raise IdentityRejected("tenant_conflict", "this identity belongs to a different tenant")
    if status != "active":
        raise IdentityRejected("account_disabled", "the account is disabled")
    return user_id


def sync_attributes(user: User, identity: MappedIdentity) -> bool:
    """Apply IdP-asserted groups / role / display name. Returns True when authorization changed."""
    changed = sorted(user.groups or []) != list(identity.groups) or user.role != identity.role
    user.groups = list(identity.groups)
    user.role = identity.role
    if identity.display_name:
        user.display_name = identity.display_name
    return changed


def provision(db: Session, identity: MappedIdentity, *, link_verified_email: bool,
              jit: bool) -> User:
    found = resolve_identity(db, identity.issuer, identity.subject)
    now = now_utc()
    if found:
        user_id = _check(found, identity)
        set_security_context(db, user_context(identity.tenant_id, user_id))
        user = db.get(User, user_id)
        binding = db.scalar(select(UserIdentity).where(UserIdentity.issuer == identity.issuer,
                                                       UserIdentity.subject == identity.subject))
        if binding is not None:
            binding.last_login_at = now
            binding.email, binding.email_verified = identity.email, identity.email_verified
    else:
        user = None
        existing = find_user_by_email(db, identity.email) if identity.email else None
        if existing and link_verified_email and identity.email_verified:
            user_id = _check(existing, identity)
            set_security_context(db, user_context(identity.tenant_id, user_id))
            user = db.get(User, user_id)
        elif existing:
            raise IdentityRejected("email_conflict", "an account with this e-mail exists but is not "
                                                     "linked to this identity")
        if user is None:
            if not jit:
                raise IdentityRejected("not_provisioned", "no local account is bound to this identity")
            user_id = str(uuid5(NAMESPACE_URL, f"{identity.issuer}|{identity.subject}"))
            set_security_context(db, user_context(identity.tenant_id, user_id))
            user = User(id=user_id, tenant_id=identity.tenant_id,
                        email=identity.email or f"{user_id}@oidc.invalid", password_hash="!",
                        role=identity.role, groups=list(identity.groups), status="active",
                        auth_source="oidc", display_name=identity.display_name, created_at=now)
            db.add(user)
            db.flush()
        db.add(UserIdentity(user_id=user.id, tenant_id=user.tenant_id, issuer=identity.issuer,
                            subject=identity.subject, email=identity.email,
                            email_verified=identity.email_verified, created_at=now, last_login_at=now))
    if user is None:  # pragma: no cover - binding to a row the context cannot read
        raise IdentityRejected("account_unavailable", "the bound account could not be loaded")
    sync_attributes(user, identity)
    user.last_login_at = now
    db.flush()
    return user
