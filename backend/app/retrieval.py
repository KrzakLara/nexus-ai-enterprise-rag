"""Retrieval strategies. Every strategy applies the SAME authorization subquery
(security.authorized_documents) inside the SQL statement that reads chunk content, i.e. before
any ranking and before LIMIT. Unauthorized chunks are never loaded, scored, fused or returned.

    bm25      in-process Python BM25 over <= 2,000 authorized chunks (v0.1 baseline, any database)
    lexical   PostgreSQL full-text search: websearch_to_tsquery('simple') + ts_rank_cd, GIN index
    semantic  pgvector exact cosine distance over multilingual-e5-small embeddings
    hybrid    lexical + semantic candidates (each permission-scoped), Reciprocal Rank Fusion
"""
from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from time import perf_counter
from typing import Literal, Protocol

import numpy as np
from sqlalchemy import Text, and_, bindparam, case, cast, func, literal_column, select
from sqlalchemy.dialects.postgresql import TSQUERY, TSVECTOR
from sqlalchemy.orm import Session

from .fusion import DEFAULT_RRF_K, reciprocal_rank_fusion
from .models import Chunk, ChunkEmbedding, Document, User
from .security import authorized_documents

RetrievalMode = Literal["bm25", "lexical", "semantic", "hybrid"]
FTS_CONFIG = "simple"


@dataclass
class Hit:
    document_id: str
    document_title: str
    chunk_id: str
    page_number: int
    content: str
    score: float
    method: str = "bm25"  # bm25 | fts | semantic | hybrid
    distance: float | None = None
    fts_rank_cd: float | None = None
    lexical_rank: int | None = None   # 1-based rank in the PostgreSQL FTS branch
    semantic_rank: int | None = None  # 1-based rank in the pgvector branch
    rrf_score: float | None = None


@dataclass
class RetrievalResult:
    mode: RetrievalMode
    hits: list[Hit]
    timings_ms: dict[str, float] = field(default_factory=dict)
    branch_counts: dict[str, int] = field(default_factory=dict)
    parameters: dict[str, object] = field(default_factory=dict)
    # Full permission-scoped candidate lists per branch (input to evidence assessment; never
    # serialized to the client).
    branch_hits: dict[str, list[Hit]] = field(default_factory=dict)


class QueryEmbedder(Protocol):
    model_name: str

    def embed_query(self, text: str) -> np.ndarray: ...


class RetrievalUnavailable(RuntimeError):
    """The requested strategy needs PostgreSQL (tsvector / pgvector)."""


class SemanticSearchUnavailable(RetrievalUnavailable):
    """Semantic retrieval needs PostgreSQL with pgvector."""


def _require_postgres(db: Session, what: str, exc: type[RetrievalUnavailable]) -> None:
    if db.get_bind().dialect.name != "postgresql":
        raise exc(f"{what} requires PostgreSQL")


def tokenize(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower(), flags=re.UNICODE)


# ----------------------------------------------------------------------------- BM25 (baseline)

def search(db: Session, user: User, question: str, top_k: int = 5) -> list[Hit]:
    # Authorization is enforced by SQL BEFORE fetching any chunk content.
    allowed_ids = authorized_documents(user).with_only_columns(Document.id)
    rows = db.execute(
        select(Chunk, Document).join(Document, Chunk.document_id == Document.id)
        .where(Chunk.document_id.in_(allowed_ids))
        .order_by(Document.id, Chunk.position).limit(2000)
    ).all()
    if not rows:
        return []
    tokens = tokenize(question)
    if not tokens:
        return []
    doc_tokens = [tokenize(chunk.content) for chunk, _ in rows]
    df = Counter(token for words in doc_tokens for token in set(words))
    mean_length = sum(map(len, doc_tokens)) / max(len(rows), 1)
    scores = []
    for (chunk, document), words in zip(rows, doc_tokens):
        counts = Counter(words)
        score = 0.0
        for word in set(tokens):
            freq = counts[word]
            if not freq:
                continue
            idf = math.log(1 + (len(rows) - df[word] + 0.5) / (df[word] + 0.5))
            score += idf * (freq * 2.2) / (freq + 1.2 * (0.25 + 0.75 * len(words) / max(mean_length, 1)))
        if score > 0:
            scores.append(Hit(document.id, document.title, chunk.id, chunk.page_number,
                              chunk.content, round(score, 4)))
    return sorted(scores, key=lambda hit: (-hit.score, hit.document_id, hit.chunk_id))[:top_k]


# ------------------------------------------------------------------- PostgreSQL full-text search

