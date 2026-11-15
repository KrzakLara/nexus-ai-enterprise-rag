"""Phase 4B PostgreSQL row-level security (defense in depth below the application ACL).

PostgreSQL only (a no-op on SQLite). Design: docs/postgresql-rls.md.

Roles (cluster-wide; created NOLOGIN when missing, LOGIN + password are provisioned separately by
scripts/provision_db_roles.py; this migration never stores a password):
    nexus_app          runtime role of the API. NOSUPERUSER, NOBYPASSRLS, owns nothing.
    nexus_worker       runtime role of background workers. Same attributes. Its policies only open
                       the ONE document of a job it currently holds a valid lease for.
    nexus_rls_definer  NOLOGIN, BYPASSRLS. Owns the SECURITY DEFINER helper functions only.
The migration / table-owner role (superuser `nexus` in Docker and CI) is the break-glass and
maintenance path and is never used by the API or the workers.

Security context: transaction-local settings (set_config(..., is_local => true)) written by the
application at the start of EVERY transaction (backend/app/database.py):
    app.tenant_id, app.user_id                   the authenticated user (API)
    app.job_id, app.lease_token                  the leased job (worker)
    app.session_hash                             SHA-256 of a presented session cookie
Missing context = no rows (every policy compares against NULL and fails closed).

FORCE ROW LEVEL SECURITY is set on every protected table, so a non-superuser table owner would be
subject to the policies too; only superusers and BYPASSRLS roles bypass them.

Revision ID: 0008_row_level_security
Revises: 0007_processing_jobs
Create Date: 2026-11-15
"""
from alembic import op

revision = "0008_row_level_security"
down_revision = "0007_processing_jobs"
branch_labels = None
depends_on = None

PROTECTED_TABLES = (
    "users", "user_identities", "auth_sessions", "documents", "document_grants", "document_versions",
    "chunks", "chunk_embeddings", "document_processing", "document_tables", "extractions",
    "extraction_fields", "review_events", "audit_events", "processing_jobs",
)
# Tables without tenant data: alembic_version, auth_login_states (pre-authentication, single use),
# worker_heartbeats (worker liveness). Asserted by backend/tests/test_rls_pg.py.
UNPROTECTED_TABLES = ("alembic_version", "auth_login_states", "worker_heartbeats")
SEQUENCES = ("audit_events_id_seq", "review_events_id_seq", "extraction_fields_id_seq",
             "document_grants_id_seq", "user_identities_id_seq")
DEFINER = "LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public"
JOB_DOC = "(SELECT nexus_rls.job_document_id())"
VISIBLE_DOCS = "(SELECT d.id FROM public.documents d)"
IS_ADMIN = "(SELECT nexus_rls.is_admin())"
IS_REVIEWER = "(SELECT nexus_rls.is_reviewer())"
ACTIVE = "(SELECT nexus_rls.user_is_active())"
MANAGEABLE_DOCS = (f"(SELECT d.id FROM public.documents d WHERE d.owner_id = nexus_rls.user_id() "
                   f"OR {IS_ADMIN})")

ROLES = r"""
DO $roles$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_app') THEN
    CREATE ROLE nexus_app NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_worker') THEN
    CREATE ROLE nexus_worker NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_rls_definer') THEN
    CREATE ROLE nexus_rls_definer NOLOGIN;
  END IF;
END
$roles$
"""

