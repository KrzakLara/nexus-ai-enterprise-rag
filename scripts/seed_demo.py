"""Populate deterministic fictitious documents & segregated demo personas (idempotent).

Phase 4: originals are stored through the configured object store (MinIO / S3 in Docker, a local
directory otherwise) under server-generated keys, with a version-1 record; documents that already
exist (any storage backend, including v0.3 local files) are left untouched. Runs with the
migration / admin connection (it creates users and tenant data for every tenant).
"""
import argparse
import sys
from pathlib import Path
from uuid import NAMESPACE_DNS, uuid4, uuid5

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.auth import hash_password
from backend.app.config import settings
from backend.app.database import SessionLocal
from backend.app.migrate import migrate
from backend.app.models import Chunk, Document, DocumentGrant, DocumentVersion, User
from backend.app.parsers import extract_contract_fields, split_page
from backend.app.storage import StorageError, default_store, object_key, store_for
from backend.app.uploads import validate_upload

root = Path(__file__).resolve().parents[1]
DEMO_PASSWORD = "DemoPass123!"
PERSONAS = [
    ("admin@northstar.demo", "northstar", "admin", ["finance", "legal", "sales", "hr"]),
    ("finance@northstar.demo", "northstar", "member", ["finance", "legal"]),
    ("hr@northstar.demo", "northstar", "member", ["hr"]),
    ("sales@northstar.demo", "northstar", "member", ["sales", "legal"]),
    ("finance@otherco.demo", "otherco", "member", ["finance"]),
    # Phase 3 document intelligence: a reviewer (may correct / approve / reject extractions) and
    # a procurement member (may read procurement documents and their extractions, not review them)
    ("reviewer@northstar.demo", "northstar", "reviewer", ["procurement", "finance", "legal"]),
    ("procurement@northstar.demo", "northstar", "member", ["procurement"]),
]
# Phase 3 demo documents from the frozen evaluation corpus (fictional), granted ONLY to the
# 'procurement' group so the Phase 2 finance / HR retrieval demo is unaffected.
DOCINTEL_DEMO = ["di-c01", "di-c09", "di-c02", "di-c06", "di-c08", "di-i01", "di-i04", "di-i06", "di-i07",
                 "di-i10", "di-o01"]
DATA = [
    ("northstar", "finance@northstar.demo", "atlas_supplier_agreement.txt", ["finance", "legal"]),
    ("northstar", "finance@northstar.demo", "cygnus_cloud_agreement.txt", ["finance", "legal"]),
    ("northstar", "hr@northstar.demo", "employee_compensation_policy.txt", ["hr"]),
    ("northstar", "sales@northstar.demo", "enterprise_sales_playbook.txt", ["sales"]),
    ("northstar", "sales@northstar.demo", "customer_data_policy.txt", ["legal", "sales"]),
    ("otherco", "finance@otherco.demo", "otherco_confidential_budget.txt", ["finance"]),
    ("northstar", "finance@northstar.demo", "pravilnik_putni_troskovi.txt", ["finance", "hr"]),
]


def store_original(doc_id: str, tenant: str, filename: str, data: bytes) -> dict:
    """Put the original into the configured store; returns the document's storage fields."""
    validated = validate_upload(filename, data, settings)
    store = default_store(settings)
    key = object_key(tenant, doc_id, 1)
    store.put(key, data, validated.content_type, validated.sha256,
              {"document-id": doc_id, "tenant-id": tenant, "version": "1", "source": "seed_demo"})
    return {"storage_backend": store.backend, "storage_key": key, "sha256": validated.sha256,
            "size_bytes": validated.size, "content_type": validated.content_type,
            "lifecycle_state": "ready", "current_version": 1}


def add_version(db, doc: Document) -> None:
    """Version-1 record of a seeded document (after the document row is flushed: FK order)."""
    db.add(DocumentVersion(id=str(uuid4()), document_id=doc.id, tenant_id=doc.tenant_id, version=1,
                           storage_backend=doc.storage_backend, storage_key=doc.storage_key, sha256=doc.sha256,
                           size_bytes=doc.size_bytes, content_type=doc.content_type,
                           original_filename=doc.filename, state="active", created_by=doc.owner_id))


