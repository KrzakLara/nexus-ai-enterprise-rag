"""Layout-aware parsing of PDF (text layer and scanned), DOCX, XLSX, TXT and Markdown into the
normalized ParsedDocument (structure.py).

PDF: pdfplumber words (with font size / weight and boxes) are grouped into lines, headings and
paragraphs in top-to-bottom reading order (single-column assumption). Ruled tables come from
pdfplumber's table finder (cell boxes and merged cells kept). Unruled tables are rebuilt from word
positions when a line is a recognisable table header. A page whose text layer has fewer than
MIN_TEXT_CHARS characters but contains an image is OCRed with Tesseract (ocr.py). The decision is
made per page, so mixed scanned / digital PDFs work.
"""
from __future__ import annotations

import re
import statistics
import zipfile
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path

from .structure import BBox, Block, Line, Page, ParsedDocument, Table
from .tables import Word, group_lines, map_header, reconstruct_from_words, table_from_grid

SUPPORTED = {".pdf": "pdf", ".docx": "docx", ".xlsx": "xlsx", ".txt": "txt", ".md": "md"}
MAX_PAGES = 100
MIN_TEXT_CHARS = 20


class UnsupportedFormat(ValueError):
    pass


def detect_format(filename: str, data: bytes) -> str:
    """Format from the extension, cross-checked against the file's magic bytes."""
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED:
        raise UnsupportedFormat("Supported formats: PDF (text or scanned), DOCX, XLSX, TXT, MD")
    fmt = SUPPORTED[ext]
    is_pdf, is_zip = data.startswith(b"%PDF"), data[:4] == b"PK\x03\x04"
    if fmt == "pdf" and not is_pdf:
        raise ValueError("File content is not a PDF")
    if fmt in ("docx", "xlsx"):
        if not is_zip:
            raise ValueError(f"File content is not a valid {fmt.upper()} (Office Open XML) archive")
        try:
            names = set(zipfile.ZipFile(BytesIO(data)).namelist())
        except zipfile.BadZipFile as exc:
            raise ValueError(f"Corrupt {fmt.upper()} archive: {exc}") from None
        marker = "word/document.xml" if fmt == "docx" else "xl/workbook.xml"
        if marker not in names:
            raise ValueError(f"File is a ZIP archive but not a {fmt.upper()} document")
    if fmt in ("txt", "md") and (is_pdf or is_zip):
        raise ValueError("File content does not match its text extension")
    return fmt


def parse_document(filename: str, data: bytes, ocr: bool = True) -> ParsedDocument:
    fmt = detect_format(filename, data)
    parsed = {"pdf": lambda: _parse_pdf(data, ocr), "docx": lambda: _parse_docx(data),
              "xlsx": lambda: _parse_xlsx(data), "txt": lambda: _parse_text(data, markdown=False),
              "md": lambda: _parse_text(data, markdown=True)}[fmt]()
    if not any(p.text.strip() for p in parsed.pages):
        raise ValueError("No selectable text found: no text layer, no OCR-able images and no cells")
    return parsed


# ----------------------------------------------------------------------------------------- PDF

