<div align="center">

# NEXUS AI

### Enterprise Knowledge Intelligence

**An evidence-first document intelligence platform with permission-aware hybrid retrieval, traceable sources, and transparent evaluation.**

`Applied AI` · `FastAPI` · `React + TypeScript` · `SQLAlchemy + Alembic` · `PostgreSQL FTS + pgvector` · `SentenceTransformers` · `Docker` · `pytest`

**Status:** v0.2.0 (Phases 1–2 complete: MVP + advanced hybrid RAG). Four permission-scoped retrieval modes (BM25, PostgreSQL FTS, pgvector semantic, hybrid RRF), query-aware verbatim answers, a dev-calibrated evidence-sufficiency (abstention) policy, and a reproducible evaluation over 100 labeled questions with zero measured authorization violations. This is a local demo and is not production-ready.

</div>

---

## Why this project?

Most RAG demos stop at *upload a PDF → chat*. Real enterprise document AI needs more than that: **isolation between tenants and departments, source evidence, predictable extraction, evaluation, and an audit trail**. This repository is an engineering portfolio case study built around those problems.

### What works today (implemented and verified)

| Feature | Verified by |
| --- | --- |
| **4 retrieval modes**: `bm25`, `lexical` (PostgreSQL FTS), `semantic` (pgvector), `hybrid` (FTS + semantic + RRF) | `test_hybrid_pg.py`, `test_pgvector.py`, `test_retrieval_modes.py`; live Docker requests below |
| **Authorization inside every retrieval query**: tenant / group / owner / admin rules are part of the SQL statement that ranks and LIMITs | Leak tests in which forbidden chunks are the global #1 in both branches; mutation-checked |
| **PostgreSQL full-text search**: generated `tsvector` (`simple` config), GIN index, `websearch_to_tsquery`, `ts_rank_cd`, no 2,000-chunk cap | Identifier, case, punctuation, Croatian, phrase, negation and empty-query tests |
| **Reciprocal Rank Fusion**: `Σ wᵢ / (60 + rankᵢ)`, 50 candidates per branch, configurable weights, dedup by chunk ID, deterministic ties | 11 unit tests + an end-to-end check against the independent branch ranks |
| **Local embeddings**: `intfloat/multilingual-e5-small`, `query:`/`passage:` prefixes, normalized, batched, no API key | Real-model tests; vectors persisted as `vector(384)` with model revision |
| **Idempotent indexing and backfill** | Re-run embeds 0; stale content hash triggers re-embedding |
| **Alembic migrations** (0001 v0.1 baseline, 0002 pgvector, 0003 FTS) | Fresh install, v0.1 stamp-and-upgrade and ORM parity on SQLite + PostgreSQL |
| **Query-aware offline answers**: the best sentences of each authorized hit are quoted verbatim with their own `[S#]`, instead of the first 330 characters | `test_extractive.py`, `test_answers_pg.py`; every quote checked to be a substring of the cited chunk; answer fact recall gated in CI |
| Citation inspector | `[S1]` markers, document/page from the real chunk, the quoted sentences (`selected_sentences`); keyword, semantic and RRF ranks in the UI |
| **Ingestion**: PDF (text layer, per-page), DOCX (paragraphs + table rows), TXT/MD; chunking with overlap and page numbers; regex contract fields with page evidence (`needs_review`) | `test_documents.py`: generated multi-page PDFs and DOCX files, blank pages, no-text-layer PDFs, malformed/truncated files, unsupported formats, size limit, upload → chunks → page-correct citation, on SQLite **and** PostgreSQL |
| **Complete MVP workflow**: login → upload → parse → chunk → embed → retrieve → answer → citation → source view → original download → audit | `test_mvp_e2e.py` on SQLite and PostgreSQL, including denial for another group and another tenant |
| **Answer generation**: offline verbatim extractive mode; optional OpenAI path with `[S#]` validation. A provider failure (`provider_error`) or an unverifiable LLM answer (`citation_validation_failed`) is **never shown**; the response falls back explicitly to the verbatim quotes | `test_generation.py` with a fake client that records the exact prompt (no network, no key). **A live OpenAI call has not been verified** |
| Audit trail, 5 demo personas | `test_permissions.py`, `test_mvp_e2e.py` |
| **Evaluation framework**: 30-document bilingual corpus, 100 hand-labeled questions (chunk-level, graded), dev/test split, Recall/Precision/MRR/nDCG/Hit Rate, bootstrap CIs, latency | `scripts/evaluate.py`; [docs/evaluation.md](docs/evaluation.md), [docs/benchmark-results.md](docs/benchmark-results.md) |
| **Evidence-sufficiency policy**: exact-identifier check + best *sentence* cosine ≥ 0.835 (selected on dev), so insufficient evidence returns `no_evidence` | Held-out confusion matrices; `test_evidence.py`; drift guards for `config.py` and `.env.example` against `eval/calibration.json` |
| **Citation and security evaluation**: every citation is mapped back to the stored chunk; an independent ACL oracle checks every candidate, source, citation and LLM prompt | [docs/security.md](docs/security.md); zero-tolerance CI gates |
| **CI regression gates**: malformed ground truth, leaks, citation errors, non-verbatim quotes, retrieval floors, abstention floor, answer fact recall floor (all mandatory) | `python scripts/evaluate.py --ci`, `eval/ci_thresholds.json` |
| Evaluation view in the UI | Reads the stored report via `/api/evaluation/summary`; no hardcoded numbers |
| Tests | **220 tests**, all passing with PostgreSQL (52 of them need real PostgreSQL + pgvector and are skipped without it); 95 % line coverage of `backend/app` (measured 2026-11-14) |

