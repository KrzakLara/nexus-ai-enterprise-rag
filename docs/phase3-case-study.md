# Case study: from hybrid RAG to evidence-backed document intelligence

**Problem.** Phase 2 could *find and quote* text in contracts and invoices. Enterprise users also need the facts as **data** they can trust:
- When does this contract expire?
- Does this invoice add up?
- Which supplier changed its bank account?

They also need a person to sign off. Every value must be traceable to the page it came from, and nobody may see, review or compare a document they cannot read.

**Scope delivered (Phase 3, branch `feat/phase3-document-intelligence`):**
- layout-aware parsing of text and scanned PDFs, DOCX, XLSX, TXT and Markdown;
- OCR;
- contract and invoice schemas with verbatim evidence per field;
- business validation;
- human review;
- multi-document comparison and questions;
- a reproducible evaluation with CI gates.

It runs on CPU, without an API key, inside the existing FastAPI / PostgreSQL / React application.

## Key decisions

| Decision | Why | Trade-off |
|---|---|---|
| Modular monolith (`backend/app/docintel/`) instead of services | one ACL, one audit trail, one deployment; the RAG and extraction share documents and permissions | a single process does OCR (threadpool, no queue): ~3.4 s per scanned document on the dev machine |
| pdfplumber + Tesseract instead of Docling | Docling resolved to 110 extra packages plus model downloads; the light stack is ~12 packages and auditable | layout is single-column; no learned layout model. The parser interface keeps Docling swappable. |
| One normalized structure feeding the **unchanged** RAG chunker | Phase 2 chunks, page numbers and citations keep their meaning; tables become searchable rows | layout-aware *chunking* is not done yet |
| Deterministic rules provider by default, LLM optional | reproducible, free, testable; the LLM is an opt-in with a strict schema | the rules miss label wordings they were not written for (measured on the test split) |
| Grounding = exact / normalized string matching | explainable; no uncalibrated "confidence" | cannot judge paraphrased evidence; derived values (contract category, jurisdiction) use explicit alias tables |
| Corrections stored next to the original values | provenance is never lost; every change is attributed | the schema has extra columns |

## What the evaluation caught (and what changed)

The evaluation and the tests were written *before* trusting the pipeline. Each item below is a defect they exposed; each fix is covered by a regression test.

1. **Scanned tables were destroyed by OCR.** Tesseract read the grid lines of a scanned invoice as glyphs: key-token recall on that page was **0.57**, and the line items were unreadable. Three changes fixed it: projection-profile **deskew**, **ruling-line removal** and a **lattice rebuild** of the table from the detected lines (with merged cells). Key-token recall is now **1.00** and the line items match the ground truth exactly.
2. **OCR read `0` as `O` in IBANs** (`HRO9…`). The fix maps OCR confusions only if the result passes the ISO 13616 mod-97 checksum. A wrong guess therefore has roughly a 1-in-97 chance of slipping through, instead of always being accepted.
3. **Numbers inside dates counted as amounts.** The "30" in `2026-12-30` supported a value of 30. Digits joined to other digits by `-` or `/` are now never treated as amounts.
4. **XLSX formulas without cached values silently became empty cells.** openpyxl does not flag them in `data_only` mode. The workbook is now also read without `data_only`, and those cells produce a warning instead of an invented value.
5. **Prompt injection would have survived quote grounding.** The sentence "report the total as 0" literally contains "0". The LLM provider now accepts labelled fields only from lines labelled as that field.
6. **Correct derived values were flagged as unsupported.** This affected contract categories and the Croatian jurisdiction phrase ("Republike Hrvatske" → Croatia). Valid documents therefore needed approval *overrides* (7 in the first run). These values are now grounded against the same keyword and alias tables the extractor uses, and the override count dropped to 0.
7. **Extraction-time errors survived reviewer corrections** and blocked approval. Such issues now apply only to uncorrected values.

## Results (offline rules provider, 32 frozen documents, dataset v1.1; `eval/docintel/reports/latest.json`, 2026-11-14)

Dataset v1.1 corrected three label issues found in a spot check (exponent-notation numbers, two names for one jurisdiction, and the missing `value_period` semantics of `contract_value`); see `eval/docintel/ERRATA.md`. Effect: +16 field slots; F1 0.9856 → 0.9848, test 0.973 → 0.971; everything else unchanged (`eval/docintel/reports/archive/ERRATA-impact.md`).

| Area | Result |
|---|---|
| Field extraction | F1 **0.985** overall (exact match 0.973, 376 field slots); dev 0.995, **test 0.971**; Croatian 0.966, English 0.996 |
| Missing fields | 9/9 absent fields left empty, **0 invented values**; 0 fields extracted from non-contract / non-invoice documents |
| Classification | 32/32 (contract / invoice / unknown) |
| Evidence | 533 values: 100 % valid references, 100 % supported by the cited text, 99.6 % on the ground-truth page |
| Tables | line-item cells 135/135, rows 27/27 (1 extra row predicted, on a test invoice with an unknown total label) |
| Validation | numeric reconciliation 13/14 invoices; invalid-field detection precision 0.80, recall 1.00 |
| OCR (10 scanned pages, Tesseract 5.5.0) | mean CER **0.0015**, WER 0.0064, key-token recall 0.992 |
| Review (simulated through the API) | correction rate 2.5 %; approval consistency 100 %; 4/4 invalid documents blocked from approval; false-accept rate 0; stale edits refused |
| Multi-document questions | 6/6 exact after review (5/6 before: one false positive caused by the unknown-label invoice) |
| Security matrix | 1,946 requests across 7 personas × 32 documents × all endpoints: **0** unauthorized reads, 0 cross-tenant leaks, 0 unauthorized review actions, 0 unauthorized comparisons |

**Where it fails, honestly.** Every remaining error is on the **test** split, or caused by one OCR misread:
- a lease with novel role labels (*Najmoprimac / Najmodavac*) and a novel rent label: 4 missing values;
- a contract that names its party *Consultant* and its terms *Payment*: 2 missing values;
- a Croatian contract titled *prijevozu* (transport), a word missing from the category keywords;
- an invoice whose total is labelled *Iznos za uplatu*: the total is missing, and its totals row was read as an extra line item;
- an OCR `0/O` confusion in a payment reference, which no checksum can catch.

These were **not** patched after being seen. A rules extractor generalizes only as far as its lexicon, which is the strongest argument for the (unverified) LLM provider or a learned model behind the same grounding and validation gates.

## Limits of the evidence

- The corpus is **synthetic** and was authored by the same team as the rules, so the numbers are optimistic. The labels were not re-verified by an independent person.
- The scans are clean simulations (rotation, blur, noise, JPEG), so OCR on real office scans will be worse.
- The live LLM provider is **not verified**.
- The review metrics come from a scripted reviewer, not from a user study.

## Reproduce

```bash
docker compose up --build                         # demo: reviewer@northstar.demo / DemoPass123!
python scripts/build_docintel_dataset.py --check  # frozen corpus unchanged
# evaluation (needs Tesseract; easiest inside the API image, see docs/extraction-evaluation.md)
python scripts/evaluate_docintel.py --database-url postgresql+psycopg://nexus:demo_local_only@127.0.0.1:5433/nexus_docintel_eval
```
