"""PostgreSQL FTS + hybrid RRF integration and security tests (real PostgreSQL, real embeddings)."""
from dataclasses import replace

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app import rag
from backend.app.embeddings import EmbeddingError, get_embedder
from backend.app.fusion import reciprocal_rank_fusion
from backend.app.indexing import embed_pending_chunks
from backend.app.main import app
from backend.app.models import Chunk, Document, DocumentGrant, User
from backend.app.retrieval import fts_search, hybrid_search, search, semantic_search

from .conftest import PEOPLE, TABLES, make_client, seed

pytestmark = [pytest.mark.postgres, pytest.mark.model]

# u0 hr@northstar [hr] · u1 finance@northstar [finance] · u2 admin@northstar · u3 finance@otherco
CORPUS = [
    ("f_atlas", "northstar", "u1", "Atlas supplier agreement", ["finance"],
     "Atlas supplier agreement, contract reference ATL-2027-NS. Either party may terminate "
     "with 90 days written notice before the renewal date."),
    ("f_cloud", "northstar", "u1", "Cygnus cloud SLA", ["finance"],
     "Cygnus guarantees 99.9% monthly availability; outages trigger service credits."),
    ("f_travel", "northstar", "u1", "Travel policy", ["finance"],
     "Travel expenses must be submitted within 30 days together with receipts."),
    ("f_hr_croatian", "northstar", "u1", "Ugovor o održavanju", ["finance"],
     "Ugovor o održavanju opreme istječe 31. prosinca 2026. godine."),
    # FORBIDDEN for finance@northstar: same tenant, HR group only
    ("x_hr", "northstar", "u0", "HR restructuring", ["hr"],
     "Codename SILVER-IBIS-73: restructuring plan, terminate supplier agreement, 90 days notice."),
    # FORBIDDEN for every Northstar user: other tenant
    ("x_other", "otherco", "u3", "Otherco plan", ["finance"],
     "Codename SILVER-IBIS-73: Otherco will terminate the supplier agreement with 90 days notice."),
]
FINANCE, HR, ADMIN, OTHERCO = "u1", "u0", "u2", "u3"
FORBIDDEN_FOR_FINANCE = {"x_hr", "x_other"}


@pytest.fixture
def pg(pg_engine, embedder):
    with pg_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    factory = sessionmaker(pg_engine, expire_on_commit=False)
    seed(factory, docs=CORPUS, people=PEOPLE)
    with factory() as db:
        embed_pending_chunks(db, embedder)
        db.commit()
    return factory, embedder


def ids(hits):
    return [h.document_id for h in hits]


# ------------------------------------------------------------------ schema / migration 0003

def test_generated_tsvector_column_and_gin_index(pg):
    factory, _ = pg
    with factory() as db:
        gen = db.execute(text("SELECT attgenerated FROM pg_attribute WHERE attrelid = "
                              "'chunks'::regclass AND attname = 'content_tsv'")).scalar()
        indexdef = db.execute(text("SELECT indexdef FROM pg_indexes "
                                   "WHERE indexname = 'ix_chunks_content_tsv'")).scalar()
        assert gen == "s" and "USING gin (content_tsv)" in indexdef
        # The planner can use the GIN index for @@ (forced, because the test table is tiny)
        db.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(db.execute(text(
            "EXPLAIN SELECT id FROM chunks WHERE content_tsv @@ 'atlas'::tsquery")).scalars())
        assert "ix_chunks_content_tsv" in plan, plan
        db.rollback()
        # Generated column follows content updates automatically
        db.get(Chunk, "f_travel_c0").content = "Mileage is reimbursed at EUR 0.40 per kilometre."
        db.commit()
        user = db.get(User, FINANCE)
        assert ids(fts_search(db, user, "kilometre reimbursed")) == ["f_travel"]
        assert fts_search(db, user, "receipts") == []


# ---------------------------------------------------------------------------- FTS behaviour

@pytest.mark.parametrize("query", ["ATL-2027-NS", "atl-2027-ns", "ATL-2027-NS??", "(ATL-2027-NS)",
                                   "What does contract ATL-2027-NS say?"])
def test_fts_exact_identifier_case_and_punctuation_insensitive(pg, query):
    factory, _ = pg
    with factory() as db:
        hits = fts_search(db, db.get(User, FINANCE), query)
        assert ids(hits)[0] == "f_atlas" and hits[0].method == "fts" and hits[0].lexical_rank == 1


