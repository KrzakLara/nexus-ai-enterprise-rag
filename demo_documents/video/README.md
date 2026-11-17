# English demo-video fixtures

Synthetic, fictional invoices used only by the recorded demo video
([`docs/videos/nexus-ai-demo.mp4`](../../docs/videos/nexus-ai-demo.mp4)). They are **not** part of
the frozen evaluation corpus (`eval/`), are not seeded into the stack, and do not feed any metric.

| File | Format | Purpose in the demo |
|---|---|---|
| `Brightline Systems Invoice BLS-2026-0417.pdf` | text-layer PDF | uploaded, extracted with evidence, approved; asked about in the assistant |
| `Harbor Facility Services Invoice HFS-2026-0388 - scanned.pdf` | image-only PDF (needs OCR) | printed total is 75.00 higher than subtotal + VAT, so validation blocks approval |

They are generated with the same renderer and invoice builder as the evaluation corpus
(`backend/evaluation/docgen.py`, `docintel_corpus.invoice`):
`python scripts/demo_video/make_demo_fixtures.py`. `manifest.json` lists the expected field values
and the expected validation error.

Verified on 2026-11-17 against a running stack with `scripts/demo_video/verify_demo_pipeline.mjs`.
On both invoices all 12 header fields matched the manifest. The text invoice also had 15 line-item
values; the scan went through OCR (Tesseract 5.5.0, mean word confidence 95.1, deskew 0.7°) and its
`total_amount` is reported invalid (expected 1627.80, found 1702.80).