> **Honest findings.** On the held-out test split **no retrieval mode is significantly better than another**: nDCG@5 is ≈ 0.70–0.74 and every paired bootstrap CI contains 0. **Hybrid does not "win"**. Cross-language questions are weak for every mode because of the embedding model's same-language bias. Two fixes were measured and **not adopted**. A stronger cross-language embedder lost far more elsewhere. Local query translation (MarianMT) tripled hybrid cross-language nDCG@5 on test (0.25 → 0.76), but lowered overall nDCG@5 (0.734 → 0.721) and same-language quality, and added ~0.5 s per query ([evaluation.md](docs/evaluation.md#query-translation-experiment-phase-2d-not-adopted)). The Phase 2d evidence policy refuses every unanswerable test question (9/9), but it also refuses **more answerable ones** than Phase 2c (8/41 instead of 6/41). Query-aware quoting raised offline answer fact recall on test from 0.333 to 0.590, so about 4 in 10 answers still miss an expected fact. Not implemented: LLM answer evaluation (no API key here; the OpenAI path is tested only against a fake client), reranking, ANN indexes, OCR/Docling, workers and production SSO. Details: [docs/evaluation.md](docs/evaluation.md).

## Measured retrieval quality

<!-- BENCHMARK:START -->
Measured by `scripts/evaluate.py` on 2026-11-12 — 100 labeled questions over 30 synthetic documents (59 chunks); held-out **test split** (50 questions), answerable cases only.

| Mode | Recall@1 | Recall@5 | Recall@10 | Precision@5 | MRR@10 | nDCG@5 | Hit@5 | p50 ms | p95 ms |
|---|---|---|---|---|---|---|---|---|---|
| BM25 (Python baseline) | 0.508 | 0.760 | 0.829 | 0.205 | 0.709 | 0.701 | 0.780 | 6.4 | 9.9 |
| PostgreSQL FTS | 0.557 | 0.797 | 0.817 | 0.210 | 0.732 | 0.738 | 0.829 | 3.7 | 6.4 |
| Semantic (pgvector) | 0.508 | 0.850 | 0.927 | 0.220 | 0.720 | 0.737 | 0.878 | 40.3 | 56.3 |
| Hybrid RRF | 0.545 | 0.809 | 0.902 | 0.215 | 0.730 | 0.734 | 0.829 | 47.1 | 72.2 |

Hybrid evidence policy (identifier rule, sentence cos ≥ 0.835; chosen on dev) on test: answerable recall 0.805, unanswerable detection 1.000, answerability precision 1.000 (Phase 2c policy: 0.854 / 0.778).
Offline answer fact recall on test (hybrid top-5): 0.333 (Phase 2c, first 330 chars) → 0.590 (query-aware quotes).
Authorization violations: 0 (retrieval candidates, API sources, citations, LLM prompts, forbidden text in output).
Full results: [docs/benchmark-results.md](docs/benchmark-results.md).
<!-- BENCHMARK:END -->

## Document intelligence (Phase 3, in development)

> Branch `feat/phase3-document-intelligence`; not released. Details: [docs/document-intelligence.md](docs/document-intelligence.md) · [human review](docs/human-review.md) · [evaluation method](docs/extraction-evaluation.md) · [generated results](docs/extraction-evaluation-results.md) · [case study](docs/phase3-case-study.md).

Upload a contract or an invoice (text PDF, **scanned PDF**, DOCX, **XLSX**, TXT, Markdown). NEXUS then:
1. parses it into pages, headings, paragraphs and tables, using Tesseract OCR with deskew and table-lattice rebuild for scans;
2. classifies it as contract, invoice or unknown;
3. extracts schema fields, each with the **verbatim evidence** (page, line or table cell, bbox when available);
4. checks that the evidence supports each value;
5. validates dates, currencies and invoice arithmetic with Decimal, without auto-correcting anything;
6. queues it for **human review**: corrections next to the original values, approve / reject, optimistic locking and an audit history.

**Multi-document queries** answer questions such as *"Which contracts expire within 90 days?"*, *"Which invoices fail validation?"* and *"Which documents conflict?"* (same contract ID with different terms; same supplier with a different IBAN). They use deterministic logic over authorized, evidence-backed fields.