def test_fts_natural_language_ranking_and_croatian_text(pg):
    factory, _ = pg
    with factory() as db:
        user = db.get(User, FINANCE)
        assert ids(fts_search(db, user, "How many days written notice to terminate?"))[0] == "f_atlas"
        assert ids(fts_search(db, user, "Kada istječe ugovor o održavanju?"))[0] == "f_hr_croatian"
        # Documented 'simple' limitations: no unaccent and no stemming
        assert fts_search(db, user, "istjece") == []
        assert "f_hr_croatian" not in ids(fts_search(db, user, "ugovora"))
        # Phrase search and negation (websearch syntax)
        assert ids(fts_search(db, user, '"written notice"')) == ["f_atlas"]
        assert "f_atlas" not in ids(fts_search(db, user, "agreement -atlas"))


@pytest.mark.parametrize("query", ["?!...", "what is the?", "   ", "je li to"])
def test_fts_empty_or_stopword_only_queries_return_nothing(pg, query):
    factory, _ = pg
    with factory() as db:
        assert fts_search(db, db.get(User, FINANCE), query) == []


def test_fts_has_no_2000_chunk_cap_unlike_bm25_baseline(pg):
    """The BM25 baseline loads at most 2,000 chunks (ordered by document id); FTS has no cap."""
    factory, _ = pg
    with factory() as db:
        db.add(Document(id="a_bulk", tenant_id="northstar", owner_id=FINANCE, title="Bulk",
                        filename="bulk.txt", storage_key="bulk.txt", sha256="0" * 64,
                        page_count=1, extracted_fields={}))
        db.add(Document(id="zz_target", tenant_id="northstar", owner_id=FINANCE, title="Target",
                        filename="t.txt", storage_key="t.txt", sha256="0" * 64,
                        page_count=1, extracted_fields={}))
        db.flush()
        db.add_all(DocumentGrant(document_id=d, group_name="finance") for d in ("a_bulk", "zz_target"))
        db.add_all(Chunk(id=f"bulk_{i:05d}", document_id="a_bulk", page_number=1, position=i,
                         content=f"Routine filler paragraph number {i}.") for i in range(2100))
        db.add(Chunk(id="zz_target_c0", document_id="zz_target", page_number=1, position=0,
                     content="The quarterly zephyrine allocation is EUR 12,000."))
        db.commit()
        user = db.get(User, FINANCE)
        assert ids(fts_search(db, user, "zephyrine allocation")) == ["zz_target"]
        assert search(db, user, "zephyrine allocation") == []  # baseline limitation, unchanged


# ----------------------------------------------------------------------- security (all modes)

LEAK_QUERY = "SILVER-IBIS-73 restructuring: terminate supplier agreement with 90 days notice"


def test_unauthorized_chunks_never_appear_in_any_branch_or_fusion(pg):
    factory, embedder = pg
    with factory() as db:
        finance = db.get(User, FINANCE)
        # Precondition: without ACL, the forbidden chunks are the global top results in BOTH branches
        global_fts = db.execute(text(
            "SELECT document_id FROM chunks WHERE content_tsv @@ websearch_to_tsquery('simple', "
            "'SILVER-IBIS-73') ORDER BY 1")).scalars().all()
        assert set(global_fts) == FORBIDDEN_FOR_FINANCE
        q = embedder.embed_query(LEAK_QUERY)
        global_vec = db.execute(text(
            "SELECT c.document_id FROM chunk_embeddings e JOIN chunks c ON c.id = e.chunk_id "
            "ORDER BY e.embedding <=> CAST(:q AS vector) LIMIT 2"), {"q": str(q.tolist())}).scalars().all()
        assert set(global_vec) == FORBIDDEN_FOR_FINANCE, global_vec

        for k in (1, 5, 10):
            assert not FORBIDDEN_FOR_FINANCE & set(ids(fts_search(db, finance, LEAK_QUERY, k)))
            assert not FORBIDDEN_FOR_FINANCE & set(ids(semantic_search(db, finance, LEAK_QUERY, embedder, k)))
            result = hybrid_search(db, finance, LEAK_QUERY, embedder, top_k=k, candidates=k)
            assert result.hits and not FORBIDDEN_FOR_FINANCE & set(ids(result.hits))
        assert fts_search(db, finance, "SILVER-IBIS-73") == []
        # Tenant isolation and admin scope
        assert ids(fts_search(db, db.get(User, OTHERCO), "SILVER-IBIS-73")) == ["x_other"]
        assert ids(fts_search(db, db.get(User, HR), "SILVER-IBIS-73")) == ["x_hr"]
        assert ids(fts_search(db, db.get(User, ADMIN), "SILVER-IBIS-73")) == ["x_hr"]
        other = hybrid_search(db, db.get(User, OTHERCO), LEAK_QUERY, embedder, top_k=10)
        assert ids(other.hits) == ["x_other"]


