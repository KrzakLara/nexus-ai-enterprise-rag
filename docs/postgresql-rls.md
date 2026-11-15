# PostgreSQL row-level security (Phase 4B)

Row-level security (RLS) is **defense in depth below** the application ACL
(`backend/app/security.py::authorized_documents`), not a replacement: every query still carries
the application filter, and the database independently refuses rows the current user may not see.
Migration `0008_row_level_security` (PostgreSQL only; a no-op on SQLite).

## Roles

| Role | Attributes | Used by |
|---|---|---|
| `nexus` (POSTGRES_USER) | superuser, table owner | Alembic migrations, seeding, backups, maintenance scripts. **Never** the API or workers |
| `nexus_app` | LOGIN, NOSUPERUSER, **NOBYPASSRLS**, owns nothing | API (`DATABASE_URL`) |
| `nexus_worker` | LOGIN, NOSUPERUSER, **NOBYPASSRLS**, owns nothing | background workers (`WORKER_DATABASE_URL`) |
| `nexus_rls_definer` | **NOLOGIN**, BYPASSRLS | owns the `SECURITY DEFINER` helper functions only |

The migration creates missing roles as NOLOGIN and re-asserts their attributes; it never stores a
password. `scripts/provision_db_roles.py` (Compose `migrate` service) sets LOGIN + password from
`DB_APP_PASSWORD` / `DB_WORKER_PASSWORD`. With `DB_REQUIRE_RLS_ROLE=true` (default outside local
environments, set in Compose) the API and worker **refuse to start** if their role is a superuser
or has BYPASSRLS. Runtime roles have no `DELETE` on documents, chunks, embeddings or history, no
`UPDATE`/`DELETE` on `audit_events` (append-only), and no column privilege on
`users.password_hash` except `INSERT`.

`FORCE ROW LEVEL SECURITY` is set on all 15 protected tables, so even a non-superuser table owner is
subject to the policies (verified by `test_force_rls_binds_a_non_superuser_table_owner`, which moves
ownership to an unprivileged role inside a rolled-back transaction: 0 rows with FORCE, all rows
without).

## Security context

`backend/app/database.py`: the first statement of **every** transaction of a Session sets all
keys with `set_config(name, value, is_local => true)`:

| Setting | Meaning |
|---|---|
| `app.tenant_id`, `app.user_id` | authenticated user (API), job tenant / submitter (worker) |
| `app.job_id`, `app.lease_token` | the job whose lease the worker transaction holds |
| `app.session_hash` | SHA-256 of a presented session cookie (session lookup only) |

Values are transaction-local: after `COMMIT`/`ROLLBACK` they are gone, and the next transaction on a
pooled connection overwrites every key (empty = no access). The context lives on the Session
object (`session.info`), not in a global or thread-local, so concurrent requests in FastAPI's
thread pool cannot share it; a context change inside an open transaction is applied immediately.
A careless session-level `SET app.tenant_id` cannot leak either, because the per-transaction value
always overrides it. Missing context = every policy compares against NULL = **no rows**.

## Policies

Helper functions (schema `nexus_rls`, `EXECUTE` revoked from PUBLIC):
`tenant()`, `user_id()`, `job_id()`, `lease_token()`, `session_hash()` (inlinable SQL accessors),
and `SECURITY DEFINER` functions with a pinned `search_path` that read the authoritative tables
without RLS recursion: `user_is_active()`, `user_role()`, `is_admin()`, `is_reviewer()`,
`granted_document_ids()`, `job_document_id()`, `job_user_id()`, `user_document_access()`, plus
narrow bootstrap / queue functions (`demo_credentials`, `resolve_identity`, `find_user_by_email`,
`revoke_idp_sessions`, `soft_delete_document`, `claim_job`, `queue_stats`, `expire_documents`).

| Table | `nexus_app` (API) | `nexus_worker` (job scope) |
|---|---|---|
| `documents` | SELECT: tenant ∧ not deleted ∧ (admin ∨ active owner ∨ group grant). INSERT: own tenant, owner = me. UPDATE: owner / admin. No DELETE | ALL: only the document of the job it holds a valid lease for (deleted ones included, for purge) |
| `document_grants` | SELECT with the document; INSERT / DELETE by owner / admin | job document |
| `chunks`, `document_processing`, `document_tables`, `document_versions`, `extractions`, `review_events` | SELECT: `document_id IN (visible documents)` | job document |
| `chunk_embeddings` | SELECT: `chunk_id IN (visible chunks)` | chunks of the job document |
| `extraction_fields` | SELECT with the extraction; INSERT / UPDATE: reviewers | job document |
| `extractions` | UPDATE: reviewers / admins of a readable document | job document |
| `review_events` | INSERT: reviewers, as themselves | job document |
| `audit_events` | SELECT: admins (tenant) / own; INSERT: as yourself. Append-only | INSERT as the job submitter |
| `users` | own row (admins: tenant); identity sync updates the own row | the job submitter (read) |
| `user_identities`, `auth_sessions` | own rows / the session whose token hash was presented | no privilege |
| `processing_jobs` | own jobs, jobs of readable documents, all tenant jobs for admins | read own job by id; UPDATE requires the current lease token (fencing) |

