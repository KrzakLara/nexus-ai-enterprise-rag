"""NEXUS AI API: enterprise document intelligence with permission-scoped hybrid RAG.

Phase 4 (enterprise hardening): OIDC authentication with server-side sessions (AUTH_MODE=oidc)
next to the explicitly local demo mode, PostgreSQL row-level security below the application ACL,
durable background processing, S3-compatible object storage with a full document lifecycle,
structured logs, Prometheus metrics and OpenTelemetry traces. See docs/enterprise-hardening.md.
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from anyio import to_thread
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from . import lifecycle
from .auth import current_user
from .config import assert_secure_configuration, settings
from .database import assert_runtime_role, engine, get_db, worker_engine
from .docintel.ocr import ocr_available
from .docintel.parsing import parse_document
from .docintel_api import router as docintel_router
from .docintel_api import summary as intelligence_summary
from .embeddings import Embedder, EmbeddingError, get_embedder
from .evidence import EvidencePolicy, assess
from .extractive import select_evidence
from .identity.routes import router as auth_router
from .identity.routes import user_view
from .jobs import queue
from .lifecycle_api import router as lifecycle_router
from .lifecycle_api import stream_object
from .migrate import assert_schema_current, current_revision, head_revision
from .models import AuditEvent, Chunk, Document, DocumentGrant, ProcessingJob, User, WorkerHeartbeat
from .observability.logging import configure_logging
from .observability.metrics import (DB_POOL, QUEUE_DEPTH, RAG_DURATION, RETRIEVAL_DURATION,
                                    register_collectors, start_metrics_server)
from .observability.middleware import RequestContextMiddleware
from .observability.tracing import init_tracing, span
from .rag import answer
from .retrieval import RetrievalMode, RetrievalUnavailable, retrieve
from .security import audit, authorized_documents, permitted_document
from .storage import StorageError, default_store
from .uploads import UploadRejected, validate_upload

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
    configure_logging(settings.service_name, settings.app_env, settings.log_level, settings.log_format)
    # Fail closed: demo credentials can never activate in a production-like environment, and OIDC
    # mode refuses to start without issuer / client / redirect / session key configuration.
    assert_secure_configuration(settings)
    # Schema is owned by Alembic (python scripts/migrate.py); fail fast instead of create_all().
    assert_schema_current(engine)
    role = assert_runtime_role(engine, settings.db_require_rls_role)
    if settings.storage_backend == "local":
        settings.storage_dir.mkdir(parents=True, exist_ok=True)
    elif settings.s3_create_bucket:
        default_store(settings).ensure_bucket()
    register_collectors()
    DB_POOL.register("api", engine)
    if settings.worker_database_url:
        DB_POOL.register("worker", worker_engine())
    sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def queue_stats():
        with sessions() as db:
            return queue.queue_stats(db)

    QUEUE_DEPTH.set_source(queue_stats)
    start_metrics_server(settings.metrics_port)
    init_tracing(settings, settings.service_name, app=app, engines=[engine])
    log.info("api_started", extra={"event": "api_started", "auth_mode": settings.auth_mode,
                                   "processing_mode": settings.processing_mode,
                                   "storage_backend": settings.storage_backend,
                                   "db_role": (role or {}).get("name")})
    if settings.embedding_warmup and engine.dialect.name == "postgresql":
        # In a worker thread: the event loop is not blocked while the model loads.
        await to_thread.run_sync(warm_up_embedder)
    yield


app = FastAPI(
    title="NEXUS AI — Enterprise Knowledge Intelligence",
    description="Permission-scoped hybrid RAG and document intelligence with verifiable evidence",
    version=settings.service_version,
    lifespan=lifespan,
)
app.include_router(auth_router)
app.include_router(docintel_router)
app.include_router(lifecycle_router)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.frontend_origins),
    allow_credentials=True,  # cookie sessions from the configured frontend origins only
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-CSRF-Token", "Idempotency-Key", "X-Request-ID"],
    expose_headers=["X-Request-ID"],
)
app.add_middleware(RequestContextMiddleware)


@app.exception_handler(OperationalError)
async def database_unavailable(request: Request, exc: OperationalError):
    """Connection-level database failures are a temporary outage (503), never a 500 with details."""
    log.warning("database_unavailable", extra={"event": "database_unavailable",
                                               "error_type": type(getattr(exc, "orig", exc)).__name__})
    return JSONResponse({"detail": "The database is temporarily unavailable; please retry."}, status_code=503,
                        headers={"Retry-After": "5"})


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


# ----------------------------------------------------------------------------- health

@app.get("/api/health")
def health():
    """Public capability summary for the UI (no hostnames, credentials or error details)."""
    return {
        "status": "ok",
        "mode": "local_demo" if settings.demo_auth else "oidc",
        "auth_mode": settings.auth_mode,
        "processing_mode": settings.processing_mode,
        "storage_backend": settings.storage_backend,
        "version": app.version,
        "database": engine.dialect.name,
        "semantic_retrieval": engine.dialect.name == "postgresql",
        "retrieval_modes": (["bm25", "lexical", "semantic", "hybrid"]
                            if engine.dialect.name == "postgresql" else ["bm25"]),
        "default_retrieval_mode": default_retrieval_mode(engine.dialect.name),
        "embedding_model": settings.embedding_model,
        "embedding_model_loaded": get_embedder().is_loaded,
        "ocr_available": ocr_available(),
        "document_formats": ["pdf", "pdf (scanned, OCR)", "docx", "xlsx", "txt", "md"],
    }


@app.get("/api/health/live")
def liveness():
    """The process is running and serving HTTP. Never touches dependencies (no restart loops
    because the database is briefly unavailable)."""
    return {"status": "alive"}


@app.get("/api/health/ready")
def readiness(response: Response, db: Session = Depends(get_db)):
    """Ready to serve traffic: critical dependencies reachable. Component states only: no hosts,
    versions, credentials or exception texts."""
    checks: dict[str, str] = {}
    try:
        db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:  # noqa: BLE001
        db.rollback()
        checks["database"] = "unavailable"
    if checks["database"] == "ok":
        try:
            checks["schema"] = "ok" if current_revision(db.get_bind()) == head_revision() else "outdated"
        except Exception:  # noqa: BLE001
            checks["schema"] = "unavailable"
    try:
        checks["object_storage"] = "ok" if default_store(settings).healthy() else "unavailable"
    except StorageError:
        checks["object_storage"] = "unavailable"
    if settings.oidc_auth:
        from .identity.oidc import OIDCError, get_oidc_client

        try:
            get_oidc_client(settings).metadata()
            checks["identity_provider"] = "ok"
        except OIDCError:
            checks["identity_provider"] = "unavailable"
    if settings.processing_mode == "queue" and checks["database"] == "ok":
        try:
            cutoff = lifecycle.now_utc().timestamp() - settings.worker_stale_seconds
            beats = db.scalars(select(WorkerHeartbeat.last_seen_at)).all()
            fresh = [b for b in beats if lifecycle._aware(b).timestamp() >= cutoff]
            checks["workers"] = "ok" if fresh else "none_active"  # degraded: uploads still queue
        except Exception:  # noqa: BLE001
            db.rollback()
            checks["workers"] = "unknown"
    checks["embedding_model"] = "loaded" if get_embedder().is_loaded else "not_loaded"
    critical = ("database", "schema", "object_storage", "identity_provider")
    ready = all(checks.get(name, "ok") == "ok" for name in critical)
    degraded = checks.get("workers") not in (None, "ok") or checks["embedding_model"] != "loaded"
    response.status_code = 200 if ready else 503
    return {"status": "ready" if ready and not degraded else ("degraded" if ready else "not_ready"),
            "checks": checks}


# ----------------------------------------------------------------------------- identity

@app.get("/api/me")
def me(user: User = Depends(current_user)):
    return user_view(user)


# ----------------------------------------------------------------------------- documents

def _grants(db: Session, ids: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {i: [] for i in ids}
    if ids:
        for doc_id, group in db.execute(select(DocumentGrant.document_id, DocumentGrant.group_name)
                                        .where(DocumentGrant.document_id.in_(ids))
                                        .order_by(DocumentGrant.id)):
            out[doc_id].append(group)
    return out


@app.get("/api/documents")
def documents(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(authorized_documents(user).order_by(Document.created_at.desc())).all()
    ids = [d.id for d in rows]
    grants, jobs = _grants(db, ids), lifecycle.latest_jobs(db, ids)
    return [
        {
            "id": d.id,
            "title": d.title,
            "filename": d.filename,
            "pages": d.page_count,
            "created_at": d.created_at.isoformat(),
            "fields": d.extracted_fields,
            "groups": grants[d.id],
            "intelligence": intelligence_summary(db, d),
            **lifecycle.lifecycle_view(d, jobs.get(d.id)),
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
        "owner_is_me": doc.owner_id == user.id,
        **lifecycle.lifecycle_view(doc, lifecycle.latest_jobs(db, [doc.id]).get(doc.id)),
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
    response = stream_object(doc.storage_backend, doc.storage_key, doc.filename, doc.content_type)
    audit(db, user, "source_download", doc.id)
    db.commit()
    return response


# A plain `def` route: FastAPI runs it in its worker threadpool, so validation, storage I/O and
# (inline mode) parsing / embedding never block the event loop.
@app.post("/api/documents", status_code=201)
def upload(
    request: Request,
    response: Response,
    file: UploadFile = File(...),
    groups: str = Form(..., description="Comma-separated groups to grant"),
    retention_days: int | None = Form(default=None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    embedder: Embedder = Depends(get_embedder),
    sessions: sessionmaker = Depends(lifecycle.get_worker_sessions),
):
    cfg = settings
    allowed_groups = list(dict.fromkeys(g.strip() for g in groups.split(",") if g.strip()))
    if not allowed_groups or len(allowed_groups) > 5 or any(len(g) > 60 for g in allowed_groups):
        raise HTTPException(400, "Provide 1 to 5 valid groups")
    if user.role != "admin" and not set(allowed_groups).issubset(set(user.groups)):
        raise HTTPException(403, "Cannot share with a group you do not belong to")
    idempotency_key = lifecycle.checked_idempotency_key(request.headers.get("Idempotency-Key"))
    if idempotency_key:
        previous = queue.by_idempotency_key(db, user.tenant_id, idempotency_key)
        if previous is not None and previous.document_id:
            response.status_code = 200
            return {"id": previous.document_id, "deduplicated": True, "job": queue.view(previous)}
    contents = file.file.read(cfg.max_upload_bytes + 1)
    try:
        validated = validate_upload(file.filename, contents, cfg)
    except UploadRejected as exc:
        raise HTTPException(exc.status, exc.message) from None
    retention = lifecycle.retention_until(retention_days, cfg)
    parsed = None
    if cfg.processing_mode == "inline":
        # Inline (local demo, tests, evaluation): keep the v0.3 contract that a file which cannot
        # be parsed is rejected with 422 and nothing is stored. Queue mode parses in the worker.
        try:
            with span("document.parse", **{"document.format": validated.format}):
                parsed = parse_document(validated.filename, contents)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(422, f"Document could not be parsed: {str(exc)[:180]}") from None
    doc_id = str(uuid4())
    backend, key = lifecycle.put_original(cfg, user.tenant_id, doc_id, 1, validated, contents)
    try:
        doc, job = lifecycle.create_document(db, user, validated, allowed_groups, document_id=doc_id,
                                             backend=backend, key=key, cfg=cfg, retention=retention,
                                             idempotency_key=idempotency_key)
        db.commit()
    except IntegrityError:
        db.rollback()
        lifecycle.discard_object(cfg, key)
        previous = queue.by_idempotency_key(db, user.tenant_id, idempotency_key) if idempotency_key else None
        if previous is None:
            raise
        response.status_code = 200
        return {"id": previous.document_id, "deduplicated": True, "job": queue.view(previous)}
    except Exception:
        db.rollback()
        lifecycle.discard_object(cfg, key)
        raise
    duplicate_of = db.scalar(authorized_documents(user).with_only_columns(Document.id).where(
        Document.sha256 == validated.sha256, Document.id != doc_id, Document.owner_id == user.id).limit(1))
    if cfg.processing_mode != "inline":
        response.status_code = 202
        return {"id": doc_id, "title": doc.title, "lifecycle_state": "uploaded", "duplicate_of": duplicate_of,
                "job": queue.view(job), "status_url": f"/api/jobs/{job.id}"}
    outcome = lifecycle.execute_inline(job.id, cfg, sessions, embedder, preparsed=parsed, preloaded=contents)
    db.expire_all()
    doc = db.get(Document, doc_id)
    result = outcome.result or {}
    if outcome.status == "completed":
        embedding_status = result.get("embedding_status", "unknown")
    elif outcome.error_code == "embedding_unavailable":
        embedding_status = outcome.error_message or "failed"
    else:
        embedding_status = f"not_completed: {outcome.error_code or outcome.status}"
    return {
        "id": doc_id,
        "title": doc.title,
        "pages": doc.page_count,
        "fields_extracted": len(doc.extracted_fields or {}),
        "embedding_status": embedding_status,
        "processing": intelligence_summary(db, doc),
        "lifecycle_state": doc.lifecycle_state,
        "duplicate_of": duplicate_of,
        "job": queue.view(db.get(ProcessingJob, job.id) or job),
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
        with span("retrieval", **{"retrieval.mode": mode, "retrieval.top_k": payload.top_k}):
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
    RETRIEVAL_DURATION.labels(mode).observe((perf_counter() - start))
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
    with span("rag.answer", **{"retrieval.mode": mode, "rag.sources": len(hits)}):
        response, answer_mode = answer(payload.question, hits, sentence_embedder, snippets)
    audit(db, user, "ask")
    db.commit()
    RAG_DURATION.labels(answer_mode).observe(perf_counter() - start)
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