def test_globally_dominant_unauthorized_results_do_not_suppress_authorized_ones(pg):
    """60 other-tenant chunks outrank everything (> 50 default candidates). A global top-N then
    filter implementation would return nothing for finance; permission-scoped branches must not."""
    factory, embedder = pg
    with factory() as db:
        db.add(Document(id="x_flood", tenant_id="otherco", owner_id=OTHERCO, title="Flood",
                        filename="f.txt", storage_key="f.txt", sha256="0" * 64,
                        page_count=1, extracted_fields={}))
        db.flush()
        db.add(DocumentGrant(document_id="x_flood", group_name="finance"))
        db.add_all(Chunk(id=f"x_flood_{i:03d}", document_id="x_flood", page_number=1, position=i,
                         content=f"Terminate the supplier agreement with 90 days written notice ({i}).")
                   for i in range(60))
        db.commit()
        embed_pending_chunks(db, embedder)
        db.commit()
        query = "terminate supplier agreement with 90 days written notice"
        n_global = db.execute(text(
            "SELECT count(*) FROM (SELECT c.document_id FROM chunks c "
            "ORDER BY ts_rank_cd(c.content_tsv, websearch_to_tsquery('simple', :q)) DESC LIMIT 50) t "
            "WHERE t.document_id = 'x_flood'"), {"q": query}).scalar()
        assert n_global == 50  # the global lexical top-50 is entirely unauthorized

        finance = db.get(User, FINANCE)
        for candidates in (1, 50):
            result = hybrid_search(db, finance, query, embedder, top_k=3, candidates=candidates)
            assert ids(result.hits)[0] == "f_atlas", ids(result.hits)
            assert all(d.startswith("f_") for d in ids(result.hits))
        assert ids(fts_search(db, finance, query, 1)) == ["f_atlas"]
        assert ids(semantic_search(db, finance, query, embedder, 1)) == ["f_atlas"]


def test_permission_changes_take_effect_immediately(pg):
    factory, embedder = pg
    with factory() as db:
        finance = db.get(User, FINANCE)
        assert "x_hr" not in ids(hybrid_search(db, finance, LEAK_QUERY, embedder, 10).hits)
        db.add(DocumentGrant(document_id="x_hr", group_name="finance"))  # grant
        db.commit()
        assert ids(fts_search(db, finance, "SILVER-IBIS-73")) == ["x_hr"]
        assert "x_hr" in ids(semantic_search(db, finance, LEAK_QUERY, embedder, 10))
        assert "x_hr" in ids(hybrid_search(db, finance, LEAK_QUERY, embedder, 10).hits)
        db.execute(text("DELETE FROM document_grants WHERE document_id = 'x_hr' "
                        "AND group_name = 'finance'"))  # revoke
        db.commit()
        assert fts_search(db, finance, "SILVER-IBIS-73") == []
        assert "x_hr" not in ids(hybrid_search(db, finance, LEAK_QUERY, embedder, 10).hits)
        finance.groups = ["finance", "hr"]  # user joins the HR group
        db.commit()
        assert ids(fts_search(db, finance, "SILVER-IBIS-73")) == ["x_hr"]
        finance.groups = ["finance"]  # ...and leaves it again
        db.commit()
        assert "x_hr" not in ids(hybrid_search(db, finance, LEAK_QUERY, embedder, 10).hits)
        hr = db.get(User, HR)
        hr.groups = []  # owner keeps access to own upload (existing ACL rule: owner OR grant)
        db.commit()
        assert ids(fts_search(db, hr, "SILVER-IBIS-73")) == ["x_hr"]


