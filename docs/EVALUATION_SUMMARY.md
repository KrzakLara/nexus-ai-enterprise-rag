# Evaluation summary (v0.4.0)

One page with every headline number of NEXUS AI, where it comes from, what it was measured on and
how far it can be trusted. Each row links to the raw evidence in the repository. Nothing here is a
production measurement: **all data is synthetic**, created for this project, and all runs were on a
developer laptop or a GitHub-hosted runner.

## How to read the numbers

| Rule | Consequence |
|---|---|
| **Synthetic data** | The retrieval corpus (30 documents, 100 questions) and the document-intelligence corpus (32 documents) were written by the same author as the system. Scores are optimistic compared with real enterprise documents. |
| **No live LLM** | Every answer and extraction number comes from the offline, deterministic path (verbatim extractive answers; `rules_v1` extraction). The optional OpenAI answer path and the optional LLM extraction provider are tested only against **fake clients** (exact prompt capture, failure handling); their live quality is **not measured**. |
| **Held-out splits** | Retrieval and extraction thresholds were chosen on a dev split and reported on a separate test split. |
| **Generated, not typed** | Retrieval tables are rendered from `eval/reports/latest.json` by `scripts/evaluate.py`; a test (`test_ci_gates.py`) fails if the README or `benchmark-results.md` drift from the stored report. |
| **Code version** | Phases 1–3 metrics were produced on the Phase 2 / Phase 3 code and re-gated in CI on every later commit (the CI gates re-run the retrieval and extraction evaluation with zero-tolerance authorization checks). Phase 4 results were measured on the Phase 4 branch. Phase 5 changed documentation and version metadata only. |

## Retrieval and answers (Phase 2)

Source: [`eval/reports/latest.json`](../eval/reports/latest.json), generated 2026-11-12 by
`scripts/evaluate.py`; method: [evaluation.md](evaluation.md); full tables:
[benchmark-results.md](benchmark-results.md).

Dataset: 100 hand-labelled questions (EN + HR, chunk-level graded relevance) over 30 synthetic
documents (59 chunks), 50 dev / 50 test. Metrics on the **test split**, answerable questions.

| Mode | Recall@5 | MRR@10 | nDCG@5 | p95 latency |
|---|---|---|---|---|
| BM25 (Python baseline) | 0.760 | 0.709 | 0.701 | 9.9 ms |
| PostgreSQL FTS | 0.797 | 0.732 | 0.738 | 6.4 ms |
| Semantic (pgvector, exact) | 0.850 | 0.720 | 0.737 | 56.3 ms |
| Hybrid RRF | 0.809 | 0.730 | 0.734 | 72.2 ms |

| Finding | Value |
|---|---|
| Is hybrid significantly better? | **No.** Every paired bootstrap 95 % CI of the nDCG@5 difference contains 0 (n = 41). Hybrid is the default because it is robust across question categories (identifiers and paraphrases), not because it wins. |
| Evidence policy (abstention) on test | unanswerable detected 9/9 (1.000); answerable recall 0.805 (8/41 answerable questions falsely refused) |
| Offline answer fact recall (hybrid, top-5) | 0.333 → **0.590** after query-aware verbatim sentence selection |
| Authorization violations | **0** in 400 retrieval runs (all branches, top-50 candidates), 400 API responses, 396 captured LLM prompts |
| Negative results kept | a stronger cross-language embedder and MarianMT query translation were measured and **not adopted** (translation tripled cross-language nDCG@5 but lowered overall nDCG@5 and added ~0.5 s per query) |

## Document intelligence (Phase 3)

Source: [`eval/docintel/reports/latest.json`](../eval/docintel/reports/latest.json), generated
2026-11-14 (dataset v1.1, corpus sha256 prefix `aa0a85216914635b`); method:
[extraction-evaluation.md](extraction-evaluation.md); generated results:
[extraction-evaluation-results.md](extraction-evaluation-results.md).

Corpus: 32 frozen synthetic documents (16 contracts, 14 invoices, 2 other; text PDF, 7 scanned
PDF, 1 mixed PDF, DOCX, XLSX, TXT, MD; 20 EN / 12 HR; 2 tenants), offline `rules_v1` provider,
Tesseract 5.5.0 (`eng+hrv`).

| Metric | Result |
|---|---|
| Field-level F1 (all / dev / **test**) | 0.985 / 0.995 / **0.971** (376 field slots; precision 0.997, recall 0.973, exact match 0.973) |
| Field F1 on scanned PDFs (OCR path) | 0.983 |
| Values invented for absent fields | 0 of 9 |
| Evidence supports the value / on the right page | 100 % / 99.6 % of 533 values |
| OCR on 10 scanned pages | CER 0.0015, key-token recall 0.992 |
| Invalid invoices blocked from approval | 4/4; 0 validation false accepts |
| Security matrix (7 personas × 32 documents, 1,946 requests) | **0** violations, 0 false denials |

