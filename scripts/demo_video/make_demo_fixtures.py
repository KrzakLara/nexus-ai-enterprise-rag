"""Generate the English demo-video fixtures in ``demo_documents/video/``.

Two synthetic invoices rendered with the SAME generator as the frozen Document Intelligence corpus
(``backend.evaluation.docgen`` + ``docintel_corpus.invoice``), so the demo exercises the real
parser / OCR / extractor / validator on documents built the same way as the evaluation data:

* a text-layer PDF invoice whose numbers are internally consistent (uploaded, extracted, approved);
* a scanned (image-only) PDF invoice whose printed total is 75.00 too high, which the business
  validation must catch (OCR path + blocked approval).

The frozen evaluation data in ``eval/`` is NOT touched. These files are not part of any metric.

    .venv/Scripts/python scripts/demo_video/make_demo_fixtures.py
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.evaluation import docgen  # noqa: E402
from backend.evaluation.docintel_corpus import invoice  # noqa: E402

OUT = ROOT / "demo_documents" / "video"
CUSTOMER = "Northstar Manufacturing Ltd"

SPECS = [
    ("Brightline Systems Invoice BLS-2026-0417.pdf",
     invoice("demo-v01", "demo", "en", "pdf", "northstar", ["finance"], "BLS-2026-0417", "Brightline Systems Ltd",
             "GB 314 2718 28", CUSTOMER, date(2026, 11, 2), date(2026, 12, 2), "EUR",
             [("Industrial IoT sensor kit", 12, "145.00", 20), ("Gateway installation and setup", 6, "95.00", 20),
              ("Annual monitoring subscription", 1, "1250.00", 20)],
             "GB47 BARC 2003 5345 1234 56", "NS-PO-8120", num_style="en", date_style="iso")),
    ("Harbor Facility Services Invoice HFS-2026-0388 - scanned.pdf",
     invoice("demo-v02", "demo", "en", "scan", "northstar", ["finance"], "HFS-2026-0388", "Harbor Facility Services Ltd",
             "GB 271 8281 82", CUSTOMER, date(2026, 10, 20), date(2026, 11, 19), "EUR",
             [("Warehouse deep cleaning", 3, "420.00", 20), ("Cleaning supplies", 1, "96.50", 20)],
             "GB63 LOYD 3096 1700 7092 48", "NS-PO-8094", num_style="en", date_style="iso",
             perturb={"total": Decimal("75.00")}, expected_errors=["total_mismatch"],
             notes="scanned; printed total is 75.00 higher than subtotal + VAT")),
]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = []
    for filename, spec in SPECS:
        ops, _tags = docgen.layout(spec.pages)
        seed = int(hashlib.sha256(spec.id.encode()).hexdigest()[:8], 16)
        data = docgen.render_text_pdf(ops) if spec.format == "pdf" else docgen.render_scan_pdf(ops, seed)
        (OUT / filename).write_bytes(data)
        manifest.append({"file": filename, "format": spec.format, "language": spec.language,
                         "expected_fields": spec.fields, "expected_errors": spec.expected_errors,
                         "sha256": hashlib.sha256(data).hexdigest(), "notes": spec.notes})
        print(f"wrote {filename} ({len(data)} bytes)")
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
