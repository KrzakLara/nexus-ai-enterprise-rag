"""The evaluation pipeline itself, on real PostgreSQL + pgvector with real embeddings (subset)."""
import pytest

from backend.app.main import EVIDENCE_POLICY
from backend.evaluation import runner
from backend.evaluation.corpus import load_corpus
from backend.evaluation.dataset import load_frozen

from .conftest import PG_URL

pytestmark = [pytest.mark.postgres, pytest.mark.model]
SUBSET = ["q001", "q021", "q031", "q041", "q065", "q073", "q085", "q087", "q094", "q095", "q097", "q098"]


@pytest.fixture(scope="module")
def prepared(embedder):
    if not PG_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    corpus = load_corpus()
    cases = [c for c in load_frozen(corpus) if c.id in SUBSET]
    url = PG_URL.rsplit("/", 1)[0] + "/nexus_evalrun_test"
    engine, info = runner.prepare_database(url, corpus, embedder)
    yield engine, info, corpus, cases
    engine.dispose()


def test_index_is_verified_and_uses_real_persisted_embeddings(prepared):
    _, info, corpus, _ = prepared
    assert info["chunks"] == info["embeddings"] == info["tsvector_rows"] == len(corpus.chunks)
    assert info["vector_dims"] == 384 and len(info["model_revision"]) == 40


def test_benchmark_metrics_are_bounded_and_no_candidate_leaks(prepared, embedder):
    engine, _, corpus, cases = prepared
    bench = runner.benchmark(engine, corpus, cases, embedder)
    assert {r["mode"] for r in bench["runs"]} == set(runner.MODES)
    for r in bench["runs"]:
        assert r["leaked_chunk_ids"] == [] and r["forbidden_evidence_hits"] == []
        if r["metrics"] is not None:
            assert all(0.0 <= v <= 1.0 for v in r["metrics"].values())
    for mode, m in bench["report"]["modes"].items():
        assert m["all"]["n"] == sum(c.answerable for c in cases)
        assert m["leaked_chunks"] == 0
        assert m["latency"]["wall_ms"]["p95"] >= m["latency"]["wall_ms"]["p50"] > 0
    # deterministic: re-running a case gives the same ranking
    again = runner.benchmark(engine, corpus, cases[:3], embedder)["runs"]
    first = {(r["id"], r["mode"]): r["ranked"] for r in bench["runs"]}
    assert all(first[(r["id"], r["mode"])] == r["ranked"] for r in again)


def test_end_to_end_citations_abstention_and_security(prepared, embedder):
    engine, _, corpus, cases = prepared
    e2e = runner.end_to_end(engine, corpus, cases, embedder, EVIDENCE_POLICY)
    s = e2e["security"]
    assert s["responses"] == len(cases) * 4
    assert s["unauthorized_sources"] == s["unauthorized_citations"] == 0
    assert s["forbidden_evidence_in_output"] == 0
    assert s["llm_context"]["prompts_checked"] > 0
    assert s["llm_context"]["prompts_with_unauthorized_text"] == 0
    assert s["permission_change"]["passed"] is True
    for mode, m in e2e["per_mode"].items():
        assert m["citations"]["valid"] == m["citations"]["total"], mode
        assert m["citations"]["source_mapping_errors"] == 0, mode
    # Identifier-bearing unanswerable / unauthorized questions are refused in hybrid mode
    hyb = e2e["per_mode"]["hybrid"]["abstention"]
    assert hyb["unanswerable_abstained"] >= 4  # q087, q095, q097, q098 carry identifiers
