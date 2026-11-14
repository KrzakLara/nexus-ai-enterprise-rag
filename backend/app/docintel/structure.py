"""Normalized, JSON-serializable representation of a parsed document.

Coordinates: PDF points (1/72 inch), origin at the TOP-LEFT of the page (pdfplumber's convention;
OCR pixel boxes are converted with the render scale). A bounding box is present only when the
parser actually produced one. DOCX, XLSX, TXT and Markdown have no geometry, so their boxes are
None and are never invented. DOCX also has no fixed pagination: it is one logical page.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

PageMethod = Literal["text_layer", "ocr", "empty", "docx", "xlsx", "text"]
BlockKind = Literal["heading", "paragraph", "table"]


class BBox(BaseModel):
    x0: float
    top: float
    x1: float
    bottom: float

    @classmethod
    def union(cls, boxes: list[BBox | None]) -> BBox | None:
        real = [b for b in boxes if b is not None]
        if not real:
            return None
        return cls(x0=min(b.x0 for b in real), top=min(b.top for b in real),
                   x1=max(b.x1 for b in real), bottom=max(b.bottom for b in real))

    def rounded(self) -> BBox:
        return BBox(x0=round(self.x0, 1), top=round(self.top, 1),
                    x1=round(self.x1, 1), bottom=round(self.bottom, 1))


class Line(BaseModel):
    text: str
    bbox: BBox | None = None
    # Tesseract's mean word confidence (0-100) for OCR lines: an engine score, NOT a probability.
    ocr_confidence: float | None = None
    table_id: str | None = None  # set when the line is a rendered table row
    row: int | None = None


class Cell(BaseModel):
    row: int
    col: int
    text: str
    row_span: int = 1
    col_span: int = 1
    bbox: BBox | None = None


class Table(BaseModel):
    id: str                     # "t{page}-{index}"
    page: int
    n_rows: int
    n_cols: int
    header_rows: int = 1
    cells: list[Cell]
    bbox: BBox | None = None
    source: Literal["pdf_ruling", "ocr_lattice", "ocr_columns", "docx", "xlsx", "markdown", "text_columns"]

    def grid(self) -> list[list[str]]:
        """Row x column text grid. A merged cell's text is placed at its top-left position only."""
        grid = [["" for _ in range(self.n_cols)] for _ in range(self.n_rows)]
        for c in self.cells:
            grid[c.row][c.col] = c.text
        return grid

    def header(self) -> list[str]:
        return self.grid()[0] if self.n_rows else []

    def render_rows(self) -> list[str]:
        """One text line per row, cells joined by ' | ' (searchable by the RAG chunker)."""
        return [line.text for line in self.row_lines()]

    def row_lines(self) -> list[Line]:
        """Non-empty rows as lines that keep their table id, row index and row bounding box."""
        out = []
        for r, row in enumerate(self.grid()):
            if not any(row):
                continue
            boxes = [c.bbox for c in self.cells if c.row == r]
            out.append(Line(text=" | ".join(cell for cell in row if cell != ""),
                            bbox=BBox.union(boxes), table_id=self.id, row=r))
        return out


class Block(BaseModel):
    id: str                     # "p{page}-b{index}"
    kind: BlockKind
    page: int
    text: str
    lines: list[Line] = Field(default_factory=list)
    level: int | None = None    # heading level
    section: str | None = None  # nearest preceding heading
    bbox: BBox | None = None
    table: Table | None = None


class Page(BaseModel):
    number: int                 # 1-based; for XLSX the worksheet index
    method: PageMethod
    width: float | None = None
    height: float | None = None
    label: str | None = None    # e.g. the worksheet name
    blocks: list[Block] = Field(default_factory=list)
    ocr: dict | None = None     # engine, version, language, dpi, mean confidence

    @property
    def text(self) -> str:
        """Reading-order text of the page; tables rendered row by row."""
        parts = []
        for b in self.blocks:
            parts.append("\n".join(b.table.render_rows()) if b.table else b.text)
        return "\n".join(p for p in parts if p)


class ParsedDocument(BaseModel):
    format: Literal["pdf", "docx", "xlsx", "txt", "md"]
    parser: str                 # e.g. "pdfplumber 0.11.10 + tesseract 5.3.0"
    pages: list[Page]
    warnings: list[str] = Field(default_factory=list)

    def page_texts(self) -> list[str]:
        return [p.text for p in self.pages]

    def tables(self) -> list[Table]:
        return [b.table for p in self.pages for b in p.blocks if b.table is not None]

    def lines(self):
        """(page, block, line) in reading order; table rows are yielded as synthetic lines."""
        for page in self.pages:
            for block in page.blocks:
                if block.table is not None:
                    for row_line in block.table.row_lines():
                        yield page, block, row_line
                else:
                    for line in block.lines or [Line(text=block.text, bbox=block.bbox)]:
                        yield page, block, line

    @property
    def ocr_pages(self) -> list[int]:
        return [p.number for p in self.pages if p.method == "ocr"]
