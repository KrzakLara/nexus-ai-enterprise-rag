# Phase 4 case study: hardening a document-intelligence RAG system

This note records the engineering decisions behind Phase 4 and the problems found while verifying
them. Results with numbers: [enterprise-hardening.md](enterprise-hardening.md),
[performance-benchmarks.md](performance-benchmarks.md), [security-test-results.md](security-test-results.md).

## Starting point

Phases 1-3 had a strict application-level ACL (one SQL subquery inside every retrieval statement),
a zero-violation security evaluation, synchronous processing inside the upload request, local
files, demo-only HS256 authentication with a token in `localStorage`, and plain-text logs.

## Decisions

**Backend-for-frontend instead of a browser OIDC client.** The SPA never sees a token. The cost is a
session table and one indexed lookup per request; the gain is that XSS cannot exfiltrate tokens,
revocation (logout, back-channel, disabled accounts, removed groups) is immediate, and refresh
tokens are rotated server-side. Tenant authority comes from IdP group paths under a server-side
tenant allowlist, bound once per identity.

**RLS as a second, independent enforcement of the same rule.** The policies replicate the
application rule (tenant ∧ not deleted ∧ (admin ∨ owner ∨ group grant)) and read group membership
from the authoritative tables, so group removal applies to the next statement. Child tables
reference the parent's RLS through uncorrelated sub-queries (hashed SubPlans). The mutation test
removes the application filter: on PostgreSQL nothing leaks, on SQLite (no RLS) it does, which
proves the test is sensitive.

**The queue lives in PostgreSQL.** The decisive property is atomicity: the job row is part of the
same transaction as the upload, so there is no outbox relay to build and no window where a committed
document has no job. Workers are fenced by a lease token that the RLS job scope also checks, so a
stalled worker cannot write after its lease was taken over.

**Object storage with server-generated keys and staged deletion.** No user input reaches a key; the
server verifies SHA-256 on upload and the worker verifies it again on download. Deletion is a soft
delete (instantly invisible to the ACL and RLS) followed by an idempotent purge that keeps a
contentless tombstone and the audit trail.

**Inline mode kept on purpose.** Tests, the evaluation harnesses and the SQLite demo run the same
job code inside the request, which kept every Phase 1-3 contract (201 responses, `embedding_status`,
422 for unparseable files) and let the 357 pre-existing tests run unchanged against the new
pipeline, on PostgreSQL as the RLS runtime roles.

## Problems found during verification

1. **PostgreSQL checks SELECT policies against the new row of an UPDATE with a WHERE clause.** The
   first RLS run failed every upload: completing a job clears its lease token, so the new row no
   longer matched the worker policy, which required the token for all commands. The lease check
   moved to the UPDATE policy (existing row), and soft deletes moved into a narrow
   `SECURITY DEFINER` function that re-applies the owner / admin rule.
2. **`claim_job` refused for the API role.** One route built its worker sessions without the
   dependency override and fell back to the API connection. PostgreSQL answered "permission denied
   for function claim_job", the correct behaviour (the API role must not claim jobs). The route now
   uses the shared dependency.
3. **Secure cookies over `http://localhost`.** Keycloak sets `Secure` cookies; browsers send them to
   localhost (a secure context), Python's cookie jar does not. The real-Keycloak login test forwards
   them explicitly.
4. **MinIO images disappeared.** The upstream project no longer distributes community images; the
   stack uses Chainguard's source build pinned by digest, documented and overridable.
5. **Two concurrent pytest sessions on one database.** A stopped background run left its Python
   process alive; its fixtures truncated tables under the other run's open transactions and both
   blocked. The test session now takes a PostgreSQL advisory lock and refuses to run concurrently.
6. **Load testing locked Keycloak accounts.** The first moderate run answered 79 % of requests
   with 401. Keycloak's brute-force quick-login check locks an account that logs in twice within a
   second, and the k6 script logged five shared accounts in from many virtual users at once. It
   then cached Keycloak's error answer as a token and sent `Bearer undefined`. The API was right
   to refuse; the harness now logs each account in once and renews with refresh tokens.
7. **Saturation produced HTTP 500.** Under 40 virtual users on 8 CPUs the 15-connection pool ran
   dry and SQLAlchemy's checkout timeout surfaced as "Exception in ASGI application". It is now a
   503 with `Retry-After`, counted and alerted. In the measured stress run, 3.3 % of requests were
   shed this way.
8. **A frozen database hung requests.** In the failure-injection drill, `docker pause` on
   PostgreSQL left readiness and API requests waiting on pooled connections until the client gave
   up. A frozen server's kernel still accepts TCP, and libpq has no client-side query timeout.
   - Readiness probes are now bounded and use a new connection with `connect_timeout`.
   - A request deadline answers 503 when no response has started.
9. **Duplicate-job detection was not atomic.** The check "is a job already running for this
   document?" passed for two simultaneous requests. In sequential tests the race never showed,
   because the queue finished a re-index within ~140 ms.
   - Advisory locks per document now serialize submissions.
   - A mutation test proves the concurrency test fails without them.

## What remains out of scope

External penetration test, multi-region and HA operation (PostgreSQL replication, S3 replication),
a hardened production reverse proxy / WAF, rate limiting at the edge, key management in an HSM /
KMS for `SESSION_ENCRYPTION_KEY`, SSO for the observability tools, and live LLM extraction
evaluation.
