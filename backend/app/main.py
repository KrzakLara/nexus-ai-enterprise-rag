"""NEXUS AI API: a local, inspectable enterprise document intelligence vertical slice.

WARNING: demo auth is intentionally NOT suitable for internet-facing deployment.
"""
import logging
from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from anyio import to_thread
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import current_user, make_token, verify_password
from .config import settings
from .database import engine, get_db
from .embeddings import Embedder, EmbeddingError, get_embedder
from .evidence import EvidencePolicy, assess
from .extractive import select_evidence
from .indexing import embed_pending_chunks
from .migrate import assert_schema_current
from .models import AuditEvent, Chunk, Document, DocumentGrant, User
from .parsers import extract_contract_fields, extract_pages, split_page
from .rag import answer
from .retrieval import RetrievalMode, RetrievalUnavailable, retrieve
from .security import audit, authorized_documents, permitted_document

log = logging.getLogger("nexus")


def warm_up_embedder() -> str:
    """Load the embedding model and embed one probe query, so the first user request does not pay
    the ~15 s cold start. A failure is reported, not fatal: semantic/hybrid then answer HTTP 503."""
    try:
        get_embedder().embed_query("warm-up probe")
        return "loaded"
    except EmbeddingError as exc:
        log.warning("Embedding model warm-up failed: %s", exc)
        return f"failed: {str(exc)[:160]}"


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not settings.demo_mode:
        raise RuntimeError("Production identity/SSO is NOT implemented. Run only in local DEMO_MODE.")
    # Schema is owned by Alembic (python scripts/migrate.py); fail fast instead of create_all().
    assert_schema_current(engine)
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    if settings.embedding_warmup and engine.dialect.name == "postgresql":
        # In a worker thread: the event loop is not blocked while the model loads.
        await to_thread.run_sync(warm_up_embedder)
    yield


app = FastAPI(
    title="NEXUS AI — Enterprise Knowledge Intelligence",
    description="Secure-by-query RAG baseline with verifiable document evidence (LOCAL DEMO ONLY)",
    version="0.2.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)


class LoginIn(BaseModel):
    email: str
    password: str


EVIDENCE_POLICY = EvidencePolicy(
    enabled=settings.evidence_policy_enabled,
    min_semantic_similarity=settings.evidence_min_similarity,
    require_identifier_match=settings.evidence_require_identifier,
    accept_on_branch_agreement=settings.evidence_accept_agreement,
    min_sentence_similarity=settings.evidence_min_sentence_similarity,
    min_term_coverage=settings.evidence_min_term_coverage,
)


class RRFWeights(BaseModel):
    lexical: float = Field(default=settings.rrf_weight_lexical, ge=0, le=10)
    semantic: float = Field(default=settings.rrf_weight_semantic, ge=0, le=10)


def default_retrieval_mode(dialect: str) -> RetrievalMode:
    """Mode used when a request omits `mode`: hybrid where it is available (PostgreSQL), else the
    bm25 baseline (SQLite). The UI makes the same choice from /api/health."""
    return "hybrid" if dialect == "postgresql" else "bm25"


class AskIn(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=10)
    # bm25 = in-process Python BM25 baseline | lexical = PostgreSQL FTS
    # semantic = pgvector cosine | hybrid = FTS + semantic + RRF
    # Omitted: default_retrieval_mode() (hybrid on PostgreSQL, bm25 on SQLite)
    mode: RetrievalMode | None = None
    candidates: int = Field(default=settings.retrieval_candidates, ge=1, le=200)  # hybrid only
    weights: RRFWeights = Field(default_factory=RRFWeights)  # hybrid only
    # semantic / hybrid: withhold candidates and answer no_evidence when evidence is insufficient
    apply_evidence_policy: bool = True


@app.get("/", include_in_schema=False)
def home():
    return RedirectResponse(url="/docs")


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "mode": "local_demo",
        "version": app.version,
        "database": engine.dialect.name,
        "semantic_retrieval": engine.dialect.name == "postgresql",
        "retrieval_modes": (["bm25", "lexical", "semantic", "hybrid"]
                            if engine.dialect.name == "postgresql" else ["bm25"]),
        "default_retrieval_mode": default_retrieval_mode(engine.dialect.name),
        "embedding_model": settings.embedding_model,
        "embedding_model_loaded": get_embedder().is_loaded,
    }


