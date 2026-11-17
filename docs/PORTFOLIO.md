# Portfolio presentation

Ready-to-use descriptions of NEXUS AI. Every technology and number here is verified in the
repository ([EVALUATION_SUMMARY.md](EVALUATION_SUMMARY.md)). Keep the qualifiers ("synthetic",
"offline", "local") when adapting the text: they are what makes the claims defensible.

## Short project description

**NEXUS AI** is a permission-aware RAG and document-intelligence system: hybrid PostgreSQL
full-text + pgvector retrieval with cited, abstaining answers, OCR-based invoice and contract
extraction with human review, and tenant isolation enforced both in the retrieval SQL and by
PostgreSQL row-level security.

*(GitHub "About", 1 line):* Permission-aware hybrid RAG and document intelligence: pgvector + FTS
with RRF, cited answers with abstention, OCR extraction with human review, OIDC and PostgreSQL RLS.

## CV description

**NEXUS AI — Enterprise RAG & Document Intelligence** (personal project; Python, FastAPI,
PostgreSQL/pgvector, React/TypeScript, Docker)

- Built a permission-aware hybrid retrieval system (PostgreSQL full-text search + pgvector,
  Reciprocal Rank Fusion) with tenant/group authorization inside every retrieval query and
  PostgreSQL row-level security as a second layer; 0 authorization violations across 400 retrieval
  runs, a 1,946-request access matrix and 4,048 requests under load.
- Designed a document-intelligence pipeline (Tesseract OCR with deskew and table reconstruction,
  schema extraction with verbatim evidence, Decimal validation, human review) reaching field-level
  F1 0.971 on a held-out synthetic test set of contracts and invoices, with calibrated abstention
  and CI regression gates.
- Hardened it with OIDC (Keycloak, PKCE, backend-for-frontend), a durable PostgreSQL job queue with
  leases and fencing, S3-compatible storage, and Prometheus/Grafana/OpenTelemetry; verified by 567
  automated tests, Docker end-to-end (22/22), failure-injection (12/12) and k6 load tests.

## LinkedIn description

**NEXUS AI — Enterprise RAG & Document Intelligence**

A portfolio project on the hard parts of enterprise document AI: keeping retrieval inside each
user's permissions, answering only from evidence, and turning invoices and contracts into
reviewed, structured data.

- Hybrid retrieval (PostgreSQL full-text + pgvector, Reciprocal Rank Fusion) with citations and
  abstention when evidence is insufficient
- OCR and structured extraction with verbatim evidence for every field, validation and human review
- Multi-tenant access control enforced in the retrieval SQL and by PostgreSQL row-level security,
  OIDC single sign-on with Keycloak
- Durable background workers, S3-compatible storage, Prometheus / Grafana / OpenTelemetry
- Evaluated on held-out synthetic data: extraction field F1 0.971, 0 authorization violations,
  567 automated tests and CI quality gates

Stack: Python, FastAPI, PostgreSQL 16 + pgvector, React + TypeScript, Docker, Keycloak, MinIO.
Code: https://github.com/KrzakLara/nexus-ai-enterprise-rag

## Technical interview explanation (about two minutes)

1. **What it is (15 s).** "NEXUS AI lets people ask questions over contracts, invoices and policies
   and turns invoices and contracts into structured, reviewed data. I built it to work on the parts
   that simple RAG demos skip: permissions, grounding, evaluation and operations."
2. **Retrieval (30 s).** "Documents are parsed, chunked and embedded with a multilingual e5 model
   into pgvector, and indexed with PostgreSQL full-text search. A question runs both branches and
   fuses them with Reciprocal Rank Fusion. The key design point is that the tenant and group check
   is inside each SQL statement before the `LIMIT`, so an unauthorized chunk never enters ranking
   or a prompt. RLS enforces the same rule again in the database."
3. **Grounding (20 s).** "Answers are verbatim sentences with citations. An evidence policy,
   calibrated on a dev split, abstains if identifiers are missing or similarity is too low. It
   refuses all unanswerable test questions but also about one in five answerable ones; I report
   that trade-off rather than hide it."
4. **Document intelligence (25 s).** "Scans go through Tesseract with deskew and table
   reconstruction. A rule-based extractor fills Pydantic schemas where every value carries its
   evidence, and Decimal validation blocks approval of an invoice whose total does not add up.
   Field F1 is 0.971 on the held-out synthetic test set."
5. **Production concerns (20 s).** "Login is OIDC with PKCE through a backend-for-frontend, uploads
   go to MinIO and a durable PostgreSQL queue, and metrics and traces follow a document from the
   API into the worker."
6. **Evidence and limits (10 s).** "There are 567 tests, CI gates with zero tolerance for
   authorization leaks, and end-to-end, failure-injection and load tests. The data is synthetic
   and the LLM path is only tested with fakes, so it is a well-tested portfolio system, not a
   production deployment."

## Engineering talking points

1. **Authorization before ranking, not after.** Why post-filtering a global top-k leaks (into
   ranking, logs and prompts) and can return nothing for small tenants; the test that puts
   forbidden chunks at the global #1 of both branches; the mutation check that proves the test
   bites. Follow-up: why there is no ANN index yet and what benchmark must come first.
2. **Two independent enforcements: application ACL + PostgreSQL RLS.** Transaction-local security
   context with pooled connections, runtime roles without `BYPASSRLS`, worker scope bound to a
   lease token, and the surprise that PostgreSQL checks `SELECT` policies against the new row of an
   `UPDATE`. Cost: +10–20 ms per retrieval query for group members on this corpus.
3. **Honest retrieval evaluation.** 100 labelled questions, dev / test split, paired bootstrap CIs:
   hybrid is not significantly better than single modes here. Two plausible improvements (a
   stronger cross-language embedder, MarianMT query translation) were measured and rejected
   because overall quality or latency got worse.
4. **Abstention as a calibrated policy.** Why a raw cosine threshold is meaningless for e5 (narrow
   band), why sentence-level similarity plus exact identifiers worked better, and how the
   precision / recall trade-off (9/9 vs 8/41) was chosen on dev and reported on test.
5. **Durable jobs in PostgreSQL.** Upload and job in one transaction (no outbox), `SKIP LOCKED`
   claims, leases with fencing tokens, idempotent stages with checkpoints, and the race that only
   concurrency testing found (non-atomic duplicate detection, fixed with advisory locks and a
   mutation-checked test).

Backup topics: evidence grounding for extracted numbers (digits inside dates do not count), the
OCR pre-processing that took a scanned table from CER 0.27 to usable, the BFF trade-offs versus a
browser OIDC client, and what the load tests showed (CPU embeddings saturate one API process;
pool exhaustion now sheds load with 503 instead of 500).

## Positioning

Lead with: applied AI engineering (retrieval, grounding, extraction), retrieval system
architecture, enterprise security (multi-tenancy, RLS, OIDC), and evaluation discipline.
Do not claim: production use, real customers, business impact, live LLM quality, or scale beyond
what the load tests on one laptop show.
