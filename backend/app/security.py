"""Authorization belongs in the database retrieval query, not in prompt instructions."""
from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session
from .models import AuditEvent, Document, DocumentGrant, User


def authorized_documents(user: User):
    q = select(Document).where(Document.tenant_id == user.tenant_id)
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


def audit(db: Session, user: User, action: str, document_id: str | None = None) -> None:
    db.add(AuditEvent(user_id=user.id, tenant_id=user.tenant_id, action=action, document_id=document_id))
