"""Component benchmarks for Phase 4 (reproducible, synthetic data, disposable database).

    python scripts/benchmark_phase4.py --database-url postgresql+psycopg://nexus:...@host/nexus_bench_test \
        --report eval/phase4/benchmarks.json

Run it inside the API image (Linux, Tesseract, the embedding model) for the numbers in
docs/performance-benchmarks.md. The database name must end in _test or _eval; it is DROPPED and
re-created. Measures (wall-clock, this machine only, not a capacity claim):
  rls          the SAME retrieval functions (FTS, pgvector, hybrid) on the same synthetic corpus,
               connected as the migration role (RLS bypassed) vs nexus_app (RLS enforced)
  sessions     /api/auth/session through the full ASGI stack: cookie session vs demo bearer JWT
  embeddings   multilingual-e5-small passages/s (batch 32) and single-query latency on CPU
  parsing      Phase 3 corpus: parse time per format, OCR seconds per scanned page
  queue        claim + complete round trips per second (no handler work), 1 and 4 threads
"""
import argparse
import json
import os
import platform
import random
import statistics
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.app.database import set_security_context, user_context
from backend.app.db_roles import provision_roles, runtime_url
from backend.app.identity.demo import hash_password
from backend.app.migrate import migrate
from backend.app.models import Chunk, ChunkEmbedding, Document, DocumentGrant, User
from backend.evaluation.safety import assert_disposable_database

ROOT = Path(__file__).resolve().parents[1]
DIM = 384
WORDS = ("contract supplier invoice payment notice renewal termination freight rebate liability warranty "
         "budget salary policy travel allowance cloud availability service level penalty audit").split()


def pct(values: list[float], q: float) -> float:
    values = sorted(values)
    return round(values[min(len(values) - 1, int(round(q * (len(values) - 1))))], 3)


def stats(values: list[float]) -> dict:
    return {"n": len(values), "p50_ms": pct(values, 0.5), "p95_ms": pct(values, 0.95), "p99_ms": pct(values, 0.99),
            "mean_ms": round(statistics.fmean(values), 3)}


def recreate(url: str) -> str:
    parsed = assert_disposable_database(url)
    admin = create_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{parsed.database}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{parsed.database}"'))
    admin.dispose()
    return parsed.render_as_string(hide_password=False)


def seed(factory, docs_per_tenant: int, chunks_per_doc: int, rng: random.Random) -> None:
    with factory() as db:
        pwd = hash_password("BenchPass123!")
        users = [("b_admin", "northstar", "admin", ["finance"]), ("b_member", "northstar", "member", ["finance"]),
                 ("b_other", "otherco", "member", ["finance"])]
        for uid, tenant, role, groups in users:
            db.add(User(id=uid, tenant_id=tenant, email=f"{uid}@bench.test", role=role, groups=groups,
                        password_hash=pwd))
        db.flush()
        for tenant, owner in (("northstar", "b_admin"), ("otherco", "b_other")):
            for d in range(docs_per_tenant):
                doc_id = f"{tenant[:2]}-{d:05d}"
                db.add(Document(id=doc_id, tenant_id=tenant, owner_id=owner, title=f"Doc {d}", filename=f"{d}.txt",
                                storage_key=f"{doc_id}.txt", sha256="0" * 64, page_count=1, extracted_fields={},
                                lifecycle_state="ready"))
                db.flush()
                # half of the documents are granted to 'finance' (the member's group)
                db.add(DocumentGrant(document_id=doc_id, group_name="finance" if d % 2 == 0 else "hr"))
                for c in range(chunks_per_doc):
                    body = " ".join(rng.choice(WORDS) for _ in range(60)) + f" ID-{tenant}-{d}-{c}"
                    db.add(Chunk(id=f"{doc_id}-c{c}", document_id=doc_id, page_number=1, position=c, content=body))
                    vec = np.array([rng.gauss(0, 1) for _ in range(DIM)])
                    db.add(ChunkEmbedding(chunk_id=f"{doc_id}-c{c}", model_name="bench-model", model_revision="b",
                                          embedding_dim=DIM, normalized=True, content_sha256="0" * 64,
                                          embedding=[float(x) for x in vec / np.linalg.norm(vec)]))
            db.commit()