Measured on a frozen 32-document corpus (16 contracts, 14 invoices, 2 other; 7 scanned and 1 mixed PDF; EN + HR; 2 tenants), offline rules provider, dataset **v1.1** (label errata in [`eval/docintel/ERRATA.md`](eval/docintel/ERRATA.md)), `eval/docintel/reports/latest.json` (2026-11-14):

| Metric | Result |
|---|---|
| Field F1 (all / dev / **test**) | 0.985 / 0.995 / **0.971** (exact match 0.973, 376 field slots) |
| Invented values for absent fields | 0 of 9 |
| Evidence supports the value / on the right page | 100 % / 99.6 % (533 values) |
| Line-item cells / rows | 135/135 / 27/27 |
| OCR on 10 scanned pages (CER / key-token recall) | 0.0015 / 0.992 |
| Invalid invoices blocked from approval / validation false accepts | 4/4 / 0 |
| Multi-document questions exact (after review) | 6/6 |
| Security matrix (1,946 requests, 7 personas × 32 documents) | **0 violations** |

Every miss is a deliberately **novel label** on a test document, or an OCR `0`/`O` confusion. The misses were reported, not patched. The corpus is synthetic and authored by the same team as the rules, so these numbers are optimistic. Live LLM extraction is **not verified** (the optional provider is tested with a fake client only).

## Retrieval modes

| `mode` | Strategy | Score returned | Works on |
| --- | --- | --- | --- |
| `bm25` (default when `mode` is omitted **on SQLite**) | v0.1 in-process Python BM25 over at most 2,000 authorized chunks | BM25 score | SQLite + PostgreSQL |
| `lexical` | PostgreSQL FTS: `chunks.content_tsv @@ query`, ranked by `ts_rank_cd(..., 1)` | `ts_rank_cd` | PostgreSQL |
| `semantic` | Exact cosine distance `embedding <=> query` (no ANN index) | cosine similarity | PostgreSQL + pgvector |
| `hybrid` (default when `mode` is omitted **on PostgreSQL**) | Both branches above (each permission-scoped, `candidates` each), fused with RRF | RRF score, **not a confidence** | PostgreSQL + pgvector |

> **Changed in v0.2.0:** when a request omits `mode`, the API now uses `hybrid` on PostgreSQL (previously always `bm25`), the same default the UI picks. `/api/health` reports it as `default_retrieval_mode`. The BM25 baseline loads at most 2,000 authorized chunks and is kept for comparison, not as the PostgreSQL default.
>
> **Changed in Phase 2b:** in the unreleased Phase 2a build, `"mode": "lexical"` meant Python BM25. It now means PostgreSQL FTS, and BM25 is `"mode": "bm25"`, which is also the default when `mode` is omitted (the v0.1 behaviour). The response field `retrieval` always reports the mode that actually ran. On SQLite, `lexical`, `semantic` and `hybrid` return **HTTP 409**. If the embedding model is unavailable, `semantic` and `hybrid` return **HTTP 503** instead of silently falling back to keyword results.

```mermaid
flowchart LR
  Q[Question] --> ACL{{"authorized_documents(user)<br/>tenant AND (admin OR owner OR group grant)"}}
  ACL --> FTS["FTS branch<br/>websearch_to_tsquery('simple')<br/>ts_rank_cd · GIN · LIMIT candidates"]
  ACL --> VEC["Semantic branch<br/>embedding &lt;=&gt; query_vec<br/>exact scan · LIMIT candidates"]
  FTS --> RRF["RRF<br/>Σ w / (60 + rank)<br/>dedup · deterministic ties"]
  VEC --> RRF
  RRF --> TOP[top_k authorized chunks] --> GEN["Extractive preview or LLM<br/>citation IDs validated"]
```

### API example

```bash
TOKEN=$(curl -s localhost:8000/api/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"finance@northstar.demo","password":"DemoPass123!"}' | jq -r .access_token)
curl -s localhost:8000/api/ask -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"question":"NS-IT-2026-017","mode":"hybrid","top_k":3,"candidates":50,"weights":{"lexical":1,"semantic":1}}'
```

Abridged real response (Docker stack, Phase 2b):

```json
{
  "retrieval": "hybrid",
  "diagnostics": {
    "mode": "hybrid", "source_count": 3,
    "retrieval_ms": {"lexical": 2.48, "semantic": 14.0, "fusion": 0.05, "total": 16.55},
    "branch_candidates": {"lexical": 1, "semantic": 3},
    "parameters": {"candidates": 50, "rrf_k": 60, "weights": {"lexical": 1.0, "semantic": 1.0}},
    "score_note": "Reciprocal Rank Fusion score sum(w/(k+rank)); a rank statistic, not a confidence or probability."
  },
  "sources": [
    {"id": "S1", "title": "Cygnus Cloud Agreement", "page": 1, "method": "hybrid",
     "lexical_rank": 1, "semantic_rank": 3, "rrf_score": 0.032266, "fts_rank_cd": 0.022392, "cosine_distance": 0.1985},
    {"id": "S2", "title": "Atlas Supplier Agreement", "page": 1, "method": "hybrid",
     "lexical_rank": null, "semantic_rank": 1, "rrf_score": 0.016393}
  ]
}
```

