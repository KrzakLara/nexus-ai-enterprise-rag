# Phase 4 progress record (resume point)

Working record for the Phase 4 enterprise-hardening implementation, kept in the repository so a
later session can resume from the last verified checkpoint. Final results live in
[enterprise-hardening.md](enterprise-hardening.md) and the linked documents.

## Branches

| Branch | Base | State |
|---|---|---|
| `feat/phase3-document-intelligence` | — | Phase 3 RC, HEAD `f34a5b6`, pushed to `origin`, **not merged into `main`** |
| `main` (local) | — | `49b0e87` (1 commit ahead of `origin/main` `6b4ff06`); contains no Phase 3 commit |
| `feat/phase4-enterprise-hardening` | `f34a5b6` | **stacked on Phase 3** |

Merge order for later (not done here): Phase 3 branch into `main` first, then this branch.

## Baseline (before any Phase 4 change, `f34a5b6`)

| Check | Result |
|---|---|
| Linux container (python 3.12, Tesseract 5, PostgreSQL 16 + pgvector) | **359 passed**, 0 skipped, coverage **94 %** |
| Windows host (no Tesseract) | 357 passed, 2 skipped (OCR), coverage 91 % |
| `uv lock --check` | consistent (99 packages) |
| Phase 2 dataset sha256 | `1a3c160c9645e075db295566e33139ad0f352c89dc1bb01343f9dcd257a1135f` |
| Phase 3 corpus sha256 | `aa0a85216914635b` (32 documents, dataset v1.1) |

## Milestones

