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
| 11-13 | Docker E2E, drills, load tests, benchmarks, Linux full run, evaluation gates, docs | in progress | see enterprise-hardening.md |

## Incidents during the run (for the record)

* Docker Desktop's engine stopped responding once (API 500, all published ports down). Docker
  Desktop was restarted; no volume was touched. Disposable test containers were restarted.
* A stopped background pytest run left its Python process alive and ran concurrently with the next
  run on the same database: cross-process lock waits. Both processes were killed; the test session
  now takes a PostgreSQL advisory lock and refuses to run concurrently.
