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

The measured counts are filled in from the executed runs below.