The exact identifier appears only in the Cygnus agreement. The keyword branch finds it at rank 1 while the semantic branch ranks it only 3rd, and RRF puts it first (1/61 + 1/63 = 0.032266). For the Croatian paraphrase *"Koliko dana unaprijed moramo najaviti raskid ugovora s dobavljačem?"*, both `bm25` and `lexical` return nothing. With `"apply_evidence_policy": false`, `semantic` and `hybrid` return the Atlas notice clause first. In Phase 2c the default policy refused it (chunk similarity 0.810 < 0.82), a measured false abstention. With the Phase 2d policy, `hybrid` answers it by quoting *"…at least 90 days before the current end date."* verbatim from the Atlas agreement, with its `[S#]` citation: that sentence's similarity (0.857) clears the dev-calibrated sentence threshold of 0.835. `test_answers_pg.py` checks this on PostgreSQL with the real model. It also checks that the same question asked by users without access returns no Atlas text, and that a question with no evidence returns `no_evidence`. The cross-language weakness in general is documented in [docs/evaluation.md](docs/evaluation.md). An identifier query such as `OC-FIN-2027-001` from a Northstar user is refused (`identifier_not_found`) instead of being answered from unrelated documents.

## Demo scenarios (checked against the running stack)

`python scripts/demo_scenarios.py` logs in as the seeded personas, calls the live API (`docker compose up`) and checks each behaviour. It exits with code 1 if any scenario regresses. Last run on 2026-11-14 against the v0.2.0 Docker stack: 9 PASS, 1 KNOWN. The KNOWN item, 2b, is a documented false abstention of the dev-calibrated policy, accepted for v0.2.0.

The demo scenarios only read. `python scripts/docker_smoke.py` also checks the write path in Docker against a **disposable** API instance (setup in its docstring): PDF upload → embeddings → default-mode hybrid answer cited on the right page → document view → byte-identical download → denial for another group and tenant → audit trail. It also checks that `/api/health` stays responsive during a large upload. Phase 3 checks follow: a scanned invoice is OCRed, flagged and blocked from approval, an XLSX invoice is approved, and review and compare respect the ACL. Last run on 2026-11-14 (Phase 3 image): 27/27 passed (a 39 s upload with 498 embeddings; maximum health latency 35 ms). Walk-through: [docs/demo.md](docs/demo.md).

| # | Scenario | Persona · mode | Observed |
|---|---|---|---|
| 1 | Exact keyword `NS-IT-2026-017` | finance · FTS | Cygnus agreement rank 1, the identifier line quoted |
| 2a | Paraphrase *"What uptime does the cloud provider promise?"* (no shared keyword) | finance · semantic, policy off | Cygnus agreement rank 1 |
| 2b | Same paraphrase with the shipped policy | finance · semantic | **KNOWN false abstention**: best sentence cosine 0.804 < 0.835 |
| 3 | Hybrid fusion for `NS-IT-2026-017` | finance · hybrid | Cygnus first (lexical rank 1, semantic rank 3) |
| 4 | Croatian question over the Croatian travel policy | finance · hybrid | quotes *"…puna dnevnica od 30 EUR."* |
| 5 | Cross-language: Croatian question, English Atlas contract | finance · hybrid | quotes *"Notice period: 90 days before the end date"* (Atlas is rank 2; a Croatian document is rank 1) |
| 6 | Question the corpus cannot answer | finance · hybrid | `no_evidence`, no sources |
| 7a | Other tenant's identifier `OC-FIN-2027-001` | Northstar finance · hybrid | `identifier_not_found`, no Otherco text |
| 7b | Atlas question without a Finance/Legal grant | hr · hybrid | no Atlas source and no Atlas text |
| 8 | Stored evaluation | `/api/evaluation/summary` | test metrics, 0 authorization violations |

## Quick start (Docker, recommended)

Prerequisites: Docker Desktop with Compose. From the repository root:

```bash
docker compose up --build
```

When the API container starts it runs:

1. `python scripts/migrate.py`: Alembic `upgrade head` (pgvector extension, embeddings table, FTS column + GIN index)
2. `python scripts/seed_demo.py`: synthetic documents and users, then embeds any chunks that are missing vectors
3. `uvicorn backend.app.main:app`. At startup the API loads the embedding model in a worker thread (`EMBEDDING_WARMUP=true`), so the first query does not pay the cold start. `/api/health` reports `embedding_model_loaded`. If loading fails, the API still starts and `semantic`/`hybrid` return HTTP 503.

The first start downloads the embedding model (~470 MB) into the `model_cache` volume. The API image is large (~2.8 GB, mostly PyTorch CPU and transformers). The frontend container runs the Vite **development** server (local demo); `npm run build` produces the production bundle.

