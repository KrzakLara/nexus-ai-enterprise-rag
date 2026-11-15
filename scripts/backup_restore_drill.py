"""Backup and restore drill on a DISPOSABLE Docker Compose project (synthetic data only).

    python scripts/backup_restore_drill.py --project nexus-p4 --report eval/phase4/backup-restore-drill.json

1. backup:  pg_dump -Fc of the application database, pg_dumpall --roles-only (cluster roles and
            their grants context), mc mirror of the private bucket -> ./backups/<timestamp>/
2. restore: into a NEW database (nexus_restore_drill) and a NEW bucket (nexus-documents-restore);
            the live database and bucket are never modified
3. verify:  row counts per table, Alembic revision, RLS enabled + forced and policy count, runtime
            role sees nothing without context and the same documents as the original with one,
            every object present with its recorded SHA-256
4. rebuild: delete all embeddings in the restored database, regenerate them from the persisted
            chunks (scripts/embed_backfill.py) and compare with the original vectors
5. cleanup: drop the restored database and bucket (the backup files stay in ./backups, gitignored)
"""
import argparse
import json
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
p.add_argument("--project", default="nexus-p4")
p.add_argument("--files", default="compose.yaml,compose.oidc.yaml")
p.add_argument("--postgres-password", default="demo_local_only")
p.add_argument("--app-password", default="nexus-app-local-only")
p.add_argument("--minio-user", default="minio-local-admin")
p.add_argument("--minio-password", default="minio-local-admin-secret")
p.add_argument("--mc-image", default="cgr.dev/chainguard/minio-client@sha256:1f2e722c3a6761c537f760e49136c769dddc0cfe21819db0ce2600c1d0ac2c6a")
p.add_argument("--report", default="")
ARGS = p.parse_args()
TABLES = ("users", "documents", "document_grants", "document_versions", "chunks", "chunk_embeddings",
          "document_processing", "extractions", "extraction_fields", "review_events", "audit_events", "processing_jobs")
results, timings = [], {}


def check(name, ok, detail=""):
    results.append({"check": name, "ok": bool(ok), "detail": str(detail)[:400]})
    print(("PASS " if ok else "FAIL ") + name + (f"  [{str(detail)[:200]}]" if detail else ""), flush=True)


def compose(*args, stdin=None, stdout=None, env=None):
    cmd = ["docker", "compose", "-p", ARGS.project] + [x for f in ARGS.files.split(",") for x in ("-f", f)] + list(args)
    return subprocess.run(cmd, cwd=ROOT, stdin=stdin, stdout=stdout or subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=900, env=env)


def psql(db: str, sql: str, user: str = "nexus", password: str | None = None) -> str:
    args = ["exec", "-T"] + (["-e", f"PGPASSWORD={password}"] if password else []) + [
        "db", "psql", "-h", "127.0.0.1" if password else "/var/run/postgresql", "-U", user, "-d", db, "-tAc", sql]
    out = compose(*args)
    return out.stdout.decode().strip() if isinstance(out.stdout, bytes) else out.stdout.strip()