# Query-side stop words (English + Croatian function words). The 'simple' configuration has no
# stop list, and ts_rank_cd has no IDF, so without this "what/is/the" dominate natural-language
# questions. Removed only from bare words: quoted phrases, -negations and identifiers are kept.
QUERY_STOP_WORDS = frozenset("""
a an and are as at be been by can could did do does for from had has have how i if in into is it
its me my no not of on or our please should so than that the their them then there these they
this those to us was we were what when where which who whom why will with would you your
a ako ali bi bio biti da do gdje i ih ili iz je jesu ju kada kako kao koja koje koji kojim li
me mi na nam ne nego ni nije o od ona one oni ono pa po pri s sa se su svi ta te ti to tu u uz
vam za što
""".split())


def build_fts_query_text(question: str) -> str:
    """Prepare text for websearch_to_tsquery: drop stop words from bare terms only.

    A question made only of stop words yields "" (no lexical evidence) rather than matching
    every chunk that contains "the".
    """
    tokens = re.findall(r'"[^"]*"|\S+', question)
    kept = [
        tok for tok in tokens
        if tok.startswith(('"', "-"))
        or re.sub(r"^\W+|\W+$", "", tok).lower() not in QUERY_STOP_WORDS
    ]
    return " ".join(kept)


def tsquery_expression(query_text: str):
    """websearch_to_tsquery with OR between terms (phrases keep <->); AND if negation is used.

    websearch_to_tsquery ANDs every term, which returned 0 matches for all natural-language
    questions in the demo evaluation set; OR + ts_rank_cd ranking recovers them. A query that
    uses websearch negation (-term -> !'term') keeps the original AND semantics, because
    "a | !b" would match almost every chunk.
    """
    web = func.websearch_to_tsquery(
        literal_column(f"'{FTS_CONFIG}'::regconfig"), bindparam("fts_q", query_text)
    )
    web_text = cast(web, Text)
    any_term = cast(func.replace(web_text, " & ", " | "), TSQUERY)
    return case((func.strpos(web_text, "!") > 0, web), else_=any_term)


def fts_search(db: Session, user: User, question: str, top_k: int = 5) -> list[Hit]:
    """Permission-scoped PostgreSQL FTS. No 2,000-chunk scan: GIN index + LIMIT in SQL."""
    _require_postgres(db, "PostgreSQL full-text search", RetrievalUnavailable)
    query_text = build_fts_query_text(question)
    if not tokenize(query_text):
        return []
    tsv = literal_column("chunks.content_tsv", TSVECTOR)  # generated (migration 0003), unmapped
    tsq = tsquery_expression(query_text)
    rank = func.ts_rank_cd(tsv, tsq, 1)  # normalization 1: divide by 1 + log(document length)
    allowed_ids = authorized_documents(user).with_only_columns(Document.id)
    stmt = (
        select(Chunk, Document, rank.label("rank"))
        .join(Document, Chunk.document_id == Document.id)
        .where(Chunk.document_id.in_(allowed_ids))
        .where(tsv.op("@@")(tsq))
        .order_by(rank.desc(), Chunk.id)
        .limit(top_k)
    )
    rows = db.execute(stmt).all()
    return [
        Hit(document.id, document.title, chunk.id, chunk.page_number, chunk.content,
            score=round(float(r), 6), method="fts", fts_rank_cd=round(float(r), 6),
            lexical_rank=i)
        for i, (chunk, document, r) in enumerate(rows, 1)
    ]


# ------------------------------------------------------------------------- pgvector semantic

def semantic_search(
    db: Session, user: User, question: str, embedder: QueryEmbedder, top_k: int = 5,
    timings: dict[str, float] | None = None,
) -> list[Hit]:
    """Exact (sequential-scan) cosine search over AUTHORIZED chunks only.

    The ACL predicate is part of the same SELECT ... ORDER BY embedding <=> :q LIMIT k statement:
    ranking happens only among rows the user may read. There is intentionally no ANN index yet,
    so results are exact and cannot lose recall to post-filtering.
    """
    _require_postgres(db, "Semantic retrieval with pgvector", SemanticSearchUnavailable)
    start = perf_counter()
    query_vector = embedder.embed_query(question)
    embedded = perf_counter()
    allowed_ids = authorized_documents(user).with_only_columns(Document.id)
    distance = ChunkEmbedding.embedding.cosine_distance([float(x) for x in query_vector])
    rows = db.execute(
        select(Chunk, Document, distance.label("distance"))
        .join(
            ChunkEmbedding,
            and_(ChunkEmbedding.chunk_id == Chunk.id, ChunkEmbedding.model_name == embedder.model_name),
        )
        .join(Document, Chunk.document_id == Document.id)
        .where(Chunk.document_id.in_(allowed_ids))
        .order_by(distance, Chunk.id)
        .limit(top_k)
    ).all()
    if timings is not None:
        timings["embedding"] = round((embedded - start) * 1000, 2)
        timings["semantic_db"] = round((perf_counter() - embedded) * 1000, 2)
    return [
        Hit(document.id, document.title, chunk.id, chunk.page_number, chunk.content,
            score=round(1.0 - float(dist), 4), method="semantic", distance=round(float(dist), 6),
            semantic_rank=i)
        for i, (chunk, document, dist) in enumerate(rows, 1)
    ]


