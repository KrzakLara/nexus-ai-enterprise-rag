"""PostgreSQL + pgvector integration tests with REAL multilingual-e5-small embeddings."""
import numpy as np
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from backend.app.embeddings import get_embedder
from backend.app.indexing import embed_pending_chunks
from backend.app.main import app
from backend.app.models import ChunkEmbedding, User
from backend.app.retrieval import semantic_search

from .conftest import DOCS, PEOPLE, make_client, seed

pytestmark = [pytest.mark.postgres, pytest.mark.model]

TABLES = "chunk_embeddings, audit_events, chunks, document_grants, documents, users"

# The query below is (almost) verbatim the text of the two UNAUTHORIZED documents, so in the
# global vector space they are the nearest neighbours. The authorized document is only loosely
# related. A correct implementation must still never return the unauthorized chunks.
LEAK_QUERY = "Project Nightingale acquisition target price is EUR 48 million, board approval pending."
LEAK_DOCS = [
    # authorized for finance@northstar (group finance, same tenant)
    ("a_fin", "northstar", "u1", "Q3 finance memo", ["finance"],
     "The finance team will review supplier payment terms in the third quarter."),
    # same tenant, but only the HR group: finance@northstar must NOT see it
    ("x_hr", "northstar", "u0", "HR acquisition note", ["hr"],
     "Project Nightingale acquisition target price is EUR 48 million; board approval pending."),
    # other tenant, even the same group name: must NOT be visible
    ("x_other", "otherco", "u3", "Otherco deal sheet", ["finance"],
     "Project Nightingale acquisition target price is EUR 48 million, board approval is pending."),
]


@pytest.fixture
def pg(pg_engine, embedder):
    with pg_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    factory = sessionmaker(pg_engine, expire_on_commit=False)
    return factory, embedder


def _index(factory, embedder):
    with factory() as db:
        stats = embed_pending_chunks(db, embedder)
        db.commit()
    return stats


def test_vector_extension_and_column_type(pg_engine):
    with pg_engine.connect() as conn:
        assert conn.execute(text("SELECT 1 FROM pg_extension WHERE extname='vector'")).scalar() == 1
        col_type = conn.execute(text(
            "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
            "WHERE attrelid = 'chunk_embeddings'::regclass AND attname = 'embedding'")).scalar()
    assert col_type == "vector(384)"


def test_real_embeddings_are_persisted_with_metadata_and_backfill_is_idempotent(pg):
    factory, embedder = pg
    seed(factory)
    first = _index(factory, embedder)
    second = _index(factory, embedder)
    assert first.embedded == 3 and second.embedded == 0 and second.skipped_up_to_date == 3
    with factory() as db:
        rows = db.execute(text(
            "SELECT chunk_id, vector_dims(embedding), model_name, model_revision, "
            "embedding::text FROM chunk_embeddings ORDER BY chunk_id")).all()
        assert [r[0] for r in rows] == ["d_fin_c0", "d_hr_c0", "d_other_c0"]
        for _, dims, model, revision, literal in rows:
            vector = np.array([float(x) for x in literal.strip("[]").split(",")])
            assert dims == 384 and model == "intfloat/multilingual-e5-small"
            assert len(revision) == 40
            assert abs(np.linalg.norm(vector) - 1.0) < 1e-3       # normalized, stored as-is
            assert np.count_nonzero(vector) > 300                  # a real dense vector
        # Stored vector equals a fresh encoding of the chunk text (genuine, not placeholder)
        stored = db.scalar(select(ChunkEmbedding).where(ChunkEmbedding.chunk_id == "d_fin_c0"))
        fresh = embedder.embed_passages([DOCS[1][5]])[0]
        assert np.allclose(np.asarray(stored.embedding), fresh, atol=1e-4)


def test_semantic_search_ranks_by_real_cosine_similarity(pg):
    factory, embedder = pg
    docs = [
        ("s_exp", "northstar", "u1", "Atlas agreement", ["finance"],
         "The Atlas supplier agreement ends on 31 March 2027; notice must be given 90 days before."),
        ("s_cloud", "northstar", "u1", "Cygnus cloud SLA", ["finance"],
         "Cygnus guarantees 99.9% monthly availability with service credits for downtime."),
        ("s_pay", "northstar", "u1", "Payroll", ["finance"],
         "Payroll is processed on the 25th of every month."),
    ]
    seed(factory, docs=docs)
    _index(factory, embedder)
    with factory() as db:
        user = db.get(User, "u1")
        # Croatian paraphrase with no shared keywords with the English passage
        hits = semantic_search(db, user, "Kada istječe ugovor s dobavljačem?", embedder, top_k=3)
        assert [h.document_id for h in hits][0] == "s_exp"
        assert [h.distance for h in hits] == sorted(h.distance for h in hits)
        uptime = semantic_search(db, user, "What uptime does the cloud provider promise?", embedder)
        assert uptime[0].document_id == "s_cloud"
        # Distance reported by PostgreSQL == 1 - cosine computed independently in numpy
        q = embedder.embed_query("What uptime does the cloud provider promise?")
        p = embedder.embed_passages([docs[1][5]])[0]
        assert abs(uptime[0].distance - (1 - float(q @ p))) < 1e-4
        assert all(h.method == "semantic" for h in uptime)