def _parse_pdf(data: bytes, ocr: bool) -> ParsedDocument:
    import pdfplumber

    from . import ocr as ocr_mod

    pages: list[Page] = []
    warnings: list[str] = []
    engines: set[str] = set()
    with pdfplumber.open(BytesIO(data)) as pdf:
        if len(pdf.pages) > MAX_PAGES:
            raise ValueError(f"Demo limit: max {MAX_PAGES} pages")
        for index, page in enumerate(pdf.pages):
            number = index + 1
            raw = page.extract_words(keep_blank_chars=False, use_text_flow=False,
                                     extra_attrs=["size", "fontname"])
            words = [Word(text=w["text"], x0=w["x0"], top=w["top"], x1=w["x1"], bottom=w["bottom"],
                          size=round(float(w["size"]), 1), bold="bold" in w["fontname"].lower())
                     for w in raw]
            if sum(len(w.text) for w in words) >= MIN_TEXT_CHARS:
                tables = _pdf_ruled_tables(page, number)
                pages.append(_layout_page(number, "text_layer", words, tables, page.width, page.height,
                                          table_source="text_columns"))
            elif page.images:
                if not ocr:
                    raise ValueError(f"Page {number} has no text layer; OCR is disabled")
                try:
                    result = ocr_mod.ocr_pdf_page(data, index)
                except ocr_mod.OcrUnavailable as exc:
                    raise ValueError(f"Page {number} needs OCR, but {exc}") from None
                engines.add(f"tesseract {result.meta['version']}")
                lattice_tables = []
                for k, (grid, boxes) in enumerate(result.tables or []):
                    if len(grid) < 2 or len(grid[0]) < 2:
                        continue
                    cell_boxes = [[BBox(x0=b[0], top=b[1], x1=b[2], bottom=b[3]) if b else None for b in row]
                                  for row in boxes]
                    all_boxes = [b for row in cell_boxes for b in row if b]
                    lattice_tables.append(table_from_grid(f"t{number}-{k}", number, grid, "ocr_lattice",
                                                          cell_boxes, BBox.union(all_boxes)))
                p = _layout_page(number, "ocr", result.words, lattice_tables, result.width_pt,
                                 result.height_pt, table_source="ocr_columns")
                p.ocr = result.meta
                pages.append(p)
            else:
                pages.append(Page(number=number, method="empty", width=page.width, height=page.height))
                if words:
                    warnings.append(f"page {number}: only {sum(len(w.text) for w in words)} characters")
    parser = f"pdfplumber {pdfplumber.__version__}" + "".join(f" + {e}" for e in sorted(engines))
    return ParsedDocument(format="pdf", parser=parser, pages=pages, warnings=warnings)


def _pdf_ruled_tables(page, number: int) -> list[Table]:
    tables = []
    for k, found in enumerate(page.find_tables()):
        grid = found.extract()
        if not grid or len(grid) < 2 or max(len(r) for r in grid) < 2:
            continue
        boxes = [[BBox(x0=c[0], top=c[1], x1=c[2], bottom=c[3]) if c else None for c in row.cells]
                 for row in found.rows]
        x0, top, x1, bottom = found.bbox
        tables.append(table_from_grid(f"t{number}-{k}", number, grid, "pdf_ruling", boxes,
                                      BBox(x0=x0, top=top, x1=x1, bottom=bottom)))
    return tables


def _inside(word: Word, box: BBox) -> bool:
    xc, yc = word.xc, (word.top + word.bottom) / 2
    return box.x0 - 1 <= xc <= box.x1 + 1 and box.top - 1 <= yc <= box.bottom + 1