# ------------------------------------------------------------------------------- hybrid (RRF)

def hybrid_search(
    db: Session,
    user: User,
    question: str,
    embedder: QueryEmbedder,
    top_k: int = 5,
    candidates: int = 50,
    weights: dict[str, float] | None = None,
    rrf_k: int = DEFAULT_RRF_K,
) -> RetrievalResult:
    """Two independent permission-scoped branches, fused by Reciprocal Rank Fusion.

    Both branches are complete SQL queries with the ACL inside them, so the fusion input can only
    contain authorized chunks; a globally better unauthorized chunk cannot occupy a candidate slot.
    """
    weights = weights or {"lexical": 1.0, "semantic": 1.0}
    timings: dict[str, float] = {}

    def timed(name: str, fn: Callable[[], list[Hit]]) -> list[Hit]:
        start = perf_counter()
        try:
            return fn()
        finally:
            timings[name] = round((perf_counter() - start) * 1000, 2)

    lexical = timed("lexical", lambda: fts_search(db, user, question, candidates))
    semantic = timed("semantic",
                     lambda: semantic_search(db, user, question, embedder, candidates, timings))

    start = perf_counter()
    fused = reciprocal_rank_fusion(
        {"lexical": [h.chunk_id for h in lexical], "semantic": [h.chunk_id for h in semantic]},
        weights=weights, k=rrf_k, top_k=top_k,
    )
    by_id = {h.chunk_id: h for h in semantic} | {h.chunk_id: h for h in lexical}
    lexical_by_id = {h.chunk_id: h for h in lexical}
    semantic_by_id = {h.chunk_id: h for h in semantic}
    hits = []
    for result in fused:
        base = by_id[result.chunk_id]
        lex, sem = lexical_by_id.get(result.chunk_id), semantic_by_id.get(result.chunk_id)
        hits.append(replace(
            base,
            method="hybrid",
            score=round(result.rrf_score, 6),
            rrf_score=round(result.rrf_score, 6),
            lexical_rank=result.ranks.get("lexical"),
            semantic_rank=result.ranks.get("semantic"),
            fts_rank_cd=lex.fts_rank_cd if lex else None,
            distance=sem.distance if sem else None,
        ))
    timings["fusion"] = round((perf_counter() - start) * 1000, 2)
    return RetrievalResult(
        mode="hybrid",
        hits=hits,
        timings_ms=timings,
        branch_counts={"lexical": len(lexical), "semantic": len(semantic)},
        parameters={"candidates": candidates, "rrf_k": rrf_k, "weights": weights},
        branch_hits={"lexical": lexical, "semantic": semantic},
    )


def retrieve(
    db: Session,
    user: User,
    question: str,
    mode: RetrievalMode,
    embedder: QueryEmbedder,
    top_k: int = 5,
    candidates: int = 50,
    weights: dict[str, float] | None = None,
    rrf_k: int = DEFAULT_RRF_K,
) -> RetrievalResult:
    """Dispatch to one strategy and return hits plus diagnostics (no document content logged)."""
    start = perf_counter()
    if mode == "hybrid":
        result = hybrid_search(db, user, question, embedder, top_k, candidates, weights, rrf_k)
    else:
        timings: dict[str, float] = {}
        if mode == "bm25":
            hits = search(db, user, question, top_k)
        elif mode == "lexical":
            hits = fts_search(db, user, question, top_k)
        elif mode == "semantic":
            hits = semantic_search(db, user, question, embedder, top_k, timings)
        else:  # pragma: no cover - guarded by the API's Literal type
            raise ValueError(f"Unknown retrieval mode {mode!r}")
        result = RetrievalResult(mode=mode, hits=hits, timings_ms=timings,
                                 branch_counts={mode: len(hits)}, branch_hits={mode: hits})
    result.timings_ms["total"] = round((perf_counter() - start) * 1000, 2)
    return result