def mc(*args):
    env_args = ["-e", f"MC_HOST_local=http://{ARGS.minio_user}:{ARGS.minio_password}@minio:9000"]
    return subprocess.run(["docker", "run", "--rm", "--network", f"{ARGS.project}_default", *env_args,
                           "-v", f"{BACKUP}:/backup", ARGS.mc_image, *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=600)


def counts(db: str) -> dict:
    return {t: int(psql(db, f"SELECT count(*) FROM {t}") or -1) for t in TABLES}


BACKUP = ROOT / "backups" / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def main() -> int:
    BACKUP.mkdir(parents=True, exist_ok=True)
    before = counts("nexus")
    # 1 ------------------------------------------------------------------ backup
    t0 = time.perf_counter()
    with (BACKUP / "nexus.dump").open("wb") as handle:
        dump = compose("exec", "-T", "db", "pg_dump", "-U", "nexus", "-d", "nexus", "-Fc", stdout=handle)
    with (BACKUP / "roles.sql").open("wb") as handle:
        roles = compose("exec", "-T", "db", "pg_dumpall", "-U", "nexus", "--roles-only", stdout=handle)
    mirror = mc("mirror", "--overwrite", "local/nexus-documents", "/backup/objects")
    timings["backup_s"] = round(time.perf_counter() - t0, 2)
    size = (BACKUP / "nexus.dump").stat().st_size
    objects = sum(1 for f in (BACKUP / "objects").rglob("*") if f.is_file()) if (BACKUP / "objects").exists() else 0
    check("1 database dump, roles dump and object mirror created", dump.returncode == 0 and roles.returncode == 0
          and mirror.returncode == 0 and size > 0 and objects > 0, f"dump={size} B objects={objects} {mirror.stderr[-200:]}")
    # 2 ------------------------------------------------------------------ restore (new db, new bucket)
    t0 = time.perf_counter()
    psql("postgres", "DROP DATABASE IF EXISTS nexus_restore_drill WITH (FORCE)")
    psql("postgres", "CREATE DATABASE nexus_restore_drill")
    with (BACKUP / "nexus.dump").open("rb") as handle:
        restore = compose("exec", "-T", "db", "pg_restore", "-U", "nexus", "-d", "nexus_restore_drill",
                          "--exit-on-error", stdin=handle)
    mc("rb", "--force", "local/nexus-documents-restore")
    made = mc("mb", "local/nexus-documents-restore")
    back = mc("mirror", "--overwrite", "/backup/objects", "local/nexus-documents-restore")
    timings["restore_s"] = round(time.perf_counter() - t0, 2)
    check("2 restore into a new database and a new bucket", restore.returncode == 0 and made.returncode == 0
          and back.returncode == 0, (restore.stderr or b"")[-300:])
    # 3 ------------------------------------------------------------------ verify
    after = counts("nexus_restore_drill")
    check("3 row counts identical in every application table", after == before,
          {t: (before[t], after[t]) for t in TABLES if before[t] != after[t]} or before)
    rev = (psql("nexus", "SELECT version_num FROM alembic_version"),
           psql("nexus_restore_drill", "SELECT version_num FROM alembic_version"))
    check("3 Alembic revision restored", rev[0] == rev[1] and rev[0], rev)
    rls = psql("nexus_restore_drill", "SELECT count(*) FROM pg_class WHERE relnamespace = 'public'::regnamespace "
                                      "AND relkind = 'r' AND relrowsecurity AND relforcerowsecurity")
    policies = (psql("nexus", "SELECT count(*) FROM pg_policies"), psql("nexus_restore_drill", "SELECT count(*) FROM pg_policies"))
    check("3 row-level security enabled + forced, same policies", rls == "15" and policies[0] == policies[1], (rls, policies))
    no_ctx = psql("nexus_restore_drill", "SELECT count(*) FROM documents", user="nexus_app", password=ARGS.app_password)
    uid = psql("nexus", "SELECT id FROM users WHERE email = 'finance@northstar.demo'")
    q = (f"SELECT set_config('app.tenant_id','northstar',true), set_config('app.user_id','{uid}',true); "
         "SELECT count(*) FROM documents")
    seen = [psql(db, f"BEGIN; {q}; COMMIT;", user="nexus_app", password=ARGS.app_password).splitlines()
            for db in ("nexus", "nexus_restore_drill")]
    check("3 runtime role: no rows without context, same visible documents as the original",
          no_ctx == "0" and seen[0][-1:] == seen[1][-1:] and seen[0][-1:] != ["0"], (no_ctx, seen[0][-1:], seen[1][-1:]))
    env = ["-e", "ORIGINAL_DATABASE_URL=postgresql+psycopg://nexus:%s@db:5432/nexus" % ARGS.postgres_password,
           "-e", "RESTORED_DATABASE_URL=postgresql+psycopg://nexus:%s@db:5432/nexus_restore_drill" % ARGS.postgres_password,
           "-e", "RESTORE_BUCKET=nexus-documents-restore", "-e", f"S3_ACCESS_KEY_ID={ARGS.minio_user}",
           "-e", f"S3_SECRET_ACCESS_KEY={ARGS.minio_password}"]
    obj = compose("exec", "-T", *env, "api", "python", "scripts/verify_restore.py", "objects")
    check("3 every restored object matches its recorded SHA-256", obj.returncode == 0, obj.stdout[-300:])
    # 4 ------------------------------------------------------------------ rebuild embeddings
    t0 = time.perf_counter()
    psql("nexus_restore_drill", "DELETE FROM chunk_embeddings")
    rebuild = compose("exec", "-T", "-e", "DATABASE_URL=postgresql+psycopg://nexus:%s@db:5432/nexus_restore_drill"
                      % ARGS.postgres_password, "api", "python", "scripts/embed_backfill.py")
    timings["embedding_rebuild_s"] = round(time.perf_counter() - t0, 2)
    compare = compose("exec", "-T", *env, "api", "python", "scripts/verify_restore.py", "vectors")
    check("4 embeddings rebuilt from persisted chunks equal the originals", rebuild.returncode == 0
          and compare.returncode == 0, (rebuild.stdout[-160:], compare.stdout[-200:]))
    # 5 ------------------------------------------------------------------ cleanup
    psql("postgres", "DROP DATABASE IF EXISTS nexus_restore_drill WITH (FORCE)")
    mc("rb", "--force", "local/nexus-documents-restore")
    passed = sum(r["ok"] for r in results)
    summary = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "backup_dir": str(BACKUP.relative_to(ROOT)),
               "checks": len(results), "passed": passed, "failed": len(results) - passed, "timings": timings,
               "row_counts": before, "results": results}
    print(f"\n{passed}/{len(results)} checks passed; {timings}")
    if ARGS.report:
        Path(ARGS.report).parent.mkdir(parents=True, exist_ok=True)
        Path(ARGS.report).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