def _layout_page(number: int, method: str, words: list[Word], tables: list[Table],
                 width: float | None, height: float | None, table_source: str) -> Page:
    """Lines -> headings / paragraphs / tables in reading order."""
    free = [w for w in words if not any(t.bbox and _inside(w, t.bbox) for t in tables)]
    lines = group_lines(free)
    # unruled tables: rebuilt from word positions when a line is a table header
    items: list[tuple[float, object]] = [(t.bbox.top if t.bbox else 0.0, t) for t in tables]
    i = 0
    rebuilt = len(tables)
    while i < len(lines):
        found = reconstruct_from_words(lines, i, f"t{number}-{rebuilt}", number, table_source)
        if found:
            table, i = found
            rebuilt += 1
            items.append((table.bbox.top if table.bbox else 0.0, table))
            continue
        items.append((min(w.top for w in lines[i]), lines[i]))
        i += 1
    items.sort(key=lambda it: it[0])
    sizes = [w.size for w in words if w.size]
    body = statistics.median(sizes) if sizes else None
    heights = [w.bottom - w.top for w in words]
    body_h = statistics.median(heights) if heights else 10.0

    blocks: list[Block] = []
    section: str | None = None
    para: list[tuple[Line, float, float]] = []

    def flush():
        nonlocal para
        if para:
            lns = [p[0] for p in para]
            blocks.append(Block(id=f"p{number}-b{len(blocks)}", kind="paragraph", page=number,
                                text="\n".join(ln.text for ln in lns), lines=lns, section=section,
                                bbox=BBox.union([ln.bbox for ln in lns])))
        para = []

    for _, item in items:
        if isinstance(item, Table):
            flush()
            blocks.append(Block(id=f"p{number}-b{len(blocks)}", kind="table", page=number,
                                text="\n".join(item.render_rows()), section=section, bbox=item.bbox,
                                table=item))
            continue
        ws: list[Word] = item  # type: ignore[assignment]
        text = " ".join(w.text for w in ws)
        box = BBox(x0=min(w.x0 for w in ws), top=min(w.top for w in ws),
                   x1=max(w.x1 for w in ws), bottom=max(w.bottom for w in ws))
        confs = [w.conf for w in ws if w.conf is not None]
        line = Line(text=text, bbox=box.rounded(),
                    ocr_confidence=round(sum(confs) / len(confs), 1) if confs else None)
        level = _heading_level(ws, text, body, body_h)
        if level:
            flush()
            section = text
            blocks.append(Block(id=f"p{number}-b{len(blocks)}", kind="heading", page=number, text=text,
                                lines=[line], level=level, section=text, bbox=line.bbox))
            continue
        if para and box.top - para[-1][2] > 0.9 * (box.bottom - box.top):
            flush()  # vertical gap -> new paragraph
        para.append((line, box.top, box.bottom))
    flush()
    return Page(number=number, method=method, width=width, height=height, blocks=blocks)


def _heading_level(ws: list[Word], text: str, body_size: float | None, body_h: float) -> int | None:
    if len(text) > 90 or text.rstrip().endswith((".", ",", ";")) or not re.search(r"[A-Za-zČĆŠĐŽčćšđž]", text):
        return None
    sizes = [w.size for w in ws if w.size]
    if sizes and body_size:
        avg = sum(sizes) / len(sizes)
        if avg >= 1.5 * body_size:
            return 1
        if avg >= 1.15 * body_size or (all(w.bold for w in ws) and ":" not in text):
            return 2
        return None
    # OCR: no font metadata; tall short lines only
    h = sum(w.bottom - w.top for w in ws) / len(ws)
    return 1 if h >= 1.45 * body_h and len(ws) <= 10 and ":" not in text else None


# ---------------------------------------------------------------------------------------- DOCX

def _parse_docx(data: bytes) -> ParsedDocument:
    from docx import Document as WordDocument
    from docx.oxml.ns import qn
    from docx.table import Table as DocxTable
    from docx.text.paragraph import Paragraph

    doc = WordDocument(BytesIO(data))
    blocks: list[Block] = []
    section: str | None = None
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            para = Paragraph(child, doc)
            text = " ".join(para.text.split())
            if not text:
                continue
            style = (para.style.name if para.style is not None else "") or ""
            m = re.match(r"Heading (\d)", style)
            level = int(m.group(1)) if m else (1 if style == "Title" else None)
            if level:
                section = text
            blocks.append(Block(id=f"p1-b{len(blocks)}", kind="heading" if level else "paragraph",
                                page=1, text=text, lines=[Line(text=text)], level=level, section=section))
        elif child.tag == qn("w:tbl"):
            table = DocxTable(child, doc)
            grid: list[list[str | None]] = []
            seen: list[list[object]] = []
            for r, row in enumerate(table.rows):
                out_row, ids = [], []
                for c, cell in enumerate(row.cells):
                    tc = cell._tc
                    merged = (c > 0 and ids[c - 1] is tc) or (r > 0 and c < len(seen[r - 1]) and seen[r - 1][c] is tc)
                    out_row.append(None if merged else cell.text)
                    ids.append(tc)
                grid.append(out_row)
                seen.append(ids)
            t = table_from_grid(f"t1-{sum(1 for b in blocks if b.table)}", 1, grid, "docx")
            blocks.append(Block(id=f"p1-b{len(blocks)}", kind="table", page=1,
                                text="\n".join(t.render_rows()), section=section, table=t))
    return ParsedDocument(format="docx", parser="python-docx (one logical page; DOCX has no fixed pagination)",
                          pages=[Page(number=1, method="docx", blocks=blocks)])