def main():
    parser = argparse.ArgumentParser(description="Seed synthetic demo data")
    parser.add_argument("--skip-embeddings", action="store_true",
                        help="do not run the local embedding model after seeding")
    args = parser.parse_args()
    print("Schema:", migrate())
    if settings.storage_backend == "local":
        settings.storage_dir.mkdir(parents=True, exist_ok=True)
    elif settings.s3_create_bucket:
        default_store(settings).ensure_bucket()
    with SessionLocal() as db:
        for email, tenant, role, groups in PERSONAS:
            user_id = str(uuid5(NAMESPACE_DNS, email))
            if db.get(User, user_id):
                continue
            db.add(User(id=user_id, email=email, tenant_id=tenant, role=role,
                        groups=groups, password_hash=hash_password(DEMO_PASSWORD)))
        db.flush()
        for tenant, owner_email, filename, groups in DATA:
            doc_id = str(uuid5(NAMESPACE_DNS, tenant + ":" + filename))
            if db.get(Document, doc_id):
                continue
            text = (root / "demo_documents" / filename).read_text(encoding="utf-8")
            data = text.encode("utf-8")
            owner = db.scalar(select(User).where(User.email == owner_email))
            doc = Document(id=doc_id, tenant_id=tenant, owner_id=owner.id,
                           title=filename.removesuffix(".txt").replace("_", " ").title(),
                           filename=filename, page_count=1, extracted_fields=extract_contract_fields([text]),
                           **store_original(doc_id, tenant, filename, data))
            db.add(doc)
            db.flush()  # parent row first: FK children below (enforced by PostgreSQL)
            add_version(db, doc)
            for group in groups:
                db.add(DocumentGrant(document_id=doc_id, group_name=group))
            for pos, piece in enumerate(split_page(text)):
                chunk_id = str(uuid5(NAMESPACE_DNS, doc_id + ":" + str(pos)))
                db.add(Chunk(id=chunk_id, document_id=doc_id, page_number=1, position=pos,
                             content=piece))
        db.commit()
        seed_document_intelligence(db)
    print("Seeded synthetic data. Demo password:", DEMO_PASSWORD)
    for email, _, _, _ in PERSONAS:
        print(" -", email)
    if args.skip_embeddings:
        print("Embeddings skipped; run: python scripts/embed_backfill.py")
        return
    from backend.app.embeddings import get_embedder
    from backend.app.indexing import embed_pending_chunks

    with SessionLocal() as db:
        stats = embed_pending_chunks(db, get_embedder())  # idempotent; raises on model failure
        db.commit()
    print(f"Embeddings: {stats.embedded} new, {stats.skipped_up_to_date} already up to date")


def seed_document_intelligence(db) -> None:
    """Add the Phase 3 demo documents and store structure + rules extraction for every document
    that has none yet (idempotent). Scanned PDFs need Tesseract (present in the Docker image)."""
    import json

    from backend.app.docintel import service
    from backend.app.docintel.ocr import ocr_available
    from backend.app.docintel.parsing import parse_document
    from backend.app.models import DocumentProcessing, Extraction

    corpus = root / "eval" / "docintel"
    labels = {r["id"]: r for r in map(json.loads, (corpus / "labels.jsonl").read_text(encoding="utf-8").splitlines())}
    admin = db.scalar(select(User).where(User.email == "admin@northstar.demo"))
    for lid in DOCINTEL_DEMO:
        lab = labels[lid]
        doc_id = str(uuid5(NAMESPACE_DNS, "northstar:" + lid))
        if db.get(Document, doc_id):
            continue
        if lab["scanned_pages"] and not ocr_available():
            print(f" - {lid}: skipped (scanned; Tesseract not installed)")
            continue
        path = corpus / lab["file"]
        data = path.read_bytes()
        parsed = parse_document(path.name, data)
        pages = parsed.page_texts()
        doc = Document(id=doc_id, tenant_id="northstar", owner_id=admin.id, title=path.stem, filename=path.name,
                       page_count=len(pages), extracted_fields=extract_contract_fields(pages),
                       **store_original(doc_id, "northstar", path.name, data))
        db.add(doc)
        db.flush()
        add_version(db, doc)
        db.add(DocumentGrant(document_id=doc_id, group_name="procurement"))
        for number, page in enumerate(pages, 1):
            for pos, piece in enumerate(split_page(page)):
                db.add(Chunk(id=str(uuid5(NAMESPACE_DNS, f"{doc_id}:{number}:{pos}")), document_id=doc_id,
                             page_number=number, position=pos, content=piece))
        service.store_structure(db, doc, parsed)
        db.flush()
    for doc in db.scalars(select(Document).where(Document.deleted_at.is_(None))).all():
        if db.get(DocumentProcessing, doc.id):
            continue
        try:  # v0.3 documents keep their local backend; new ones come from the object store
            data = store_for(doc.storage_backend, settings).get(doc.storage_key)
            service.store_structure(db, doc, parse_document(doc.filename, data))
        except (StorageError, OSError) as exc:
            print(f" - {doc.filename}: original not available ({type(exc).__name__})")
            continue
        except Exception as exc:  # noqa: BLE001
            print(f" - {doc.filename}: not parsed ({str(exc)[:80]})")
            continue
    db.flush()
    for row in db.scalars(select(DocumentProcessing)).all():
        if db.scalar(select(Extraction).where(Extraction.document_id == row.document_id)):
            continue
        doc = db.get(Document, row.document_id)
        owner = db.get(User, doc.owner_id)
        service.process(db, owner, doc)
    db.commit()
    print("Document intelligence: structure + extraction stored for", db.query(DocumentProcessing).count(), "documents")


if __name__ == "__main__":
    main()
