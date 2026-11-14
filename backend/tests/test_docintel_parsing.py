"""Phase 3 parsing: format detection, layout, tables (incl. merged cells), normalization, OCR
pre-processing. Uses the REAL parsers on the frozen corpus files (one per supported format).
Tests marked `ocr` need the Tesseract binary (Docker image / CI) and are skipped without it."""
import io
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from backend.app.docintel import normalize as nz
from backend.app.docintel.ocr import ocr_available
from backend.app.docintel.ocr_tables import deskew, lattices, rulings
from backend.app.docintel.parsing import UnsupportedFormat, detect_format, parse_document
from backend.app.docintel.tables import map_header, table_from_grid

CORPUS = Path(__file__).resolve().parents[2] / "eval" / "docintel"
LABELS = {r["id"]: r for r in map(json.loads, (CORPUS / "labels.jsonl").read_text(encoding="utf-8").splitlines())}
needs_ocr = pytest.mark.skipif(not ocr_available(), reason="Tesseract OCR binary not installed")


def corpus(doc_id: str):
    path = CORPUS / LABELS[doc_id]["file"]
    return path.name, path.read_bytes()


# --------------------------------------------------------------------------- normalization

@pytest.mark.parametrize("text, value", [
    ("EUR 184,000.00", "184000.00"), ("48.000,00 EUR", "48000.00"), ("1.234,56", "1234.56"),
    ("1,234.56", "1234.56"), ("184,000", "184000"), ("12,50 €", "12.50"), ("1 280,00 kn", "1280.00"),
    ("-50,00", "-50.00"),
])
def test_money_parsing_handles_european_and_english_formats(text, value):
    amount, _ = nz.parse_money(text)
    assert nz.canonical_decimal(amount) == value


@pytest.mark.parametrize("text, iso", [
    ("31.03.2026.", "2026-03-31"), ("31/03/2026", "2026-03-31"), ("2026-03-31", "2026-03-31"),
    ("31 March 2026", "2026-03-31"), ("March 31, 2026", "2026-03-31"), ("31. ožujka 2026.", "2026-03-31"),
    ("1. siječnja 2027.", "2027-01-01"),
])
def test_dates_in_english_croatian_and_numeric_forms(text, iso):
    assert nz.parse_date(text).isoformat() == iso


def test_invalid_or_absent_values_are_none_not_guesses():
    assert nz.parse_date("31.02.2026.") is None          # impossible date
    assert nz.parse_date("no date here") is None
    assert nz.parse_money("no amount") is None
    assert nz.parse_decimal("1,23,45") is None           # malformed grouping
    assert nz.find_currency("EUR and USD") is None        # ambiguous currency


@pytest.mark.parametrize("text, iso", [("90 days", "P90D"), ("60 dana", "P60D"), ("3 months", "P3M"),
                                       ("3 mjeseca", "P3M"), ("Net 30", "P30D"), ("1 year", "P1Y")])
def test_durations_are_iso_8601(text, iso):
    assert nz.parse_duration(text) == iso


def test_iban_checksum_and_checksum_gated_ocr_normalization():
    assert nz.iban_checksum_ok("HR1210010051863000160")
    assert not nz.iban_checksum_ok("HR1210010051863000161")
    assert nz.find_iban("IBAN: HRO9 2340 0091 1100 0123 4") == "HR0923400091110001234"  # O read for 0
    assert nz.find_iban("IBAN: HR99 2340 0091 1100 0123 4") == "HR9923400091110001234"  # wrong: kept raw


def test_commercial_rounding_is_half_up():
    from decimal import Decimal

    assert nz.money2(Decimal("0.005")) == Decimal("0.01")
    assert nz.money2(Decimal("2.675")) == Decimal("2.68")   # binary float would give 2.67


# ------------------------------------------------------------------------- format detection

