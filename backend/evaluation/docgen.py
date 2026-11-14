"""Deterministic renderer for the synthetic Phase 3 corpus (dev tool, used by
scripts/build_docintel_dataset.py; the generated files are committed and frozen).

One layout pass places elements on A4 pages, in points, top-left origin. It produces drawing
operations that are rendered either as a text-layer PDF (reportlab, embedded DejaVu Sans) or as a
'scanned' raster page (Pillow at SCAN_DPI, with a small rotation, noise, blur and JPEG compression)
that has NO text layer. It also records which page each tagged element (a labelled field) landed
on, and the exact text drawn on each page (the OCR ground truth).

Elements:
    ("title", text)            ("h", text)              ("p", text, tags)
    ("kv", label, value, tag)  ("table", header, rows, options)
options: {"tag": "line_items" | None, "merge": [(r0, c0, r1, c1)], "widths": [...], "numeric": [cols]}
"""
from __future__ import annotations

import io
import random
from dataclasses import dataclass, field
from pathlib import Path

W, H, MARGIN = 595.0, 842.0, 56.0
SCAN_DPI = 200
FONT_CANDIDATES = [
    Path("C:/Python310/Lib/site-packages/matplotlib/mpl-data/fonts/ttf"),
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/usr/share/fonts/dejavu"),
]


def font_dir() -> Path:
    for d in FONT_CANDIDATES:
        if (d / "DejaVuSans.ttf").is_file() and (d / "DejaVuSans-Bold.ttf").is_file():
            return d
    try:
        import matplotlib

        d = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
        if (d / "DejaVuSans.ttf").is_file():
            return d
    except ImportError:
        pass
    raise FileNotFoundError("DejaVu Sans not found (install fonts-dejavu or matplotlib)")


@dataclass
class TextOp:
    x: float
    baseline: float
    size: float
    bold: bool
    text: str


@dataclass
class LineOp:
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass
class PageOps:
    texts: list[TextOp] = field(default_factory=list)
    lines: list[LineOp] = field(default_factory=list)
    drawn: list[str] = field(default_factory=list)  # reading-order text (OCR ground truth)


def _width(text: str, size: float, bold: bool) -> float:
    from reportlab.pdfbase.pdfmetrics import stringWidth

    _register()
    return stringWidth(text, "DV-B" if bold else "DV", size)


_REGISTERED = False