| # | Milestone | Status | Evidence |
|---|---|---|---|
| 1 | Baseline audit, branch | done | table above |
| 2-6 | Config / OIDC / RLS / jobs / storage core | done (`0e14632`) | 357 existing tests pass with RLS-enforced runtime roles |
| 7-10 | Tests, frontend, Docker, observability configs, drills, CI | done (`a121a30`) | full Windows run (PostgreSQL + MinIO + real Keycloak): 545 passed, 3 failed (timing-flaky retry tests, fixed: deterministic back-off; DB-clock scheduling), 5 skipped (Tesseract absent on Windows, PostgreSQL-only tests on SQLite); affected files re-run: 157 passed |
| 11 | k6 load profiles on the OIDC + queue + observability stack | done (`a67003e`, `9627225`) | smoke / moderate / sustained 0 % failed, stress 3.5 % shed with 503; 0 authorization violations ([performance-benchmarks.md](performance-benchmarks.md)) |
| 11a | Defects found by load testing, fixed with tests | done (`004cf47`, `543ffde`, `a67003e`) | pool exhaustion answered 500 → 503 + Retry-After; JWKS refresh hardened (single flight, stale-while-revalidate, 503 on IdP failure); k6 harness locked Keycloak accounts (quick-login check) and cached the error as a token |
| 11b | Secret scan | done (`741cb36`) | gitleaks v8.24.0: full history 0 findings after triage of 3 synthetic redaction-test inputs; CI job `secrets` added (not yet run on GitHub) |
| 12a | Failure-injection drill | done (`beac158`, `cec42ea`) | first run 8/10 found two defects (frozen database hung requests; non-atomic duplicate-job detection), fixed with tests; rerun **12/12** (`eval/phase4/resilience-latest.json`) |
| 12b | Backup / restore drill | done (`cec42ea`) | **8/8** (`eval/phase4/backup-restore-drill.json`) |
| 12c | Component benchmarks | done (`aec92fc`) | paired RLS comparison, sessions, queue, embeddings, parsing / OCR (`eval/phase4/benchmarks.json`) |
| 12d | Final Docker E2E on the final image | done (this checkpoint) | **22/22** at 19:50 UTC, image `3e049a245db9` (= `aec92fc`), standard configuration (`eval/phase4/e2e-latest.json`); real-browser SSO check passed on the image before the last two fixes |
| 12e | Full regression on Linux with coverage | done (GitHub Actions) | job `backend` of run [43756922292](https://github.com/KrzakLara/nexus-ai-enterprise-rag/actions/runs/43756922292) on `f4a9f91`: pytest over the 567 collected tests with PostgreSQL 16 / RLS, MinIO, Keycloak, Tesseract and the real embedding model, `--cov-fail-under=90`: **passed** (0 failures; coverage ≥ 90 %; the exact pass / skip split and coverage percentage are in the job log, which needs a GitHub login) |
| 12f | Phase 2 / Phase 3 evaluation gates | done (GitHub Actions) | same job: `evaluate.py --ci` and `evaluate_docintel.py --ci` (zero-tolerance authorization gates, retrieval / extraction floors) and the retrieval / ACL smoke: **passed** |
| 12g | promtool / actionlint / compose / dashboard | done (`f4a9f91`) | first run `43755469472` failed at actionlint (shellcheck SC2034, unused loop variable in a CI script), fixed; job `config` of run 43756922292: **passed** |
| 12h | Docker E2E and k6 smoke in GitHub Actions | done | job `docker-e2e` (first execution on GitHub): stack built and healthy in 3 min 45 s, `e2e_phase4.py` and the k6 smoke profile (0 authorization violations threshold): **passed** |
| 13 | Documentation and final report | done | README, security-test-results, enterprise-hardening (Definition of Done), CHANGELOG updated with the CI results |
| — | GitHub Actions | **green** | run [43756922292](https://github.com/KrzakLara/nexus-ai-enterprise-rag/actions/runs/43756922292) on `f4a9f91`: backend, frontend, secrets, config, docker-e2e all passed (2026-11-15 20:14–20:28 UTC) |

## Incidents during the run (for the record)

* Docker Desktop's engine stopped responding once (API 500, all published ports down). Docker
  Desktop was restarted; no volume was touched. Disposable test containers were restarted.
* A stopped background pytest run left its Python process alive and ran concurrently with the next
  run on the same database: cross-process lock waits. Both processes were killed; the test session
  now takes a PostgreSQL advisory lock and refuses to run concurrently.
* Load testing (moderate, first attempt): 79 % of requests answered 401. Root cause: Keycloak's
  brute-force quick-login check locked accounts that k6 virtual users logged in concurrently, and
  the k6 script cached Keycloak's error answer as a token (`Bearer undefined`). The API behaved
  correctly; the script now logs each account in once and renews tokens.
* Load testing (stress, first attempt): 8 HTTP 500 from SQLAlchemy pool checkout timeouts; fixed
  (503 + Retry-After) before the measured runs.

## Final verification (2026-11-15)

Phase 4 is verified against its agreed scope: locally (drills, load profiles, benchmarks, Docker
end-to-end 22/22, real browser) and in GitHub Actions run [43756922292](https://github.com/KrzakLara/nexus-ai-enterprise-rag/actions/runs/43756922292) on `f4a9f91`, where all
five jobs passed. The branch is pushed; it is **not merged** into `main`, untagged and unreleased.
It is stacked on the Phase 3 branch, so Phase 3 must reach `main` first.

Open items (none blocks the Phase 4 scope):
* transcribe the exact test counts (passed / skipped) and the coverage percentage from the
  `backend` job log (needs a GitHub login; CI proves 0 failures and coverage ≥ 90 %);
* the manual `load-test.yml` workflow has not been run on GitHub (the k6 smoke runs in
  `docker-e2e`; all four profiles were measured locally);
* out of scope by design: external penetration test, HA / multi-region, WAF / edge rate limiting,
  KMS / HSM for the session key, SSO for the observability tools, live LLM evaluation.

The commands used for the local verification remain valid for re-runs: see
[performance-benchmarks.md](performance-benchmarks.md) (load, drills, benchmarks) and the CI
workflow `.github/workflows/ci.yml` (tests, gates, Docker E2E).