@pytest.mark.parametrize("filename, data, message", [
    ("a.pdf", b"PK\x03\x04 not a pdf", "not a PDF"),
    ("a.docx", b"%PDF-1.4", "not a valid DOCX"),
    ("a.xlsx", b"PK\x03\x04garbage", "Corrupt XLSX"),
    ("a.txt", b"%PDF-1.4 binary", "does not match"),
])
def test_content_must_match_extension(filename, data, message):
    with pytest.raises(ValueError, match=message):
        detect_format(filename, data)


def test_docx_extension_on_an_xlsx_archive_is_rejected():
    _, xlsx = corpus("di-i03")
    with pytest.raises(ValueError, match="not a DOCX"):
        detect_format("renamed.docx", xlsx)
    with pytest.raises(UnsupportedFormat):
        detect_format("slides.pptx", xlsx)


# ------------------------------------------------------------------------------- layout

def test_text_pdf_keeps_pages_headings_tables_and_boxes():
    doc = parse_document(*corpus("di-c01"))
    assert doc.format == "pdf" and [p.method for p in doc.pages] == ["text_layer", "text_layer"]
    headings = [b.text for p in doc.pages for b in p.blocks if b.kind == "heading"]
    assert "Atlas Logistics Supply Agreement" in headings and "1. Scope" in headings
    [table] = doc.tables()
    assert table.source == "pdf_ruling" and table.page == 2 and (table.n_rows, table.n_cols) == (3, 3)
    assert table.grid()[1] == ["Mobilisation", "2026-02-01", "EUR 20,000.00"]
    assert all(c.bbox is not None for c in table.cells) and table.bbox is not None
    para = next(b for b in doc.pages[0].blocks if b.kind == "paragraph")
    assert all(line.bbox is not None for line in para.lines)
    section = next(b for b in doc.pages[1].blocks if b.kind == "paragraph" and "insurance" in b.text)
    assert section.section == "1. Scope"


def test_docx_reading_order_and_vertically_merged_cell():
    doc = parse_document(*corpus("di-c03"))
    assert len(doc.pages) == 1 and doc.pages[0].method == "docx"
    [table] = doc.tables()
    merged = next(c for c in table.cells if c.row == 1 and c.col == 0)
    assert merged.text == "Availability" and merged.row_span == 2 and merged.col_span == 1
    assert not any(c.row == 2 and c.col == 0 for c in table.cells)   # covered position has no cell
    assert all(c.bbox is None for c in table.cells)                   # no invented geometry
    kinds = [b.kind for b in doc.pages[0].blocks]
    assert kinds.index("table") > kinds.index("heading")              # body order preserved


def test_xlsx_line_item_table_with_numeric_cells():
    doc = parse_document(*corpus("di-i03"))
    page = doc.pages[0]
    assert page.method == "xlsx" and page.label == "Invoice"
    [table] = doc.tables()
    assert map_header(table.header()) == {0: "description", 1: "quantity", 2: "unit_price", 3: "tax_rate", 4: "amount"}
    assert table.grid()[1][:3] == ["Cloud hosting May 2026", "1", "5200"]   # numeric cell, not text
    assert any("Invoice number | CY-INV-88213" == ln.text for ln in page.blocks[1].lines)


def test_xlsx_formula_without_cached_value_is_reported_not_invented():
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"], ws["B1"], ws["A2"], ws["B2"] = "Subtotal", 100, "Total", "=B1*1.25"
    buf = io.BytesIO()
    wb.save(buf)
    doc = parse_document("formula.xlsx", buf.getvalue())
    assert any("formula without cached value" in w for w in doc.warnings)
    assert "125" not in doc.pages[0].text


def test_markdown_pipe_table_and_text_aligned_table():
    md = parse_document(*corpus("di-i11"))
    assert md.tables()[0].source == "markdown" and md.tables()[0].n_cols == 5
    txt = parse_document(*corpus("di-i08"))
    [table] = txt.tables()
    assert table.source == "text_columns" and table.bbox is None
    assert table.grid()[1] == ["Strategy consulting (hours)", "24", "210.00", "0%", "5,040.00"]


