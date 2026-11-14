"""Table construction shared by the parsers.

* `table_from_grid`: a text grid in which merged cells are marked with None (the convention of
  pdfplumber's `Table.extract()`); spans are derived from the None runs, without guessing content.
* `reconstruct_from_words`: rebuild a table from positioned words (OCR or an unruled text layer)
  when a line looks like a table header (>= 3 known column names). Column ranges come from the header
  words; following lines are assigned to columns by word position until a blank gap or a totals
  line. This is a heuristic; its accuracy is measured in the Phase 3 evaluation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .normalize import fold
from .structure import BBox, Cell, Table

# Column-name vocabulary (English + Croatian) for line-item tables. Also used by the extractor.
COLUMN_SYNONYMS: dict[str, tuple[str, ...]] = {
    "description": ("description", "item", "items", "service", "services", "product", "opis",
                    "naziv", "stavka", "usluga", "artikl", "deliverable", "milestone"),
    "quantity": ("qty", "qty.", "quantity", "količina", "kol.", "kol", "units", "hours", "sati"),
    "unit_price": ("unit price", "price", "rate", "unit cost", "jed. cijena", "jedinična cijena",
                   "cijena", "cijena/jed.", "price per unit"),
    "tax_rate": ("vat %", "vat", "tax %", "tax", "tax rate", "pdv %", "pdv", "stopa pdv", "porez",
                 "vat rate", "stopa"),
    "amount": ("amount", "line total", "total", "net amount", "iznos", "ukupno", "vrijednost",
               "net", "subtotal"),
}
_HEADER_TOKENS = {s for values in COLUMN_SYNONYMS.values() for s in values}
TOTAL_LABELS = re.compile(r"^\s*(sub\s*total|subtotal|total|grand total|amount due|balance due|"
                          r"vat|tax|međuzbroj|osnovica|ukupno|sveukupno|pdv|iznos pdv|za platiti)\b",
                          re.IGNORECASE)


def map_header(cells: list[str]) -> dict[int, str]:
    """Column index -> canonical column name, for cells that are known column names."""
    mapping: dict[int, str] = {}
    used: set[str] = set()
    for i, cell in enumerate(cells):
        key = fold(cell).strip(" :")
        for name, synonyms in COLUMN_SYNONYMS.items():
            if name not in used and key in synonyms:
                mapping[i] = name
                used.add(name)
                break
    return mapping


def table_from_grid(table_id: str, page: int, grid: list[list[str | None]], source: str,
                    cell_boxes: list[list[BBox | None]] | None = None,
                    bbox: BBox | None = None, header_rows: int = 1) -> Table:
    n_rows = len(grid)
    n_cols = max((len(r) for r in grid), default=0)
    grid = [list(r) + [None] * (n_cols - len(r)) for r in grid]
    covered = [[False] * n_cols for _ in range(n_rows)]
    cells: list[Cell] = []
    for r in range(n_rows):
        for c in range(n_cols):
            if covered[r][c] or grid[r][c] is None:
                continue
            col_span = 1
            while c + col_span < n_cols and grid[r][c + col_span] is None and not covered[r][c + col_span]:
                col_span += 1
            row_span = 1
            while (r + row_span < n_rows
                   and all(grid[r + row_span][cc] is None for cc in range(c, c + col_span))):
                row_span += 1
            for rr in range(r, r + row_span):
                for cc in range(c, c + col_span):
                    covered[rr][cc] = True
            box = cell_boxes[r][c] if cell_boxes and r < len(cell_boxes) and c < len(cell_boxes[r]) else None
            cells.append(Cell(row=r, col=c, text=" ".join((grid[r][c] or "").split()),
                              row_span=row_span, col_span=col_span, bbox=box))
    return Table(id=table_id, page=page, n_rows=n_rows, n_cols=n_cols, header_rows=header_rows,
                 cells=cells, bbox=bbox, source=source)


@dataclass
class Word:
    text: str
    x0: float
    top: float
    x1: float
    bottom: float
    conf: float | None = None
    size: float | None = None   # font size (text layer only)
    bold: bool = False

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2


def group_lines(words: list[Word], tolerance: float = 3.0) -> list[list[Word]]:
    """Words -> lines (sorted top-to-bottom, left-to-right)."""
    lines: list[list[Word]] = []
    for w in sorted(words, key=lambda w: (round(w.top), w.x0)):
        for line in lines:
            if abs(line[0].top - w.top) <= tolerance or abs(line[0].bottom - w.bottom) <= tolerance:
                line.append(w)
                break
        else:
            lines.append([w])
    for line in lines:
        line.sort(key=lambda w: w.x0)
    lines.sort(key=lambda ln: min(w.top for w in ln))
    return lines


def header_columns(line: list[Word]) -> list[tuple[str, float, float]] | None:
    """[(column name, x0, x1)] if EVERY word of the line belongs to a known column name and there
    are at least 3 columns; otherwise None (the line is not a table header)."""
    labels: list[tuple[str, float, float]] = []
    i = 0
    while i < len(line):
        for n in (3, 2, 1):  # longest multi-word column name first ("unit price", "jed. cijena")
            chunk = line[i:i + n]
            if len(chunk) < n:
                continue
            text = fold(" ".join(w.text for w in chunk)).strip(" :")
            if text in _HEADER_TOKENS:
                labels.append((text, chunk[0].x0, chunk[-1].x1))
                i += n
                break
        else:
            return None
    return labels if len(labels) >= 3 else None


def reconstruct_from_words(lines: list[list[Word]], start: int, table_id: str, page: int,
                           source: str) -> tuple[Table, int] | None:
    """If lines[start] is a header line, build a table; returns (table, index after the table)."""
    cols = header_columns(lines[start])
    if not cols:
        return None
    # column boundaries: midpoints between header columns
    bounds = []
    for k, (_, x0, x1) in enumerate(cols):
        left = -1e9 if k == 0 else (cols[k - 1][2] + x0) / 2
        right = 1e9 if k == len(cols) - 1 else (x1 + cols[k + 1][1]) / 2
        bounds.append((left, right))
    header_line = lines[start]
    line_h = max(w.bottom - w.top for w in header_line)
    grid: list[list[str | None]] = [
        [" ".join(w.text for w in header_line if left <= w.xc < right) for left, right in bounds]]
    boxes: list[list[BBox | None]] = [[BBox.union([BBox(x0=w.x0, top=w.top, x1=w.x1, bottom=w.bottom)
                                                   for w in header_line if left <= w.xc < right])
                                       for left, right in bounds]]
    prev_bottom = max(w.bottom for w in header_line)
    in_totals = False
    i = start + 1
    while i < len(lines):
        line = lines[i]
        text = " ".join(w.text for w in line)
        is_total = bool(TOTAL_LABELS.match(text))
        if min(w.top for w in line) - prev_bottom > 2.5 * line_h or (in_totals and not is_total):
            break  # a vertical gap, or the first non-totals line after the totals block
        in_totals = in_totals or is_total
        row = ["" for _ in cols]
        row_boxes: list[list[BBox]] = [[] for _ in cols]
        for w in line:
            for k, (left, right) in enumerate(bounds):
                if left <= w.xc < right:
                    row[k] = (row[k] + " " + w.text).strip()
                    row_boxes[k].append(BBox(x0=w.x0, top=w.top, x1=w.x1, bottom=w.bottom))
                    break
        grid.append(row)
        boxes.append([BBox.union(b) for b in row_boxes])
        prev_bottom = max(w.bottom for w in line)
        i += 1
    all_boxes = [b for row in boxes for b in row]
    table = table_from_grid(table_id, page, grid, source, boxes, BBox.union(all_boxes))
    return table, i
