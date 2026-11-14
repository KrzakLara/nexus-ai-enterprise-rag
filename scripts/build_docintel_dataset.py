"""Render the frozen Phase 3 document-intelligence corpus into eval/docintel/.

    python scripts/build_docintel_dataset.py            # (re)build documents, labels, manifest
    python scripts/build_docintel_dataset.py --check    # verify the committed files are unchanged

Outputs (committed):
    eval/docintel/documents/<id>.<ext>   the 32 fictional documents
    eval/docintel/labels.jsonl           ground truth (authored values, NOT extractor output)
    eval/docintel/ocr_truth.jsonl        exact text drawn on every scanned page
    eval/docintel/queries.json           multi-document questions (answers derived by the oracle)
    eval/docintel/manifest.json          SHA-256 of every file + generator metadata
Requires the dev extra (reportlab) and the DejaVu Sans font. Rebuilds are byte-identical on the
same platform; the committed files, not a rebuild, are the source of truth for evaluation.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.evaluation import docgen  # noqa: E402
from backend.evaluation.docintel_corpus import DATASET_VERSION, OWNER, QUERIES, build  # noqa: E402

OUT = ROOT / "eval" / "docintel"
EXT = {"pdf": "pdf", "scan": "pdf", "mixed": "pdf", "docx": "docx", "xlsx": "xlsx", "txt": "txt", "md": "md"}


def render(spec) -> tuple[bytes, dict[str, int], dict[int, str]]:
    ops, tags = docgen.layout(spec.pages)
    seed = int(hashlib.sha256(spec.id.encode()).hexdigest()[:8], 16)
    if spec.format == "pdf":
        data = docgen.render_text_pdf(ops)
    elif spec.format == "scan":
        data = docgen.render_scan_pdf(ops, seed)
    elif spec.format == "mixed":
        data = docgen.render_scan_pdf(ops, seed, scanned=set(spec.scanned_pages))
    elif spec.format == "docx":
        data = docgen.render_docx(spec.pages)
    elif spec.format == "xlsx":
        data = docgen.render_xlsx(spec.pages, spec.xlsx_numbers)
    else:
        data = docgen.render_text(spec.pages, markdown=spec.format == "md")
    if spec.doc_type == "contract":
        tags.setdefault("contract_type", 1)                 # derived from the title on page 1
        if "contract_value" in tags:
            tags.setdefault("currency", tags["contract_value"])  # printed in the value line
            tags.setdefault("value_period", tags["contract_value"])  # stated by the value label
    if spec.doc_type == "invoice" and "line_items" in tags:
        for name in ("subtotal", "tax_amount", "total_amount"):  # totals rows of the item table
            tags.setdefault(name, tags["line_items"])
    if spec.format in ("docx", "xlsx", "md"):
        tags = {t: 1 for t in tags}  # one logical page (MD has no page breaks)
    ocr = {p: "\n".join(ops[p - 1].drawn) for p in spec.scanned_pages}
    return data, tags, ocr


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    specs = build()
    files: dict[str, bytes] = {}
    labels, ocr_rows = [], []
    for spec in specs:
        data, tags, ocr = render(spec)
        name = f"documents/{spec.id}.{EXT[spec.format]}"
        files[name] = data
        labels.append({
            "id": spec.id, "file": name, "split": spec.split, "tenant": spec.tenant, "groups": spec.groups,
            "owner": OWNER[spec.tenant], "format": spec.format, "language": spec.language,
            "doc_type": spec.doc_type, "fields": spec.fields, "parties": spec.parties,
            "line_items": spec.line_items, "clauses": spec.clauses, "pages": tags,
            "expected_errors": spec.expected_errors, "expected_warnings": spec.expected_warnings,
            "scanned_pages": spec.scanned_pages, "notes": spec.notes})
        ocr_rows += [{"id": spec.id, "page": p, "text": t} for p, t in ocr.items()]
    files["labels.jsonl"] = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in labels).encode()
    files["ocr_truth.jsonl"] = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in ocr_rows).encode()
    files["queries.json"] = (json.dumps(QUERIES, ensure_ascii=False, indent=2) + "\n").encode()
    manifest = {"generator": "scripts/build_docintel_dataset.py", "dataset_version": DATASET_VERSION,
                "errata": "eval/docintel/ERRATA.md", "documents": len(specs),
                "scan_dpi": docgen.SCAN_DPI,
                "files": {k: hashlib.sha256(v).hexdigest() for k, v in sorted(files.items())}}
    if args.check:
        stored = json.loads((OUT / "manifest.json").read_text(encoding="utf-8"))["files"]
        on_disk = {k: hashlib.sha256((OUT / k).read_bytes()).hexdigest() for k in stored}
        changed = sorted(k for k in stored if on_disk[k] != stored[k])
        print("frozen files unchanged" if not changed else f"modified: {changed}")
        return 1 if changed else 0
    for k, v in files.items():
        (OUT / k).parent.mkdir(parents=True, exist_ok=True)
        (OUT / k).write_bytes(v)
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(specs)} documents, {len(labels)} labels, {len(ocr_rows)} scanned pages to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
