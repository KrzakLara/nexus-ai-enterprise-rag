# Demo walk-through (v0.2.0)

A 10-minute script for showing NEXUS AI to a reviewer. Everything runs locally with `docker compose up --build`, with no API key. All companies and documents are fictional.

## 1. Start and check

```bash
docker compose up --build                 # first start downloads the ~470 MB embedding model
curl -s localhost:8000/api/health         # "default_retrieval_mode": "hybrid", "embedding_model_loaded": true
python scripts/demo_scenarios.py          # expected: 9 PASS, 1 KNOWN, exit 0
```

Open http://localhost:5173 and sign in as `finance@northstar.demo` (password `DemoPass123!`).

## 2. What to show, in order

| Step | Action in the UI | What it demonstrates |
|---|---|---|
| 1 | Quick prompt **1 · Exact identifier** (`NS-IT-2026-017`, Keyword) | PostgreSQL FTS finds the exact agreement ID; the source panel shows `ts_rank_cd` |
| 2 | Quick prompt **2 · Paraphrase** (Semantic) | Semantic search without shared keywords. With the shipped policy, paraphrases like this can **abstain**: demo scenario 2b, *"What uptime does the cloud provider promise?"*, has best sentence cosine 0.804 < 0.835. This is the documented false abstention: explain the precision/recall trade-off chosen on dev |
| 3 | Quick prompt **3 · Fusion** (Hybrid) | RRF: the source panel shows the lexical and semantic ranks and the RRF score (a rank statistic, not a confidence) |
| 4 | Quick prompt **4 · Croatian question** | Croatian question over a Croatian policy; verbatim quote with `[S1]` |
| 5 | Quick prompt **5 · Cross-language** | Croatian question, English Atlas contract: *"…at least 90 days before the current end date."* |
| 6 | Quick prompt **6 · Abstains** | `no_evidence`: nothing is cited and nothing would be sent to an LLM |
| 7 | Quick prompt **7 · Blocked** | Another tenant's identifier: `identifier_not_found`, no Otherco text |
| 8 | Switch to `hr@northstar.demo` and repeat prompt 5 | Same question, no Atlas grant: no Atlas text in the answer or the sources |
| 9 | **Upload** a PDF or DOCX as finance (group `finance`) | The notice reports pages and embedding status. Ask about it; open the cited source and download the original |
| 10 | **Access & Audit** as `admin@northstar.demo` | Tenant-scoped audit events for the upload, view, download and questions |
| 11 | **Evaluation** | Stored benchmark: four modes on the held-out test split, ablation, abstention and zero authorization violations |

## 3. Talking points (honest findings)

- **No mode is significantly better than another** on the test split. Every paired bootstrap CI for nDCG@5 contains 0. Hybrid is the default because it is robust across categories, not because it wins.
- **Cross-language** questions are the weakest category for every mode. Query translation was measured and **not adopted**: it helped cross-language questions but lowered overall quality and added ~0.5 s per query.
- The evidence policy refuses 9/9 unanswerable test questions, but also 8/41 answerable ones.
- **Authorization lives in the SQL** of every retrieval branch, before `ORDER BY … LIMIT`. An independent oracle checked 400 retrieval runs and 400 API responses and found 0 violations.

## 4. Write-path smoke test (optional)

`python scripts/docker_smoke.py` uploads documents, so it must run against a **disposable** API instance. The exact commands are in the script's docstring. Expected output: `27/27 checks passed` (Phases 1–3).

## 4b. Document intelligence (Phase 3)

Sign in as `reviewer@northstar.demo` (or `procurement@northstar.demo` for read-only), then open **Document Intelligence**:

1. **di-i06** (scanned invoice): the page header shows the OCR engine, its mean word confidence (an engine score) and the deskew angle; the line-item table is rebuilt from the scan. Click `p. 1` next to `total_amount` to highlight the cited row.
2. **di-i07** (scanned, wrong total): `total_mismatch` with the expected value; **Approve** is blocked and lists the errors; reject it with a reason, or approve with a recorded override.
3. **di-c01** and **di-c09** (contract and its amendment): select both, then **Compare**. **Insights → Conflicts** reports the same contract ID with different end date, value and notice period, and the Atlas invoices' different IBANs.
4. **Insights → Expiring contracts** with *As of* 2026-12-01 and 90 days; the Croatian question *"Koji ugovori ističu u sljedećih 90 dana?"* gives the same answer.
5. Sign in as `finance@otherco.demo`: none of these documents, extractions or comparisons are visible.

## 5. Demo assets not yet in the repository

These are needed for the GitHub page and are **not** generated automatically:

- [ ] Screenshot: chat with a hybrid answer and the source inspector open (ranks and RRF score visible)
- [ ] Screenshot: `no_evidence` abstention with the evidence-policy badge
- [ ] Screenshot: the Evaluation view
- [ ] Screenshot: Access & Audit as admin
- [ ] Short GIF or video (≤ 60 s): steps 3 → 7 → 8 above