def test_table_from_grid_derives_spans_from_merged_markers():
    t = table_from_grid("t1-0", 1, [["A", None, "C"], ["D", "E", "F"], [None, "H", "I"]], "pdf_ruling")
    first = next(c for c in t.cells if (c.row, c.col) == (0, 0))
    assert (first.row_span, first.col_span) == (1, 2)
    d = next(c for c in t.cells if (c.row, c.col) == (1, 0))
    assert (d.row_span, d.col_span) == (2, 1)
    assert t.render_rows() == ["A | C", "D | E | F", "H | I"]


def test_scanned_pdf_without_ocr_is_an_explicit_error():
    with pytest.raises(ValueError, match="OCR is disabled"):
        parse_document(*corpus("di-i06"), ocr=False)


def test_mixed_pdf_without_ocr_names_the_page_that_needs_it():
    with pytest.raises(ValueError, match="Page 2 has no text layer"):
        parse_document(*corpus("di-c08"), ocr=False)


# ------------------------------------------------------------------ OCR pre-processing (no OCR)

def _grid_image(angle: float = 0.0, merge_last_row: bool = False) -> Image.Image:
    img = Image.new("L", (1200, 700), 255)
    d = ImageDraw.Draw(img)
    xs, ys = [100, 400, 700, 1000], [100, 175, 250, 325]
    for y in ys:
        d.line([(xs[0], y), (xs[-1], y)], fill=0, width=3)
    for i, x in enumerate(xs):
        if merge_last_row and i in (1, 2):   # last row: one cell spanning all three columns
            d.line([(x, ys[0]), (x, ys[2])], fill=0, width=3)
        else:
            d.line([(x, ys[0]), (x, ys[-1])], fill=0, width=3)
    for k in range(6):
        d.text((120 + k * 150, 520), "text 1.234,56", fill=0)
    return img.rotate(angle, fillcolor=255) if angle else img


def test_deskew_recovers_a_small_rotation():
    _, angle = deskew(_grid_image(angle=1.2))
    assert angle == pytest.approx(-1.2, abs=0.25)


def test_ruling_lines_give_a_lattice_and_detect_merged_cells():
    gray = np.asarray(_grid_image(merge_last_row=True))
    h, v = rulings(gray, scale=300 / 72)
    [lat] = lattices(h, v, scale=300 / 72)
    assert len(lat["ys"]) == 4 and len(lat["xs"]) == 4
    assert {(2, 0), (2, 1)} <= lat["merged_right"]      # bottom row is one merged cell
    assert not any(r < 2 for r, _ in lat["merged_right"])
    assert not h[500:560, :].any()                      # text strokes are not ruling lines


# ------------------------------------------------------------------------- real OCR (Tesseract)

@needs_ocr
@pytest.mark.ocr
def test_scanned_invoice_text_and_ruled_table_are_recovered():
    doc = parse_document(*corpus("di-i06"))
    assert doc.ocr_pages == [1] and "tesseract" in doc.parser
    page = doc.pages[0]
    assert page.ocr["languages"] == "eng+hrv" and page.ocr["ruled_tables"] == 1
    [table] = doc.tables()
    assert table.source == "ocr_lattice" and table.grid()[1][:3] == ["Carton boxes type A", "500", "1.20"]
    assert "Invoice number: BOR-55120" in page.text


@needs_ocr
@pytest.mark.ocr
def test_croatian_diacritics_and_mixed_pdf():
    scan = parse_document(*corpus("di-c07"))
    assert "Čisto i Sjajno d.o.o." in scan.pages[0].text
    mixed = parse_document(*corpus("di-c08"))
    assert [p.method for p in mixed.pages] == ["text_layer", "ocr"] and mixed.ocr_pages == [2]
    assert "automatically renews" in mixed.pages[1].text
