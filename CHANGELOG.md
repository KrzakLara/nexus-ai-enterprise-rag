# Changelog

All notable changes to NEXUS AI. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/). NEXUS AI is a **local demo**. No version is production-ready.

## [0.4.0] - prepared for tagging - Phases 1–4 on `main` + Phase 5 portfolio release

Release notes: [docs/releases/v0.4.0.md](docs/releases/v0.4.0.md). Phases 3 and 4 reached `main`
through PR #2, the Phase 5 documentation through PR #3. The `v0.4.0` tag and GitHub Release are
created from `main` after the owner's approval.

### Phase 5: recorded demo and portfolio polish
- **Recorded English product demo** `docs/videos/nexus-ai-demo.mp4` (2:25, 1920 × 1080, H.264,
  no audio, on-screen captions) and thumbnail `docs/images/nexus-ai-demo-thumbnail.png`: an
  automated browser recording of the running stack (Keycloak SSO, background ingestion, invoice
  extraction and OCR, human review with a validation failure, hybrid RAG with citations, tenant
  isolation, evaluation, Grafana). Reproducible with `scripts/demo_video/`; uses two synthetic
  English invoices in `demo_documents/video/` that are not part of the frozen evaluation data.
- README: prominent *Watch the Demo* section, verified headline metrics below the subtitle,
  *Engineering Ownership* section; the authorization and extraction rows now state their exact
  scope (what was counted, test split vs. all documents). No evaluation result changed.

### Fixed
- Frontend: the breadcrumb and workspace icon were hardcoded to the Northstar tenant; they now
  follow the signed-in tenant, and an unknown tenant id is shown as itself
  (`frontend/src/tenant.ts`, regression test `frontend/src/tenant.test.ts`, run by `npm test` in CI).

### Phase 5: portfolio release (documentation and metadata only)
- New README as a project landing page: problem, solution, verified features, architecture
  diagram, screenshots, evaluation highlights with provenance, Docker quick start, security,
  engineering decisions, limitations. The generated benchmark block is unchanged.
- `docs/architecture.md`: verified v0.4.0 system overview (components, ingestion and question
  flows, trade-off table); early ADR text marked where Phase 4 superseded it.
- `docs/DEMO.md` (renamed from `docs/demo.md` and rewritten for the OIDC / queue / MinIO stack):
  prerequisites, accounts, 16 steps with observed results, troubleshooting, a 2–3 minute video
  script (the video was recorded afterwards; see above).
- `docs/EVALUATION_SUMMARY.md`, `docs/CASE_STUDY.md`, `docs/PORTFOLIO.md`,
  `docs/TECHNICAL_REFERENCE.md` (the former detailed README body), `docs/releases/v0.4.0.md`.
- Five screenshots captured from the running stack (`docs/images/ui-assistant-*.png`,
  `ui-evaluation.png`, `ui-review-*.png`).
- Re-verification from `main`: Docker quick start from scratch, Docker E2E 22/22
  (`eval/phase5/e2e-phase5-main.json`).
- Package metadata 0.3.0 → 0.4.0 in `pyproject.toml`, `uv.lock`, `frontend/package.json` and
  `frontend/package-lock.json`, matching the API and the UI.

### Phase 4: enterprise hardening

Overview: [docs/enterprise-hardening.md](docs/enterprise-hardening.md). Measured results:
[docs/security-test-results.md](docs/security-test-results.md), [docs/performance-benchmarks.md](docs/performance-benchmarks.md).

### Added
- **OpenID Connect** (`AUTH_MODE=oidc`): Authorization Code Flow + PKCE (S256) through the backend
  (BFF), discovery, JWKS cache with rate-limited refresh on unknown `kid` (key rotation), strict
  validation (asymmetric algorithm allowlist, `iss`, `aud`, `azp`, `exp`/`nbf`/`iat`, `nonce`,
  token `typ`), server-side sessions in HttpOnly / SameSite=Lax / `__Host-` cookies, CSRF tokens and
  Origin checks, idle / absolute expiry, refresh-token revalidation (group removal, revoked IdP
  sessions), RP-initiated and back-channel logout, bearer access tokens for API clients, identity
  binding (issuer + subject), tenant / group / role mapping from IdP group paths under a server-side
  tenant allowlist, verified-e-mail linking, JIT provisioning. Local **Keycloak 26.4.2** realm.