FUNCTIONS = [
    # --- context accessors: plain SQL, inlinable, SECURITY INVOKER
    """CREATE FUNCTION nexus_rls.tenant() RETURNS text LANGUAGE sql STABLE
       AS $f$ SELECT nullif(current_setting('app.tenant_id', true), '') $f$""",
    """CREATE FUNCTION nexus_rls.user_id() RETURNS text LANGUAGE sql STABLE
       AS $f$ SELECT nullif(current_setting('app.user_id', true), '') $f$""",
    """CREATE FUNCTION nexus_rls.job_id() RETURNS text LANGUAGE sql STABLE
       AS $f$ SELECT nullif(current_setting('app.job_id', true), '') $f$""",
    """CREATE FUNCTION nexus_rls.lease_token() RETURNS text LANGUAGE sql STABLE
       AS $f$ SELECT nullif(current_setting('app.lease_token', true), '') $f$""",
    """CREATE FUNCTION nexus_rls.session_hash() RETURNS text LANGUAGE sql STABLE
       AS $f$ SELECT nullif(current_setting('app.session_hash', true), '') $f$""",
    # --- membership of the CURRENT user, read from the authoritative tables (group removal and
    #     role changes apply to the very next statement). SECURITY DEFINER: bypasses RLS, so the
    #     documents policy can consult users / document_grants without policy recursion.
    f"""CREATE FUNCTION nexus_rls.user_is_active() RETURNS boolean {DEFINER}
       AS $f$ SELECT EXISTS (SELECT 1 FROM public.users u WHERE u.id = nexus_rls.user_id()
                             AND u.tenant_id = nexus_rls.tenant() AND u.status = 'active') $f$""",
    f"""CREATE FUNCTION nexus_rls.user_role() RETURNS text {DEFINER}
       AS $f$ SELECT u.role FROM public.users u WHERE u.id = nexus_rls.user_id()
              AND u.tenant_id = nexus_rls.tenant() AND u.status = 'active' $f$""",
    f"""CREATE FUNCTION nexus_rls.is_admin() RETURNS boolean {DEFINER}
       AS $f$ SELECT coalesce(nexus_rls.user_role() = 'admin', false) $f$""",
    f"""CREATE FUNCTION nexus_rls.is_reviewer() RETURNS boolean {DEFINER}
       AS $f$ SELECT coalesce(nexus_rls.user_role() IN ('admin', 'reviewer'), false) $f$""",
    f"""CREATE FUNCTION nexus_rls.granted_document_ids() RETURNS SETOF text {DEFINER}
       AS $f$ SELECT g.document_id FROM public.document_grants g
              JOIN public.documents d ON d.id = g.document_id
              JOIN public.users u ON u.id = nexus_rls.user_id() AND u.tenant_id = d.tenant_id
                                 AND u.status = 'active'
              WHERE d.tenant_id = nexus_rls.tenant()
                AND g.group_name IN (SELECT json_array_elements_text(u.groups)) $f$""",
    # --- worker job scope: the document / submitter of the job whose lease the transaction holds
    f"""CREATE FUNCTION nexus_rls.job_document_id() RETURNS text {DEFINER}
       AS $f$ SELECT j.document_id FROM public.processing_jobs j
              WHERE j.id = nexus_rls.job_id() AND j.lease_token = nexus_rls.lease_token()
                AND j.tenant_id = nexus_rls.tenant() AND j.status = 'running'
                AND j.locked_until > now() $f$""",
    f"""CREATE FUNCTION nexus_rls.job_user_id() RETURNS text {DEFINER}
       AS $f$ SELECT j.user_id FROM public.processing_jobs j
              WHERE j.id = nexus_rls.job_id() AND j.lease_token = nexus_rls.lease_token()
                AND j.tenant_id = nexus_rls.tenant() AND j.status = 'running'
                AND j.locked_until > now() $f$""",
    # Worker re-authorization: may the job's submitter (still) read / manage this document?
    f"""CREATE FUNCTION nexus_rls.user_document_access(p_user text, p_document text)
       RETURNS TABLE (access text, role text) {DEFINER}
       AS $f$ SELECT CASE
                WHEN u.id IS NULL OR d.id IS NULL OR d.deleted_at IS NOT NULL THEN 'none'
                WHEN u.role = 'admin' THEN 'admin'
                WHEN d.owner_id = u.id THEN 'owner'
                WHEN EXISTS (SELECT 1 FROM public.document_grants g WHERE g.document_id = d.id
                             AND g.group_name IN (SELECT json_array_elements_text(u.groups))) THEN 'read'
                ELSE 'none' END,
              u.role
              FROM (SELECT 1) AS one
              LEFT JOIN public.users u ON u.id = p_user AND u.tenant_id = nexus_rls.tenant()
                                       AND u.status = 'active'
              LEFT JOIN public.documents d ON d.id = p_document AND d.tenant_id = nexus_rls.tenant() $f$""",
    # --- authentication bootstrap (before any tenant context exists)
    f"""CREATE FUNCTION nexus_rls.demo_credentials(p_email text)
       RETURNS TABLE (user_id text, tenant_id text, password_hash text, status text) {DEFINER}
       AS $f$ SELECT u.id, u.tenant_id, u.password_hash, u.status FROM public.users u
              WHERE u.email = lower(btrim(p_email)) AND u.auth_source = 'local' $f$""",
    f"""CREATE FUNCTION nexus_rls.resolve_identity(p_issuer text, p_subject text)
       RETURNS TABLE (user_id text, tenant_id text, status text) {DEFINER}
       AS $f$ SELECT i.user_id, u.tenant_id, u.status FROM public.user_identities i
              JOIN public.users u ON u.id = i.user_id
              WHERE i.issuer = p_issuer AND i.subject = p_subject $f$""",
    f"""CREATE FUNCTION nexus_rls.find_user_by_email(p_email text)
       RETURNS TABLE (user_id text, tenant_id text, status text) {DEFINER}
       AS $f$ SELECT u.id, u.tenant_id, u.status FROM public.users u
              WHERE u.email = lower(btrim(p_email)) $f$""",
    """CREATE FUNCTION nexus_rls.revoke_idp_sessions(p_issuer text, p_sid text, p_sub text, p_reason text)
       RETURNS integer LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public
       AS $f$
       DECLARE n integer;
       BEGIN
         UPDATE public.auth_sessions s SET revoked_at = now(), revoked_reason = left(p_reason, 40)
          WHERE s.revoked_at IS NULL AND s.issuer = p_issuer
            AND ((p_sid IS NOT NULL AND s.idp_session_id = p_sid)
                 OR (p_sid IS NULL AND p_sub IS NOT NULL AND s.idp_subject = p_sub));
         GET DIAGNOSTICS n = ROW_COUNT;
         RETURN n;
       END $f$""",
    # --- durable queue (worker only)
    """CREATE FUNCTION nexus_rls.claim_job(p_worker text, p_lease_seconds integer,
                                         p_job_types text[] DEFAULT NULL, p_job_id text DEFAULT NULL)
       RETURNS SETOF public.processing_jobs
       LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public
       AS $f$
       DECLARE v_id text;
       BEGIN
         IF p_lease_seconds IS NULL OR p_lease_seconds < 5 OR p_lease_seconds > 3600 THEN
           RAISE EXCEPTION 'invalid lease duration';
         END IF;
         -- crash recovery: an expired lease means the holder died or stalled -> retry or dead-letter
         UPDATE public.processing_jobs j
            SET status = CASE WHEN j.attempts >= j.max_attempts THEN 'failed' ELSE 'retrying' END,
                error_code = 'lease_expired',
                error_message = 'The worker lease expired before the job finished (worker crash or stall).',
                locked_by = NULL, locked_until = NULL, lease_token = NULL, run_after = now(),
                finished_at = CASE WHEN j.attempts >= j.max_attempts THEN now() ELSE NULL END,
                updated_at = now()
          WHERE j.id IN (SELECT x.id FROM public.processing_jobs x
                          WHERE x.status = 'running' AND x.locked_until < now()
                          FOR UPDATE SKIP LOCKED);
         IF p_job_id IS NOT NULL THEN
           SELECT j.id INTO v_id FROM public.processing_jobs j
            WHERE j.id = p_job_id AND j.status IN ('queued', 'retrying') AND NOT j.cancel_requested
            FOR UPDATE SKIP LOCKED;
         ELSE
           SELECT j.id INTO v_id FROM public.processing_jobs j
            WHERE j.status IN ('queued', 'retrying') AND j.run_after <= now() AND NOT j.cancel_requested
              AND (p_job_types IS NULL OR j.job_type = ANY (p_job_types))
            ORDER BY j.priority, j.run_after, j.created_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1;
         END IF;
         IF v_id IS NULL THEN
           RETURN;
         END IF;
         RETURN QUERY
           UPDATE public.processing_jobs j
              SET status = 'running', attempts = j.attempts + 1, locked_by = left(p_worker, 120),
                  locked_until = now() + make_interval(secs => p_lease_seconds),
                  lease_token = replace(gen_random_uuid()::text, '-', ''),
                  heartbeat_at = now(), started_at = coalesce(j.started_at, now()),
                  last_started_at = now(), updated_at = now()
            WHERE j.id = v_id
           RETURNING j.*;
       END $f$""",
    # Soft delete. A plain UPDATE cannot do it under RLS: PostgreSQL checks the SELECT policy
    # (deleted_at IS NULL) against the NEW row. The function applies the same owner / admin rule
    # as documents_app_update for the CURRENT user context and touches exactly one row.
    """CREATE FUNCTION nexus_rls.soft_delete_document(p_document text)
       RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public
       AS $f$
       DECLARE n integer;
       BEGIN
         UPDATE public.documents d
            SET deleted_at = now(), deleted_by = nexus_rls.user_id(), lifecycle_state = 'deleted',
                updated_at = now()
          WHERE d.id = p_document AND d.tenant_id = nexus_rls.tenant() AND d.deleted_at IS NULL
            AND (nexus_rls.is_admin() OR (d.owner_id = nexus_rls.user_id() AND nexus_rls.user_is_active()));
         GET DIAGNOSTICS n = ROW_COUNT;
         RETURN n = 1;
       END $f$""",
    f"""CREATE FUNCTION nexus_rls.queue_stats()
       RETURNS TABLE (job_type text, status text, jobs bigint, oldest_seconds double precision) {DEFINER}
       AS $f$ SELECT j.job_type::text, j.status::text, count(*),
                     extract(epoch FROM now() - min(j.run_after))::double precision
              FROM public.processing_jobs j
              WHERE j.status IN ('queued', 'retrying', 'running', 'failed')
              GROUP BY 1, 2 $f$""",
    """CREATE FUNCTION nexus_rls.expire_documents(p_limit integer, p_max_attempts integer)
       RETURNS integer LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public
       AS $f$
       DECLARE r record; n integer := 0;
       BEGIN
         FOR r IN SELECT d.id, d.tenant_id FROM public.documents d
                   WHERE d.retention_until IS NOT NULL AND d.retention_until <= now()
                     AND d.deleted_at IS NULL
                   ORDER BY d.retention_until LIMIT p_limit FOR UPDATE SKIP LOCKED
         LOOP
           UPDATE public.documents SET deleted_at = now(), deleted_by = 'system:retention',
                  lifecycle_state = 'deleted', updated_at = now() WHERE id = r.id;
           INSERT INTO public.processing_jobs (id, tenant_id, user_id, document_id, job_type, status,
                  priority, attempts, max_attempts, run_after, idempotency_key, payload,
                  cancel_requested, created_at, updated_at)
           VALUES (gen_random_uuid()::text, r.tenant_id, 'system', r.id, 'purge', 'queued', 50, 0,
                   p_max_attempts, now(), 'purge:' || r.id,
                   json_build_object('reason', 'retention_expired'), false, now(), now())
           ON CONFLICT (tenant_id, idempotency_key) DO NOTHING;
           INSERT INTO public.audit_events (user_id, tenant_id, action, document_id, created_at)
           VALUES ('system', r.tenant_id, 'document_expired', r.id, now());
           n := n + 1;
         END LOOP;
         RETURN n;
       END $f$""",
]