Child tables use **uncorrelated** `IN (SELECT ...)` sub-queries against the parent table's own RLS;
PostgreSQL evaluates them once per statement as a hashed SubPlan, not once per row
(`test_rls_overhead_explain_uses_hashed_subplans`). The documents policy uses initPlan-cached
helper calls (`(SELECT nexus_rls.is_admin())`).

Two PostgreSQL subtleties shaped the design: (1) `UPDATE ... WHERE` also checks the SELECT policy
against the **new** row, so a soft delete (which makes the row invisible) goes through
`soft_delete_document()`, which re-applies the owner / admin rule; and a finished job no longer
carries a lease token, so the worker's SELECT policy on its job row is by id and the lease check
sits on UPDATE; (2) FK checks and cascades bypass RLS by design (integrity); they are not an
existence oracle because the RLS `WITH CHECK` of an insert fails first.

Tables without RLS (asserted by a test that fails when a new table appears without RLS or an
allowlist entry): `alembic_version`, `auth_login_states` (pre-authentication, tenant-less, single
use), `worker_heartbeats` (no tenant data).

## Worker scope

A worker claims a job through `nexus_rls.claim_job()` (definer; `FOR UPDATE SKIP LOCKED`). Every
later transaction sets `app.job_id` + `app.lease_token`; `job_document_id()` returns the document
only while that job is `running` with that token and an unexpired lease. A forged token, another
tenant or an expired lease open **nothing**. User-submitted jobs additionally re-check the
submitter's current access (`user_document_access`) before writing, so a revoked grant between
submission and execution fails the job (`authorization_revoked`).

## Verification (`backend/tests/test_rls_pg.py`, direct SQL as the runtime roles)

| # | Requirement | Test |
|---|---|---|
| 1 | Tenant A cannot read tenant B documents | `test_tenant_isolation_across_every_table` (every table, by count and by id probing) |
| 2 | Tenant A cannot inspect tenant B embeddings | `test_cross_tenant_embeddings_are_invisible_even_to_an_unfiltered_nearest_neighbour_query` |
| 3 | Forbidden rows cannot be updated | `test_forbidden_rows_cannot_be_updated_or_deleted` (0 rows) |
| 4 | Forbidden rows cannot be deleted | same (DELETE has no grant / 0 rows) |
| 5 | Reviewer permissions only where authorized | `test_reviewer_rights_apply_only_to_documents_the_reviewer_can_read` |
| 6 | Group removal applies immediately | `test_group_removal_and_account_disabling_apply_to_the_next_statement` |
| 7 | Reused pooled connections do not leak context | `test_security_context_is_transaction_local_and_never_leaks_through_the_pool` (pool size 1, same backend pid, after an exception, after a session-level SET), `test_concurrent_sessions_in_threads_keep_their_own_context` |
| 8 | Background jobs cannot bypass tenant permissions | `test_worker_role_is_confined_to_the_document_of_its_leased_job` |
| 9 | Direct SQL under the runtime role respects policies | all of the above; `test_no_context_means_no_rows_for_both_runtime_roles`, `test_inserts_into_another_tenant_or_as_another_user_are_rejected` |
| 10 | Service roles are not unrestricted paths | `test_worker_role_has_no_access_to_sessions_identities_or_password_hashes`, `test_runtime_roles_are_unprivileged_and_cannot_switch_role` (no `SET ROLE`, `row_security=off` fails), `test_app_role_cannot_claim_jobs_or_read_password_hashes`, `test_helper_functions_are_not_public` |
| — | Mutation: RLS blocks access when the application filter is missing | `test_rls_blocks_access_when_the_application_acl_is_missing[postgres]`; the same mutation **leaks** on SQLite (no RLS), proving the test is sensitive |
| — | Phase 1-3 behaviour under RLS | the whole pre-existing PostgreSQL API suite runs as `nexus_app` / `nexus_worker` |

Measured overhead: [performance-benchmarks.md](performance-benchmarks.md#row-level-security-overhead).

## Limitations

* RLS protects against application bugs (a missing filter, an unscoped query), not against a
  compromised API process: the API sets its own context, so code execution in the API can claim any
  user of any tenant. Credentials of the runtime roles must be treated as production secrets.
* The superuser / table owner (`nexus`) bypasses RLS by definition; it is the break-glass path and
  must not be used by services.
* Roles are cluster-wide; the downgrade of 0008 keeps them.
* Policies that call `SECURITY DEFINER` functions are not inlined; the measured cost is reported.