- **Fail-closed configuration** (`APP_ENV`, `validate_settings`): demo authentication, insecure
  cookies, local storage, inline processing, privileged DB roles, http IdP URLs and published
  development secrets are refused in staging / production.
- **PostgreSQL row-level security** (migration 0008): FORCE RLS on 15 tables, runtime roles
  `nexus_app` / `nexus_worker` (NOBYPASSRLS), transaction-local security context per transaction,
  worker job scope bound to a lease token, SECURITY DEFINER helpers in schema `nexus_rls`, column
  privilege protection of password hashes, append-only audit.
- **Durable background processing**: PostgreSQL-backed queue (`processing_jobs`, SKIP LOCKED
  claims, leases, fencing tokens, exponential backoff with jitter, dead letter, crash recovery,
  idempotency keys), `python -m backend.app.worker`, job types ingest / reprocess / reindex / purge,
  job API (`/api/jobs`, retry, cancel), `PROCESSING_MODE=inline|queue`.
- **Object storage** (S3 / MinIO, local): server-generated keys, server-side SHA-256 checks,
  private bucket, optional short-lived presigned downloads, upload validation (content sniffing,
  zip-bomb and archive limits, UTF-8 text), `document_versions`, **document lifecycle** (versions,
  staged deletion with purge and tombstones, retention expiry), `scripts/storage_backfill.py`
  (dry-run default, checksum-verified, resumable, never deletes local files),
  `scripts/storage_reconcile.py`.
- **Observability**: JSON logs with redaction, Prometheus metrics on an internal port,
  OpenTelemetry tracing with trace-context propagation across the queue, liveness / readiness,
  `compose.observability.yaml` (Prometheus 3.5.0, Grafana 12.1.1 with a provisioned dashboard,
  Jaeger 2.10.0), alert rules.
- **Verification**: security regression, RLS, OIDC (fake IdP and real Keycloak), MinIO, jobs and
  lifecycle, observability test suites; `scripts/e2e_phase4.py`, `scripts/resilience_drill.py`,
  `scripts/backup_restore_drill.py`, `scripts/benchmark_phase4.py`, k6 profiles
  (`load/k6/nexus.js`, `scripts/run_load_test.py`), CI jobs for config validation and Docker E2E,
  manual load-test workflow.
- Frontend: SSO login, cookie sessions with CSRF (no token in `localStorage`), session restoration
  and expiry handling, live processing status (real stages, no estimated percentages), retry,
  lifecycle panel (versions, re-index, delete, processing history).

### Changed
- Docker Compose: `migrate` one-shot service (migrations, runtime roles, demo seed), `api` as
  `nexus_app`, `worker` as `nexus_worker`, MinIO + `minio-init` (bucket-scoped access key),
  same-origin `/api` proxy in the frontend container, non-root API image, overlays for OIDC,
  observability and drills. The legacy `document_storage` volume is mounted read-only.
- Authentication error bodies are `{"code", "message"}` objects. `/api/documents` and
  `/api/documents/{id}` gain lifecycle fields.
- Test suite: PostgreSQL API tests run as the runtime role with RLS enforced; a PostgreSQL advisory
  lock prevents two concurrent test sessions on the same database.

### Fixed during verification (load tests, failure-injection drill, CI)
- Pool exhaustion under load answered HTTP 500; now 503 + `Retry-After` (metric and alert).
- A frozen dependency (PostgreSQL paused) hung readiness and requests; readiness probes are bounded
  and use fresh connections, and `REQUEST_TIMEOUT_SECONDS` answers 503 when no response has started.
- Duplicate-job detection was not atomic under concurrent submissions; advisory locks per document.
- JWKS refresh: single flight, stale-while-revalidate, IdP failures answer 503 (not 401).
- CI: actionlint (shellcheck SC2034) in a workflow script. GitHub Actions run 43756922292: all jobs passed.

## [0.3.0] - release candidate (not yet tagged) - Phase 3 document intelligence

Release notes: [docs/releases/v0.3.0.md](docs/releases/v0.3.0.md).