DEFINER_FUNCTIONS = (
    "user_is_active()", "user_role()", "is_admin()", "is_reviewer()", "granted_document_ids()",
    "job_document_id()", "job_user_id()", "user_document_access(text, text)",
    "demo_credentials(text)", "resolve_identity(text, text)", "find_user_by_email(text)",
    "revoke_idp_sessions(text, text, text, text)", "claim_job(text, integer, text[], text)",
    "soft_delete_document(text)", "queue_stats()", "expire_documents(integer, integer)",
)
APP_FUNCTIONS = ("tenant()", "user_id()", "job_id()", "lease_token()", "session_hash()",
                 "user_is_active()", "user_role()", "is_admin()", "is_reviewer()",
                 "granted_document_ids()", "demo_credentials(text)", "resolve_identity(text, text)",
                 "find_user_by_email(text)", "revoke_idp_sessions(text, text, text, text)",
                 "soft_delete_document(text)", "queue_stats()")
WORKER_FUNCTIONS = ("tenant()", "user_id()", "job_id()", "lease_token()", "session_hash()",
                    "user_is_active()", "user_role()", "is_admin()", "is_reviewer()",
                    "granted_document_ids()", "job_document_id()", "job_user_id()",
                    "user_document_access(text, text)", "claim_job(text, integer, text[], text)",
                    "queue_stats()", "expire_documents(integer, integer)")

