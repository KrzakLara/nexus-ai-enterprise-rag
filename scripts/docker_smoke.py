"""Docker end-to-end smoke test: upload -> parse -> chunk -> embed -> retrieve -> answer -> cite ->
inspect -> download -> ACL denial -> audit, plus event-loop responsiveness during a large upload
and frontend availability. Exit code 1 if any check fails.

It WRITES (uploads two documents), so run it against a DISPOSABLE API instance, never the demo DB:

    docker compose up -d                       # main stack (db, api, frontend on :5173)
    docker compose exec -T db psql -U nexus -d postgres -c "CREATE DATABASE nexus_smoke_test"
    docker compose run -d --name nexus-smoke-api --no-deps -p 127.0.0.1:8001:8000       -e DATABASE_URL=postgresql+psycopg://nexus:demo_local_only@db:5432/nexus_smoke_test       -e STORAGE_DIR=/tmp/smoke_uploads api
    python scripts/docker_smoke.py             # --api http://127.0.0.1:8001 --frontend http://127.0.0.1:5173
    docker rm -f nexus-smoke-api
    docker compose exec -T db psql -U nexus -d postgres -c "DROP DATABASE nexus_smoke_test WITH (FORCE)"
"""
import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.tests.fixtures import KESTREL_FACT, KESTREL_PAGES, make_pdf  # noqa: E402

_args = argparse.ArgumentParser(description=__doc__.splitlines()[0])
_args.add_argument("--api", default="http://127.0.0.1:8001")
_args.add_argument("--frontend", default="http://127.0.0.1:5173")
ARGS = _args.parse_args()
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
API = ARGS.api.rstrip("/")
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)