### Added (release preparation)
- `LICENSE` (MIT, Copyright (c) 2026 Lara Krzak) for the project's own code, and `THIRD_PARTY_NOTICES.md`. The notices cover the third-party licenses, including the verbatim DejaVu fonts license for the font subsets embedded in the evaluation PDFs. MIT does not relicense third-party components.
- `docs/ground-truth-review-checklist.md` for a human spot check of 10 representative documents.
- Versions are 0.3.0 in `pyproject.toml`, `uv.lock`, the FastAPI app, `frontend/package.json` and the UI.

### Added
- **Layout-aware parsing** (`backend/app/docintel/`) into one normalized structure: pages, headings, paragraphs, lines, tables (merged cells), and bounding boxes when the parser has them.
  - Formats: text PDF, scanned PDF, mixed PDF, DOCX, **XLSX**, TXT, Markdown.
  - Format detection by extension **and** magic bytes.
  - XLSX formulas without cached values are reported as warnings, never computed.
- **OCR:**
  - Tesseract 5 (`eng+hrv`) on 300-dpi renders, decided per page;
  - projection-profile deskew, ruling-line removal and table-lattice reconstruction with merged cells;
  - checksum-gated correction of OCR digit confusions in IBANs.
  - Installed in the Docker image and in CI.
- **Classification** into contract / invoice / unknown with explicit uncertainty (`ambiguous`, `insufficient_evidence`).
- **Pydantic v2 schemas** for contracts, invoices and line items. Every field carries its value, raw text, verbatim evidence (document, page, line or row, block, bbox, table cell), method, grounding and issues.
- **Offline deterministic rules provider** (label, table and sentence rules, EN + HR, European number and date formats).
- **Optional LLM provider:**
  - strict JSON schema, verbatim evidence quotes and re-grounding;
  - a labelled-line rule against injected instructions;
  - explicit HTTP 502 on failure, with no fallback.
  - Live behaviour is **NOT VERIFIED**.
- **Grounding:**
  - evidence must come from the right document, page, text and cell;
  - numbers are compared numerically, and digits inside dates or IDs never count.
- **Validation** (Decimal, ROUND_HALF_UP):
  - line amounts, subtotal, tax, total, currency consistency, invoice and due dates, IBAN checksum;
  - contract dates, amounts and plausibility.
  - Issues are field-level, with expected and actual values; nothing is auto-corrected.
- **Human review:**
  - pending / needs_review / approved / rejected;
  - corrections stored next to the original values;
  - approval blocked by validation errors unless an override reason is recorded;
  - optimistic locking (`expected_version`, HTTP 409);
  - append-only review history and audit events;
  - a `reviewer` role.
- **Multi-document intelligence:**
  - comparison of 2–10 documents with citations;
  - structured queries (contracts expiring within N days, automatic renewal, invalid invoices, conflicting contract or supplier information);
  - an EN/HR question router that refuses unsupported questions.
- Alembic migration `0004_document_intelligence`: five new tables; no existing table changed.
- **API:** `/api/intelligence/*`, `/api/documents/{id}/process|processing|structure|extraction`, `/api/extractions/{id}/*`.
  - `/api/documents` gains an `intelligence` summary; `/api/health` reports `ocr_available`.
- **Frontend:** Document Intelligence workspace.
  - library with type and statuses;
  - document structure view with evidence highlighting;
  - editable extraction panel with validation issues, approve/reject/override and review history;
  - comparison and insights views;
  - reviewer and procurement demo personas.
- **Evaluation:**
  - frozen 32-document corpus (`eval/docintel/`, SHA-256 manifest, authored ground truth, dev/test split);
  - `scripts/evaluate_docintel.py` measures field metrics, evidence, tables, validation, OCR CER/WER, simulated review, multi-document queries and a persona × document × endpoint security matrix;
  - CI gates (`eval/docintel/ci_thresholds.json`).
- Documentation: `docs/document-intelligence.md`, `docs/extraction-evaluation.md`, `docs/extraction-evaluation-results.md` (generated), `docs/human-review.md`, `docs/phase3-case-study.md`.

### Changed
- Upload uses the layout-aware parser. Page texts still feed the unchanged chunker, and they are byte-identical to v0.2 for the existing PDF fixtures.
- Upload accepts XLSX and scanned PDFs. A broken `.xlsx` now returns 422 (malformed) instead of 415 (unsupported); the existing test case was split accordingly.
- `scripts/seed_demo.py`:
  - adds the reviewer and procurement personas and 11 corpus documents, granted only to `procurement` so the Phase 2 demo is unaffected;
  - stores structure and extraction for every demo document.
