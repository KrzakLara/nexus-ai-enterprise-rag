"""Small synthetic document fixtures, generated in code (no binary files in the repository).

`make_pdf` writes a real, spec-conformant PDF 1.4 file (catalog, page tree, Helvetica font, one
content stream per page, a correct xref table) with a selectable text layer, exactly like a PDF
exported from a word processor. `make_docx` uses python-docx, the library the parser reads with.
"""
from io import BytesIO

from docx import Document as WordDocument


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: list[list[str]]) -> bytes:
    """One inner list of text lines per page. An empty list gives a page without any text."""
    n = len(pages)
    page_ids = [4 + 2 * i for i in range(n)]
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{' '.join(f'{p} 0 R' for p in page_ids)}] /Count {n} >>".encode(),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    }
    for page_id, lines in zip(page_ids, pages):
        content_id = page_id + 1
        objects[page_id] = (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
        ).encode()
        ops = ["BT", "/F1 10 Tf", "13 TL", "50 750 Td"]
        ops += [f"({_pdf_escape(line)}) Tj T*" for line in lines]
        ops.append("ET")
        stream = "\n".join(ops).encode("cp1252")
        objects[content_id] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n".encode() + objects[number] + b"\nendobj\n"
    xref = len(out)
    size = max(objects) + 1
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offsets[k]:010d} 00000 n \n".encode() for k in range(1, size))
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def make_docx(paragraphs: list[str], table: list[list[str]] | None = None,
              heading: str | None = None) -> bytes:
    doc = WordDocument()
    if heading:
        doc.add_heading(heading, level=1)
    for text in paragraphs:
        doc.add_paragraph(text)
    if table:
        grid = doc.add_table(rows=len(table), cols=len(table[0]))
        for r, row in enumerate(table):
            for c, value in enumerate(row):
                grid.cell(r, c).text = value
    buffer = BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


# A three-page supplier agreement. Page 2 is long enough to be split into several chunks.
KESTREL_FACT = "Agreement KR-2026-77 grants a freight rebate of 7 percent of annual spend."
LONG_CLAUSES = [
    f"Clause {i}. The supplier shall document every shipment in the shared logistics ledger "
    f"and reconcile the ledger with the buyer at the end of each calendar month number {i}."
    for i in range(1, 15)
]
KESTREL_PAGES = [
    ["Kestrel Freight Services Agreement", "Counterparty: Kestrel Freight d.o.o.",
     "End date: 2028-06-30", "Annual value: EUR 96,000", "Notice period: 60 days"],
    LONG_CLAUSES,
    ["Commercial terms", KESTREL_FACT, "Invoices are payable within 30 days."],
]