def test_unauthorized_documents_never_returned_even_when_semantically_closest(pg):
    factory, embedder = pg
    seed(factory, docs=LEAK_DOCS)
    _index(factory, embedder)
    q = embedder.embed_query(LEAK_QUERY)
    with factory() as db:
        # Precondition: WITHOUT the ACL, the two forbidden chunks are the global nearest neighbours.
        global_order = db.execute(text(
            "SELECT c.document_id FROM chunk_embeddings e JOIN chunks c ON c.id = e.chunk_id "
            "ORDER BY e.embedding <=> CAST(:q AS vector)"), {"q": str(q.tolist())}).scalars().all()
        assert set(global_order[:2]) == {"x_hr", "x_other"}, global_order
        assert global_order[-1] == "a_fin"

        finance = db.get(User, "u1")
        for top_k in (1, 3, 10):
            hits = semantic_search(db, finance, LEAK_QUERY, embedder, top_k=top_k)
            assert [h.document_id for h in hits] == ["a_fin"]
        # Other-tenant member sees only its own tenant's document
        other = db.get(User, "u3")
        assert [h.document_id for h in semantic_search(db, other, LEAK_QUERY, embedder, 10)] == ["x_other"]
        # Admin is tenant-scoped too
        admin = db.get(User, "u2")
        assert {h.document_id for h in semantic_search(db, admin, LEAK_QUERY, embedder, 10)} == {"a_fin", "x_hr"}

    # Through the API: nothing from the forbidden chunks in sources OR in the answer text
    client, token = make_client(factory)
    app.dependency_overrides[get_embedder] = lambda: embedder
    try:
        raw = client.post("/api/ask", headers=token("finance@northstar.demo"), json={
            "question": LEAK_QUERY, "mode": "semantic", "top_k": 10,
            "apply_evidence_policy": False}).json()
        guarded = client.post("/api/ask", headers=token("finance@northstar.demo"),
                              json={"question": LEAK_QUERY, "mode": "semantic", "top_k": 10}).json()
    finally:
        app.dependency_overrides.clear()
    # Without the evidence policy the only authorized (but irrelevant) chunk is returned ...
    assert raw["retrieval"] == "semantic"
    assert [s["document_id"] for s in raw["sources"]] == ["a_fin"]
    assert raw["sources"][0]["method"] == "semantic" and raw["sources"][0]["bm25_score"] is None
    # ... with the default policy its similarity is below the calibrated threshold -> abstain.
    assert guarded["mode"] == "no_evidence" and guarded["sources"] == []
    assert guarded["diagnostics"]["evidence"]["reasons"] == ["evidence_similarity_below_threshold"]
    for body in (raw, guarded):
        assert "Nightingale" not in body["answer"] and "48 million" not in body["answer"]


def test_upload_embeds_immediately_and_is_searchable(pg, tmp_path, monkeypatch):
    from dataclasses import replace

    from backend.app import main

    factory, embedder = pg
    seed(factory, docs=[], people=PEOPLE)
    monkeypatch.setattr(main, "settings", replace(main.settings, storage_dir=tmp_path))
    client, token = make_client(factory)
    app.dependency_overrides[get_embedder] = lambda: embedder
    try:
        hr = token("hr@northstar.demo")
        up = client.post("/api/documents", headers=hr, data={"groups": "hr"}, files={
            "file": ("leave.txt", b"Parental leave policy: employees receive 26 weeks of paid leave.")})
        assert up.status_code == 201, up.text
        assert up.json()["embedding_status"] == "embedded:1"
        body = client.post("/api/ask", headers=hr, json={
            "question": "Koliko traje plaćeni roditeljski dopust?", "mode": "semantic"}).json()
        assert body["sources"][0]["document_id"] == up.json()["id"]
        denied = client.post("/api/ask", headers=token("finance@northstar.demo"), json={
            "question": "parental leave weeks", "mode": "semantic"}).json()
        assert denied["sources"] == [] and denied["mode"] == "no_evidence"
    finally:
        app.dependency_overrides.clear()