def _register():
    global _REGISTERED
    if _REGISTERED:
        return
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    d = font_dir()
    pdfmetrics.registerFont(TTFont("DV", str(d / "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont("DV-B", str(d / "DejaVuSans-Bold.ttf")))
    _REGISTERED = True


def _wrap(text: str, size: float, bold: bool, width: float) -> list[str]:
    lines, current = [], ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if _width(trial, size, bold) <= width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def layout(pages_spec: list[list[tuple]]) -> tuple[list[PageOps], dict[str, int]]:
    """Lay out explicit pages. Returns per-page drawing ops and {tag: page number}."""
    pages: list[PageOps] = []
    tag_pages: dict[str, int] = {}
    for number, elements in enumerate(pages_spec, 1):
        ops = PageOps()
        y = MARGIN
        for el in elements:
            kind = el[0]
            if kind in ("title", "h"):
                size = 16 if kind == "title" else 12
                y += size + 6
                ops.texts.append(TextOp(MARGIN, y, size, True, el[1]))
                ops.drawn.append(el[1])
                y += 6
            elif kind in ("p", "kv"):
                size = 10
                text = el[1] if kind == "p" else f"{el[1]}: {el[2]}"
                tags = (el[2] if len(el) > 2 else []) if kind == "p" else ([el[3]] if len(el) > 3 and el[3] else [])
                for t in tags:
                    tag_pages.setdefault(t, number)
                for line in _wrap(text, size, False, W - 2 * MARGIN):
                    y += 13
                    ops.texts.append(TextOp(MARGIN, y, size, False, line))
                    ops.drawn.append(line)
                y += 5 if kind == "p" else 1
            elif kind == "table":
                header, rows, opt = el[1], el[2], el[3] if len(el) > 3 else {}
                if opt.get("tag"):
                    tag_pages.setdefault(opt["tag"], number)
                y += 8
                y = _table(ops, header, rows, opt, y)
                y += 10
            elif kind == "spacer":
                y += 14
            if y > H - MARGIN:
                raise ValueError(f"page {number} overflows; split the spec into more pages")
        pages.append(ops)
    return pages, tag_pages


def _table(ops: PageOps, header: list[str], rows: list[list[str]], opt: dict, y: float) -> float:
    grid = [header] + rows
    n_cols = len(header)
    total = W - 2 * MARGIN
    widths = opt.get("widths") or [total / n_cols] * n_cols
    scale = total / sum(widths)
    widths = [w * scale for w in widths]
    xs = [MARGIN]
    for w in widths:
        xs.append(xs[-1] + w)
    row_h = 18.0
    numeric = set(opt.get("numeric", []))
    covered: dict[tuple[int, int], tuple[int, int, int, int]] = {}
    for (r0, c0, r1, c1) in opt.get("merge", []):
        for r in range(r0, r1 + 1):
            for c in range(c0, c1 + 1):
                covered[(r, c)] = (r0, c0, r1, c1)
    top = y
    for r, row in enumerate(grid):
        drawn_cells = []
        for c in range(n_cols):
            span = covered.get((r, c))
            if span and (r, c) != (span[0], span[1]):
                continue
            r1, c1 = (span[2], span[3]) if span else (r, c)
            x0, x1 = xs[c], xs[c1 + 1]
            y0, y1 = top + r * row_h, top + (r1 + 1) * row_h
            for seg in ((x0, y0, x1, y0), (x0, y1, x1, y1), (x0, y0, x0, y1), (x1, y0, x1, y1)):
                ops.lines.append(LineOp(*seg))
            text = row[c] if c < len(row) else ""
            if text:
                bold = r == 0
                size = 9
                tw = _width(text, size, bold)
                tx = x1 - 4 - tw if (c in numeric and r > 0) else x0 + 4
                ops.texts.append(TextOp(tx, y0 + 12.5, size, bold, text))
                drawn_cells.append(text)
        if drawn_cells:
            ops.drawn.append(" ".join(drawn_cells))
    return top + len(grid) * row_h


# ------------------------------------------------------------------------------- renderers

def render_text_pdf(pages: list[PageOps]) -> bytes:
    from reportlab.pdfgen import canvas

    _register()
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W, H), invariant=1)
    c.setCreator("NEXUS AI synthetic corpus")
    for ops in pages:
        c.setLineWidth(0.7)
        for ln in ops.lines:
            c.line(ln.x0, H - ln.y0, ln.x1, H - ln.y1)
        for t in ops.texts:
            c.setFont("DV-B" if t.bold else "DV", t.size)
            c.drawString(t.x, H - t.baseline, t.text)
        c.showPage()
    c.save()
    return buf.getvalue()


def render_scan_image(ops: PageOps, seed: int):
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    d = font_dir()
    s = SCAN_DPI / 72.0
    img = Image.new("L", (round(W * s), round(H * s)), 255)
    draw = ImageDraw.Draw(img)
    fonts: dict = {}
    for ln in ops.lines:
        draw.line([(ln.x0 * s, ln.y0 * s), (ln.x1 * s, ln.y1 * s)], fill=30, width=2)
    for t in ops.texts:
        key = (t.size, t.bold)
        if key not in fonts:
            fonts[key] = ImageFont.truetype(str(d / ("DejaVuSans-Bold.ttf" if t.bold else "DejaVuSans.ttf")),
                                            round(t.size * s))
        draw.text((t.x * s, t.baseline * s), t.text, font=fonts[key], fill=20, anchor="ls")
    rng = random.Random(seed)
    img = img.rotate(rng.uniform(-0.8, 0.8), resample=Image.BICUBIC, fillcolor=255, expand=False)
    img = img.filter(ImageFilter.GaussianBlur(radius=0.6))
    arr = np.asarray(img, dtype=np.int16)
    noise = np.random.default_rng(seed).normal(0, 9, arr.shape).astype(np.int16)
    img = Image.fromarray(np.clip(arr + noise, 0, 255).astype("uint8"), "L")
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=72)  # scanner-like JPEG artefacts
    return Image.open(io.BytesIO(out.getvalue()))


