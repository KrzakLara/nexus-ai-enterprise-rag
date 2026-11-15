"""Failure-injection drill against a DISPOSABLE Docker Compose project (real processes, real outages).

    docker compose -p nexus-p4 -f compose.yaml -f compose.oidc.yaml -f compose.drill.yaml up -d --build
    python scripts/resilience_drill.py --project nexus-p4 --report eval/phase4/resilience-latest.json

compose.drill.yaml shortens JOB_LEASE_SECONDS so a killed worker's job is recovered within the
drill. Scenarios:
  A  worker killed (SIGKILL) in the middle of an OCR job -> after restart the expired lease is
     recovered, the job re-runs (attempt 2) and completes; no duplicate chunks / documents
  B  PostgreSQL paused -> readiness 503 and API 503 (no hang, no 500 with details); recovery after
     unpause without restarting anything
  C  object storage stopped -> upload 503 with nothing stored; readiness reports object_storage;
     uploads work again after restart
  D  two workers + duplicate submissions -> every job executes exactly once
  E  document deleted while its ingest job runs -> job cancelled, purge completes, nothing left
Never run against a project that holds data you want to keep.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
p.add_argument("--project", default="nexus-p4")
p.add_argument("--files", default="compose.yaml,compose.oidc.yaml,compose.drill.yaml")
p.add_argument("--api", default="http://localhost:8000")
p.add_argument("--keycloak", default="http://localhost:8080")
p.add_argument("--report", default="")
ARGS = p.parse_args()
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
results: list[dict] = []
observations: dict = {}
SCANNED = ROOT / "eval" / "docintel" / "documents" / "di-c06.pdf"   # scanned: OCR on every page


def check(name, ok, detail=""):
    results.append({"check": name, "ok": bool(ok), "detail": str(detail)[:300]})
    print(("PASS " if ok else "FAIL ") + name + (f"  [{str(detail)[:200]}]" if detail else ""), flush=True)


def compose(*args, timeout=300):
    cmd = ["docker", "compose", "-p", ARGS.project] + [x for f in ARGS.files.split(",") for x in ("-f", f)] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=ROOT)


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)


def token(email):
    r = httpx.post(f"{ARGS.keycloak}/realms/nexus/protocol/openid-connect/token", data={
        "grant_type": "password", "client_id": "nexus-loadtest", "client_secret": "nexus-loadtest-local-dev-secret",
        "username": email, "password": "DemoPass123!", "scope": "openid"}, timeout=20)
    r.raise_for_status()
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def api(method, path, headers, **kw):
    return httpx.request(method, ARGS.api + path, headers=headers, timeout=kw.pop("timeout", 30), **kw)


def wait_job(headers, job_id, timeout=300, until=("completed", "failed", "cancelled")):
    deadline, job = time.time() + timeout, {}
    while time.time() < deadline:
        r = api("GET", f"/api/jobs/{job_id}", headers)
        if r.status_code == 200:
            job = r.json()
            if job["status"] in until:
                return job
        time.sleep(1)
    return job


def upload(headers, name, data, groups="finance"):
    return api("POST", "/api/documents", headers, data={"groups": groups},
               files={"file": (name, data, "application/pdf")}, timeout=60)


def worker_containers():
    out = docker("ps", "--filter", f"label=com.docker.compose.project={ARGS.project}",
                 "--filter", "label=com.docker.compose.service=worker", "--format", "{{.Names}}").stdout
    return [n for n in out.split() if n]


def db_count(sql):
    out = compose("exec", "-T", "db", "psql", "-U", "nexus", "-d", "nexus", "-tAc", sql).stdout.strip()
    return int(out.splitlines()[-1]) if out else -1


def scenario_a(h):
    pdf = SCANNED.read_bytes()
    r = upload(h, "drill-scanned.pdf", pdf)
    job_id, doc_id = r.json()["job"]["id"], r.json()["id"]
    deadline = time.time() + 120
    while time.time() < deadline:  # wait until the worker is inside the OCR / parsing stage
        job = api("GET", f"/api/jobs/{job_id}", h).json()
        if job["status"] == "running" and job.get("stage") == "parsing":
            break
        time.sleep(0.3)
    victims = worker_containers()
    killed_at = time.time()
    for name in victims:
        docker("kill", "--signal", "KILL", name)
    check("A worker processes killed mid-OCR (SIGKILL)", bool(victims) and job.get("stage") == "parsing",
          f"{victims} stage={job.get('stage')}")
    compose("up", "-d", "--no-deps", "worker")
    job = wait_job(h, job_id, timeout=240)
    observations["A_recovery_seconds"] = round(time.time() - killed_at, 1)
    check("A job recovered after lease expiry and completed on attempt 2",
          job.get("status") == "completed" and job.get("attempts") == 2, f"{job.get('status')} attempts={job.get('attempts')}")
    pages = api("GET", f"/api/documents/{doc_id}", h).json()
    chunks = db_count(f"SELECT count(*) FROM chunks WHERE document_id = '{doc_id}'")
    distinct = db_count(f"SELECT count(DISTINCT (page_number, position)) FROM chunks WHERE document_id = '{doc_id}'")
    same = db_count(f"SELECT count(*) FROM documents WHERE deleted_at IS NULL AND sha256 = "
                    f"(SELECT sha256 FROM documents WHERE id = '{doc_id}') AND owner_id = "
                    f"(SELECT owner_id FROM documents WHERE id = '{doc_id}')")
    check("A no duplicate chunks or documents after the retry", chunks == distinct > 0 and same == 1
          and pages.get("lifecycle_state") == "ready", f"chunks={chunks} distinct={distinct} documents={same}")
    api("DELETE", f"/api/documents/{doc_id}", h)


def scenario_b(h):
    docker("pause", f"{ARGS.project}-db-1")
    t0 = time.time()
    try:
        ready = httpx.get(ARGS.api + "/api/health/ready", timeout=30)
        listing = api("GET", "/api/documents", h, timeout=30)
        observations["B_ready_status_during_outage"] = ready.status_code
        observations["B_request_seconds_during_outage"] = round(time.time() - t0, 1)
        check("B PostgreSQL paused: readiness 503, API 503 (no 500, no details)",
              ready.status_code == 503 and listing.status_code == 503 and "psycopg" not in listing.text,
              f"ready={ready.status_code} documents={listing.status_code}")
        live = httpx.get(ARGS.api + "/api/health/live", timeout=10)
        check("B liveness unaffected by the dependency outage", live.status_code == 200)
    finally:
        docker("unpause", f"{ARGS.project}-db-1")
    deadline, ok = time.time() + 60, False
    while time.time() < deadline and not ok:
        ok = httpx.get(ARGS.api + "/api/health/ready", timeout=10).status_code == 200
        time.sleep(1)
    observations["B_recovery_seconds"] = round(time.time() - t0, 1)
    check("B recovered without restarts", ok and api("GET", "/api/documents", h).status_code == 200)


def scenario_c(h):
    before = db_count("SELECT count(*) FROM documents")
    compose("stop", "minio")
    try:
        r = upload(h, "drill-c.pdf", b"%PDF-1.4\n1 0 obj << >> endobj\ntrailer << >>\n%%EOF")
        ready = httpx.get(ARGS.api + "/api/health/ready", timeout=60).json()
        check("C storage down: upload 503, nothing stored", r.status_code == 503
              and db_count("SELECT count(*) FROM documents") == before, f"{r.status_code} {r.text[:120]}")
        check("C readiness reports object storage unavailable", ready["checks"].get("object_storage") == "unavailable")
    finally:
        compose("start", "minio")
    time.sleep(5)
    pdf = (ROOT / "eval" / "docintel" / "documents" / "di-c01.pdf").read_bytes()
    r = upload(h, "drill-c-after.pdf", pdf)
    job = wait_job(h, r.json()["job"]["id"]) if r.status_code == 202 else {}
    check("C uploads work again after storage restart", job.get("status") == "completed", r.status_code)
    if r.status_code == 202:
        api("DELETE", f"/api/documents/{r.json()['id']}", h)


def scenario_d(h):
    compose("up", "-d", "--no-deps", "--scale", "worker=2", "worker")
    time.sleep(15)
    docs = [d["id"] for d in api("GET", "/api/documents", h).json()][:8]
    jobs = []
    for doc in docs:
        for _ in range(2):  # duplicate submission
            r = api("POST", f"/api/documents/{doc}/reindex", h, json={"force": True})
            jobs.append(r.json()["job"]["id"])
    unique = sorted(set(jobs))
    finished = [wait_job(h, j, timeout=300) for j in unique]
    check("D duplicate submissions deduplicated (one job per document)", len(unique) == len(docs), f"{len(jobs)} -> {len(unique)}")
    check("D every job executed exactly once across two workers",
          all(j.get("status") == "completed" and j.get("attempts") == 1 for j in finished),
          [(j.get("status"), j.get("attempts")) for j in finished])
    compose("up", "-d", "--no-deps", "--scale", "worker=1", "worker")


def scenario_e(h):
    r = upload(h, "drill-e.pdf", SCANNED.read_bytes())
    job_id, doc_id = r.json()["job"]["id"], r.json()["id"]
    wait_job(h, job_id, timeout=60, until=("running", "completed", "failed"))
    gone = api("DELETE", f"/api/documents/{doc_id}", h)
    job = wait_job(h, job_id, timeout=240)
    purge = wait_job(h, gone.json()["purge_job"]["id"], timeout=240)
    chunks = db_count(f"SELECT count(*) FROM chunks WHERE document_id = '{doc_id}'")
    check("E deletion during processing: ingest cancelled or finished, purge completed, nothing left",
          job.get("status") in ("cancelled", "completed") and purge.get("status") == "completed" and chunks == 0,
          f"ingest={job.get('status')}/{job.get('error_code')} purge={purge.get('status')} chunks={chunks}")


def main() -> int:
    h = token("finance@northstar.demo")
    for name, fn in (("A", scenario_a), ("B", scenario_b), ("C", scenario_c), ("D", scenario_d), ("E", scenario_e)):
        try:
            fn(h if name not in ("D",) else token("admin@northstar.demo"))
        except Exception as exc:  # noqa: BLE001 - a crashed scenario is a failed check, not a crash
            check(f"{name} scenario raised", False, f"{type(exc).__name__}: {exc}")
        h = token("finance@northstar.demo")
    passed = sum(r["ok"] for r in results)
    summary = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "checks": len(results),
               "passed": passed, "failed": len(results) - passed, "observations": observations, "results": results}
    print(f"\n{passed}/{len(results)} checks passed; {observations}")
    if ARGS.report:
        Path(ARGS.report).parent.mkdir(parents=True, exist_ok=True)
        Path(ARGS.report).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
