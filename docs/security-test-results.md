# Security test results (Phase 4)

Test counts and outcomes in this document come from executed runs only (sources at the end).
Passing tests do not prove the absence of vulnerabilities; they show that the listed attack paths
were exercised and blocked. No external penetration test has been performed.

## Coverage of the Phase 4 security requirements

### Authentication

| Requirement | Tests (all passing) |
|---|---|
| Invalid JWT signatures | foreign key with a known `kid`, tampered payload (`test_signature_from_a_foreign_key_with_a_known_kid_is_rejected`, `test_tampered_payload_is_rejected`) |
| Invalid issuer / audience / expired / not-before / issued-in-future / missing claim | `test_claim_violations_are_rejected` (6 cases); access tokens without the API audience; discovery issuer mismatch |
| Algorithm attacks | `alg=none`, HS256 signed with the RSA public key (refused even if HS256 were configured), wrong key type, missing `kid` |
| Key rotation | fake IdP (new key picked up with exactly one refetch, random kids do not cause refetches, removed key rejected after TTL); **real Keycloak**: new RSA key provider created through the admin API, token signed with the new `kid` validated after one JWKS refetch |
| CSRF / session handling | cookie POST without / with forged CSRF token → 403; hostile `Origin` → 403; stable CSRF per session; new session id at login (no fixation); idle / absolute expiry; revoked session + stolen cookie reuse; disabled account |
| Login / logout | state single use, login-CSRF browser binding, PKCE tampering refused by the IdP, open redirects neutralised (4 payloads), RP-initiated logout URL, back-channel logout (fake IdP and Keycloak in Docker) |
| Demo isolation | demo mode refused in staging / production; demo password login 404 and demo JWT 401 in OIDC mode; identical answers for wrong password and unknown account |
| Unauthorized access | no / malformed credentials (`Bearer `, `Basic`, `a.b.c`, forged cookie) → 401 / 403 |

### Authorization and row-level security

| Requirement | Tests |
|---|---|
| Cross-tenant documents and every derived table | `test_tenant_isolation_across_every_table` (direct SQL as `nexus_app`), Phase 1-3 suite under RLS, document-id probing (identical 404s for missing / other tenant / same-tenant forbidden / deleted, 9 read paths + 4 write paths) |
| Cross-tenant embeddings | unfiltered nearest-neighbour SQL under RLS returns only own-tenant vectors |
| Reviewer privilege escalation | members cannot correct / approve / reject (403), other-tenant reviewer gets 404, `validate` does not persist for readers, sharing into foreign groups refused; RLS: reviewer updates only readable extractions |
| Direct SQL policy bypass attempts | `SET ROLE` to privileged roles fails, `row_security = off` fails, no `DELETE` / `UPDATE` grants where not needed, INSERT into another tenant / as another user / unauthorized grant / job → RLS violation |
| Worker security context | job scope limited to the leased job's document; forged token, other tenant and expired lease open nothing; no access to sessions, identities or password hashes; permission revoked between submission and execution fails the job |
| Connection-pool context leakage | pool of one connection (same backend pid): no context after commit, after an exception, after a session-level `SET`; concurrent threads keep their own context |
| Revocation | group removal and account disabling effective at the next statement (SQL) and at the next request (API); IdP group removal and IdP logout reach the local session (Keycloak) |
| Missing application filter (mutation) | RLS blocks the leak on PostgreSQL; the same mutation leaks on SQLite → the test detects real leaks |

### Storage

| Requirement | Tests |
|---|---|
| Unauthorized downloads | other group / other tenant → 404 for `/source`, versions and presigned URLs; no auth → 401 |
| Object key manipulation / path traversal | keys built only from validated ids; `../`, absolute, Windows and nested traversal keys refused; local store refuses paths outside its root |
| Unsafe MIME types | HTML, SVG, executables → 415; HTML or PDF bytes under a mismatched extension → 422; `Content-Type` derived server-side; downloads `attachment` + `nosniff` |
| Oversized uploads, zip bombs, malformed files | 413; 70 MB declared DOCX member / traversal member names → 422; malformed PDF / DOCX / XLSX → 422 (inline) or `parse_failed` (worker) |
| Presigned URL misuse | valid URL works only for its key; tampered signature → 403; expired → 403; deleted object → 404; anonymous bucket access → 403 |
| Deleted-object access | after deletion: 404 for every read path, absent from retrieval, comparisons, structured queries; purge removes the object |