Every miss is a deliberately novel label on a test document or an OCR `0`/`O` confusion; misses
were reported, not patched ([ERRATA](../eval/docintel/ERRATA.md)).

## Enterprise hardening (Phase 4)

Sources: [security-test-results.md](security-test-results.md),
[performance-benchmarks.md](performance-benchmarks.md), raw JSON in [`eval/phase4/`](../eval/phase4/).

| Verification | Result | Evidence |
|---|---|---|
| Automated test suite | **567 tests** collected; all run in CI with PostgreSQL 16 + pgvector (RLS runtime roles), real MinIO, real Keycloak, Tesseract and the real embedding model; 0 failures; coverage of `backend/app` enforced at ≥ 90 % (`--cov-fail-under=90`) | CI job `backend` |
| Docker end-to-end (OIDC + PKCE login → upload → worker → review → RLS → denial → traces → delete/purge → logout) | **22/22** | [`eval/phase4/e2e-latest.json`](../eval/phase4/e2e-latest.json) (2026-11-15) |
| Failure injection (worker kill, frozen DB, storage outage, concurrent duplicates, delete during processing) | **12/12** after two defects were found (first run 8/10) and fixed with tests | [`eval/phase4/resilience-latest.json`](../eval/phase4/resilience-latest.json) |
| Backup / restore drill | **8/8** incl. FORCE RLS on 15 tables with the same 44 policies after restore | [`eval/phase4/backup-restore-drill.json`](../eval/phase4/backup-restore-drill.json) |
| k6 load: smoke / moderate / stress / sustained (4,048 requests) | 0 authorization violations under load; 0 % failed requests except stress (3.5 %, shed as HTTP 503 + `Retry-After` by design) | [`eval/phase4/load/`](../eval/phase4/load/) |
| RLS overhead (paired comparison, 60 queries per cell) | +10 to +20 ms per retrieval query for group members on this corpus; faster for admins (tenant predicate prunes rows) | [`eval/phase4/benchmarks.json`](../eval/phase4/benchmarks.json) |
| Secret scan | gitleaks v8.24.0 over the full history: 0 findings after triage of 3 synthetic redaction-test inputs | CI job `secrets` |

The load numbers describe one laptop (i7-1165G7, Docker Desktop, one API and one worker
container, CPU embeddings). They are **not** capacity claims; under the stress profile hybrid
`/api/ask` p95 reached ~18.8 s because embedding queries on CPU saturate one API process.

## Phase 5 re-verification (2026-11-17)

Phase 5 changed no application code. To confirm that the merged `main` still works as documented:

| Check | Result | Evidence |
|---|---|---|
| GitHub Actions on rewritten `main` HEAD `77da2e2` | workflow *Quality checks*, run [43765779112](https://github.com/KrzakLara/nexus-ai-enterprise-rag/actions/runs/43765779112): `backend`, `frontend`, `secrets`, `config`, `docker-e2e` all **success** | GitHub Actions API |
| Docker Quick Start from scratch (new Compose project, empty volumes; `compose.yaml` + OIDC + observability overlays, `--build`) | all 9 services up in ~2.5 min on a warm Docker build cache (the embedding model was downloaded into the new volume); `/api/health/ready`: database, schema, workers, object storage, identity provider and embedding model all ok | local run |
| Docker end-to-end on that stack | **22/22** | [`eval/phase5/e2e-phase5-main.json`](../eval/phase5/e2e-phase5-main.json) |
| Real-browser walk-through (headless Chromium, Keycloak SSO) | screenshots in [`docs/images/`](images/) were captured from this stack | [DEMO.md](DEMO.md) |
| Fast local subset on Windows (`-m "not model and not postgres"`) | see [releases/v0.4.0.md](releases/v0.4.0.md#verification) | local run |

## Not measured (and not claimed)

- Live LLM answer quality, groundedness of generated claims, live LLM extraction accuracy.
- Retrieval quality on real enterprise corpora, at scale (no ANN index; exact vector scan), or
  with a reranker.
- External penetration test, compliance certification, HA / multi-region operation.
- Business outcomes (time saved, cost per document). No such numbers exist for this project.

## Note on commit hashes

The Git history was rewritten on 2026-11-15 (commit metadata only). Phase 3 and Phase 4 documents
cite the original hashes; the same commits on `main` are, for example: `f4a9f91` → `78bb4a6`,
`aec92fc` → `3567c0e`, `beac158` → `23c649a`, `004cf47` → `7660888`, `93da418` → `018c184`,
`f34a5b6` → `09ce94d`. CI run 43756922292 cited in Phase 4 documents ran on the pre-rewrite
commit `f4a9f91`; run 43765779112 is the run on the rewritten `main`.