- CORS allows `PATCH` (review corrections).

### Fixed (dataset v1.1, `eval/docintel/ERRATA.md`)
- **Label erratum A:** 8 numbers in exponent notation (`4E+1`) are now canonical decimals (`40`). The review API now reads scientific notation as a number; before, it would have parsed `4E+1` as `4`.
- **Label erratum B:** one canonical `governing_law` (*Croatia*) instead of *Republic of Croatia* and *Croatia*. The rule lives in `rules.py`, and comparisons no longer show equivalent jurisdictions as different.
- **Label erratum C:** new field `value_period` (annual / total / monthly). `contract_value` had mixed annual, total and monthly amounts; di-c13 states a **monthly** rent. Comparisons no longer rank amounts with different periods.
- **Impact:** source documents are unchanged (verified by SHA-256). The v1.0 labels and report are archived. Field F1 changed 0.9856 → 0.9848 and test F1 0.973 → 0.971; the CI floors are unchanged.

### Verified (2026-11-14)
- **Tests, Linux container** (API image + dev extras, Tesseract 5.5.0, PostgreSQL 16 + pgvector): `pytest` **359 passed, 0 skipped** (after the v1.1 errata), 94 % line coverage of `backend/app`.
- **Tests, Windows** (no Tesseract): 348 passed, 2 skipped (the two OCR tests).
- **Phase 2 gates:** `scripts/evaluate.py --ci` passes with test-split metrics identical to the stored report and 0 authorization violations, so retrieval did not regress.
- **Phase 3 gates:** `scripts/evaluate_docintel.py --ci` passes. The full run gives field F1 0.985 (test 0.971, dataset v1.1), evidence support 100 %, OCR CER 0.0015 and **0 violations in 1,946 security checks** (see `docs/extraction-evaluation-results.md`).
- **Docker:**
  - `scripts/demo_scenarios.py` on the Phase 3 stack: 9 PASS, 1 KNOWN (unchanged from v0.2.0);
  - `scripts/docker_smoke.py` on a disposable instance: **27/27**.
- **Frontend:**
  - `tsc` and `npm run build` pass;
  - a headless-browser check of the Document Intelligence workspace (reviewer login, library, scanned invoice detail with evidence highlight, conflicts insight) shows 0 console errors.
- **Not verified:** live LLM extraction (no API key); GitHub Actions on this branch (not pushed).

## [0.2.0] - 2026-11-14

First tagged release. It completes Phase 1 (MVP) and Phase 2 (advanced enterprise hybrid RAG). Every benchmark number below is generated by `scripts/evaluate.py`. Release notes: [docs/releases/v0.2.0.md](docs/releases/v0.2.0.md).

### Added
- **Retrieval (Phase 2a/2b):**
  - PostgreSQL + pgvector with local `intfloat/multilingual-e5-small` embeddings (`vector(384)`), persisted with model name, revision and content hash;
  - idempotent embedding backfill;
  - PostgreSQL full-text search (`simple` configuration, generated `tsvector`, GIN index);
  - hybrid retrieval with weighted Reciprocal Rank Fusion;
  - four retrieval modes (`bm25`, `lexical`, `semantic`, `hybrid`) in the API and UI, with per-stage diagnostics.
  - Authorization is part of every retrieval SQL statement, before ranking and `LIMIT`.
- **Evaluation (Phase 2c):**
  - a 100-question labeled dataset over a 30-document bilingual synthetic corpus, with a dev/test split;
  - Recall, Precision, MRR, nDCG and Hit@k with bootstrap CIs;
  - an RRF ablation selected on dev;
  - an evidence-sufficiency (abstention) policy calibrated on dev;
  - citation and security evaluation with an independent ACL oracle;
  - mandatory CI regression gates.
- **Answer quality (Phase 2d):**
  - query-aware verbatim sentence extraction (offline answer fact recall on test: 0.333 → 0.590);
  - a sentence-level evidence policy;
  - an embedder comparison and a query-translation experiment, both measured and **not adopted**;
  - scripted demo scenarios (`scripts/demo_scenarios.py`).
