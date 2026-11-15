"""Authorization belongs in the database retrieval query, not in prompt instructions.

Two independent layers enforce the same rule (Phase 4B):
  1. application layer (this module): every retrieval / document / extraction query embeds
     `authorized_documents(user)` in its SQL;
  2. PostgreSQL row-level security (migration 0008): the database applies the same rule to every
     statement of the runtime role, so an accidentally missing application filter still returns
     nothing the user may not read.

Rule: same tenant AND not deleted AND (admin OR owner OR shares a group with a grant).
"""
from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session

from .models import AuditEvent, Document, DocumentGrant, User


def authorized_documents(user: User):
    q = select(Document).where(Document.tenant_id == user.tenant_id, Document.deleted_at.is_(None))
    if user.role != "admin":
        # No grants / missing groups means default deny except own uploaded documents.
        grant = exists(
            select(DocumentGrant.id).where(
                DocumentGrant.document_id == Document.id,
                DocumentGrant.group_name.in_(user.groups or ["__no_groups__"]),
            )
        )
        q = q.where(or_(Document.owner_id == user.id, grant))
    return q


def permitted_document(db: Session, user: User, document_id: str) -> Document | None:
    return db.scalar(authorized_documents(user).where(Document.id == document_id))


def can_manage(user: User, doc: Document) -> bool:
    """Delete, new version, retention and grant changes: the owner or a tenant admin."""
    return doc.tenant_id == user.tenant_id and (user.role == "admin" or doc.owner_id == user.id)


def audit(db: Session, user: User, action: str, document_id: str | None = None) -> None:
    db.add(AuditEvent(user_id=user.id, tenant_id=user.tenant_id, action=action, document_id=document_id))
