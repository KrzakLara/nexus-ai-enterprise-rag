"""Locale-aware value parsing for English and Croatian business documents.

Every parser returns None when the text does not contain an unambiguous value of that type; values
are never guessed. Money uses Decimal (never binary floats).

Number rules (documented, deterministic):
  * both '.' and ',' present -> the LAST one is the decimal separator ("1.234,56", "1,234.56")
  * only one kind of separator, followed by exactly 3 digits -> thousands separator ("184,000",
    "1.500"); followed by 1-2 digits -> decimal separator ("12,50", "99.9")
  * spaces / non-breaking spaces / apostrophes between digit groups are thousands separators
Dates: ISO, day-first numeric (EU: 31.03.2026., 31/03/2026, 31-03-2026), English and Croatian
month names ("31 March 2026", "March 31, 2026", "31. ožujka 2026."). US month-first numeric dates
are NOT supported (03/04/2026 is read as 3 April).
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

ISO_CURRENCIES = frozenset("EUR USD GBP CHF HRK SEK NOK DKK PLN CZK HUF RSD BAM JPY CAD AUD".split())
_SYMBOLS = {"€": "EUR", "$": "USD", "£": "GBP", "kn": "HRK", "kuna": "HRK", "eura": "EUR",
            "euro": "EUR", "eur": "EUR", "usd": "USD", "gbp": "GBP", "chf": "CHF", "hrk": "HRK"}
_CURRENCY_TOKEN = re.compile(r"(?<![A-Za-z])(EUR|USD|GBP|CHF|HRK|SEK|NOK|DKK|PLN|CZK|HUF|RSD|BAM|"
                             r"JPY|CAD|AUD|eura|euro|kuna|kn)(?![A-Za-z])|[€$£]", re.IGNORECASE)
_NUMBER = re.compile(r"(?<![\w.,])[-−]?\d{1,3}(?:[  '’]\d{3})+(?:[.,]\d+)?(?![\w])"
                     r"|(?<![\w.,])[-−]?\d+(?:[.,]\d+)*(?![\w])")

MONTHS = {
    # English
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
    "oct": 10, "nov": 11, "dec": 12,
    # Croatian (nominative and genitive)
    "siječanj": 1, "siječnja": 1, "veljača": 2, "veljače": 2, "ožujak": 3, "ožujka": 3,
    "travanj": 4, "travnja": 4, "svibanj": 5, "svibnja": 5, "lipanj": 6, "lipnja": 6,
    "srpanj": 7, "srpnja": 7, "kolovoz": 8, "kolovoza": 8, "rujan": 9, "rujna": 9,
    "listopad": 10, "listopada": 10, "studeni": 11, "studenoga": 11, "studenog": 11,
    "prosinac": 12, "prosinca": 12,
}
_MONTH_ALT = "|".join(sorted(MONTHS, key=len, reverse=True))
_DATE_PATTERNS = [
    ("iso", re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")),
    ("dmy", re.compile(r"(?<!\d)(\d{1,2})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(\d{4})\.?(?!\d)")),
    ("d_month_y", re.compile(rf"(?<!\d)(\d{{1,2}})\.?\s+({_MONTH_ALT})\.?,?\s+(\d{{4}})\.?",
                             re.IGNORECASE)),
    ("month_d_y", re.compile(rf"\b({_MONTH_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})",
                             re.IGNORECASE)),
]
_DURATION = re.compile(
    r"(?:net\s*)?(\d{1,4})\s*(?:\(\w+(?:[- ]\w+)?\)\s*)?"
    r"(calendar\s+days?|business\s+days?|working\s+days?|days?|dana|dan|radnih\s+dana|"
    r"months?|mjeseci|mjeseca|mjesec|years?|godina|godine|godinu|weeks?|tjedana|tjedna)\b",
    re.IGNORECASE)
_NET = re.compile(r"\bnet\s*(\d{1,3})\b", re.IGNORECASE)
_PERCENT = re.compile(r"(?<![\d.,])(\d{1,2}(?:[.,]\d{1,2})?)\s*%")
_IBAN = re.compile(r"\b([A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,3})?)\b")


def fold(text: str) -> str:
    """Lowercase, collapse whitespace (diacritics are kept)."""
    return " ".join(unicodedata.normalize("NFC", text).lower().split())


def parse_decimal(token: str) -> Decimal | None:
    t = token.strip().replace("−", "-").replace(" ", " ")
    t = re.sub(r"(?<=\d)[ '’](?=\d{3}\b)", "", t)
    if not re.fullmatch(r"-?\d+(?:[.,]\d+)*", t):
        return None
    if "." in t and "," in t:
        dec = "." if t.rfind(".") > t.rfind(",") else ","
        thou = "," if dec == "." else "."
        if t.count(dec) != 1:
            return None
        t = t.replace(thou, "").replace(dec, ".")
    elif "," in t or "." in t:
        sep = "," if "," in t else "."
        groups = t.lstrip("-").split(sep)
        if len(groups) > 2 or len(groups[-1]) == 3:
            # thousands separators only: every group after the first has exactly 3 digits
            if not all(len(g) == 3 for g in groups[1:]) or len(groups[0]) > 3:
                return None
            t = t.replace(sep, "")
        else:
            t = t.replace(sep, ".")
    try:
        return Decimal(t)
    except InvalidOperation:
        return None


def find_currency(text: str) -> str | None:
    found = {_SYMBOLS.get(m.group(0).lower(), m.group(0).upper()) for m in _CURRENCY_TOKEN.finditer(text)}
    return found.pop() if len(found) == 1 else None


def parse_money(text: str) -> tuple[Decimal, str | None] | None:
    """First amount in the text with the (unambiguous) currency mentioned in the same text."""
    numbers = [m for m in _NUMBER.finditer(text) if not _looks_like_date_or_id(text, m)]
    for m in numbers:
        value = parse_decimal(m.group(0))
        if value is not None:
            return value, find_currency(text)
    return None


def _looks_like_date_or_id(text: str, m: re.Match) -> bool:
    before, after = text[max(0, m.start() - 1):m.start()], text[m.end():m.end() + 1]
    return before in "-/" and before != "" or after in "-/" and after != "" or "%" in text[m.end():m.end() + 2]


def parse_amount_only(text: str) -> Decimal | None:
    """The single number in a cell such as '1.234,56' or 'EUR 12.50' (None if 0 or >1 numbers)."""
    nums = [parse_decimal(m.group(0)) for m in _NUMBER.finditer(text)]
    nums = [n for n in nums if n is not None]
    return nums[0] if len(nums) == 1 else None


def parse_date(text: str) -> date | None:
    for kind, pattern in _DATE_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        try:
            if kind == "iso":
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if kind == "dmy":
                return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            if kind == "d_month_y":
                return date(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)))
            return date(int(m.group(3)), MONTHS[m.group(1).lower()], int(m.group(2)))
        except (ValueError, KeyError):
            return None
    return None


def parse_duration(text: str) -> str | None:
    """ISO-8601 duration: '90 days' -> 'P90D', '3 months' -> 'P3M', 'Net 30' -> 'P30D'."""
    m = _DURATION.search(text)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        if unit.startswith(("month", "mjesec")):
            return f"P{n}M"
        if unit.startswith(("year", "godin")):
            return f"P{n}Y"
        if unit.startswith(("week", "tjed")):
            return f"P{n * 7}D"
        return f"P{n}D"
    m = _NET.search(text)
    return f"P{int(m.group(1))}D" if m else None


def duration_days(iso: str) -> int | None:
    """Approximate length in days (months = 30, years = 365) for PLAUSIBILITY checks only."""
    m = re.fullmatch(r"P(\d+)([DMY])", iso or "")
    if not m:
        return None
    return int(m.group(1)) * {"D": 1, "M": 30, "Y": 365}[m.group(2)]


def parse_percent(text: str) -> Decimal | None:
    m = _PERCENT.search(text)
    return parse_decimal(m.group(1)) if m else None


_IBAN_LOOSE = re.compile(r"\b([A-Z]{2})([0-9OIL]{2})((?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,3})?)\b")
_OCR_DIGITS = str.maketrans({"O": "0", "I": "1", "L": "1"})


def find_iban(text: str) -> str | None:
    """IBAN in compact form. OCR often reads 0 as O (and 1 as I / L). A variant with those
    characters mapped to digits is used ONLY if it passes the ISO 13616 mod-97 checksum, which
    makes a wrong 'correction' very unlikely. Otherwise the raw reading is returned (and fails
    validation visibly)."""
    upper = text.upper()
    m = _IBAN.search(upper)
    raw = m.group(1).replace(" ", "") if m else None
    if raw and iban_checksum_ok(raw):
        return raw
    loose = _IBAN_LOOSE.search(upper)
    if loose:
        country, check, bban = loose.group(1), loose.group(2), loose.group(3).replace(" ", "")
        candidate = country + check.translate(_OCR_DIGITS) + bban.translate(_OCR_DIGITS)
        if iban_checksum_ok(candidate):
            return candidate
    return raw


def iban_checksum_ok(iban: str) -> bool:
    s = iban.replace(" ", "").upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", s):
        return False
    digits = "".join(str(int(ch, 36)) for ch in s[4:] + s[:4])
    return int(digits) % 97 == 1


def money2(value: Decimal) -> Decimal:
    """Commercial rounding to cents (ROUND_HALF_UP)."""
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def canonical_decimal(value: Decimal) -> str:
    """Stable string form without exponent ('1234.50' stays '1234.50')."""
    return format(value, "f")
