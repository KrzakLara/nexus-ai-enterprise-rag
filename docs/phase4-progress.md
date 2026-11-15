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
| 12e | Full Linux regression with coverage | **NOT RUN** | see resume point |
| 12f | Phase 2 / Phase 3 evaluation gates | **NOT RUN** | see resume point |
| 12g | promtool / actionlint | **NOT RUN** | see resume point |
| 13 | Documentation and final report | partly done | security-test-results counts, README test section and the Definition-of-Done table wait for 12e-12g |
| — | GitHub Actions | **never executed** | nothing was pushed (requires the owner's approval) |

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

## Resume point (session checkpoint, 2026-11-15 ~19:55 UTC)

State at the checkpoint: branch `feat/phase4-enterprise-hardening`, clean working tree after the
checkpoint commit, nothing pushed. Docker: the disposable stack `nexus-p4` (OIDC + observability,
standard configuration) and the disposable test services `nexus-p4-testdb` (PostgreSQL on
127.0.0.1:5434, password `test_only_local`, network alias `postgres` on `nexus-p4-net`) and
`nexus-p4-minio` (127.0.0.1:9000) are running; `./backups/` (gitignored) holds the drill backup.
No test, load or drill process is running.

On Windows (Git Bash) run `export MSYS_NO_PATHCONV=1` first and use `$(pwd -W)` instead of
`$PWD` in the volume arguments.

Remaining verification, in this order (the benchmark sets runtime-role passwords on the test
cluster, so never run a benchmark and a test session at the same time):

```bash
# 1. full Linux regression with Tesseract, real MinIO and a fresh disposable Keycloak + coverage
#    (CI threshold: --cov-fail-under=90). nexus-p4-testrunner:baseline = docker/test-runner.Dockerfile.
docker run -d --name nexus-p4-kc-test --network nexus-p4-net --memory 1500m \
  -e KC_BOOTSTRAP_ADMIN_USERNAME=kc-test-admin -e KC_BOOTSTRAP_ADMIN_PASSWORD=kc-test-admin-secret \
  -v "$PWD/docker/keycloak/nexus-realm.json:/opt/keycloak/data/import/nexus-realm.json:ro" \
  quay.io/keycloak/keycloak:26.4.2 start-dev --import-realm
docker run --rm --network nexus-p4-net -v "$PWD:/src" -v nexus-p4_hf_models:/hf -e HF_HUB_OFFLINE=1 \
  -e TEST_DATABASE_URL=postgresql+psycopg://nexus:test_only_local@postgres:5432/nexus_test \
  -e TEST_S3_ENDPOINT_URL=http://nexus-p4-minio:9000 -e TEST_KEYCLOAK_URL=http://nexus-p4-kc-test:8080 \
  nexus-p4-testrunner:baseline sh -c "uv sync --locked --extra dev >/dev/null && \
  python -m pytest -q -rs -p no:cacheprovider --cov=backend/app --cov-report=term --cov-fail-under=90"

# 2. evaluation gates (same container image, disposable *_eval databases on the test cluster)
docker run --rm --network nexus-p4-net -v "$PWD:/src" -v nexus-p4_hf_models:/hf -e HF_HUB_OFFLINE=1 \
  -e EVAL_DATABASE_URL=postgresql+psycopg://nexus:test_only_local@postgres:5432/nexus_eval \
  nexus-p4-testrunner:baseline sh -c "uv sync --locked --extra dev >/dev/null && python scripts/evaluate.py --ci && \
  python scripts/evaluate_docintel.py --ci --database-url postgresql+psycopg://nexus:test_only_local@postgres:5432/nexus_docintel_eval"

# 3. static configuration checks
docker run --rm -v "$PWD/docker/observability:/etc/prometheus:ro" --entrypoint promtool prom/prometheus:v3.5.0 check config /etc/prometheus/prometheus.yml
docker run --rm -v "$PWD/docker/observability:/etc/prometheus:ro" --entrypoint promtool prom/prometheus:v3.5.0 check rules /etc/prometheus/alerts.yml
python scripts/build_grafana_dashboard.py && git diff --exit-code docker/observability/grafana/dashboards/
docker run --rm -v "$PWD:/repo:ro" -w /repo rhysd/actionlint:1.7.7
```

Then fill in `docs/security-test-results.md` (per-file counts), the README test section and the
Definition-of-Done table in `docs/enterprise-hardening.md` from those runs, commit, and ask the
owner before any push (GitHub Actions has never run for this branch).