GRANTS = [
    "GRANT USAGE ON SCHEMA public TO nexus_app, nexus_worker, nexus_rls_definer",
    "GRANT SELECT ON alembic_version TO nexus_app, nexus_worker",
    # what the SECURITY DEFINER helpers read / write
    "GRANT SELECT ON users, documents, document_grants, user_identities, processing_jobs "
    "TO nexus_rls_definer",
    # soft_delete_document() reads the current user's role through is_admin()
    "GRANT UPDATE ON processing_jobs, documents, auth_sessions TO nexus_rls_definer",
    "GRANT SELECT ON auth_sessions TO nexus_rls_definer",
    "GRANT INSERT ON processing_jobs, audit_events TO nexus_rls_definer",
    "GRANT USAGE ON SEQUENCE audit_events_id_seq TO nexus_rls_definer",
    # ---- API (nexus_app): no DELETE on documents, chunks, embeddings or history; no column
    #      privilege on users.password_hash except INSERT (JIT provisioning writes the '!' marker)
    "GRANT SELECT (id, tenant_id, email, role, groups, status, auth_source, display_name, "
    "last_login_at, created_at) ON users TO nexus_app, nexus_worker",
    "GRANT INSERT (id, tenant_id, email, password_hash, role, groups, status, auth_source, "
    "display_name, last_login_at, created_at) ON users TO nexus_app",
    "GRANT UPDATE (email, role, groups, status, display_name, last_login_at) ON users TO nexus_app",
    "GRANT SELECT, INSERT, UPDATE ON user_identities TO nexus_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON auth_sessions TO nexus_app",
    "GRANT SELECT, INSERT, DELETE ON auth_login_states TO nexus_app",
    "GRANT SELECT, INSERT, UPDATE ON documents TO nexus_app",
    "GRANT SELECT, INSERT, DELETE ON document_grants TO nexus_app",
    "GRANT SELECT, INSERT ON document_versions TO nexus_app",
    "GRANT SELECT ON chunks, chunk_embeddings, document_processing, document_tables TO nexus_app",
    "GRANT SELECT, UPDATE ON extractions TO nexus_app",
    "GRANT SELECT, INSERT, UPDATE ON extraction_fields TO nexus_app",
    "GRANT SELECT, INSERT ON review_events, audit_events TO nexus_app",
    "GRANT SELECT, INSERT, UPDATE ON processing_jobs TO nexus_app",
    "GRANT SELECT ON worker_heartbeats TO nexus_app",
    # ---- worker (nexus_worker): job-scoped writes of derived data, no user or session tables
    "GRANT SELECT, UPDATE ON documents TO nexus_worker",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON chunks, chunk_embeddings, document_processing, "
    "document_tables, extractions, extraction_fields, document_versions, document_grants, "
    "review_events TO nexus_worker",
    "GRANT SELECT, INSERT ON audit_events TO nexus_worker",
    "GRANT SELECT, UPDATE ON processing_jobs TO nexus_worker",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON worker_heartbeats TO nexus_worker",
] + [f"GRANT USAGE, SELECT ON SEQUENCE {s} TO nexus_app, nexus_worker" for s in SEQUENCES]


