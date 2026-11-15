"""Image pre-processing for OCR of scanned pages with ruled tables (numpy + Pillow only).

1. deskew     the projection-profile method: try small angles and keep the one whose row profile
              of dark pixels is sharpest (scans are usually tilted by < 2 degrees).
2. rulings    pixels in long horizontal / vertical runs of dark pixels are table lines; text strokes
              are far shorter than MIN_RUN.
3. clean      ruling pixels are whitened before OCR. Grid lines otherwise make Tesseract emit
              garbage for every table row.
4. lattice    line positions give row and column boundaries; a missing border segment between two
              cells means the cells are merged. OCR words are assigned to cells by their centre.
All geometry is in pixels of the DESKEWED raster; ocr.py converts it to PDF points.
"""
from __future__ import annotations

import numpy as np
from PIL import Image

DARK = 140
MIN_RUN_PT = 30.0       # a ruling line is at least 30 pt long (text strokes are much shorter)


def deskew(image: Image.Image, max_angle: float = 2.0, step: float = 0.1) -> tuple[Image.Image, float]:
    small = image.convert("L").resize((max(1, image.width // 4), max(1, image.height // 4)))
    best_angle, best_score = 0.0, -1.0
    for angle in np.arange(-max_angle, max_angle + step / 2, step):
        rotated = np.asarray(small.rotate(float(angle), resample=Image.BILINEAR, fillcolor=255))
        profile = (rotated < DARK).sum(axis=1).astype(np.float64)
        score = float(np.square(np.diff(profile)).sum())
        if score > best_score:
            best_angle, best_score = float(angle), score
    if abs(best_angle) < step / 2:
        return image.convert("L"), 0.0
    return image.convert("L").rotate(best_angle, resample=Image.BICUBIC, fillcolor=255), round(best_angle, 2)


def _runs(dark: np.ndarray, length: int) -> np.ndarray:
    """Mask of pixels that belong to a horizontal run of >= `length` dark pixels."""
    h, w = dark.shape
    if w < length:
        return np.zeros_like(dark, dtype=bool)
    d = dark.astype(np.int32)
    cs = np.concatenate([np.zeros((h, 1), np.int32), np.cumsum(d, axis=1)], axis=1)
    full = (cs[:, length:] - cs[:, :-length]) == length              # window starts, w-length+1
    fc = np.concatenate([np.zeros((h, 1), np.int32), np.cumsum(full, axis=1)], axis=1)
    j = np.arange(w)
    lo = np.clip(j - length + 1, 0, w - length + 1)
    hi = np.clip(j + 1, 0, w - length + 1)
    return (fc[:, hi] - fc[:, lo]) > 0


def rulings(gray: np.ndarray, scale: float) -> tuple[np.ndarray, np.ndarray]:
    dark = gray < DARK
    length = max(10, int(MIN_RUN_PT * scale))
    return _runs(dark, length), _runs(dark.T, length).T


def remove_rulings(gray: np.ndarray, h_mask: np.ndarray, v_mask: np.ndarray) -> np.ndarray:
    lines = h_mask | v_mask
    # grow the mask by one pixel so anti-aliased line edges disappear too
    grown = lines.copy()
    grown[1:, :] |= lines[:-1, :]
    grown[:-1, :] |= lines[1:, :]
    grown[:, 1:] |= lines[:, :-1]
    grown[:, :-1] |= lines[:, 1:]
    out = gray.copy()
    out[grown] = 255
    return out


def _centres(profile: np.ndarray, minimum: float) -> list[int]:
    """Centres of consecutive index runs where profile >= minimum."""
    idx = np.where(profile >= minimum)[0]
    if idx.size == 0:
        return []
    groups, start, prev = [], idx[0], idx[0]
    for i in idx[1:]:
        if i != prev + 1:
            groups.append((start + prev) // 2)
            start = i
        prev = i
    groups.append((start + prev) // 2)
    return [int(g) for g in groups]


def lattices(h_mask: np.ndarray, v_mask: np.ndarray, scale: float) -> list[dict]:
    """Ruled tables: [{'ys': [...], 'xs': [...], 'merged_right': set, 'merged_down': set}] in pixels."""
    min_len = MIN_RUN_PT * scale
    ys_all = _centres(h_mask.sum(axis=1), min_len)
    tables = []
    group: list[int] = []
    max_gap = 60 * scale  # rows of one table are < 60 pt apart
    for y in ys_all + [10 ** 9]:
        if group and y - group[-1] > max_gap:
            if len(group) >= 2:
                tables.append(group)
            group = []
        group.append(y)
    out = []
    for ys in tables:
        y0, y1 = ys[0], ys[-1]
        band = v_mask[y0:y1 + 1, :]
        xs = _centres(band.sum(axis=0), 0.5 * (y1 - y0))
        if len(xs) < 2:
            continue
        merged_right, merged_down = set(), set()
        for r in range(len(ys) - 1):
            for c in range(len(xs) - 1):
                if c + 1 < len(xs) - 1:  # border between (r, c) and (r, c+1) at xs[c+1]
                    seg = v_mask[ys[r] + 2:ys[r + 1] - 1, max(0, xs[c + 1] - 3):xs[c + 1] + 4].any(axis=1)
                    if seg.size and seg.mean() < 0.5:
                        merged_right.add((r, c))
                if r + 1 < len(ys) - 1:  # border between (r, c) and (r+1, c) at ys[r+1]
                    seg = h_mask[max(0, ys[r + 1] - 3):ys[r + 1] + 4, xs[c] + 2:xs[c + 1] - 1].any(axis=0)
                    if seg.size and seg.mean() < 0.5:
                        merged_down.add((r, c))
        out.append({"ys": ys, "xs": xs, "merged_right": merged_right, "merged_down": merged_down})
    return out


def lattice_grid(lattice: dict, words) -> tuple[list[list[str | None]], list[list[tuple | None]]]:
    """Assign words (pixel boxes) to lattice cells; merged cells -> None (table_from_grid style)."""
    ys, xs = lattice["ys"], lattice["xs"]
    n_rows, n_cols = len(ys) - 1, len(xs) - 1
    anchor = {}
    for r in range(n_rows):
        for c in range(n_cols):
            a = (r, c)
            if c > 0 and (r, c - 1) in lattice["merged_right"]:
                a = anchor[(r, c - 1)]
            elif r > 0 and (r - 1, c) in lattice["merged_down"]:
                a = anchor[(r - 1, c)]
            anchor[(r, c)] = a
    texts: dict[tuple, list] = {}
    for w in words:
        xc, yc = (w.x0 + w.x1) / 2, (w.top + w.bottom) / 2
        r = next((i for i in range(n_rows) if ys[i] <= yc < ys[i + 1]), None)
        c = next((j for j in range(n_cols) if xs[j] <= xc < xs[j + 1]), None)
        if r is not None and c is not None:
            texts.setdefault(anchor[(r, c)], []).append(w)
    grid: list[list[str | None]] = []
    boxes: list[list[tuple | None]] = []
    for r in range(n_rows):
        row, brow = [], []
        for c in range(n_cols):
            if anchor[(r, c)] != (r, c):
                row.append(None)
                brow.append(None)
                continue
            ws = sorted(texts.get((r, c), []), key=lambda w: (round(w.top / 8), w.x0))
            row.append(" ".join(w.text for w in ws))
            brow.append((xs[c], ys[r], xs[c + 1], ys[r + 1]))
        grid.append(row)
        boxes.append(brow)
    return grid, boxes