- **App:** http://localhost:5173. The retrieval selector under the chat input offers *BM25 (baseline)*, *Keyword · PostgreSQL FTS*, *Semantic · pgvector* and *Hybrid · RRF* (the default on PostgreSQL).
- **API docs:** http://localhost:8000/docs
- **Health:** http://localhost:8000/api/health (lists the available `retrieval_modes`)

**Demo password:** `DemoPass123!`

| User | Demonstrates |
| --- | --- |
| `admin@northstar.demo` | All Northstar groups; audit events |
| `finance@northstar.demo` | Finance + Legal agreements |
| `hr@northstar.demo` | HR-only compensation policy |
| `sales@northstar.demo` | Sales + Legal documents |
| `finance@otherco.demo` | Complete isolation from Northstar tenant |

### Optional: OpenAI for generated answers

Without a key, the app returns **ranked extractive evidence** that is labeled as such. Retrieval never needs a key. For generated, citation-checked answers, put these in `.env` (never commit it):

```bash
OPENAI_API_KEY=your_key_here
OPENAI_MODEL=gpt-4.1-mini
```

When a key is configured, the authorized excerpts are sent to the external API. Do not use real confidential data.

The answer is accepted only if it cites at least one `[S#]` and every marker resolves to a supplied source (`mode: "llm"`). If the provider fails, the response has `mode: "provider_error"`. If the citations cannot be verified, it has `mode: "citation_validation_failed"` and the model text is discarded. In both cases the response contains a notice followed by the verbatim extractive quotes. Marker validation does not check that each claim is supported by the cited text. Claim-level grounding and LLM answer evaluation are future work.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `RETRIEVAL_CANDIDATES` | `50` | Candidates per branch in hybrid mode (the request can override with `candidates`, 1–200) |
| `RRF_K` | `60` | RRF constant `k` |
| `RRF_WEIGHT_LEXICAL` / `RRF_WEIGHT_SEMANTIC` | `1.0` / `1.0` | Branch weights (the request can override with `weights`) |
| `EMBEDDING_MODEL`, `EMBEDDING_MODEL_REVISION`, `EMBEDDING_DEVICE`, `EMBEDDING_BATCH_SIZE`, `EMBED_ON_UPLOAD` | see `.env.example` | Local embedding model (must produce 384-d vectors) |
| `EMBEDDING_WARMUP` | `true` | Load the embedding model at API startup (PostgreSQL only) instead of on the first semantic query |
| `EVIDENCE_POLICY_ENABLED` / `EVIDENCE_MIN_SENTENCE_SIMILARITY` / `EVIDENCE_MIN_SIMILARITY` / `EVIDENCE_MIN_TERM_COVERAGE` / `EVIDENCE_REQUIRE_IDENTIFIER` / `EVIDENCE_ACCEPT_AGREEMENT` | `true` / `0.835` / `none` / `none` / `true` / `false` | Evidence-sufficiency policy for semantic and hybrid answers, calibrated on the dev split. `none` **disables** a rule (it is not a threshold of 0). Setting the Phase 2c value `EVIDENCE_MIN_SIMILARITY=0.82` would OR-in the chunk rule and make the policy more permissive than the measured one. The request can pass `apply_evidence_policy: false` to inspect raw retrieval |

## Database migrations

The schema is owned by **Alembic** (`alembic.ini`, `backend/migrations/`). The API checks on startup that the database is at the head revision and refuses to start otherwise.

```bash
python scripts/migrate.py            # upgrade DATABASE_URL to head (also upgrades v0.1 databases)
alembic upgrade head                 # equivalent for already-versioned databases
alembic downgrade 0002_pgvector_embeddings   # remove the FTS column + index
```

| Revision | Contents |
| --- | --- |
| `0001_v01_baseline` | The exact v0.1 schema (users, documents, grants, chunks, audit events) |
| `0002_pgvector_embeddings` | `CREATE EXTENSION vector`; `chunk_embeddings` (`vector(384)`, model name/revision, content SHA-256, `CHECK embedding_dim = 384`); unique grants and chunk positions |
| `0003_chunk_fulltext_search` | `chunks.content_tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED` + `GIN` index (PostgreSQL only; a no-op on SQLite) |

`content_tsv` is intentionally not mapped in the ORM, because SQLite unit tests have no tsvector. It is excluded from autogenerate comparison by `migrate.include_object`.

### Full-text search: `simple` configuration and its limits

- Lowercases text and keeps diacritics, so English and Croatian are treated the same way. Identifiers stay searchable: `SILVER-IBIS-73` becomes the phrase `'silver-ibis' <-> 'silver' <-> 'ibis' <-> '73'`.
- **No stemming, no unaccent:** `ugovor` does not match `ugovora`, and `istjece` does not match `istječe` (both are covered by tests that document the limitation).
- `websearch_to_tsquery` ANDs every term, which matched **0 of 6** natural-language eval questions on the demo corpus. NEXUS therefore removes English/Croatian stop words from bare query terms (quoted phrases, `-negations` and identifiers are kept) and joins the remaining terms with **OR**, with ranking left to `ts_rank_cd`. In that probe this matched the expected document at rank 1 for 6/6 questions (AND 0/6, OR without stop-word removal 4/6). A query that uses negation keeps AND semantics. A question made only of stop words returns no lexical evidence.
- `ts_rank_cd` has no IDF, so scores are only comparable within one query.

