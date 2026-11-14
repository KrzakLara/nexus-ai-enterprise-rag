"""Local OCR with Tesseract 5 (eng + hrv) for PDF pages that have no usable text layer.

The page is rendered with pypdfium2 at OCR_DPI and passed to Tesseract (`image_to_data`), which
returns words with pixel boxes and a per-word confidence. Boxes are converted to PDF points
(top-left origin) by dividing by the render scale, so OCR and text-layer geometry share one unit.
Tesseract's confidence is an engine score in 0..100, not a calibrated probability.

If the binary is missing, OcrUnavailable is raised. The page is never silently treated as empty.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from functools import lru_cache

from .tables import Word

OCR_DPI = int(os.getenv("OCR_DPI", "300"))
OCR_LANGUAGES = os.getenv("OCR_LANGUAGES", "eng+hrv")


class OcrUnavailable(RuntimeError):
    """Tesseract (or a requested language pack) is not installed."""


@dataclass
class OcrPage:
    words: list[Word]
    width_pt: float
    height_pt: float
    meta: dict
    # ruled tables found on the raster: (text grid with None for merged cells, cell boxes in pt)
    tables: list[tuple[list[list[str | None]], list[list[tuple | None]]]] = None  # type: ignore[assignment]


@lru_cache(maxsize=1)
def tesseract_info() -> dict:
    import pytesseract

    cmd = os.getenv("TESSERACT_CMD") or shutil.which("tesseract")
    if not cmd:
        raise OcrUnavailable("Tesseract OCR binary not found (install tesseract-ocr, or set TESSERACT_CMD)")
    pytesseract.pytesseract.tesseract_cmd = cmd
    try:
        version = str(pytesseract.get_tesseract_version())
        langs = set(pytesseract.get_languages(config=""))
    except Exception as exc:  # noqa: BLE001
        raise OcrUnavailable(f"Tesseract is not usable: {exc}") from exc
    missing = [lang for lang in OCR_LANGUAGES.split("+") if lang not in langs]
    if missing:
        raise OcrUnavailable(f"Tesseract language data missing: {', '.join(missing)}")
    return {"engine": "tesseract", "version": version, "languages": OCR_LANGUAGES}


def ocr_available() -> bool:
    try:
        tesseract_info()
        return True
    except OcrUnavailable:
        return False


def ocr_image(image, scale: float) -> list[Word]:
    """Tesseract words of a PIL image; boxes divided by `scale` (pixels per point)."""
    import pytesseract

    tesseract_info()
    data = pytesseract.image_to_data(image, lang=OCR_LANGUAGES, config="--psm 4",
                                     output_type=pytesseract.Output.DICT)
    words = []
    for i, text in enumerate(data["text"]):
        text = (text or "").strip()
        conf = float(data["conf"][i])
        if not text or conf < 0:
            continue
        x, y, w, h = (data[k][i] for k in ("left", "top", "width", "height"))
        words.append(Word(text=text, x0=x / scale, top=y / scale, x1=(x + w) / scale,
                          bottom=(y + h) / scale, conf=conf))
    return words


def ocr_pdf_page(pdf_bytes: bytes, page_index: int) -> OcrPage:
    """Render -> deskew -> detect and remove ruling lines -> Tesseract -> rebuild ruled tables."""
    import numpy as np
    import pypdfium2 as pdfium
    from PIL import Image

    from .ocr_tables import deskew, lattice_grid, lattices, remove_rulings, rulings

    meta = dict(tesseract_info())
    pdf = pdfium.PdfDocument(pdf_bytes)
    try:
        page = pdf[page_index]
        width, height = page.get_size()
        scale = OCR_DPI / 72.0
        image = page.render(scale=scale, grayscale=True).to_pil()
    finally:
        pdf.close()
    image, angle = deskew(image)
    gray = np.asarray(image)
    h_mask, v_mask = rulings(gray, scale)
    words = ocr_image(Image.fromarray(remove_rulings(gray, h_mask, v_mask)), scale)
    tables = []
    for lattice in lattices(h_mask, v_mask, scale):
        pt = {"ys": [y / scale for y in lattice["ys"]], "xs": [x / scale for x in lattice["xs"]],
              "merged_right": lattice["merged_right"], "merged_down": lattice["merged_down"]}
        tables.append(lattice_grid(pt, words))
    confs = [w.conf for w in words if w.conf is not None]
    meta.update(dpi=OCR_DPI, words=len(words), deskew_degrees=angle, ruled_tables=len(tables),
                mean_confidence=round(sum(confs) / len(confs), 1) if confs else None,
                geometry="PDF points on the deskewed page raster")
    return OcrPage(words=words, width_pt=width, height_pt=height, meta=meta, tables=tables)