def test_only_authorized_sources_enter_the_llm_prompt(pg, monkeypatch):
    factory, embedder = pg
    captured: dict = {}

    class FakeResponses:
        def create(self, **kwargs):
            captured.update(kwargs)
            return type("R", (), {"output_text": "Notice is 90 days [S1]."})()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.responses = FakeResponses()

    import openai
    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(rag, "settings", replace(rag.settings, openai_api_key="sk-test-not-real"))
    client, token = make_client(factory)
    app.dependency_overrides[get_embedder] = lambda: embedder
    try:
        for mode in ("semantic", "hybrid"):
            # With the evidence policy (default): the forbidden identifier is not in any authorized
            # chunk -> no_evidence, nothing reaches the LLM at all.
            captured.clear()
            body = client.post("/api/ask", headers=token("finance@northstar.demo"), json={
                "question": LEAK_QUERY, "mode": mode, "top_k": 10}).json()
            assert body["mode"] == "no_evidence" and body["sources"] == [] and not captured
            assert body["diagnostics"]["evidence"]["reasons"] == ["identifier_not_found"]
        for mode in ("bm25", "lexical", "semantic", "hybrid"):
            # Policy disabled: maximum retrieved text goes into the prompt; it must all be authorized.
            captured.clear()
            body = client.post("/api/ask", headers=token("finance@northstar.demo"), json={
                "question": LEAK_QUERY, "mode": mode, "top_k": 10,
                "apply_evidence_policy": False}).json()
            assert body["mode"] == "llm", body
            question_part, sources_part = captured["input"].split("AUTHORIZED SOURCES:", 1)
            assert LEAK_QUERY in question_part  # user's own question is echoed, nothing else
            assert "SILVER-IBIS-73" not in sources_part and "restructuring plan" not in sources_part
            assert "Otherco" not in sources_part
            prompt = sources_part
            assert not FORBIDDEN_FOR_FINANCE & {s["document_id"] for s in body["sources"]}
            # every [S#] in the prompt is one of the returned, authorized sources
            assert prompt.count("[S") == len(body["sources"])
    finally:
        app.dependency_overrides.clear()


# ------------------------------------------------------------------------------- hybrid / API

def test_hybrid_fuses_independent_branch_ranks_exactly(pg):
    factory, embedder = pg
    query = "ATL-2027-NS: how reliable is the cloud provider's uptime?"
    with factory() as db:
        finance = db.get(User, FINANCE)
        lexical = fts_search(db, finance, query, 50)
        semantic = semantic_search(db, finance, query, embedder, 50)
        result = hybrid_search(db, finance, query, embedder, top_k=5, candidates=50)
    assert ids(lexical) == ["f_atlas"]        # keyword branch: only the exact identifier
    assert len(semantic) == 4                 # semantic branch: every authorized chunk, ranked
    assert ids(result.hits)[0] == "f_atlas"   # rank 1 in both branches
    # union of candidates: chunks found only by the semantic branch are fused in as well
    assert any(h.lexical_rank is None and h.semantic_rank for h in result.hits)
    expected = reciprocal_rank_fusion(
        {"lexical": [h.chunk_id for h in lexical], "semantic": [h.chunk_id for h in semantic]},
        top_k=5)
    assert [h.chunk_id for h in result.hits] == [r.chunk_id for r in expected]
    lex_rank = {h.chunk_id: i for i, h in enumerate(lexical, 1)}
    sem_rank = {h.chunk_id: i for i, h in enumerate(semantic, 1)}
    for hit in result.hits:
        assert hit.method == "hybrid" and hit.score == hit.rrf_score
        assert hit.lexical_rank == lex_rank.get(hit.chunk_id)
        assert hit.semantic_rank == sem_rank.get(hit.chunk_id)
        manual = sum(1 / (60 + r) for r in (hit.lexical_rank, hit.semantic_rank) if r)
        assert hit.rrf_score == pytest.approx(manual, abs=1e-6)
    assert result.branch_counts == {"lexical": len(lexical), "semantic": len(semantic)}
    assert len({h.chunk_id for h in result.hits}) == len(result.hits)  # deduplicated