# ---------------------------------------------------------------------------------------- XLSX

def _cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == datetime.min.time() else value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        return format(Decimal(repr(value)).normalize(), "f")
    return " ".join(str(value).split())


def _parse_xlsx(data: bytes) -> ParsedDocument:
    import openpyxl

    wb = openpyxl.load_workbook(BytesIO(data), data_only=True)        # cached formula results
    formulas = openpyxl.load_workbook(BytesIO(data), data_only=False)  # which cells are formulas
    pages, warnings = [], []
    for index, ws in enumerate(wb.worksheets, 1):
        fws = formulas.worksheets[index - 1]
        grid: list[list[str | None]] = []
        for row in ws.iter_rows(min_row=1, max_row=ws.max_row, max_col=ws.max_column):
            out = []
            for cell in row:
                if cell.value is None and fws[cell.coordinate].data_type == "f":
                    # a formula never calculated by a spreadsheet app has no stored value: report
                    # it instead of computing (or inventing) the result
                    warnings.append(f"{ws.title}!{cell.coordinate}: formula without cached value")
                out.append(_cell_text(cell.value))
            grid.append(out)
        for rng in ws.merged_cells.ranges:  # non-anchor cells of a merged range -> None
            for r in range(rng.min_row, rng.max_row + 1):
                for c in range(rng.min_col, rng.max_col + 1):
                    if (r, c) != (rng.min_row, rng.min_col) and r - 1 < len(grid) and c - 1 < len(grid[r - 1]):
                        grid[r - 1][c - 1] = None
        blocks: list[Block] = []
        section = None
        groups: list[list[list[str | None]]] = []
        current: list[list[str | None]] = []
        for row in grid:
            if not any(v for v in row):
                if current:
                    groups.append(current)
                current = []
            else:
                current.append(row)
        if current:
            groups.append(current)
        for group in groups:
            header = [v or "" for v in group[0]]
            if len(map_header(header)) >= 3 and len(group) >= 2:
                cols = [c for c in range(len(header)) if any((row[c] if c < len(row) else None)
                                                            for row in group)]
                sub = [[row[c] if c < len(row) else "" for c in cols] for row in group]
                t = table_from_grid(f"t{index}-{sum(1 for b in blocks if b.table)}", index, sub, "xlsx")
                blocks.append(Block(id=f"p{index}-b{len(blocks)}", kind="table", page=index,
                                    text="\n".join(t.render_rows()), section=section, table=t))
                continue
            lines = [Line(text=" | ".join(v for v in row if v)) for row in group]
            if len(group) == 1 and sum(1 for v in group[0] if v) == 1:
                section = lines[0].text
                blocks.append(Block(id=f"p{index}-b{len(blocks)}", kind="heading", page=index,
                                    text=lines[0].text, lines=lines, level=1, section=section))
            else:
                blocks.append(Block(id=f"p{index}-b{len(blocks)}", kind="paragraph", page=index,
                                    text="\n".join(ln.text for ln in lines), lines=lines, section=section))
        pages.append(Page(number=index, method="xlsx", label=ws.title, blocks=blocks))
    return ParsedDocument(format="xlsx", parser=f"openpyxl {openpyxl.__version__} (one page per worksheet)",
                          pages=pages, warnings=warnings)