@app.post("/api/auth/login")
def login(payload: LoginIn, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == payload.email.lower().strip()))
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(401, "Invalid credentials")
    return {
        "access_token": make_token(user),
        "token_type": "bearer",
        "user": {
            "email": user.email,
            "groups": user.groups,
            "role": user.role,
            "tenant": user.tenant_id,
        },
    }


@app.get("/api/me")
def me(user: User = Depends(current_user)):
    return {"email": user.email, "groups": user.groups, "role": user.role, "tenant": user.tenant_id}


@app.get("/api/documents")
def documents(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(authorized_documents(user).order_by(Document.created_at.desc())).all()
    return [
        {
            "id": d.id,
            "title": d.title,
            "filename": d.filename,
            "pages": d.page_count,
            "created_at": d.created_at.isoformat(),
            "fields": d.extracted_fields,
            "groups": db.scalars(
                select(DocumentGrant.group_name).where(DocumentGrant.document_id == d.id)
            ).all(),
        }
        for d in rows
    ]


@app.get("/api/documents/{document_id}")
def document_details(
    document_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    doc = permitted_document(db, user, document_id)
    if not doc:
        raise HTTPException(404, "Document not found")
    audit(db, user, "document_view", doc.id)
    db.commit()
    return {
        "id": doc.id,
        "title": doc.title,
        "filename": doc.filename,
        "page_count": doc.page_count,
        "fields": doc.extracted_fields,
        "pages": [
            {"page": c.page_number, "content": c.content}
            for c in db.scalars(
                select(Chunk).where(Chunk.document_id == doc.id).order_by(
                    Chunk.page_number, Chunk.position
                )
            ).all()
        ],
    }


@app.get("/api/documents/{document_id}/source")
def original_file(
    document_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    doc = permitted_document(db, user, document_id)
    if not doc:
        raise HTTPException(404, "Document not found")
    path = settings.storage_dir / doc.storage_key
    if not path.is_file():
        raise HTTPException(404, "Original file missing")
    audit(db, user, "source_download", doc.id)
    db.commit()
    return FileResponse(path, filename=doc.filename)


# A plain `def` route: FastAPI runs it in its worker threadpool, so parsing and CPU-bound
# embedding do not block the event loop (other requests keep being served meanwhile).
@app.post("/api/documents", status_code=201)
def upload(
    file: UploadFile = File(...),
    groups: str = Form(..., description="Comma-separated groups to grant"),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    embedder: Embedder = Depends(get_embedder),
):
    filename = Path(file.filename or "").name
    extension = Path(filename).suffix.lower()
    if extension not in {".pdf", ".docx", ".txt", ".md"}:
        raise HTTPException(415, "Supported: PDF with a text layer, DOCX, TXT, MD")
    allowed_groups = list(dict.fromkeys(g.strip() for g in groups.split(",") if g.strip()))
    if not allowed_groups or len(allowed_groups) > 5 or any(len(g) > 60 for g in allowed_groups):
        raise HTTPException(400, "Provide 1 to 5 valid groups")
    if user.role != "admin" and not set(allowed_groups).issubset(set(user.groups)):
        raise HTTPException(403, "Cannot share with a group you do not belong to")
    contents = file.file.read(settings.max_upload_bytes + 1)
    if not contents:
        raise HTTPException(400, "The uploaded file is empty")
    if len(contents) > settings.max_upload_bytes:
        raise HTTPException(413, f"Upload exceeds {settings.max_upload_bytes // (1024 * 1024)} MB")
    try:
        pages = extract_pages(filename, contents)
    except Exception as exc:
        raise HTTPException(422, f"Document could not be parsed: {str(exc)[:180]}") from None
    doc_id = str(uuid4())
    storage_key = f"{doc_id}{extension}"
    file_path = settings.storage_dir / storage_key
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(contents)
    try:
        doc = Document(
            id=doc_id,
            tenant_id=user.tenant_id,
            owner_id=user.id,
            title=Path(filename).stem[:200],
            filename=filename[:255],
            storage_key=storage_key,
            sha256=sha256(contents).hexdigest(),
            page_count=len(pages),
            extracted_fields=extract_contract_fields(pages),
        )
        db.add(doc)
        db.flush()  # no ORM relationships: parent row must exist before FK children (PostgreSQL)
        for group in allowed_groups:
            db.add(DocumentGrant(document_id=doc_id, group_name=group))
        for page_num, page in enumerate(pages, 1):
            for position, chunk in enumerate(split_page(page)):
                db.add(
                    Chunk(
                        id=str(uuid4()),
                        document_id=doc_id,
                        page_number=page_num,
                        position=position,
                        content=chunk,
                    )
                )
        audit(db, user, "upload", doc_id)
        db.commit()
    except Exception:
        db.rollback()
        file_path.unlink(missing_ok=True)
        raise
    # Embeddings are generated after the document is durably stored. A model failure is reported
    # explicitly (no placeholder vectors); `python scripts/embed_backfill.py` can retry later.
    embedding_status = "skipped_requires_postgresql"
    if not settings.embed_on_upload:
        embedding_status = "disabled"
    elif db.get_bind().dialect.name == "postgresql":
        try:
            stats = embed_pending_chunks(db, embedder, document_ids=[doc_id])
            db.commit()
            embedding_status = f"embedded:{stats.embedded}"
        except EmbeddingError as exc:
            db.rollback()
            embedding_status = f"failed: {str(exc)[:160]}"
    return {
        "id": doc_id,
        "title": doc.title,
        "pages": doc.page_count,
        "fields_extracted": len(doc.extracted_fields),
        "embedding_status": embedding_status,
    }


SCORE_NOTES = {
    "bm25": "BM25 score (Python baseline); relative within this query only.",
    "lexical": "PostgreSQL ts_rank_cd; relative within this query only.",
    "semantic": "Cosine similarity (1 - cosine distance); not a probability.",
    "hybrid": "Reciprocal Rank Fusion score sum(w/(k+rank)); a rank statistic, "
              "not a confidence or probability.",
}


@app.post("/api/ask")
def ask(
    payload: AskIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    embedder: Embedder = Depends(get_embedder),
):
    start = perf_counter()
    mode = payload.mode or default_retrieval_mode(db.get_bind().dialect.name)
    weights = payload.weights.model_dump()
    if mode == "hybrid" and not any(w > 0 for w in weights.values()):
        raise HTTPException(422, "At least one hybrid weight must be positive")
    try:
        result = retrieve(
            db, user, payload.question, mode, embedder,
            top_k=payload.top_k, candidates=payload.candidates,
            weights=weights, rrf_k=settings.rrf_k,
        )
    except RetrievalUnavailable as exc:
        raise HTTPException(409, f"{exc}; mode '{mode}' is unavailable") from None
    except EmbeddingError as exc:
        # No silent fallback to keyword-only results: the caller asked for a specific strategy.
        raise HTTPException(503, f"Embedding model unavailable: {str(exc)[:160]}") from None
    hits = result.hits
    # Query-aware sentence selection over the AUTHORIZED hits only. The semantic sentence scorer is
    # used where the embedding model is part of the deployment (PostgreSQL); SQLite stays lexical.
    sentence_embedder = embedder if db.get_bind().dialect.name == "postgresql" else None
    extract_start = perf_counter()
    try:
        snippets = select_evidence(payload.question, hits, sentence_embedder) if hits else []
    except EmbeddingError:
        snippets = select_evidence(payload.question, hits, None)  # lexical scoring only
    result.timings_ms["evidence_selection"] = round((perf_counter() - extract_start) * 1000, 2)
    evidence = {"applied": False}
    if mode in ("semantic", "hybrid") and payload.apply_evidence_policy:
        decision = assess(payload.question, result, EVIDENCE_POLICY, snippets)
        evidence = {
            "applied": True,
            "sufficient": decision.sufficient,
            "reasons": decision.reasons,
            "signals": decision.signals,
            "policy": EVIDENCE_POLICY.describe(),
            "withheld_candidates": 0 if decision.sufficient else len(hits),
            "note": "Heuristic signals calibrated on a synthetic dev split; "
                    "not probabilities or confidence scores.",
        }
        if not decision.sufficient:
            hits, snippets = [], []  # insufficient evidence: nothing is cited or sent to an LLM
    # Provider failures and unverifiable LLM citations fall back to verbatim quotes inside answer().
    response, answer_mode = answer(payload.question, hits, sentence_embedder, snippets)
    audit(db, user, "ask")
    db.commit()
    return {
        "answer": response,
        "mode": answer_mode,
        "retrieval": result.mode,
        "duration_ms": round((perf_counter() - start) * 1000),
        "diagnostics": {
            "mode": result.mode,
            "source_count": len(hits),
            "retrieval_ms": result.timings_ms,
            "branch_candidates": result.branch_counts,
            "parameters": result.parameters,
            "score_note": SCORE_NOTES[result.mode],
            "evidence": evidence,
        },
        "sources": [
            {
                "id": f"S{i}",
                "document_id": h.document_id,
                "chunk_id": h.chunk_id,
                "title": h.document_title,
                "page": h.page_number,
                "excerpt": h.content[:800],
                # Query-selected sentences from THIS chunk (verbatim substrings; quoted with [S#]
                # in offline mode). Empty when the source was not among the quoted top sources.
                "selected_sentences": [s.sentence for s in snippets if s.source_index == i],
                "method": h.method,
                "score": h.score,
                "bm25_score": h.score if h.method == "bm25" else None,
                "fts_rank_cd": h.fts_rank_cd,
                "cosine_distance": h.distance,
                "lexical_rank": h.lexical_rank,
                "semantic_rank": h.semantic_rank,
                "rrf_score": h.rrf_score,
            }
            for i, h in enumerate(hits, 1)
        ],
    }


EVAL_REPORT = Path(__file__).resolve().parents[2] / "eval" / "reports" / "latest.json"


@app.get("/api/evaluation/summary")
def evaluation_summary(user: User = Depends(current_user)):
    """Stored results of the last full `scripts/evaluate.py` run (no document content inside)."""
    if not EVAL_REPORT.is_file():
        raise HTTPException(404, "No evaluation report yet. Run: python scripts/evaluate.py")
    import json

    r = json.loads(EVAL_REPORT.read_text(encoding="utf-8"))
    cal = r.get("calibration", {}).get("modes", {})
    return {
        "generated_at": r["meta"]["generated_at"],
        "dataset": r["meta"]["cases"] | {"sha256": r["meta"]["dataset_sha256"]},
        "corpus": r["meta"]["corpus"],
        "model": {"name": r["meta"]["index"]["model"], "revision": r["meta"]["index"]["model_revision"]},
        "modes": {m: {"test": v["test"], "all": v["all"], "latency_ms": v["latency"]["wall_ms"],
                      "by_category": {k: g.get("ndcg@5") for k, g in v["by_category"].items()}}
                  for m, v in r["benchmark"]["modes"].items()},
        "significance": r["benchmark"].get("significance_test_split"),
        "ablation": {k: r["ablation"][k] for k in ("default", "selected", "test_delta_ndcg@5",
                                                     "test_delta_recall@5", "grid_size")}
        if "ablation" in r else None,
        "abstention": {m: {"selected": v["selected"], "no_policy_test": v["baselines"]["no_policy"]["test"]}
                       for m, v in cal.items()},
        "security": r.get("security"),
    }


@app.get("/api/audit")
def audit_events(user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.role != "admin":
        raise HTTPException(403, "Admin role required")
    events = db.scalars(
        select(AuditEvent)
        .where(AuditEvent.tenant_id == user.tenant_id)
        .order_by(AuditEvent.id.desc())
        .limit(30)
    ).all()
    return [
        {
            "id": row.id,
            "action": row.action,
            "user_id": row.user_id,
            "document_id": row.document_id,
            "created_at": row.created_at.isoformat(),
        }
        for row in events
    ]