class FixedEmbedder:
    model_name = "bench-model"

    def __init__(self, rng):
        vec = np.array([rng.gauss(0, 1) for _ in range(DIM)])
        self.vector = vec / np.linalg.norm(vec)

    def embed_query(self, _text):
        return self.vector


def bench_rls(admin_factory, app_factory, rng, repeats: int) -> dict:
    from backend.app.retrieval import fts_search, hybrid_search, semantic_search

    embedder = FixedEmbedder(rng)
    out = {}
    with admin_factory() as db:
        users = {u.id: u for u in db.query(User)}
    for mode in ("lexical", "semantic", "hybrid"):
        # PAIRED comparison: every role / persona runs the SAME query list (an earlier version drew
        # new random queries per cell, so query mix noise dominated the RLS difference)
        queries = [" ".join(rng.choice(WORDS) for _ in range(4)) for _ in range(repeats + 5)]
        for role, factory in (("bypass_rls_migration_role", admin_factory), ("nexus_app_rls", app_factory)):
            for persona in ("b_admin", "b_member"):
                user = users[persona]
                times = []
                with factory() as db:
                    set_security_context(db, user_context(user.tenant_id, user.id))
                    for i, q in enumerate(queries):
                        t0 = time.perf_counter()
                        if mode == "lexical":
                            hits = fts_search(db, user, q, 10)
                        elif mode == "semantic":
                            hits = semantic_search(db, user, q, embedder, 10)
                        else:
                            hits = hybrid_search(db, user, q, embedder, 10, 50).hits
                        elapsed = (time.perf_counter() - t0) * 1000
                        if i >= 5:  # warm-up excluded
                            times.append(elapsed)
                        assert all(h.document_id.startswith("no") for h in hits)  # never the other tenant
                out[f"{mode}/{persona}/{role}"] = stats(times)
    for mode in ("lexical", "semantic", "hybrid"):
        for persona in ("b_admin", "b_member"):
            a = out[f"{mode}/{persona}/bypass_rls_migration_role"]["p50_ms"]
            b = out[f"{mode}/{persona}/nexus_app_rls"]["p50_ms"]
            out[f"{mode}/{persona}/rls_overhead_p50_percent"] = round((b - a) / a * 100, 1) if a else None
    return out


def bench_sessions(app_factory, repeats: int) -> dict:
    from fastapi.testclient import TestClient

    from backend.app import main
    from backend.app.database import get_db

    def override():
        with app_factory() as db:
            yield db
    main.app.dependency_overrides[get_db] = override
    client = TestClient(main.app)
    try:
        login = client.post("/api/auth/login", json={"email": "b_member@bench.test", "password": "BenchPass123!"})
        assert login.status_code == 200, login.text
        bearer = {"Authorization": "Bearer " + login.json()["access_token"]}
        out = {}
        for name, kwargs in (("cookie_session", {}), ("demo_bearer_jwt", {"headers": bearer})):
            times = []
            for i in range(repeats + 5):
                t0 = time.perf_counter()
                r = client.get("/api/auth/session", **kwargs)
                if i >= 5:
                    times.append((time.perf_counter() - t0) * 1000)
                assert r.status_code == 200
            out[name] = stats(times)
            if name == "cookie_session":
                client.cookies.clear()
        return out
    finally:
        main.app.dependency_overrides.clear()


def bench_embeddings(rng) -> dict:
    from backend.app.embeddings import get_embedder

    embedder = get_embedder()
    embedder.embed_query("warm-up")
    passages = [" ".join(rng.choice(WORDS) for _ in range(80)) for _ in range(256)]
    t0 = time.perf_counter()
    embedder.embed_passages(passages)
    elapsed = time.perf_counter() - t0
    q_times = []
    for _ in range(30):
        t1 = time.perf_counter()
        embedder.embed_query(" ".join(rng.choice(WORDS) for _ in range(8)))
        q_times.append((time.perf_counter() - t1) * 1000)
    return {"model": embedder.model_name, "passages": len(passages), "passages_per_second": round(len(passages) / elapsed, 1),
            "query": stats(q_times)}