def call(path, body=None, token=None, raw=None, content_type="application/json"):
    headers = {"Content-Type": content_type} if (body is not None or raw is not None) else {}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    req = urllib.request.Request(API + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            payload = r.read()
            return r.status, payload
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def login(email):
    status, body = call("/api/auth/login", {"email": email, "password": "DemoPass123!"})
    assert status == 200, body
    return json.loads(body)["access_token"]


def upload(token, filename, data, groups):
    """Upload; with PROCESSING_MODE=queue (Phase 4 Compose) the API answers 202 and a background
    worker processes the job: wait for it and return the same shape as the inline (201) answer."""
    boundary = uuid.uuid4().hex
    parts = [f"--{boundary}\r\nContent-Disposition: form-data; name=\"groups\"\r\n\r\n{groups}\r\n".encode(),
             f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
             "Content-Type: application/octet-stream\r\n\r\n".encode() + data + b"\r\n",
             f"--{boundary}--\r\n".encode()]
    status, body = call("/api/documents", token=token, raw=b"".join(parts),
                        content_type=f"multipart/form-data; boundary={boundary}")
    if status != 202:
        return status, body
    accepted = json.loads(body)
    job = accepted["job"]
    for _ in range(600):
        job = json.loads(call(f"/api/jobs/{job['id']}", token=token)[1])
        if job["status"] in ("completed", "failed", "cancelled"):
            break
        time.sleep(0.5)
    if job["status"] != "completed":
        return 500, json.dumps({"job": job}).encode()
    listed = {d["id"]: d for d in json.loads(call("/api/documents", token=token)[1])}
    doc = listed[accepted["id"]]
    result = job.get("result") or {}
    return 201, json.dumps({"id": accepted["id"], "title": doc["title"], "pages": result.get("pages"),
                            "fields_extracted": len(doc.get("fields") or {}),
                            "embedding_status": result.get("embedding_status", ""),
                            "processing": doc.get("intelligence"), "job": job}).encode()


def ask(token, question, **extra):
    status, body = call("/api/ask", {"question": question, "top_k": 5, **extra}, token)
    assert status == 200, body
    return json.loads(body)


health = json.loads(call("/api/health")[1])
check("health: postgres, 4 modes, hybrid default, model loaded at startup",
      health["database"] == "postgresql" and len(health["retrieval_modes"]) == 4
      and health["default_retrieval_mode"] == "hybrid" and health["embedding_model_loaded"])

fin, hr, oc, admin = (login(e) for e in ("finance@northstar.demo", "hr@northstar.demo",
                                         "finance@otherco.demo", "admin@northstar.demo"))
check("authentication: 4 personas logged in", True)
status, _ = call("/api/me", token="not-a-token")
check("invalid token rejected", status == 401, str(status))

pdf = make_pdf(KESTREL_PAGES)
status, body = upload(fin, "kestrel_agreement.pdf", pdf, "finance")
up = json.loads(body)
check("PDF upload parsed and embedded", status == 201 and up["pages"] == 3
      and up["embedding_status"].startswith("embedded:"), f"{status} {up.get('embedding_status')}")
doc_id = up["id"]

b = ask(fin, "What rebate does agreement KR-2026-77 grant?")
top = b["sources"][0] if b["sources"] else {}
check("uploaded doc retrieved by default (hybrid) mode, cited on page 3",
      b["retrieval"] == "hybrid" and b["mode"] == "extractive" and top.get("document_id") == doc_id
      and top.get("page") == 3 and KESTREL_FACT in b["answer"], f"{b['retrieval']} {b['mode']}")
b = ask(fin, "How much freight rebate do we get from Kestrel?", mode="semantic")
check("semantic retrieval finds the uploaded doc", any(s["document_id"] == doc_id for s in b["sources"])
      or b["mode"] == "no_evidence", f"{b['mode']} {[s['title'] for s in b['sources']]}")
semantic_raw = ask(fin, "How much freight rebate do we get from Kestrel?", mode="semantic",
                   apply_evidence_policy=False)
check("semantic retrieval (policy off) ranks the uploaded doc first",
      semantic_raw["sources"][0]["document_id"] == doc_id)

status, body = call(f"/api/documents/{doc_id}", token=fin)
detail = json.loads(body)
check("source inspection: page 3 holds the cited sentence", status == 200 and any(
    p["page"] == 3 and KESTREL_FACT in p["content"] for p in detail["pages"]))
status, body = call(f"/api/documents/{doc_id}/source", token=fin)
check("original download is byte-identical", status == 200 and body == pdf)

for name, tok in (("hr (other group)", hr), ("otherco (other tenant)", oc)):
    s1, _ = call(f"/api/documents/{doc_id}", token=tok)
    s2, _ = call(f"/api/documents/{doc_id}/source", token=tok)
    b = ask(tok, "What rebate does agreement KR-2026-77 grant?")
    check(f"denied for {name}: 404/404, no_evidence, no text",
          s1 == s2 == 404 and b["mode"] == "no_evidence" and "7 percent" not in json.dumps(b))

b = ask(fin, "Koliko dana unaprijed moramo najaviti raskid ugovora s dobavljačem?")
check("Croatian Atlas question: verbatim cited '90 days' from Atlas",
      b["mode"] == "extractive" and "90 days" in b["answer"]
      and any("Atlas" in s["title"] for s in b["sources"]), b["mode"])
b = ask(fin, "What is the company car policy for sales managers?")
check("no-evidence question abstains with no sources", b["mode"] == "no_evidence" and not b["sources"])
b = ask(fin, "What is the SILVER-IBIS-73 capex forecast?")
check("cross-tenant identifier refused", b["mode"] == "no_evidence"
      and b["diagnostics"]["evidence"]["reasons"] == ["identifier_not_found"])

status, body = call("/api/audit", token=admin)
actions = {(e["action"], e["document_id"]) for e in json.loads(body)}
check("audit trail has upload, view, download for the doc",
      {("upload", doc_id), ("document_view", doc_id), ("source_download", doc_id)} <= actions)
status, _ = call("/api/audit", token=fin)
check("audit endpoint admin-only", status == 403)
status, body = call("/api/evaluation/summary", token=fin)
check("evaluation summary served", status == 200 and json.loads(body)["security"]["total_violations"] == 0)

# Event loop not blocked: poll /api/health while a large upload is parsed and embedded.
big = "\n".join(f"Paragraph {i}. The logistics ledger entry {i} records shipment weight, route and "
                f"carrier for reconciliation in month {i % 12 + 1} of the Kestrel agreement." for i in range(2500))
latencies, done = [], threading.Event()


def poll():
    while not done.is_set():
        t = time.perf_counter()
        urllib.request.urlopen(API + "/api/health", timeout=60).read()
        latencies.append((time.perf_counter() - t) * 1000)
        time.sleep(0.1)


poller = threading.Thread(target=poll)
poller.start()
t0 = time.perf_counter()
status, body = upload(fin, "ledger.txt", big.encode(), "finance")
upload_ms = (time.perf_counter() - t0) * 1000
done.set()
poller.join()
emb = json.loads(body).get("embedding_status")
check("large upload embedded", status == 201 and emb.startswith("embedded:"), emb)
check("event loop stays responsive during upload (health max < 1 s)",
      latencies and max(latencies) < 1000,
      f"upload {upload_ms:.0f} ms, {len(latencies)} health calls, max {max(latencies):.0f} ms")

# ----------------------------------------------- Phase 3: document intelligence over HTTP
corpus = Path(__file__).resolve().parents[1] / "eval" / "docintel" / "documents"
rev = login("reviewer@northstar.demo")
status, body = upload(fin, "di-i07.pdf", (corpus / "di-i07.pdf").read_bytes(), "finance")
scan = json.loads(body)
check("scanned invoice: OCR + classification + extraction stored",
      status == 201 and scan["processing"]["ocr_pages"] == [1] and scan["processing"]["doc_type"] == "invoice",
      json.dumps(scan.get("processing")))
ext = json.loads(call(f"/api/documents/{scan['id']}/extraction", token=rev)[1])
codes = {i["code"] for f in ext["fields"] for i in f["issues"]}
check("scanned invoice with a wrong total is flagged (total_mismatch, needs_review)",
      ext["status"] == "needs_review" and "total_mismatch" in codes, ext["status"])
s1, _ = call(f"/api/extractions/{ext['id']}/approve", {"expected_version": ext["version"]}, rev)
s2, body = call(f"/api/extractions/{ext['id']}/reject", {"expected_version": ext["version"], "comment": "total is wrong"}, rev)
check("approval blocked by validation, rejection recorded", s1 == 409 and s2 == 200 and json.loads(body)["status"] == "rejected",
      f"{s1}/{s2}")
status, body = upload(fin, "di-i03.xlsx", (corpus / "di-i03.xlsx").read_bytes(), "finance")
xlsx = json.loads(body)
ext2 = json.loads(call(f"/api/documents/{xlsx['id']}/extraction", token=rev)[1])
s3, body = call(f"/api/extractions/{ext2['id']}/approve", {"expected_version": ext2["version"], "comment": "ok"}, rev)
check("XLSX invoice extracted and approved", status == 201 and s3 == 200 and json.loads(body)["status"] == "approved", f"{status}/{s3}")
s4, _ = call(f"/api/documents/{xlsx['id']}/extraction", token=hr)
s5, _ = call(f"/api/extractions/{ext2['id']}/approve", {"expected_version": 1}, oc)
s6, cbody = call("/api/intelligence/compare", {"document_ids": [xlsx["id"], doc_id]}, hr)
check("extraction, review and compare respect the ACL (404 for other group / tenant)",
      s4 == 404 and s5 == 404 and s6 == 404 and xlsx["id"] not in cbody.decode(), f"{s4}/{s5}/{s6}")
q = json.loads(call("/api/intelligence/query", {"type": "invalid_invoices"}, rev)[1])
check("rejected invoice excluded from the invalid-invoice query", all(m["document_id"] != scan["id"] for m in q["matches"]))
hist = json.loads(call(f"/api/extractions/{ext['id']}/history", token=rev)[1])["events"]
check("review history: process, reject", [e["action"] for e in hist] == ["process", "reject"])

with urllib.request.urlopen(ARGS.frontend.rstrip("/") + "/", timeout=30) as r:
    html = r.read().decode()
check("frontend (main stack) serves the app", r.status == 200 and 'id="root"' in html)

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