def render_scan_pdf(pages: list[PageOps], seed: int, scanned: set[int] | None = None) -> bytes:
    """All pages scanned (scanned=None) or a mixed PDF: page numbers in `scanned` are raster,
    the others keep their text layer."""
    from pypdf import PdfReader, PdfWriter

    text_pdf = PdfReader(io.BytesIO(render_text_pdf(pages)))
    writer = PdfWriter()
    for i, ops in enumerate(pages, 1):
        if scanned is None or i in scanned:
            img = render_scan_image(ops, seed + i)
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="PDF", resolution=SCAN_DPI)
            writer.add_page(PdfReader(io.BytesIO(buf.getvalue())).pages[0])
        else:
            writer.add_page(text_pdf.pages[i - 1])
    writer.add_metadata({"/Producer": "NEXUS AI synthetic corpus"})
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def render_docx(pages_spec: list[list[tuple]]) -> bytes:
    from docx import Document as WordDocument

    doc = WordDocument()
    for elements in pages_spec:
        for el in elements:
            kind = el[0]
            if kind == "title":
                doc.add_heading(el[1], level=1)
            elif kind == "h":
                doc.add_heading(el[1], level=2)
            elif kind == "p":
                doc.add_paragraph(el[1])
            elif kind == "kv":
                doc.add_paragraph(f"{el[1]}: {el[2]}")
            elif kind == "table":
                header, rows, opt = el[1], el[2], el[3] if len(el) > 3 else {}
                grid = [header] + rows
                t = doc.add_table(rows=len(grid), cols=len(header))
                t.style = "Table Grid"
                for r, row in enumerate(grid):
                    for c, value in enumerate(row):
                        if value:
                            t.cell(r, c).text = value
                for (r0, c0, r1, c1) in opt.get("merge", []):
                    t.cell(r0, c0).merge(t.cell(r1, c1))
    buf = io.BytesIO()
    doc.core_properties.author = "NEXUS AI synthetic corpus"
    doc.save(buf)
    return _normalize_zip(buf.getvalue())


def render_xlsx(pages_spec: list[list[tuple]], numbers: dict | None = None) -> bytes:
    """One worksheet; KV rows as two cells; tables with numeric cells written as numbers.
    `numbers` maps a printed string to the number stored in the cell (so the sheet holds real
    numeric values, as an accounting export would)."""
    import openpyxl
    from openpyxl.styles import Font

    numbers = numbers or {}
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Invoice"
    r = 1
    for elements in pages_spec:
        for el in elements:
            kind = el[0]
            if kind in ("title", "h"):
                ws.cell(r, 1, el[1]).font = Font(bold=True, size=14 if kind == "title" else 11)
                if kind == "title":
                    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=5)
                r += 2
            elif kind == "kv":
                ws.cell(r, 1, el[1])
                ws.cell(r, 2, numbers.get(el[2], el[2]))
                r += 1
            elif kind == "p":
                ws.cell(r, 1, el[1])
                r += 2
            elif kind == "table":
                header, rows = el[1], el[2]
                r += 1 if ws.cell(r - 1, 1).value else 0
                for c, value in enumerate(header, 1):
                    ws.cell(r, c, value).font = Font(bold=True)
                r += 1
                for row in rows:
                    for c, value in enumerate(row, 1):
                        if value != "":
                            ws.cell(r, c, numbers.get(value, value))
                    r += 1
                r += 1
    for col in "ABCDE":
        ws.column_dimensions[col].width = 22
    buf = io.BytesIO()
    wb.properties.creator = "NEXUS AI synthetic corpus"
    wb.save(buf)
    return _normalize_zip(buf.getvalue())


def render_text(pages_spec: list[list[tuple]], markdown: bool) -> bytes:
    out: list[str] = []
    for n, elements in enumerate(pages_spec):
        if n:
            out.append("\f" if not markdown else "\n---\n")
        for el in elements:
            kind = el[0]
            if kind == "title":
                out += [f"# {el[1]}" if markdown else el[1].upper(), ""]
            elif kind == "h":
                out += [f"## {el[1]}" if markdown else el[1].upper(), ""]
            elif kind == "p":
                out += [_wrap_chars(el[1], 88), ""]
            elif kind == "kv":
                out.append(f"{el[1]}: {el[2]}")
            elif kind == "table":
                header, rows = el[1], el[2]
                out.append("")
                if markdown:
                    out.append("| " + " | ".join(header) + " |")
                    out.append("|" + "|".join("---" for _ in header) + "|")
                    out += ["| " + " | ".join(row) + " |" for row in rows]
                else:
                    widths = [max(len(str(r[c])) for r in [header] + rows) + 3 for c in range(len(header))]
                    for row in [header] + rows:
                        out.append("".join(str(v).ljust(w) if c == 0 else str(v).rjust(w)
                                           for c, (v, w) in enumerate(zip(row, widths))).rstrip())
                out.append("")
    return ("\n".join(out).rstrip() + "\n").encode("utf-8")


def _wrap_chars(text: str, width: int) -> str:
    lines, current = [], ""
    for word in text.split():
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return "\n".join(lines)


def _normalize_zip(data: bytes) -> bytes:
    """Rewrite an OOXML zip with fixed timestamps so rebuilds are byte-identical."""
    import zipfile

    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in sorted(src.infolist(), key=lambda i: i.filename):
            content = src.read(info.filename)
            if info.filename == "docProps/core.xml":
                import re

                content = re.sub(rb"<dcterms:(created|modified)[^>]*>[^<]*</dcterms:\1>", b"", content)
            zi = zipfile.ZipInfo(info.filename, date_time=(2026, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            dst.writestr(zi, content)
    return out.getvalue()
