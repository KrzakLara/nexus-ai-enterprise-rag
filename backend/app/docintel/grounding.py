"""Evidence grounding: does each value really come from the cited place in THIS document?

Checks, in order (the first failure decides):
  1. a value without evidence                      -> 'no_evidence' + error missing_evidence
  2. evidence for another document                 -> 'unsupported' + error evidence_wrong_document
  3. page outside the document                     -> 'unsupported' + error evidence_invalid_page
  4. evidence text not on that page (whitespace-
     insensitive), or a table reference that does
     not exist                                     -> 'unsupported' + error evidence_not_in_document
  5. the value's canonical form appears verbatim   -> 'exact'
     the value is one of the values of its type
     parsed from the evidence (or the cited cell)  -> 'normalized'
     otherwise                                     -> 'unsupported' + error value_not_supported
This is exact, rule-based matching. It is not a semantic or model-based judgement, and it yields no
probability. A field that is not 'exact' or 'normalized' cannot be accepted without review.
"""
from __future__ import annotations

import re
from decimal import Decimal

from . import normalize as nz
from .rules import (CONTRACT_TYPES, JURISDICTION_CANONICAL, RENEWAL_NEGATIVE, RENEWAL_POSITIVE,
                    VALUE_PERIOD_OF_LABEL)


PERIOD_WORDS = {"annual": ("annual", "yearly", "per year", "godišnj"),
                "monthly": ("monthly", "per month", "mjesečn"),
                "total": ("total", "ukupn", "contract value", "vrijednost ugovora")}


def derived_support(name: str, value: str, text: str) -> str | None:
    """Support for values that are DERIVED, not copied: a contract category inferred from title
    keywords, or a jurisdiction translated from Croatian genitive phrasing. Checked against the
    same keyword / alias tables the extractor uses ('normalized', never 'exact')."""
    low = ws(text)
    if name == "contract_type":
        words = dict(CONTRACT_TYPES).get(value, ())
        return "normalized" if any(w in low for w in words) else None
    if name == "value_period":
        words = [k for k, v in VALUE_PERIOD_OF_LABEL.items() if v == value] + list(PERIOD_WORDS.get(value, ()))
        return "normalized" if any(w in low for w in words) else None
    if name == "governing_law":
        aliases = [k for k, v in JURISDICTION_CANONICAL.items() if v == value]
        return "normalized" if any(a in low for a in aliases) else None
    return None
from .schemas import Evidence, ExtractedField, ValidationIssue
from .structure import ParsedDocument


def ws(text: str) -> str:
    return " ".join(text.split()).lower()


def _decimals(text: str) -> set[Decimal]:
    """Standalone numbers in the text; digits joined to other digits by '-' or '/' are parts of
    a date or an identifier (2026-12-30, NS-2026-14, 12/2026) and are not amounts."""
    out = set()
    for m in nz._NUMBER.finditer(text):
        token, start, end = m.group(0), m.start(), m.end()
        digits_start = start + (1 if token[0] in "-−" else 0)
        joined_left = digits_start >= 1 and text[digits_start - 1] in "-/" and digits_start >= 2 \
            and text[digits_start - 2].isalnum()
        joined_right = end + 1 < len(text) and text[end] in "-/" and text[end + 1].isalnum()
        if joined_left or joined_right:
            continue
        d = nz.parse_decimal(token)
        if d is not None:
            out.add(d)
    return out


def _dates(text: str) -> set[str]:
    found = set()
    for _, pattern in nz._DATE_PATTERNS:
        for m in pattern.finditer(text):
            d = nz.parse_date(m.group(0))
            if d:
                found.add(d.isoformat())
    return found