# ----------------------------------------------------------------------------------- TXT / MD

_MD_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")


def _parse_text(data: bytes, markdown: bool) -> ParsedDocument:
    text = data.decode("utf-8-sig")  # UnicodeDecodeError -> the API answers 422
    pages = []
    for number, page_text in enumerate(text.split("\f"), 1):
        pages.append(Page(number=number, method="text", blocks=_text_blocks(page_text, number, markdown)))
    return ParsedDocument(format="md" if markdown else "txt",
                          parser="markdown (headings, pipe tables)" if markdown else "plain text",
                          pages=pages)


def _text_blocks(text: str, number: int, markdown: bool) -> list[Block]:
    raw_lines = text.splitlines()
    blocks: list[Block] = []
    section = None
    para: list[str] = []

    def add(kind, body, lines, level=None, table=None):
        blocks.append(Block(id=f"p{number}-b{len(blocks)}", kind=kind, page=number, text=body,
                            lines=lines, level=level, section=section, table=table))

    def flush():
        nonlocal para
        if para:
            add("paragraph", "\n".join(para), [Line(text=t) for t in para])
        para = []

    words_by_line: Callable[[int], list[Word]] = lambda k: [  # noqa: E731 - char offsets as x
        Word(text=m.group(0), x0=m.start(), top=k * 10.0, x1=m.end(), bottom=k * 10.0 + 4)
        for m in re.finditer(r"\S+(?: \S+)*", raw_lines[k])]
    i = 0
    while i < len(raw_lines):
        line = raw_lines[i].rstrip()
        stripped = line.strip()
        if not stripped:
            flush()
            i += 1
            continue
        if markdown and stripped.startswith("|") and i + 1 < len(raw_lines) and _MD_SEPARATOR.match(raw_lines[i + 1]):
            flush()
            rows = [stripped]
            j = i + 2
            while j < len(raw_lines) and raw_lines[j].strip().startswith("|"):
                rows.append(raw_lines[j].strip())
                j += 1
            grid = [[c.strip() for c in r.strip("|").split("|")] for r in rows]
            t = table_from_grid(f"t{number}-{sum(1 for b in blocks if b.table)}", number, grid, "markdown")
            add("table", "\n".join(t.render_rows()), [], table=t)
            i = j
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped) if markdown else None
        if m or (not markdown and _is_caps_heading(stripped)):
            flush()
            heading = m.group(2).strip() if m else stripped
            section = heading
            add("heading", heading, [Line(text=heading)], level=len(m.group(1)) if m else 1)
            i += 1
            continue
        # aligned plain-text table: columns separated by 2+ spaces under a recognised header
        if re.search(r"\S {2,}\S", line):
            lines_words = [words_by_line(k) for k in range(i, len(raw_lines))]
            lines_words = [lw for lw in lines_words if lw]
            found = reconstruct_from_words(lines_words, 0, f"t{number}-{sum(1 for b in blocks if b.table)}",
                                           number, "text_columns") if lines_words else None
            if found:
                flush()
                table, consumed = found
                add("table", "\n".join(table.render_rows()), [], table=_strip_geometry(table))
                # advance past the consumed non-empty lines
                count = 0
                while i < len(raw_lines) and count < consumed:
                    if raw_lines[i].strip():
                        count += 1
                    i += 1
                continue
        para.append(stripped)
        i += 1
    flush()
    return blocks


def _strip_geometry(table: Table) -> Table:
    """Character offsets are not page geometry: never expose them as bounding boxes."""
    return table.model_copy(update={"bbox": None, "cells": [c.model_copy(update={"bbox": None})
                                                            for c in table.cells]})


def _is_caps_heading(text: str) -> bool:
    letters = [ch for ch in text if ch.isalpha()]
    return 4 <= len(letters) and len(text) <= 80 and all(ch.isupper() for ch in letters) and ":" not in text