def bench_parsing() -> dict:
    from backend.app.docintel.ocr import ocr_available
    from backend.app.docintel.parsing import parse_document

    corpus = ROOT / "eval" / "docintel"
    labels = [json.loads(line) for line in (corpus / "labels.jsonl").read_text(encoding="utf-8").splitlines()]
    per_format: dict[str, list[float]] = {}
    ocr_page_seconds = []
    for lab in labels:
        path = corpus / lab["file"]
        if lab.get("scanned_pages") and not ocr_available():
            continue
        t0 = time.perf_counter()
        parsed = parse_document(path.name, path.read_bytes())
        elapsed = (time.perf_counter() - t0) * 1000
        key = "pdf_scanned" if parsed.ocr_pages else path.suffix.lstrip(".")
        per_format.setdefault(key, []).append(elapsed)
        if parsed.ocr_pages:
            ocr_page_seconds.append(elapsed / 1000 / len(parsed.ocr_pages))
    return {"ocr_available": ocr_available(), "per_format": {k: stats(v) for k, v in per_format.items()},
            "ocr_seconds_per_scanned_page_mean": round(statistics.fmean(ocr_page_seconds), 2) if ocr_page_seconds else None}


def bench_queue(admin_factory, worker_factory, jobs: int) -> dict:
    from backend.app.jobs import queue

    out = {}
    for threads in (1, 4):
        with admin_factory() as db:
            db.execute(text("DELETE FROM processing_jobs"))
            for _ in range(jobs):
                queue.enqueue(db, tenant_id="northstar", user_id="b_admin", job_type="reindex", document_id="no-00000")
            db.commit()
        done, lock = [0], threading.Lock()

        def run():
            while True:
                with worker_factory() as db:
                    job = queue.claim(db, f"bench-{threading.get_ident()}", 60)
                    if job is None:
                        return
                    queue.finish(db, job, "completed", result={})
                    db.commit()
                with lock:
                    done[0] += 1
        t0 = time.perf_counter()
        workers = [threading.Thread(target=run) for _ in range(threads)]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
        elapsed = time.perf_counter() - t0
        out[f"{threads}_threads"] = {"jobs": done[0], "seconds": round(elapsed, 2),
                                     "claim_complete_per_second": round(done[0] / elapsed, 1)}
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database-url", default=os.getenv("BENCH_DATABASE_URL", ""))
    parser.add_argument("--docs-per-tenant", type=int, default=300)
    parser.add_argument("--chunks-per-doc", type=int, default=4)
    parser.add_argument("--repeats", type=int, default=60)
    parser.add_argument("--report", default="")
    parser.add_argument("--skip", default="", help="comma-separated: rls,sessions,embeddings,parsing,queue")
    args = parser.parse_args()
    skip = set(filter(None, args.skip.split(",")))
    rng = random.Random(42)
    url = recreate(args.database_url)
    migrate(url)
    admin = create_engine(url)
    provision_roles(admin, "bench-runtime-only-1", "bench-runtime-only-1")
    admin_factory = sessionmaker(admin, expire_on_commit=False)
    app_factory = sessionmaker(create_engine(runtime_url(url, "nexus_app", "bench-runtime-only-1")), expire_on_commit=False)
    worker_factory = sessionmaker(create_engine(runtime_url(url, "nexus_worker", "bench-runtime-only-1")),
                                  expire_on_commit=False)
    t0 = time.perf_counter()
    seed(admin_factory, args.docs_per_tenant, args.chunks_per_doc, rng)
    report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "environment": {"platform": platform.platform(), "python": platform.python_version(),
                              "cpu_count": os.cpu_count(), "processor": platform.processor() or None},
              "corpus": {"tenants": 2, "documents": 2 * args.docs_per_tenant,
                         "chunks": 2 * args.docs_per_tenant * args.chunks_per_doc, "seed_seconds": round(time.perf_counter() - t0, 1)}}
    if "rls" not in skip:
        report["rls"] = bench_rls(admin_factory, app_factory, rng, args.repeats)
    if "sessions" not in skip:
        from backend.app import main as app_main
        app_main.settings = replace(app_main.settings, auth_mode="demo")
        report["sessions"] = bench_sessions(app_factory, args.repeats * 2)
    if "queue" not in skip:
        report["queue"] = bench_queue(admin_factory, worker_factory, 300)
    if "embeddings" not in skip:
        report["embeddings"] = bench_embeddings(rng)
    if "parsing" not in skip:
        report["parsing"] = bench_parsing()
    print(json.dumps(report, indent=2))
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
