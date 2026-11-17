# Case study: NEXUS AI — Enterprise RAG & Document Intelligence

A technical case study of a portfolio project, written for system-design and AI-engineering
interviews. All data is synthetic; no real customer or production deployment is involved.
Detailed phase notes: [phase3-case-study.md](phase3-case-study.md),
[phase4-case-study.md](phase4-case-study.md). Numbers: [EVALUATION_SUMMARY.md](EVALUATION_SUMMARY.md).

## 1. Problem statement

Organisations keep contracts, invoices and policies as text PDFs, scans, Word and Excel files, in
more than one language. They want to ask questions over them and to turn invoices and contracts
into structured data. Three things make this hard in an enterprise setting:

1. **Trust.** A plausible but unsupported answer, or a citation that does not say what the answer
   claims, is worse than no answer.
2. **Confidentiality.** Users may only see documents of their own tenant and groups. In a
   retrieval system the leak path is subtle: a forbidden chunk that is the nearest neighbour can
   reach the ranking, a log line or an LLM prompt even if it is never displayed.
3. **Operations.** Processing must survive crashes, be observable, and leave an audit trail of who
   accepted which extracted value.

## 2. System requirements

| Type | Requirement |
|---|---|
| Functional | Upload PDF (text and scanned), DOCX, XLSX, TXT, MD; ask questions in English and Croatian; answers with citations to page-level evidence; extract contract and invoice fields with evidence; validate them; human review and approval; compare documents |
| Security | Tenant and group isolation in every read path, including retrieval, citations, prompts, downloads and comparisons; SSO; no tokens in the browser; audit |
| Quality | Abstain when evidence is insufficient; measured retrieval and extraction quality on held-out data; regressions caught in CI |
| Operability | Durable background processing with retries; object storage; metrics, logs and traces; backup / restore; documented behaviour under load and failure |
| Constraints | Runs locally on one laptop in Docker; no paid API required; reproducible evaluation |

## 3. Architecture overview