## Embeddings and backfill

```bash
python scripts/embed_backfill.py --dry-run   # report missing/stale vectors only
python scripts/embed_backfill.py             # embed them (idempotent; safe to re-run)
docker compose exec api python scripts/embed_backfill.py
```

Embedding failures raise `EmbeddingError`, and no placeholder vectors are ever written. On upload the document is still stored, the response reports `embedding_status: "failed: …"`, and the backfill retries later.

## Run locally without Docker

Prerequisites: Python 3.11–3.13 (verified with 3.12), Node 22+, [uv](https://docs.astral.sh/uv/) (`pip install uv`). Dependencies are locked in `uv.lock` (CPU-only PyTorch) and `frontend/package-lock.json`.

```bash
uv sync --locked --extra dev
# Windows PowerShell: .venv\Scripts\Activate.ps1   macOS/Linux: source .venv/bin/activate
```

**SQLite mode** (no services needed; only `bm25` works, the other modes return HTTP 409):

```bash
python scripts/seed_demo.py --skip-embeddings
uvicorn backend.app.main:app --reload
```

**PostgreSQL mode** (all four modes):

```bash
docker compose up -d db
# PowerShell: $env:DATABASE_URL="postgresql+psycopg://nexus:demo_local_only@127.0.0.1:5433/nexus"
export DATABASE_URL=postgresql+psycopg://nexus:demo_local_only@127.0.0.1:5433/nexus
python scripts/seed_demo.py           # migrates, seeds and embeds
uvicorn backend.app.main:app --reload
```

Frontend: `cd frontend && npm ci && npm run dev`.

## Tests and evaluation

```bash
python -m pytest -q                                   # without PostgreSQL: 168 pass, 52 skip
python -m pytest -q -m "not model and not postgres"   # fast subset: no model, no DB

docker compose up -d db
export TEST_DATABASE_URL=postgresql+psycopg://nexus:demo_local_only@127.0.0.1:5433/nexus_test
python -m pytest -q -rs --cov=backend/app             # 220 tests (the *_test DBs are DROPPED and re-created)

export EVAL_DATABASE_URL=postgresql+psycopg://nexus:demo_local_only@127.0.0.1:5433/nexus_eval
python scripts/evaluate.py            # full benchmark (~10 min CPU): reports, ablation, calibration, extraction study, docs + README
python scripts/evaluate.py --ci       # CI gate on the test split (~4.5 min; exit 1 on any breach)
python scripts/evaluate.py --render-only   # re-render docs/README/calibration from eval/reports/latest.json
EVAL_DATABASE_URL=postgresql+psycopg://nexus:demo_local_only@127.0.0.1:5433/nexus_embcmp_eval \
  python scripts/compare_embedders.py # embedder comparison, dev split only -> eval/reports/embedders-dev.json
uv sync --locked --extra dev --extra mt   # optional: local MarianMT for the translation experiment (~571 MB weights)
EVAL_DATABASE_URL=postgresql+psycopg://nexus:demo_local_only@127.0.0.1:5433/nexus_mt_eval \
  python scripts/experiment_query_translation.py   # dev only; --split test --variants <name> once, per its rule
python scripts/demo_scenarios.py      # the 8 demo scenarios against the running Docker stack
python scripts/docker_smoke.py        # 27 write-path checks (Phases 1-3) against a DISPOSABLE API instance (see its docstring)
python scripts/build_docintel_dataset.py --check   # Phase 3 frozen corpus unchanged
python scripts/evaluate_docintel.py --database-url ...nexus_docintel_eval   # Phase 3 evaluation (needs Tesseract)
python scripts/build_eval_dataset.py  # rebuild + validate labels after editing eval/questions.jsonl
python scripts/evaluate_retrieval.py  # legacy v0.1 BM25 smoke set on the demo database (8 examples)
```

The evaluation database (`EVAL_DATABASE_URL`, which must be given explicitly) is dropped and re-created on every run. `backend/evaluation/safety.py` refuses anything that is not a local PostgreSQL database named `*_eval` / `*_test`, and anything equal to `DATABASE_URL`, so the application database `nexus` cannot be dropped. The methodology is described in [docs/evaluation.md](docs/evaluation.md) and the security results in [docs/security.md](docs/security.md).

| Test file | What it verifies | Backend |
| --- | --- | --- |
| `test_hybrid_pg.py` (20) | FTS: generated column, GIN usable, identifiers/case/punctuation, Croatian, phrase, negation, empty and stop-word queries, no 2,000-chunk cap. **Security:** forbidden chunks that are the global #1 in both branches never appear in FTS, semantic or hybrid results; 60 other-tenant chunks filling the global top-50 do not suppress authorized results; grant, revoke and group changes apply immediately; only authorized text enters the LLM prompt in every mode. Hybrid: fusion equals RRF over the independent branch ranks, weights, empty branch, diagnostics, citations, 503 on embedding failure | PostgreSQL + real model |
| `test_fusion.py` (11) | RRF formula, k, weights, dedup, deterministic ties, top_k, bounds, validation | pure Python |
| `test_answers_pg.py` (3) | End-to-end `/api/ask` with the real demo documents and model: the Croatian Atlas notice question is answered with a verbatim, cited "90 days" quote; users without access never see the clause; a question without evidence abstains | PostgreSQL + real model |
| `test_pgvector.py` (5) | `vector(384)`, persisted real embeddings, cosine == numpy, vector-space leak test, upload → searchable | PostgreSQL + real model |
| `test_permissions.py` (16) | Auth, tenant/group isolation, file ACL, upload grants, audit | SQLite **and** PostgreSQL |
| `test_documents.py` (35) | PDF text layer per page, blank pages, PDF without text layer (OCR not supported) and 100-page limit; DOCX paragraphs, headings and table rows; TXT/MD with BOM and Croatian text; malformed, truncated, non-UTF-8 and unsupported files; chunk size, overlap and boundaries; upload → chunks with page numbers → embeddings (PostgreSQL) → page-correct retrieval; HTTP 400/413/415/422 with nothing stored | pure Python, SQLite **and** PostgreSQL |
| `test_generation.py` (17) | Exact LLM prompt (question + numbered authorized sources only), client settings, valid/missing/unknown citations, provider failure → explicit verbatim fallback, no provider call without evidence or key, only authorized text in the prompt through the API | fake OpenAI client; SQLite **and** PostgreSQL |
| `test_mvp_e2e.py` (5) | Login → PDF upload → listing and fields → default-mode ask → `[S#]` resolves to page 3 → document view → byte-identical download → denial for another group and tenant → audit trail; upload/ask run in the threadpool; default mode; model warm-up success and failure | SQLite **and** PostgreSQL |
| `test_migrations.py` (5) | Fresh upgrade == ORM, v0.1 stamp-and-upgrade, downgrade round trip | SQLite and PostgreSQL |
| `test_embeddings.py` (14), `test_indexing.py` (5) | Embedding provider guards, prefixes, batching; idempotent indexing | Real model / SQLite |
| `test_retrieval_modes.py` (6), `test_parsers_and_retrieval.py` (5) | BM25 default on SQLite, 409 on SQLite, request validation, evaluation endpoint, parsers | SQLite |
| `test_eval_metrics.py` (7) | Recall/Precision/Hit/MRR/nDCG against hand-computed values, undefined-metric handling, percentiles | pure Python |
| `test_eval_dataset.py` (11) | Frozen labels == fresh rebuild, labels point at authorized chunks containing the evidence, 8 kinds of malformed ground truth rejected, hand-edited labels detected | pure Python |
| `test_evidence.py` (21) | Identifier extraction and exact matching, threshold boundary, sentence and coverage rules, agreement rule, RRF never used as a signal, `none` disables a rule, app defaults **and `.env.example`** == dev calibration | pure Python |
| `test_extractive.py` (18) | Sentence splitting keeps numbers/dates/identifiers and joins hard-wrapped prose, late facts are selected (not the first 330 characters), Croatian inflection, verbatim per-source quotes, only authorized hits quotable, length bounds; destructive-DB guard | pure Python |
| `test_evaluation_pg.py` (3) | Evaluation index verified (59 chunks = 59 embeddings, 384-d), metrics bounded, deterministic rankings, end-to-end citations, LLM prompts and permission change | PostgreSQL + real model |
| `test_ci_gates.py` (13) | Each CI gate fails when breached; a missing threshold fails instead of being skipped; the Phase 2c extractor would fail the answer floor; published benchmark docs and README equal a render of the stored report | pure Python |

**Mutation check:** with the ACL subquery temporarily replaced by an unscoped `select(Document)`, all four keyword/fusion security tests failed (leak, suppression, permission change, LLM prompt), as did the vector-space leak test.

## Project structure

```text
backend/app/
  main.py        FastAPI routes; /api/ask with bm25|lexical|semantic|hybrid + diagnostics
  security.py    SQL ACL subquery shared by every retrieval strategy
  retrieval.py   BM25 baseline, PostgreSQL FTS, pgvector semantic, hybrid orchestration
  fusion.py      Reciprocal Rank Fusion (pure, typed)
  embeddings.py  multilingual-e5-small provider, validation, no fallback
  indexing.py    idempotent chunk embedding / backfill
  evidence.py    evidence-sufficiency policy (identifier + calibrated sentence-cosine threshold)
  extractive.py  query-aware verbatim sentence selection for offline answers
  migrate.py     Alembic runner, v0.1 upgrade detection, autogenerate filter
  models.py, rag.py, parsers.py, auth.py, config.py, database.py
  docintel_api.py  Phase 3 HTTP API (processing, extraction, review, comparison)
  docintel/      Phase 3: parsing, ocr, ocr_tables, tables, structure, normalize, classify, schemas,
                 rules, llm, grounding, validation, service, review, compare
backend/evaluation/  corpus + ACL oracle, dataset builder/validator, IR metrics, runner, report renderer;
                     docgen.py, docintel_corpus.py, docintel_metrics.py, docintel_eval.py, docintel_report.py (Phase 3)
eval/docintel/       Phase 3 frozen corpus (32 documents), labels.jsonl, ocr_truth.jsonl, queries.json, manifest.json,
                     ci_thresholds.json, reports/latest.json
eval/                corpus/, questions.jsonl, retrieval.jsonl (frozen), calibration.json, ci_thresholds.json, reports/
backend/migrations/versions/  0001 baseline · 0002 pgvector · 0003 FTS · 0004 document intelligence
backend/tests/                220 tests (see table above); fixtures.py generates the PDF/DOCX fixtures
frontend/                     React + TS: retrieval selector, ranks, timings, evidence status, evaluation view,
                              Document Intelligence workspace (DocIntel.tsx, diApi.ts)
scripts/                      migrate.py, seed_demo.py, embed_backfill.py, build_eval_dataset.py, evaluate.py, compare_embedders.py, experiment_query_translation.py, demo_scenarios.py, docker_smoke.py, evaluate_retrieval.py,
                              build_docintel_dataset.py, evaluate_docintel.py (Phase 3)
docs/                         architecture.md, evaluation.md, security.md, demo.md, benchmark-results.md (generated), releases/,
                              document-intelligence.md, extraction-evaluation.md, extraction-evaluation-results.md (generated),
                              human-review.md, phase3-case-study.md
```

## Design invariants

1. **No cross-tenant leakage:** no SQL statement returns chunks that belong to a different tenant.
2. **Authorization inside retrieval:** every branch applies the ACL before `ORDER BY ... LIMIT`. There is never a global top-k followed by filtering, and fusion only ever sees authorized candidates.
3. **Every cited source must exist:** citations resolve to the retrieved, authorized chunk IDs.
4. **No fake AI output:** no placeholder vectors, no silent mode fallback, extractive mode is labeled, regex fields are `needs_review`.
5. **Scores are explained, not inflated:** each mode returns its native score with a note, and RRF is a rank statistic, not a confidence.
6. **Abstain rather than guess:** semantic and hybrid answers require sufficient evidence (exact identifiers present, similarity above a dev-calibrated threshold). Otherwise the response is `no_evidence` and nothing is cited or sent to an LLM.
7. **Measured, not claimed:** every benchmark number is generated by `scripts/evaluate.py`, and a test fails if the published docs drift from the stored report.

## Roadmap

- [x] **v0.1, vertical slice:** upload → parse → chunk → ACL-filtered BM25 → evidence/answer → audit.
- [x] **v0.2a:** lockfiles, Alembic, pgvector, local embeddings, backfill, permission-scoped semantic search.
- [x] **v0.2b:** PostgreSQL FTS, hybrid RRF, four API/UI modes, diagnostics, PostgreSQL security tests.
- [x] **v0.2c, evaluation:** 100-question chunk-level dataset, four-mode benchmark with CIs, RRF ablation (dev → test), calibrated abstention, citation/answer checks, security oracle, CI gates, evaluation UI.
- [x] **v0.2d, quality:** query-aware verbatim extractive answers (answer fact recall 0.333 → 0.590 on test), sentence-level evidence policy (dev-calibrated, trade-off documented), embedder comparison (e5-small kept), query-translation experiment (not adopted), hard-wrapped sentence joining, mandatory answer-quality CI gate, scripted demo scenarios.
- [x] **v0.2.0, release:** PDF/DOCX parser and upload tests, a complete MVP end-to-end test, tested generation fallbacks, non-blocking upload, model warm-up, hybrid as the PostgreSQL default mode, and a reproducible evaluation re-run. See [CHANGELOG.md](CHANGELOG.md).
- [ ] **Deferred from v0.2d:** a gated translated-query branch (the ungated one was measured and not adopted), LLM answer evaluation with the same deterministic checks, smaller-chunk ablation.
- [x] **v0.3 (Phase 3, branch `feat/phase3-document-intelligence`, not released yet), document intelligence:** layout-aware parsing (PDF, scans, DOCX, XLSX, TXT, MD), Tesseract OCR with deskew and table lattice, contract and invoice schemas with verbatim evidence, grounding, validation, human review, multi-document comparison, 32-document evaluation with CI gates. Not done: layout-aware *chunking*, Docling, live LLM extraction evaluation.
- [ ] **v0.4, enterprise hardening:** OIDC, Postgres RLS, ANN index with filtered-recall benchmarks, reranking of authorized candidates, workers, prompt-injection regression tests.

---

**Safety note:** All sample companies, customers and document contents are fictional. This local demo has **not** been audited for regulatory compliance. Do not deploy it publicly or use it with real confidential material.