def test_hybrid_weights_and_single_branch_behaviour(pg):
    factory, embedder = pg
    query = "terminate the agreement with written notice; receipts for travel"
    with factory() as db:
        finance = db.get(User, FINANCE)
        lexical = ids(fts_search(db, finance, query, 50))
        semantic = ids(semantic_search(db, finance, query, embedder, 50))
        lex_only = ids(hybrid_search(db, finance, query, embedder, 10,
                                     weights={"lexical": 1, "semantic": 0}).hits)
        sem_only = ids(hybrid_search(db, finance, query, embedder, 10,
                                     weights={"lexical": 0, "semantic": 1}).hits)
        assert len(lexical) >= 2
        assert lex_only[:len(lexical)] == lexical  # semantic weight 0 -> pure keyword order
        assert sem_only == semantic                # lexical weight 0 -> pure semantic order
        # Croatian paraphrase with no keyword overlap: lexical branch is empty, hybrid still works
        paraphrase = "Koliko je pouzdan pružatelj usluga u oblaku?"
        assert fts_search(db, finance, paraphrase, 50) == []
        result = hybrid_search(db, finance, paraphrase, embedder, 4)
        assert result.branch_counts["lexical"] == 0
        assert ids(result.hits) == ids(semantic_search(db, finance, paraphrase, embedder, 4))
        assert all(h.lexical_rank is None for h in result.hits)


def test_api_modes_diagnostics_and_citations(pg):
    factory, embedder = pg
    client, token = make_client(factory)
    app.dependency_overrides[get_embedder] = lambda: embedder
    try:
        headers = token("finance@northstar.demo")
        query = "ATL-2027-NS: how reliable is the cloud provider's uptime?"
        body = client.post("/api/ask", headers=headers, json={
            "question": query, "mode": "hybrid", "top_k": 3, "candidates": 20}).json()
        d = body["diagnostics"]
        assert body["retrieval"] == d["mode"] == "hybrid" and d["source_count"] == 3
        assert {"lexical", "semantic", "fusion", "total", "embedding", "semantic_db"} <= set(d["retrieval_ms"])
        assert d["parameters"] == {"candidates": 20, "rrf_k": 60,
                                   "weights": {"lexical": 1.0, "semantic": 1.0}}
        assert "not a confidence" in d["score_note"]
        with factory() as db:
            for s in body["sources"]:
                chunk = db.get(Chunk, s["chunk_id"])
                assert chunk.document_id == s["document_id"] and chunk.page_number == s["page"]
                assert s["excerpt"] == chunk.content[:800]
                assert s["method"] == "hybrid" and s["rrf_score"] == s["score"]
        lexical = client.post("/api/ask", headers=headers, json={
            "question": "ATL-2027-NS", "mode": "lexical"}).json()
        assert lexical["retrieval"] == "lexical"
        assert lexical["sources"][0]["method"] == "fts" and lexical["sources"][0]["lexical_rank"] == 1
        assert lexical["sources"][0]["fts_rank_cd"] > 0 and lexical["sources"][0]["bm25_score"] is None
        empty = client.post("/api/ask", headers=headers, json={
            "question": "what is the?", "mode": "lexical"}).json()
        assert empty["sources"] == [] and empty["mode"] == "no_evidence"
    finally:
        app.dependency_overrides.clear()


def test_hybrid_reports_embedding_failure_instead_of_degrading(pg):
    factory, _ = pg

    class BrokenEmbedder:
        model_name = "intfloat/multilingual-e5-small"

        def embed_query(self, text):
            raise EmbeddingError("model files missing")

    client, token = make_client(factory)
    app.dependency_overrides[get_embedder] = lambda: BrokenEmbedder()
    try:
        headers = token("finance@northstar.demo")
        for mode in ("semantic", "hybrid"):
            r = client.post("/api/ask", headers=headers, json={"question": "ATL-2027-NS", "mode": mode})
            assert r.status_code == 503 and "Embedding model unavailable" in r.json()["detail"]
        ok = client.post("/api/ask", headers=headers, json={"question": "ATL-2027-NS", "mode": "lexical"})
        assert ok.status_code == 200  # keyword mode does not need the model
        zero = client.post("/api/ask", headers=headers, json={
            "question": "ATL-2027-NS", "mode": "hybrid", "weights": {"lexical": 0, "semantic": 0}})
        assert zero.status_code == 422
    finally:
        app.dependency_overrides.clear()