def policies() -> list[str]:
    p: list[str] = []

    def add(name: str, table: str, command: str, role: str, using: str | None = None,
            check: str | None = None) -> None:
        sql = f"CREATE POLICY {name} ON public.{table} AS PERMISSIVE FOR {command} TO {role}"
        if using:
            sql += f" USING ({using})"
        if check:
            sql += f" WITH CHECK ({check})"
        p.append(sql)

    tenant = "tenant_id = nexus_rls.tenant()"
    me = "nexus_rls.user_id()"
    # users: own row (admins: their tenant); identity sync updates only the own row
    add("users_app_select", "users", "SELECT", "nexus_app", f"{tenant} AND (id = {me} OR {IS_ADMIN})")
    add("users_app_insert", "users", "INSERT", "nexus_app", check=f"{tenant} AND id = {me}")
    add("users_app_update", "users", "UPDATE", "nexus_app", f"{tenant} AND id = {me}", f"{tenant} AND id = {me}")
    add("users_worker_select", "users", "SELECT", "nexus_worker",
        f"{tenant} AND id = (SELECT nexus_rls.job_user_id())")
    add("user_identities_app", "user_identities", "ALL", "nexus_app",
        f"{tenant} AND user_id = {me}", f"{tenant} AND user_id = {me}")
    # sessions: the one whose token hash was presented, or the user's own sessions
    add("auth_sessions_app", "auth_sessions", "ALL", "nexus_app",
        f"token_hash = nexus_rls.session_hash() OR ({tenant} AND user_id = {me})",
        f"{tenant} AND user_id = {me}")
    # documents: the application ACL rule, evaluated by the database
    visible = (f"{tenant} AND deleted_at IS NULL AND ({IS_ADMIN} OR (owner_id = {me} AND {ACTIVE}) "
               "OR id IN (SELECT nexus_rls.granted_document_ids()))")
    add("documents_app_select", "documents", "SELECT", "nexus_app", visible)
    add("documents_app_insert", "documents", "INSERT", "nexus_app",
        check=f"{tenant} AND owner_id = {me} AND {ACTIVE}")
    add("documents_app_update", "documents", "UPDATE", "nexus_app",
        f"{tenant} AND deleted_at IS NULL AND ({IS_ADMIN} OR (owner_id = {me} AND {ACTIVE}))",
        f"{tenant} AND ({IS_ADMIN} OR owner_id = {me})")
    add("documents_worker", "documents", "ALL", "nexus_worker",
        f"{tenant} AND id = {JOB_DOC}", f"{tenant} AND id = {JOB_DOC}")
    # grants: visible with their document; only owner / admin may grant or revoke
    add("document_grants_app_select", "document_grants", "SELECT", "nexus_app", f"document_id IN {VISIBLE_DOCS}")
    add("document_grants_app_insert", "document_grants", "INSERT", "nexus_app", check=f"document_id IN {MANAGEABLE_DOCS}")
    add("document_grants_app_delete", "document_grants", "DELETE", "nexus_app", f"document_id IN {MANAGEABLE_DOCS}")
    # derived data: readable with the document, written only by the leased worker job
    for table in ("chunks", "document_processing", "document_tables", "document_versions",
                  "extractions", "review_events", "document_grants"):
        add(f"{table}_worker", table, "ALL", "nexus_worker", f"document_id = {JOB_DOC}", f"document_id = {JOB_DOC}")
    for table in ("chunks", "document_processing", "document_tables", "document_versions",
                  "extractions", "review_events"):
        add(f"{table}_app_select", table, "SELECT", "nexus_app", f"document_id IN {VISIBLE_DOCS}")
    add("chunk_embeddings_app_select", "chunk_embeddings", "SELECT", "nexus_app",
        "chunk_id IN (SELECT c.id FROM public.chunks c)")
    add("chunk_embeddings_worker", "chunk_embeddings", "ALL", "nexus_worker",
        "chunk_id IN (SELECT c.id FROM public.chunks c)", "chunk_id IN (SELECT c.id FROM public.chunks c)")
    add("extraction_fields_app_select", "extraction_fields", "SELECT", "nexus_app",
        "extraction_id IN (SELECT e.id FROM public.extractions e)")
    add("extraction_fields_worker", "extraction_fields", "ALL", "nexus_worker",
        "extraction_id IN (SELECT e.id FROM public.extractions e)",
        "extraction_id IN (SELECT e.id FROM public.extractions e)")
    # new versions: owner / admin only
    add("document_versions_app_insert", "document_versions", "INSERT", "nexus_app",
        check=f"{tenant} AND created_by = {me} AND document_id IN {MANAGEABLE_DOCS}")
    # human review: reviewers / admins of a readable document
    add("extractions_app_update", "extractions", "UPDATE", "nexus_app",
        f"document_id IN {VISIBLE_DOCS} AND {IS_REVIEWER}",
        f"{tenant} AND document_id IN {VISIBLE_DOCS} AND {IS_REVIEWER}")
    readable_extraction = "extraction_id IN (SELECT e.id FROM public.extractions e)"
    add("extraction_fields_app_insert", "extraction_fields", "INSERT", "nexus_app",
        check=f"{readable_extraction} AND {IS_REVIEWER}")
    add("extraction_fields_app_update", "extraction_fields", "UPDATE", "nexus_app",
        f"{readable_extraction} AND {IS_REVIEWER}", f"{readable_extraction} AND {IS_REVIEWER}")
    add("review_events_app_insert", "review_events", "INSERT", "nexus_app",
        check=f"{tenant} AND user_id = {me} AND document_id IN {VISIBLE_DOCS} AND {IS_REVIEWER}")
    # audit: append-only (no UPDATE / DELETE policy for any runtime role)
    add("audit_events_app_select", "audit_events", "SELECT", "nexus_app",
        f"{tenant} AND ({IS_ADMIN} OR user_id = {me})")
    add("audit_events_app_insert", "audit_events", "INSERT", "nexus_app", check=f"{tenant} AND user_id = {me}")
    job_user = "(SELECT nexus_rls.job_user_id())"
    add("audit_events_worker_select", "audit_events", "SELECT", "nexus_worker",
        f"{tenant} AND user_id = {job_user} AND document_id = {JOB_DOC}")
    add("audit_events_worker_insert", "audit_events", "INSERT", "nexus_worker",
        check=f"{tenant} AND user_id = {job_user}")
    # jobs: own jobs, jobs of readable documents, all tenant jobs for admins; workers only the
    # job whose lease token they hold (fencing)
    add("processing_jobs_app_select", "processing_jobs", "SELECT", "nexus_app",
        f"{tenant} AND (user_id = {me} OR {IS_ADMIN} OR (document_id IS NOT NULL AND document_id IN {VISIBLE_DOCS}))")
    add("processing_jobs_app_insert", "processing_jobs", "INSERT", "nexus_app",
        check=f"{tenant} AND user_id = {me} AND {ACTIVE} AND (document_id IS NULL OR document_id IN {VISIBLE_DOCS})")
    add("processing_jobs_app_update", "processing_jobs", "UPDATE", "nexus_app",
        f"{tenant} AND (user_id = {me} OR {IS_ADMIN})", f"{tenant} AND (user_id = {me} OR {IS_ADMIN})")
    # Reading its own job row is harmless; WRITING (and SELECT ... FOR UPDATE) requires the current
    # lease token on the existing row. (PostgreSQL also checks SELECT policies against the NEW row
    # of an UPDATE with a WHERE clause, and a finished job no longer carries a lease token.)
    add("processing_jobs_worker_select", "processing_jobs", "SELECT", "nexus_worker",
        f"{tenant} AND id = nexus_rls.job_id()")
    add("processing_jobs_worker_update", "processing_jobs", "UPDATE", "nexus_worker",
        f"{tenant} AND id = nexus_rls.job_id() AND lease_token = nexus_rls.lease_token()",
        f"{tenant} AND id = nexus_rls.job_id()")
    return p


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(ROLES)
    # Defensive: a pre-existing role must not carry elevated attributes.
    op.execute("ALTER ROLE nexus_app NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS")
    op.execute("ALTER ROLE nexus_worker NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS")
    op.execute("ALTER ROLE nexus_rls_definer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS")
    op.execute("CREATE SCHEMA IF NOT EXISTS nexus_rls")
    op.execute("REVOKE ALL ON SCHEMA nexus_rls FROM PUBLIC")
    op.execute("GRANT USAGE ON SCHEMA nexus_rls TO nexus_app, nexus_worker, nexus_rls_definer")
    for stmt in GRANTS:
        op.execute(stmt)
    for stmt in FUNCTIONS:
        op.execute(stmt)
    op.execute("REVOKE ALL ON ALL FUNCTIONS IN SCHEMA nexus_rls FROM PUBLIC")
    for fn in DEFINER_FUNCTIONS:
        op.execute(f"ALTER FUNCTION nexus_rls.{fn} OWNER TO nexus_rls_definer")
    # definer functions call the invoker accessors
    op.execute("GRANT EXECUTE ON FUNCTION nexus_rls.tenant(), nexus_rls.user_id(), nexus_rls.job_id(), "
               "nexus_rls.lease_token(), nexus_rls.session_hash(), nexus_rls.user_role() "
               "TO nexus_rls_definer")
    for fn in APP_FUNCTIONS:
        op.execute(f"GRANT EXECUTE ON FUNCTION nexus_rls.{fn} TO nexus_app")
    for fn in WORKER_FUNCTIONS:
        op.execute(f"GRANT EXECUTE ON FUNCTION nexus_rls.{fn} TO nexus_worker")
    for table in PROTECTED_TABLES:
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
    for stmt in policies():
        op.execute(stmt)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    rows = bind.exec_driver_sql(
        "SELECT tablename, policyname FROM pg_policies WHERE schemaname = 'public'").all()
    for table, name in rows:
        if table in PROTECTED_TABLES:
            op.execute(f'DROP POLICY "{name}" ON public."{table}"')
    for table in PROTECTED_TABLES:
        op.execute(f"ALTER TABLE public.{table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} DISABLE ROW LEVEL SECURITY")
    op.execute("DROP SCHEMA IF EXISTS nexus_rls CASCADE")
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM nexus_app, nexus_worker, nexus_rls_definer")
    op.execute("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM nexus_app, nexus_worker, nexus_rls_definer")
    # Roles are cluster-wide (other databases may use them) and are intentionally kept.
