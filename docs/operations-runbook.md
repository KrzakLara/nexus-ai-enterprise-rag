# Operations runbook (Phase 4)

Procedures for the Docker Compose deployment. Commands assume the project directory and a
project name (`-p nexus` in examples). **Practise destructive steps on a disposable project
first** (`scripts/backup_restore_drill.py`, `scripts/resilience_drill.py`).

## Configuration profiles

| Profile | How | Notes |
|---|---|---|
| Local demo | `docker compose up --build` | `AUTH_MODE=demo`, synthetic personas, MinIO, queue + worker |
| Local SSO | `-f compose.yaml -f compose.oidc.yaml` | Keycloak, `AUTH_MODE=oidc`, demo login disabled |
| Observability | `-f compose.observability.yaml` | Prometheus, Grafana, Jaeger, OTel on |
| Production-oriented | `APP_ENV=production` + [`deploy/production.env.example`](../deploy/production.env.example) | services refuse to start with demo auth, insecure cookies, local storage, inline processing, a privileged DB role, http IdP URLs or local-development secrets |

All credentials in the Compose files are **local-only defaults**; override them with an `.env` file
or environment variables. Never publish MinIO, PostgreSQL, Keycloak's admin console or the metrics
ports to untrusted networks (Compose binds published ports to 127.0.0.1).

## Start, upgrade and migrate

```bash
docker compose up -d --build          # migrate (one-shot) -> api + worker start after it succeeded
docker compose logs -f migrate        # Alembic + role provisioning + demo seed
docker compose ps                     # api / worker / keycloak healthchecks
```

**Upgrading an existing v0.3 demo database** (the default Compose project reuses the
`postgres_data` volume): migrations 0005-0008 are additive (new columns with defaults, new tables,
RLS). Existing document ids, chunks, embeddings, review history and audit data are unchanged;
existing originals stay on the local backend (the old `document_storage` volume is mounted
read-only). Recommended order:

1. back up first (next section);
2. `docker compose up -d --build` (migrate runs 0005-0008 and provisions the runtime roles);
3. optional: copy local originals to MinIO, dry run first:
   `docker compose exec -T -e MIGRATION_DATABASE_URL=postgresql+psycopg://nexus:<pw>@db:5432/nexus api python scripts/storage_backfill.py`
   then `--apply` ([object-storage-lifecycle.md](object-storage-lifecycle.md#migrating-existing-local-originals));
4. verify with `scripts/storage_reconcile.py` (report mode) and a few downloads.
The legacy volume is never modified by any of these steps.

## Backups

| What | How | Notes |
|---|---|---|
| Database | `docker compose exec -T db pg_dump -U nexus -d nexus -Fc > nexus.dump` | consistent snapshot; includes RLS policies, functions, grants and the Alembic revision |
| Roles | `docker compose exec -T db pg_dumpall -U nexus --roles-only > roles.sql` | cluster-wide `nexus_app`, `nexus_worker`, `nexus_rls_definer` (contains password hashes: store as a secret) |
| Objects | `mc mirror local/nexus-documents <backup-dir>/objects` (or S3 versioning / replication / AWS Backup) | object keys are immutable per version; enable bucket versioning in production so a delete can be recovered within the retention window |
| Order | database first, then objects | an object newer than the dump is an orphan after restore (reconcile reports it); a version row whose object is missing is an integrity problem — take objects after the dump, or use point-in-time features of both systems |

Encrypt backups at rest, restrict access, and test restores regularly. Retention of backups must
follow the document retention policy (a purged document survives in older backups until those
expire).

## Restore

```bash
docker compose exec -T db createdb -U nexus nexus_restored
docker compose exec -T db pg_restore -U nexus -d nexus_restored --exit-on-error < nexus.dump
# objects: mc mirror <backup-dir>/objects local/<bucket>
```

Restoring roles on a fresh cluster: `psql -f roles.sql` before `pg_restore`, then
`scripts/provision_db_roles.py` to set fresh runtime passwords. Point `DATABASE_URL` /
`WORKER_DATABASE_URL` at the restored database, run `scripts/migrate.py` (no-op if current), then
verify: `GET /api/health/ready`, `scripts/storage_reconcile.py`, a sample retrieval.

**Drill evidence**: `scripts/backup_restore_drill.py` restores into a new database and a new bucket
(the live ones are untouched) and verifies row counts, Alembic revision, forced RLS and policy
count, runtime-role visibility, the SHA-256 of every object, and an embedding rebuild. Results:
[performance-benchmarks.md](performance-benchmarks.md#backup-and-restore-drill).

## Failed jobs

```bash
curl -s -b cookies http://localhost:8000/api/jobs?status=failed       # or the UI: Retry buttons
curl -s -X POST -b cookies -H "X-CSRF-Token: $CSRF" http://localhost:8000/api/jobs/<id>/retry
```

`error_code` tells what to do: `storage_unavailable` / `database_unavailable` /
`embedding_unavailable` (fix the dependency, retry), `parse_failed` / `unsupported_format` (the file
is broken: delete or upload a new version), `checksum_mismatch` (the stored object differs from its
recorded hash: restore it from backup, never retry blindly), `authorization_revoked` (the submitter
lost access; resubmit as an authorized user), `lease_expired` (a worker died; usually recovered
automatically). Stuck `running` jobs recover by themselves after `JOB_LEASE_SECONDS`.

## Rebuilding embeddings

From the persisted chunks (no original needed):
`docker compose exec api python scripts/embed_backfill.py` (missing / stale only, idempotent), or
per document `POST /api/documents/{id}/reindex {"force": true}`, or for a tenant
`POST /api/admin/reindex` (admin; queued for the workers).

**Embedding model change**: vectors are keyed by `(chunk_id, model_name)` and the schema fixes
384 dimensions. A same-dimension model: set `EMBEDDING_MODEL` (+ `EMBEDDING_MODEL_REVISION`) on api
and worker, run `embed_backfill.py` (old vectors stay until removed), re-run the Phase 2 evaluation
(`scripts/evaluate.py`) before switching, then delete the old model's rows. A different dimension
needs a migration of `chunk_embeddings` and a full re-embedding; keep the old index until the
evaluation passes.

## Incidents

| Symptom | Check | Action |
|---|---|---|
| readiness 503, `database: unavailable` | `docker compose ps db`, DB logs | API answers 503 with Retry-After; workers back off; nothing to repair afterwards (verified by the resilience drill) |
| readiness 503, `object_storage: unavailable` | MinIO / S3 health, credentials | uploads answer 503 and store nothing; purge jobs retry |
| `workers: none_active` | `docker compose ps worker`, worker logs | uploads still queue; start / scale workers |
| queue backlog alert | `nexus_jobs_queue_depth`, oldest age | scale workers (`--scale worker=N`), check failures |
| auth failure spike | `nexus_auth_failures_total` by reason | `invalid_signature` / `unknown_key` after an IdP key change normally clears within `OIDC_JWKS_MIN_REFRESH_SECONDS`; sustained `invalid_credentials` = credential stuffing (rate-limit at the edge) |
| user lost access unexpectedly | IdP groups; `users.groups` | groups sync at login / every `SESSION_REVALIDATE_SECONDS`; check the `/tenants/...` group paths |
| leaked session suspected | — | IdP logout (back-channel ends local sessions) or revoke rows in `auth_sessions`; rotate `SESSION_ENCRYPTION_KEY` to invalidate stored refresh tokens |

## Secrets rotation

`SESSION_ENCRYPTION_KEY` (invalidates stored refresh tokens and CSRF tokens: users sign in again),
`OIDC_CLIENT_SECRET` (IdP + API together), runtime DB passwords (`provision_db_roles.py`, then
restart api / worker), MinIO / S3 application key (bucket-scoped), `JWT_SECRET` (demo only).
