"""Phase 4 end-to-end verification against a RUNNING Docker Compose stack (no mocks).

    docker compose -p nexus-p4 -f compose.yaml -f compose.oidc.yaml -f compose.observability.yaml up -d --build
    python scripts/e2e_phase4.py --project nexus-p4 --report eval/phase4/e2e-latest.json

Flow (each step is a PASS / FAIL check; exit code 1 if any fails):
  1  OIDC login through the frontend proxy and the real Keycloak login form (Authorization Code
     + PKCE handled by the backend); identity mapped to tenant + groups
  2  upload (cookie session + CSRF) -> 202 + durable job; original stored privately in MinIO
  3  the background worker parses / extracts / embeds; the document becomes searchable (hybrid)
  4  a reviewer (OIDC) approves the extraction
  5  row-level security on the live database (runtime role, no context / wrong user)
  6  unauthorized users (other group, other tenant) cannot read or find the document
  7  logs / metrics / traces identify the operation (Prometheus endpoints, Jaeger trace that spans
     nexus-api and nexus-worker)
  8  re-index; delete -> immediately gone from retrieval, purge removes the object
  9  logout (RP-initiated URL) and back-channel logout (Keycloak admin ends the user's sessions)
It WRITES to the stack (uploads, deletes); run it against a disposable Compose project.
"""
import argparse
import html
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.tests.fixtures import make_pdf  # noqa: E402

p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
p.add_argument("--frontend", default="http://localhost:5173")
p.add_argument("--keycloak", default="http://localhost:8080")
p.add_argument("--jaeger", default="http://localhost:16686")
p.add_argument("--project", default="nexus-p4")
p.add_argument("--files", default="compose.yaml,compose.oidc.yaml,compose.observability.yaml")
p.add_argument("--kc-admin", default=os.getenv("KEYCLOAK_ADMIN", "kc-local-admin"))
p.add_argument("--kc-admin-password", default=os.getenv("KEYCLOAK_ADMIN_PASSWORD", "kc-local-admin-secret"))
p.add_argument("--db-app-password", default=os.getenv("DB_APP_PASSWORD", "nexus-app-local-only"))
p.add_argument("--report", default="")
ARGS = p.parse_args()
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
PASSWORD = "DemoPass123!"  # synthetic local-only realm credential
results: list[dict] = []
timings: dict[str, float] = {}


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append({"check": name, "ok": bool(ok), "detail": detail[:300]})
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail[:200]}]" if detail else ""), flush=True)
    return ok


def compose(*args: str, input_: str | None = None) -> str:
    cmd = ["docker", "compose", "-p", ARGS.project] + [x for f in ARGS.files.split(",") for x in ("-f", f)] + list(args)
    out = subprocess.run(cmd, capture_output=True, text=True, input=input_, timeout=180,
                         cwd=Path(__file__).resolve().parents[1])
    return (out.stdout or "") + (out.stderr if out.returncode else "")


def in_container(service: str, code: str) -> str:
    return compose("exec", "-T", service, "python", "-c", code).strip()


def login(email: str) -> tuple[httpx.Client, dict]:
    """Browser-equivalent SSO login: frontend -> /api/auth/oidc/login -> Keycloak form -> callback."""
    client = httpx.Client(base_url=ARGS.frontend, follow_redirects=False, timeout=120)
    start = client.get("/api/auth/oidc/login", params={"return_to": "/"})
    assert start.status_code == 302, start.text[:200]
    with httpx.Client(follow_redirects=False, timeout=30) as browser:
        page = browser.get(start.headers["location"])
        action = html.unescape(re.search(r'<form[^>]+id="kc-form-login"[^>]+action="([^"]+)"', page.text).group(1))
        cookies = "; ".join(c.split(";", 1)[0] for c in page.headers.get_list("set-cookie"))
        posted = browser.post(action, data={"username": email, "password": PASSWORD, "credentialId": ""},
                              headers={"Cookie": cookies})
    callback = posted.headers["location"]
    done = client.get(callback)
    assert done.status_code == 302 and "auth_error" not in done.headers.get("location", ""), done.headers
    session = client.get("/api/auth/session").json()
    client.headers["X-CSRF-Token"] = session["csrf_token"]
    return client, session


def wait_job(client: httpx.Client, job_id: str, timeout: float = 300) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("completed", "failed", "cancelled"):
            return job
        time.sleep(1)
    return job