A **modular monolith**: one FastAPI codebase and one Docker image run as `api` (FastAPI as the
backend-for-frontend and query service), `worker` (durable job processing) and `migrate` (one-shot
schema and seed). PostgreSQL 16 holds relational data, the job queue, full-text indexes and
pgvector embeddings, with row-level security forced on 15 tables. MinIO provides S3-compatible
private storage for originals. Keycloak is the local OIDC provider. Prometheus, Grafana and Jaeger
(OpenTelemetry) provide observability. The React + TypeScript SPA talks to the API on the same
origin. Diagram and data flows: [architecture.md](architecture.md#system-overview-v040).

Why a monolith: one ACL implementation (`security.authorized_documents`) and one audit trail
shared by RAG and document intelligence, transactional consistency between documents, jobs and
extractions, and a deployable that one person can reason about. Workers scale independently
(`--scale worker=N`).

## 4. Technical challenges

| Challenge | Why it is hard |
|---|---|
| Permission-aware vector search | ANN indexes filter after the index walk; for a small tenant the top-k can be entirely foreign and filtered to nothing, or a post-filter design leaks foreign chunks into ranking |
| Exact identifiers vs paraphrases | Embeddings blur `NS-IT-2026-017`; keyword search misses paraphrases and cross-language questions |
| Knowing when not to answer | Cosine scores of e5 fall in a narrow band (~0.75–0.85); a guessed threshold is meaningless |
| Scanned tables | OCR on a raw scan destroyed the invoice table (CER 0.27, key-token recall 0.57 before pre-processing) |
| Evidence that really supports a value | A number on the page is not proof (digits inside dates or IDs); evidence must be on the right page, line or cell |
| Two layers of authorization | RLS policies must express the same rule as the application, including group membership changes taking effect immediately, without breaking worker writes |
| Crash-safe processing | A worker may die mid-document, a lease may expire while a slow worker is still running, two users may submit the same re-index |
| Honest evaluation | Synthetic data written by the same author as the rules invites overfitting |

## 5. Key engineering decisions

1. **Authorization inside the retrieval SQL.** Each branch (FTS, vector, BM25) is one statement
   with the ACL subquery before `ORDER BY … LIMIT`. No global top-k, no post-filtering, no shared
   unscoped index. Tests place forbidden chunks at the global #1 of both branches and assert they
   never appear; a mutation (ACL removed) makes them fail.
2. **Exact vector search instead of ANN.** At demo scale a sequential scan over authorized rows is
   fast (~20 ms warm) and exact. Adding HNSW is deferred until a per-tenant filtered-recall
   benchmark exists.
3. **Hybrid retrieval with Reciprocal Rank Fusion.** Rank-based fusion avoids calibrating BM25
   against cosine. The evaluation then showed **no significant difference** between modes on the
   test split; hybrid is the default for robustness across question types, and that finding is
   published rather than hidden.
4. **Abstention calibrated on dev, reported on test.** Exact identifiers must occur in the
   evidence; the best *sentence* cosine must reach 0.835. On test it refuses 9/9 unanswerable
   questions and 8/41 answerable ones.
5. **Verbatim extractive answers by default.** Without a key, answers are query-selected
   sentences quoted from authorized chunks; with a key, an LLM answer must cite only supplied
   sources or it is discarded. Answer fact recall rose from 0.333 to 0.590 when sentence selection
   became query-aware.
6. **Rules-first document intelligence.** A deterministic, offline extractor with grounding and
   Decimal validation; the LLM extractor is optional, schema-constrained and never a silent
   fallback. Nothing is auto-corrected: humans approve, correct or override with a reason.
7. **BFF for OIDC.** Authorization Code + PKCE is handled server-side; the SPA only has an
   HttpOnly cookie. Revocation (logout, back-channel logout, removed groups) is immediate.
8. **RLS as a second, independent enforcement.** Runtime roles without `BYPASSRLS`; the security
   context is transaction-local; workers are scoped to the leased job's document.
9. **Queue in PostgreSQL.** The job row is written in the same transaction as the upload, so no
   outbox relay is needed. Leases with fencing tokens stop a stalled worker from writing after a
   takeover.

## 6. Implementation

| Component | Implementation |
|---|---|
| Retrieval | `retrieval.py` (FTS with `websearch_to_tsquery` + stop-word removal and OR semantics, pgvector `<=>`, BM25 baseline), `fusion.py` (RRF), `evidence.py`, `extractive.py`, `rag.py` |
| Embeddings | `multilingual-e5-small` (384-d, CPU), `query:` / `passage:` prefixes, persisted with model revision and content hash; idempotent backfill |
| Document intelligence | `docintel/`: one normalized structure for every format, Tesseract with deskew, ruling-line removal and lattice tables, classification, rules extractor, grounding, validation, review, comparison |
| Identity | `identity/`: OIDC discovery, JWKS cache with single-flight refresh, claim mapping to tenant / groups / roles, encrypted server-side sessions, CSRF |
| Data | Alembic migrations 0001–0008; RLS policies and `SECURITY DEFINER` helpers in schema `nexus_rls` |
| Jobs | `jobs/queue.py` (claim via `FOR UPDATE SKIP LOCKED`, leases, backoff, dead letter, idempotency keys, advisory locks), `jobs/handlers.py` (ingest, reprocess, reindex, purge with checkpoints) |
| Storage | `storage.py`: S3 / local stores, server-generated keys, SHA-256 checks, presigned URLs; `lifecycle.py`: versions, staged deletion, purge, retention |
| Observability | `observability/`: JSON logs with redaction, Prometheus metrics, OpenTelemetry spans propagated through the job row |
| Frontend | React 19 + TypeScript: assistant with source inspector, library with live processing status, document-intelligence workspace, audit and evaluation views |

## 7. Security and access control

- **Same rule, two enforcements:** `tenant ∧ not deleted ∧ (admin ∨ owner ∨ group grant)` in the
  application SQL and in FORCE RLS policies for `nexus_app` / `nexus_worker`.
- **Mutation-tested:** removing the application filter leaks on SQLite (no RLS) but not on
  PostgreSQL, which proves both that the test detects leaks and that RLS blocks them.
- **AI-specific:** only authorized text reaches prompts (prompt capture tests in all four modes);
  retrieved text is framed as untrusted data; injected instructions cannot add extraction values;
  citations to unknown sources discard LLM output; identical 404s for missing and forbidden
  documents prevent ID probing.
- **Session and token security:** asymmetric algorithm allowlist, `alg=none` / HS256-with-public-key
  attacks refused, issuer / audience / `azp` / nonce / time claims checked, key rotation tested
  against real Keycloak.
- **Storage:** private bucket, server-generated keys, content sniffing, archive and zip-bomb limits.
- Details: [security.md](security.md), [security-test-results.md](security-test-results.md),
  [postgresql-rls.md](postgresql-rls.md), [authentication-oidc.md](authentication-oidc.md).

## 8. Testing and evaluation

| Layer | How |
|---|---|
| Unit and integration | 567 pytest tests on SQLite and PostgreSQL (RLS runtime roles), real MinIO, real Keycloak, Tesseract and the real embedding model in CI; coverage floor 90 % |
| Retrieval evaluation | 100 hand-labelled questions, chunk-level graded relevance, dev / test split, Recall / MRR / nDCG / Hit with bootstrap CIs, ablation, embedder comparison, query-translation experiment |
| Extraction evaluation | 32-document frozen corpus with labels and OCR ground truth; field F1, evidence correctness, OCR CER, validation and review outcomes, a 1,946-request security matrix |
| CI gates | zero-tolerance authorization checks; retrieval, abstention, answer-recall and extraction floors; a test fails if published numbers drift from stored reports |
| System | Docker E2E (22 checks), failure injection (12), backup / restore (8), k6 load (4 profiles) with authorization checked under load |

## 9. Results

| Result | Value |
|---|---|
| Authorization violations | **0** across retrieval / API / prompt checks, the document-intelligence security matrix and k6 load |
| Extraction field F1 (held-out test) | **0.971**; 0 of 9 absent fields invented; evidence supports 100 % of 533 values |
| OCR on scans | CER 0.0015, key-token recall 0.992 |
| Retrieval (test, nDCG@5) | 0.70–0.74 for all four modes; differences not significant |
| Abstention | 9/9 unanswerable refused; 8/41 answerable falsely refused |
| Verification | Docker E2E 22/22 (re-run from `main` on 2026-11-17), failure injection 12/12, backup / restore 8/8 |

**What verification found** (each fixed with a regression test): RLS `SELECT` policies are checked
against the new row of an `UPDATE`, which broke job completion; a pool exhaustion answered HTTP 500
(now 503 + `Retry-After`); a frozen database hung readiness and requests (bounded probes and a
request deadline); duplicate-job detection was not atomic under concurrency (advisory locks); a
load-test harness locked Keycloak accounts and sent `Bearer undefined`. Full list:
[phase4-case-study.md](phase4-case-study.md#problems-found-during-verification).

## 10. Limitations and future improvements

| Limitation | Next step |
|---|---|
| Synthetic, self-authored data; optimistic scores | Evaluate on an external or independently labelled corpus |
| No live LLM measurement | Run the existing deterministic answer and extraction checks against a live model; claim-level groundedness |
| Exact vector scan; no reranker | HNSW with pgvector iterative scans plus a per-tenant filtered-recall benchmark; rerank authorized candidates only |
| `simple` FTS: no stemming, weak cross-language retrieval | Language-aware text search configuration; gated query translation (measured once, not adopted) |
| Load measured on one laptop; CPU embeddings saturate one API process | Separate embedding service or GPU, horizontal API scaling, capacity tests on target hardware |
| No external validation | Penetration test, HA / multi-region, WAF and edge rate limiting, KMS for the session key, SSO for observability tools |