def supports(value_type: str, value: str, text: str) -> str | None:
    """'exact' / 'normalized' if `text` supports `value`, else None.

    Numbers are compared numerically against every number token in the text (a substring test
    would accept '30' inside '2026-12-30'); text values must appear verbatim (case- and
    whitespace-insensitive)."""
    if value_type in ("money", "decimal", "percent"):
        return "normalized" if Decimal(value) in _decimals(text) else None
    if value_type in ("text", "date") and ws(value) in ws(text):
        return "exact"
    if value_type == "date":
        return "normalized" if value in _dates(text) else None
    if value_type == "duration":
        found = {nz.parse_duration(m.group(0)) for m in nz._DURATION.finditer(text)}
        found |= {f"P{int(m.group(1))}D" for m in nz._NET.finditer(text)}
        if not found and re.search(r"\d+ dana unaprijed", text):
            found.add(nz.parse_duration(re.search(r"\d+ dana", text).group(0)))
        return "normalized" if value in found else None
    if value_type == "currency":
        codes = {nz._SYMBOLS.get(m.group(0).lower(), m.group(0).upper()) for m in nz._CURRENCY_TOKEN.finditer(text)}
        return "normalized" if value in codes else None
    if value_type == "iban":
        if value in text.replace(" ", "").upper():
            return "normalized"
        return "normalized" if nz.find_iban(text) == value else None  # checksum-gated OCR reading
    if value_type == "tax_id":
        return "normalized" if value in text.replace(" ", "") else None
    if value_type == "bool":
        positive = bool(RENEWAL_POSITIVE.search(text)) and not RENEWAL_NEGATIVE.search(text)
        negative = bool(RENEWAL_NEGATIVE.search(text))
        return "normalized" if (value == "true" and positive) or (value == "false" and negative) else None
    return None


def locate(doc: ParsedDocument, evidence: Evidence) -> tuple[str | None, str | None]:
    """(problem code, text that supports the value) for an evidence reference."""
    if not 1 <= evidence.page <= len(doc.pages):
        return "evidence_invalid_page", None
    page = doc.pages[evidence.page - 1]
    if ws(evidence.text) not in ws(page.text):
        return "evidence_not_in_document", None
    if evidence.table is not None:
        table = next((t for t in doc.tables() if t.id == evidence.table.table_id), None)
        if table is None or table.page != evidence.page or not 0 <= evidence.table.row < table.n_rows:
            return "evidence_not_in_document", None
        if evidence.table.col is not None:
            if not 0 <= evidence.table.col < table.n_cols:
                return "evidence_not_in_document", None
            return None, table.grid()[evidence.table.row][evidence.table.col]
    return None, evidence.text


def ground(field: ExtractedField, doc: ParsedDocument, document_id: str,
           value: str | None = None, evidence: Evidence | None = None) -> tuple[str, list[ValidationIssue]]:
    """Grounding of `value` (default: the field's value) against `evidence` (default: its own)."""
    value = field.value if value is None else value
    evidence = field.evidence if evidence is None else evidence
    if value is None:
        return "no_evidence", []
    if evidence is None:
        return "no_evidence", [ValidationIssue(field=field.name, code="missing_evidence", severity="error",
                                               message="value has no supporting evidence")]
    if evidence.document_id != document_id:
        return "unsupported", [ValidationIssue(field=field.name, code="evidence_wrong_document",
                                               severity="error", message="evidence cites another document")]
    problem, support_text = locate(doc, evidence)
    if problem:
        return "unsupported", [ValidationIssue(field=field.name, code=problem, severity="error",
                                               message=f"evidence check failed: {problem}")]
    result = supports(field.value_type, value, support_text or "") or derived_support(field.name, value,
                                                                                      support_text or "")
    if result is None:
        return "unsupported", [ValidationIssue(field=field.name, code="value_not_supported", severity="error",
                                               message="the cited evidence does not contain this value",
                                               actual=value)]
    return result, []


def locate_quote(doc: ParsedDocument, page: int | None, quote: str) -> tuple[int, str] | None:
    """Find a (model- or reviewer-supplied) quote in the document, whitespace- and
    case-insensitively; returns (page, the matching line / sentence text as it appears)."""
    target = ws(quote)
    if not target:
        return None
    pages = [doc.pages[page - 1]] if page and 1 <= page <= len(doc.pages) else doc.pages
    for p in pages:
        if target in ws(p.text):
            for _, block, line in doc.lines():
                if block.page == p.number and target in ws(line.text):
                    return p.number, line.text
            return p.number, quote  # spans several lines: the quote itself, verified above
    return None
