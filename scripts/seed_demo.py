"""Populate deterministic fictitious documents & three segregated demo personas."""
import argparse
from pathlib import Path
import sys
from uuid import uuid5, NAMESPACE_DNS
from hashlib import sha256
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.database import SessionLocal
from backend.app.migrate import migrate
from backend.app.auth import hash_password
from backend.app.models import User, Document, DocumentGrant, Chunk
from backend.app.parsers import extract_contract_fields, split_page
from backend.app.config import settings

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


def main():
    parser = argparse.ArgumentParser(description="Seed synthetic demo data")
    parser.add_argument("--skip-embeddings", action="store_true",
                        help="do not run the local embedding model after seeding")
    args = parser.parse_args()
    print("Schema:", migrate())
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
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
            key = doc_id + ".txt"
            (settings.storage_dir / key).write_bytes(data)
            db.add(Document(id=doc_id, tenant_id=tenant, owner_id=owner.id,
                            title=filename.removesuffix(".txt").replace("_", " ").title(),
                            filename=filename, storage_key=key, sha256=sha256(data).hexdigest(),
                            page_count=1, extracted_fields=extract_contract_fields([text])))
            db.flush()  # parent row first: FK children below (enforced by PostgreSQL)
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
    from backend.app.models import DocumentProcessing

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
        key = doc_id + path.suffix
        (settings.storage_dir / key).write_bytes(data)
        doc = Document(id=doc_id, tenant_id="northstar", owner_id=admin.id, title=path.stem, filename=path.name,
                       storage_key=key, sha256=sha256(data).hexdigest(), page_count=len(pages),
                       extracted_fields=extract_contract_fields(pages))
        db.add(doc)
        db.flush()
        db.add(DocumentGrant(document_id=doc_id, group_name="procurement"))
        for number, page in enumerate(pages, 1):
            for pos, piece in enumerate(split_page(page)):
                db.add(Chunk(id=str(uuid5(NAMESPACE_DNS, f"{doc_id}:{number}:{pos}")), document_id=doc_id,
                             page_number=number, position=pos, content=piece))
        service.store_structure(db, doc, parsed)
        db.flush()
    for doc in db.scalars(select(Document)).all():
        if db.get(DocumentProcessing, doc.id):
            continue
        path = settings.storage_dir / doc.storage_key
        if not path.is_file():
            continue
        try:
            service.store_structure(db, doc, parse_document(doc.filename, path.read_bytes()))
        except Exception as exc:  # noqa: BLE001
            print(f" - {doc.filename}: not parsed ({str(exc)[:80]})")
            continue
    db.flush()
    from backend.app.models import Extraction

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