- **Release verification (v0.2.0):**
  - `test_documents.py`: generated multi-page PDFs and DOCX files, blank pages, PDFs without a text layer, malformed and truncated files, unsupported formats, size limits, chunk overlap and boundaries, and upload → page-correct retrieval;
  - `test_generation.py`: exact LLM prompt, citation validation and provider failures, using a fake client with no network calls;
  - `test_mvp_e2e.py`: login → upload → parse → chunk → retrieve → answer → citation → source view → download → audit, with denial for other groups and tenants.
  - `scripts/docker_smoke.py`: write-path end-to-end checks against a disposable Docker API instance.
  - `docs/demo.md` (walk-through and missing demo assets) and `docs/releases/v0.2.0.md`.
- `/api/health` reports `default_retrieval_mode` and `embedding_model_loaded`.
- `EMBEDDING_WARMUP` setting (default `true`).

### Changed
- **Default retrieval mode:** a request without `mode` now uses `hybrid` on PostgreSQL (previously always `bm25`). SQLite still defaults to `bm25`. The UI already made the same choice.
- **Generation fallbacks:**
  - If the LLM provider fails (`provider_error`), or its answer cites no source or an unknown one (`citation_validation_failed`), the response now returns the verbatim authorized quotes after an explanatory notice. Previously it returned only an error sentence.
  - Unverified model text is never shown.
- **Upload route:** it is now a synchronous route that FastAPI runs in its threadpool. Parsing and CPU-bound embedding no longer block the event loop.
- **Embedding model loading:** the model is loaded at API startup in a worker thread, so the first query no longer pays the ~15 s cold start. A failed load is logged and is not fatal.
- **Upload errors:** an empty upload returns HTTP 400 instead of 413. An over-size upload returns 413 with the limit in the message.
- **UI:**
  - The answer badge names every answer mode (abstention, fallbacks), instead of labelling everything except `llm` as "Offline extractive".
  - The upload notice reports the real embedding status.
  - The governance panel no longer lists implemented features as "not implemented".
- **Versions:** `0.2.0` everywhere (`pyproject.toml`, `uv.lock`, FastAPI, `frontend/package.json`, UI).

### Fixed
- `scripts/demo_scenarios.py` crashed with `UnicodeEncodeError` on Windows consoles (cp1252) when printing Croatian quotes. It now forces UTF-8 output.
- Outdated statements in `docs/architecture.md` ("FTS will replace it in v0.2b", lazy model loading) and `QUICKSTART-HR.md` (SQLite mode, test counts).

### Verified for this release (2026-11-14, Windows 10, Python 3.12, PostgreSQL 16 + pgvector in Docker)
- Tests:
  - `pytest` with PostgreSQL: **220 passed, 0 skipped, 0 failed**;
  - 95 % line coverage of `backend/app`;
  - without PostgreSQL: 168 passed, 52 skipped.
- A full `scripts/evaluate.py` run from the previous commit reproduced `eval/reports/latest.json` exactly. The only differences were timings and the database name.
- `scripts/evaluate.py --ci` gates pass on this code. Authorization violations: **0**.
- Lint and frontend:
  - `ruff check backend scripts` passes;
  - `tsc` and the frontend production build (`npm run build`) pass.
- Docker Compose (images rebuilt from this code):
  - `scripts/demo_scenarios.py`: **9 PASS, 1 KNOWN**. The KNOWN item is the documented false abstention, accepted.
  - New `scripts/docker_smoke.py` on a disposable API instance: **20/20**.

### Known limitations (not part of the Phase 1–2 scope)
- Local demo authentication: HS256 JWT, shared demo passwords, no rate limiting.
- No PostgreSQL row-level security.
- No OCR or layout/table extraction; contract fields are regex-based.
- No reranker and no ANN index.
- No background workers; upload is synchronous within the request.
- The optional OpenAI path is tested only against a fake client. No live LLM call and no LLM answer-quality evaluation exist.
- The evaluation corpus is small and synthetic: 59 chunks and 41 answerable test questions, so confidence intervals are wide.

## [0.1.0] - unreleased vertical slice

- FastAPI + React demo:
  - demo JWT authentication;
  - PDF/DOCX/TXT upload;
  - chunking;
  - ACL-filtered in-process BM25;
  - extractive evidence answers with `[S#]` citations;
  - regex contract fields;
  - audit events;
  - two-tenant synthetic demo data.
