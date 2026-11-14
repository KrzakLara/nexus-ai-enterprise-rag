"""Evaluation corpus: manifest loading, production chunking, an independent ACL oracle, seeding.

Chunk IDs are stable and human-readable: "<document id>:p<page>:c<position>". They are produced by
the SAME chunker the application uses on upload (backend.app.parsers.split_page), so ground-truth
labels refer to exactly the rows that retrieval reads from PostgreSQL.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

from sqlalchemy.orm import Session

from backend.app.auth import hash_password
from backend.app.models import Chunk, Document, DocumentGrant, User
from backend.app.parsers import split_page

ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = ROOT / "eval"
CORPUS_DIR = EVAL_DIR / "corpus"


@dataclass(frozen=True)
class Persona:
    id: str
    email: str
    tenant: str
    role: str
    groups: tuple[str, ...]


@dataclass(frozen=True)
class CorpusDocument:
    id: str
    tenant: str
    owner: str
    lang: str
    groups: tuple[str, ...]
    title: str
    text: str


@dataclass(frozen=True)
class CorpusChunk:
    id: str
    document_id: str
    page_number: int
    position: int
    content: str


@dataclass
class Corpus:
    password: str
    personas: dict[str, Persona]
    documents: dict[str, CorpusDocument]
    chunks: list[CorpusChunk] = field(default_factory=list)

    def chunks_of(self, document_id: str) -> list[CorpusChunk]:
        return [c for c in self.chunks if c.document_id == document_id]

    @property
    def chunk_by_id(self) -> dict[str, CorpusChunk]:
        return {c.id: c for c in self.chunks}

    def authorized(self, persona_id: str, document_id: str) -> bool:
        """Independent re-implementation of the ACL rule (NOT the SQL used by the app):
        same tenant AND (admin OR owner OR shares a group with the document grants)."""
        p, d = self.personas[persona_id], self.documents[document_id]
        if p.tenant != d.tenant:
            return False
        return p.role == "admin" or d.owner == p.id or bool(set(p.groups) & set(d.groups))

    def authorized_chunk_ids(self, persona_id: str) -> set[str]:
        return {c.id for c in self.chunks if self.authorized(persona_id, c.document_id)}


def chunk_id(document_id: str, page: int, position: int) -> str:
    cid = f"{document_id}:p{page}:c{position}"
    if len(cid) > 40:
        raise ValueError(f"Chunk id too long for chunks.id (40): {cid}")
    return cid


def load_corpus(corpus_dir: Path = CORPUS_DIR) -> Corpus:
    manifest = json.loads((corpus_dir / "manifest.json").read_text(encoding="utf-8"))
    personas = {p["id"]: Persona(p["id"], p["email"], p["tenant"], p["role"], tuple(p["groups"]))
                for p in manifest["personas"]}
    documents: dict[str, CorpusDocument] = {}
    chunks: list[CorpusChunk] = []
    for d in manifest["documents"]:
        if d["owner"] not in personas:
            raise ValueError(f"Unknown owner {d['owner']} for {d['id']}")
        if personas[d["owner"]].tenant != d["tenant"]:
            raise ValueError(f"Owner of {d['id']} belongs to another tenant")
        text = (corpus_dir / f"{d['id']}.txt").read_text(encoding="utf-8")
        documents[d["id"]] = CorpusDocument(d["id"], d["tenant"], d["owner"], d["lang"],
                                            tuple(d["groups"]), d["title"], text)
        # Text corpus documents are a single logical page, exactly like TXT uploads.
        for position, content in enumerate(split_page(text)):
            chunks.append(CorpusChunk(chunk_id(d["id"], 1, position), d["id"], 1, position, content))
    return Corpus(manifest["password"], personas, documents, chunks)


def seed_corpus(db: Session, corpus: Corpus) -> None:
    """Insert personas, documents, grants and chunks (embeddings are added separately)."""
    pwd = hash_password(corpus.password)
    for p in corpus.personas.values():
        db.add(User(id=p.id, email=p.email, tenant_id=p.tenant, role=p.role,
                    groups=list(p.groups), password_hash=pwd))
    db.flush()
    for d in corpus.documents.values():
        db.add(Document(id=d.id, tenant_id=d.tenant, owner_id=d.owner, title=d.title,
                        filename=f"{d.id}.txt", storage_key=f"{d.id}.txt",
                        sha256=sha256(d.text.encode("utf-8")).hexdigest(), page_count=1,
                        extracted_fields={}))
    db.flush()
    for d in corpus.documents.values():
        for group in d.groups:
            db.add(DocumentGrant(document_id=d.id, group_name=group))
    db.add_all(Chunk(id=c.id, document_id=c.document_id, page_number=c.page_number,
                     position=c.position, content=c.content) for c in corpus.chunks)
    db.flush()
