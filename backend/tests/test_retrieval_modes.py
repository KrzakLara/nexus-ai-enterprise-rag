"""Retrieval-mode behaviour that does not need PostgreSQL."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.database import Base
from backend.app.models import User
from backend.app.retrieval import SemanticSearchUnavailable, semantic_search

from .conftest import make_client, seed


@pytest.fixture
def sqlite_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    seed(factory)
    yield factory
    engine.dispose()


def test_semantic_search_refuses_sqlite_explicitly(sqlite_factory):
    with sqlite_factory() as db:
        with pytest.raises(SemanticSearchUnavailable):
            semantic_search(db, db.get(User, "u1"), "anything", embedder=None)


@pytest.mark.parametrize("mode", ["lexical", "semantic", "hybrid"])
def test_postgres_only_modes_are_refused_on_sqlite_with_409(sqlite_factory, mode):
    from backend.app.main import app

    client, token = make_client(sqlite_factory)
    try:
        r = client.post("/api/ask", headers=token("finance@northstar.demo"),
                        json={"question": "Atlas notice period", "mode": mode})
        assert r.status_code == 409 and mode in r.json()["detail"]
    finally:
        app.dependency_overrides.clear()


def test_bm25_is_the_default_when_mode_is_omitted(sqlite_factory):
    from backend.app.main import app

    client, token = make_client(sqlite_factory)
    try:
        headers = token("finance@northstar.demo")
        body = client.post("/api/ask", headers=headers,
                           json={"question": "Atlas notice period"}).json()
        assert body["retrieval"] == "bm25" and body["diagnostics"]["mode"] == "bm25"
        src = body["sources"][0]
        assert src["method"] == "bm25" and src["bm25_score"] == src["score"] > 0
        assert src["lexical_rank"] is None and src["rrf_score"] is None
        assert body["diagnostics"]["source_count"] == len(body["sources"])
        explicit = client.post("/api/ask", headers=headers,
                               json={"question": "Atlas notice period", "mode": "bm25"}).json()
        assert [s["chunk_id"] for s in explicit["sources"]] == [s["chunk_id"] for s in body["sources"]]
        for bad in ({"mode": "vector"}, {"mode": "hybrid", "candidates": 0},
                    {"mode": "hybrid", "weights": {"lexical": -1}}):
            r = client.post("/api/ask", headers=headers, json={"question": "Atlas", **bad})
            assert r.status_code == 422, bad
    finally:
        app.dependency_overrides.clear()


def test_evaluation_summary_endpoint_serves_stored_report_only(sqlite_factory, monkeypatch, tmp_path):
    from backend.app import main
    from backend.app.main import app
    from backend.evaluation.corpus import load_corpus

    client, token = make_client(sqlite_factory)
    try:
        assert client.get("/api/evaluation/summary").status_code == 401
        headers = token("finance@northstar.demo")
        body = client.get("/api/evaluation/summary", headers=headers)
        assert body.status_code == 200, body.text
        data = body.json()
        assert set(data["modes"]) == {"bm25", "lexical", "semantic", "hybrid"}
        assert data["dataset"]["total"] >= 80 and data["security"]["total_violations"] == 0
        # The summary carries metrics and ids only, never corpus text
        payload = body.text
        assert not any(c.content[:60] in payload for c in load_corpus().chunks)
        monkeypatch.setattr(main, "EVAL_REPORT", tmp_path / "missing.json")
        assert client.get("/api/evaluation/summary", headers=headers).status_code == 404
    finally:
        app.dependency_overrides.clear()