def main() -> int:
    marker = f"E2E-{uuid.uuid4().hex[:8].upper()}"
    # 1 ------------------------------------------------------------------ OIDC login
    t0 = time.perf_counter()
    finance, fs = login("finance@northstar.demo")
    timings["oidc_login_s"] = round(time.perf_counter() - t0, 3)
    check("1 OIDC login via Keycloak (PKCE, BFF cookie session)", fs["method"] == "session" and fs["auth_mode"] == "oidc",
          f"{fs['user']['email']} tenant={fs['user']['tenant']} groups={fs['user']['groups']}")
    check("1 identity mapped to tenant + groups from the IdP", fs["user"]["tenant"] == "northstar"
          and set(fs["user"]["groups"]) == {"finance", "legal"})
    check("1 no token in the browser-visible session payload", "access_token" not in json.dumps(fs))
    # 2 ------------------------------------------------------------------ upload
    pdf = make_pdf([[f"Agreement {marker} for logistics services.", "Counterparty: Kestrel Freight d.o.o.",
                     "End date: 2028-06-30", "Annual value: EUR 96,000", "Notice period: 60 days"],
                    [f"Clause 7. {marker} grants a freight rebate of 11 percent of annual spend."]])
    t0 = time.perf_counter()
    up = finance.post("/api/documents", data={"groups": "finance"},
                      files={"file": (f"{marker.lower()}.pdf", pdf, "application/pdf")})
    timings["upload_accept_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    ok = check("2 upload accepted asynchronously (202 + durable job)", up.status_code == 202, up.text[:200])
    if not ok:
        return finish()
    doc_id, job_id = up.json()["id"], up.json()["job"]["id"]
    head = in_container("api", f"""
import json; from backend.app.config import settings; from backend.app.storage import S3ObjectStore
s = S3ObjectStore(settings); o = s.head('tenants/northstar/documents/{doc_id}/v1/original')
print(json.dumps({{'size': o.size if o else None, 'sha256': o.sha256 if o else None}}))""")
    stored = json.loads(head.splitlines()[-1]) if head.strip().startswith("{") or "\n{" in head else {}
    check("2 original stored in private MinIO bucket under a server-generated key", (stored.get("size") or 0) == len(pdf),
          head[-200:])
    anon = in_container("api", f"""
import urllib.request, urllib.error
try: urllib.request.urlopen('http://minio:9000/nexus-documents/tenants/northstar/documents/{doc_id}/v1/original'); print('200')
except urllib.error.HTTPError as e: print(e.code)""")
    check("2 anonymous object access refused (private bucket)", anon.endswith("403"), anon[-50:])
    # 3 ------------------------------------------------------------------ worker
    t0 = time.perf_counter()
    job = wait_job(finance, job_id)
    timings["ingest_job_s"] = round(time.perf_counter() - t0, 2)
    check("3 worker completed ingest (parse, extract, embed)", job["status"] == "completed",
          f"attempts={job['attempts']} result={job.get('result')}")
    ask = finance.post("/api/ask", json={"question": f"What rebate does {marker} grant?", "mode": "hybrid"}).json()
    check("3 document searchable via hybrid RAG with a verbatim citation",
          any(s["document_id"] == doc_id for s in ask.get("sources", [])) and "11 percent" in ask.get("answer", ""),
          ask.get("mode", ""))
    # 4 ------------------------------------------------------------------ review
    reviewer, rs = login("reviewer@northstar.demo")
    ext = reviewer.get(f"/api/documents/{doc_id}/extraction").json()
    approved = reviewer.post(f"/api/extractions/{ext['id']}/approve",
                             json={"expected_version": ext["version"], "comment": "e2e", "override_reason": "end-to-end verification run"})
    check("4 reviewer (OIDC role mapping) approved the extraction", approved.status_code == 200
          and approved.json()["status"] == "approved", approved.text[:200])
    # 5 ------------------------------------------------------------------ RLS on the live database
    no_ctx = compose("exec", "-T", "-e", f"PGPASSWORD={ARGS.db_app_password}", "db", "psql", "-h", "127.0.0.1",
                     "-U", "nexus_app", "-d", "nexus", "-tAc", "SELECT count(*) FROM documents")
    check("5 runtime role without security context sees no rows", no_ctx.strip().splitlines()[-1] == "0", no_ctx[-80:])
    hr_id = in_container("api", "import uuid;print(uuid.uuid5(uuid.NAMESPACE_DNS,'hr@northstar.demo'))")
    wrong_user = compose("exec", "-T", "-e", f"PGPASSWORD={ARGS.db_app_password}", "db", "psql", "-h", "127.0.0.1",
                         "-U", "nexus_app", "-d", "nexus", "-tA", input_=(
                             "BEGIN; SELECT set_config('app.tenant_id','northstar',true), "
                             f"set_config('app.user_id','{hr_id.splitlines()[-1]}',true);\n"
                             f"SELECT 'visible=' || count(*) FROM documents WHERE id = '{doc_id}';\n"
                             f"SELECT 'chunks=' || count(*) FROM chunks WHERE document_id = '{doc_id}';\nCOMMIT;\n"))
    check("5 RLS hides the document and its chunks from a same-tenant user without the grant",
          "visible=0" in wrong_user and "chunks=0" in wrong_user, wrong_user.replace("\n", " ")[-120:])
    # 6 ------------------------------------------------------------------ unauthorized users
    for email in ("hr@northstar.demo", "finance@otherco.demo"):
        other, _ = login(email)
        denied = other.get(f"/api/documents/{doc_id}").status_code
        found = other.post("/api/ask", json={"question": f"What rebate does {marker} grant?", "mode": "hybrid"}).json()
        check(f"6 {email} cannot read or retrieve the document", denied == 404
              and doc_id not in {s["document_id"] for s in found.get("sources", [])} and "11 percent" not in found.get("answer", ""))
        other.close()
    # 7 ------------------------------------------------------------------ observability
    metrics_api = in_container("api", "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:9100/metrics').read().decode())")
    metrics_worker = in_container("worker", "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:9100/metrics').read().decode())")
    check("7 API metrics: request, retrieval and queue gauges", all(m in metrics_api for m in (
        "nexus_http_requests_total", 'nexus_retrieval_duration_seconds_count{mode="hybrid"}', "nexus_jobs_queue_depth")))
    check("7 worker metrics: job duration, stages, OCR/ingestion", all(m in metrics_worker for m in (
        'nexus_jobs_finished_total{job_type="ingest",outcome="completed"}', "nexus_processing_stage_duration_seconds",
        "nexus_document_ingestion_duration_seconds")))
    logs = compose("logs", "--no-color", "--since", "10m", "worker")
    job_lines = [line for line in logs.splitlines() if job_id in line and '"job_finished"' in line]
    check("7 structured worker log line for the job (JSON, no document text)", bool(job_lines) and marker not in logs,
          job_lines[0][-160:] if job_lines else "")
    trace_ok, detail = False, ""
    for _ in range(20):
        try:
            data = httpx.get(f"{ARGS.jaeger}/api/traces", params={"service": "nexus-worker", "limit": 50,
                                                                    "lookback": "1h"}, timeout=10).json()
            for trace in data.get("data", []):
                services = {p["serviceName"] for p in trace["processes"].values()}
                ops = {s["operationName"] for s in trace["spans"]}
                if {"nexus-api", "nexus-worker"} <= services and "job.execute" in ops:
                    trace_ok, detail = True, f"trace {trace['traceID'][:16]}: {len(trace['spans'])} spans, {sorted(services)}"
                    break
        except (httpx.HTTPError, ValueError) as exc:
            detail = type(exc).__name__
        if trace_ok:
            break
        time.sleep(3)
    check("7 one trace spans the API request and the worker (context crossed the queue)", trace_ok, detail)
    # 8 ------------------------------------------------------------------ reindex, delete
    re_idx = finance.post(f"/api/documents/{doc_id}/reindex", json={"force": True}).json()
    rj = wait_job(finance, re_idx["job"]["id"])
    check("8 re-index job completed", rj["status"] == "completed", json.dumps(rj.get("result")))
    gone = finance.delete(f"/api/documents/{doc_id}")
    after = finance.post("/api/ask", json={"question": f"What rebate does {marker} grant?", "mode": "hybrid"}).json()
    check("8 deleted document immediately absent from retrieval and reads",
          gone.status_code == 202 and finance.get(f"/api/documents/{doc_id}").status_code == 404
          and doc_id not in {s["document_id"] for s in after.get("sources", [])})
    purge = wait_job(finance, gone.json()["purge_job"]["id"])
    head_after = in_container("api", f"""
from backend.app.config import settings; from backend.app.storage import S3ObjectStore
print(S3ObjectStore(settings).head('tenants/northstar/documents/{doc_id}/v1/original'))""")
    check("8 purge job removed the object and derived data", purge["status"] == "completed"
          and head_after.splitlines()[-1] == "None", json.dumps(purge.get("result")))
    # 9 ------------------------------------------------------------------ logout
    out = reviewer.post("/api/auth/logout").json()
    check("9 RP-initiated logout URL returned, local session revoked",
          (out.get("logout_url") or "").startswith(f"{ARGS.keycloak}/realms/nexus/protocol/openid-connect/logout")
          and reviewer.get("/api/auth/session").status_code == 401)
    admin_token = httpx.post(f"{ARGS.keycloak}/realms/master/protocol/openid-connect/token", data={
        "grant_type": "password", "client_id": "admin-cli", "username": ARGS.kc_admin,
        "password": ARGS.kc_admin_password}).json()["access_token"]
    hdr = {"Authorization": f"Bearer {admin_token}"}
    uid = httpx.get(f"{ARGS.keycloak}/admin/realms/nexus/users", params={"email": "finance@northstar.demo", "exact": "true"},
                    headers=hdr).json()[0]["id"]
    httpx.post(f"{ARGS.keycloak}/admin/realms/nexus/users/{uid}/logout", headers=hdr).raise_for_status()
    revoked = None
    for _ in range(10):
        revoked = finance.get("/api/auth/session")
        if revoked.status_code == 401:
            break
        time.sleep(1)
    check("9 back-channel logout from Keycloak ended the BFF session", revoked.status_code == 401
          and revoked.json()["detail"]["code"] == "session_revoked", revoked.text[:120])
    return finish()


def finish() -> int:
    passed = sum(r["ok"] for r in results)
    summary = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "checks": len(results),
               "passed": passed, "failed": len(results) - passed, "timings": timings, "results": results}
    print(f"\n{passed}/{len(results)} checks passed; timings {timings}")
    if ARGS.report:
        Path(ARGS.report).parent.mkdir(parents=True, exist_ok=True)
        Path(ARGS.report).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