### AI-specific

| Requirement | Tests |
|---|---|
| Prompt injection in retrieved documents | an authorized document instructing the model to reveal another tenant's codename: the prompt contains only authorized text, the injected text is framed as untrusted data |
| Prompt injection in OCR / extraction text | Phase 3 LLM extraction tests (labelled-line rule, injected instructions cannot add values) still pass |
| Unauthorized context entering LLM prompts | Phase 2 prompt-capture tests now run under RLS; evaluation gate counts 0 |
| Citation leakage | LLM output citing an unknown source is discarded (`citation_validation_failed`), old chunk ids of a deleted or superseded document never return |
| Document id probing / comparison leakage | identical 404 bodies; comparison with a foreign id returns the same generic 404 as with a non-existent id and names no document |

## Results

The full suite (567 collected tests; per file: OIDC 64, Keycloak 12, RLS 20, Phase 4 security 14,
jobs / lifecycle 41, storage 22, observability 35, plus the Phase 1-3 suites) ran in GitHub Actions
run [43756922292](https://github.com/KrzakLara/nexus-ai-enterprise-rag/actions/runs/43756922292) on `f4a9f91`, job `backend`. It used PostgreSQL 16 with the RLS runtime roles,
real MinIO, real Keycloak, Tesseract and the real embedding model, and **passed**: 0 failures, and
coverage of `backend/app` at or above the enforced 90 % floor. The exact pass / skip split and
coverage percentage are in the job log, which needs a GitHub login. The same run passed the Phase 2
and Phase 3 evaluation gates (zero-tolerance authorization checks), the gitleaks history scan and
the Docker end-to-end job.

Further verification:

| Verification | Result | Evidence |
|---|---|---|
| Targeted suites (auth / OIDC, observability, Phase 4 security, permissions, jobs, storage, MVP end-to-end), Windows + PostgreSQL 16 | 160 passed / 3 skipped, then after the last fixes 105 passed / 12 skipped (MinIO, Keycloak and Tesseract tests need those services: they run in the Linux regression); 0 failed | local runs, 2026-11-15 |
| Mutation check of the duplicate-submission test | removing the advisory lock makes the test fail (two jobs) | local run |
| Authorization under load (k6, 4 profiles, 4 048 requests) | **0** authorization violations; cross-tenant probes always 404 | `eval/phase4/load/*.json` |
| Failure injection (worker kill, frozen database, storage outage, concurrent duplicates, delete during processing) | **12/12** after two defects found and fixed (first run 8/10) | `eval/phase4/resilience-latest.json` |
| Backup / restore | **8/8**, incl. RLS enabled + forced on 15 tables with the same 44 policies and no rows for the runtime role without context after restore | `eval/phase4/backup-restore-drill.json` |
| Docker end-to-end on the final image | **22/22**: OIDC + PKCE login, private storage, RLS on the live database, other group / other tenant denied, trace across API and worker, deletion + purge, RP-initiated and back-channel logout | `eval/phase4/e2e-latest.json` |
| Real browser (headless Chromium) | SSO through Keycloak with PKCE S256, no token in `localStorage`, session cookie not readable from JavaScript, session restored after reload, 401 after logout | screenshots in `docs/images/` |
| Secret scan | gitleaks v8.24.0 over the full history: 0 findings after triage of 3 synthetic redaction-test inputs (`.gitleaksignore`) | CI job `secrets`, run 43756922292: passed |
| Docker end-to-end in CI | `e2e_phase4.py` and the k6 smoke profile on a stack built from scratch on a GitHub runner | CI job `docker-e2e`, run 43756922292: passed |
