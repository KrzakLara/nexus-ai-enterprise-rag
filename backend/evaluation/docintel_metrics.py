"""Pure metric functions for the Phase 3 evaluation (unit-tested in test_docintel_metrics.py).

Field-level counting over (document, field) slots that are present in the ground truth or the
prediction:
    TP  prediction == ground truth (both not null)
    FP  prediction not null and (ground truth null or different)
    FN  ground truth not null and (prediction null or different)
A WRONG value therefore counts as both a FP and a FN (standard for extraction). Exact match
counts slots where prediction == ground truth, including both null.
OCR: character / word error rate = Levenshtein distance / reference length, on
whitespace-normalized text.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

NUMERIC = {"money", "decimal", "percent"}


def norm(value_type: str, value: str | None):
    if value is None:
        return None
    if value_type in NUMERIC:
        try:
            return Decimal(value).normalize()
        except InvalidOperation:
            return value
    if value_type == "text":
        return re.sub(r"[\s]+", " ", value).strip().strip("\"'“”").rstrip(".,;:").casefold()
    return value.strip()


def equal(value_type: str, a: str | None, b: str | None) -> bool:
    return norm(value_type, a) == norm(value_type, b)


def prf(tp: int, fp: int, fn: int) -> dict:
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    f1 = 2 * p * r / (p + r) if p is not None and r is not None and p + r else (0.0 if p is not None and r is not None else None)
    return {"tp": tp, "fp": fp, "fn": fn, "precision": _r(p), "recall": _r(r), "f1": _r(f1)}


def _r(x):
    return None if x is None else round(x, 4)


def score_slots(slots: list[tuple[str, str | None, str | None]]) -> dict:
    """slots: (value_type, prediction, ground truth)."""
    tp = fp = fn = exact = 0
    for vtype, pred, gt in slots:
        same = equal(vtype, pred, gt)
        exact += same
        if pred is not None and gt is not None and same:
            tp += 1
            continue
        if pred is not None:
            fp += 1
        if gt is not None:
            fn += 1
    out = prf(tp, fp, fn)
    out.update(slots=len(slots), exact_match=_r(exact / len(slots)) if slots else None)
    return out


def levenshtein(a, b) -> int:
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def _ws(text: str) -> str:
    return " ".join(text.split())


def cer(reference: str, hypothesis: str) -> float:
    ref, hyp = _ws(reference), _ws(hypothesis)
    return round(levenshtein(ref, hyp) / max(len(ref), 1), 4)


def wer(reference: str, hypothesis: str) -> float:
    ref, hyp = _ws(reference).split(), _ws(hypothesis).split()
    return round(levenshtein(ref, hyp) / max(len(ref), 1), 4)


KEY_TOKEN = re.compile(r"[A-Za-zČĆŠĐŽčćšđž]*\d[\w.,/\-]*")


def key_token_recall(reference: str, hypothesis: str) -> float | None:
    """Share of the reference's numeric / identifier tokens (amounts, dates, IDs, IBAN groups)
    that appear verbatim in the hypothesis: 'extraction completeness' for business values."""
    ref = [t.strip(".,") for t in KEY_TOKEN.findall(reference)]
    if not ref:
        return None
    hyp = set(t.strip(".,") for t in KEY_TOKEN.findall(hypothesis))
    return round(sum(t in hyp for t in ref) / len(ref), 4)


def rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def set_scores(predicted: set, expected: set) -> dict:
    tp = len(predicted & expected)
    out = prf(tp, len(predicted - expected), len(expected - predicted))
    out["exact"] = predicted == expected
    return out
