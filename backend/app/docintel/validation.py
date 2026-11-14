"""Field-level business validation over EFFECTIVE values (extracted, or reviewer-corrected).

Validation never changes a value: it reports issues (with the expected value where it can be
computed) and leaves the correction to a human. All arithmetic uses Decimal with commercial
rounding to cents (ROUND_HALF_UP); line-level tolerance is 0.01, sums allow 0.01 per summed line.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from . import normalize as nz
from .schemas import ValidationIssue, required_fields

CENT = Decimal("0.01")


def _issue(field, code, message, severity="error", expected=None, actual=None) -> ValidationIssue:
    return ValidationIssue(field=field, code=code, severity=severity, message=message,
                           expected=None if expected is None else str(expected),
                           actual=None if actual is None else str(actual))


def _dec(values: dict, name: str) -> Decimal | None:
    v = values.get(name)
    return Decimal(v) if v is not None else None


def _date(values: dict, name: str) -> date | None:
    v = values.get(name)
    return date.fromisoformat(v) if v else None


def _required(doc_type: str, values: dict) -> list[ValidationIssue]:
    issues = []
    for name in required_fields(doc_type):
        present = values.get(name) is not None or any(
            k.startswith(f"{name}[") and v is not None for k, v in values.items())
        if not present:
            issues.append(_issue(name, "required_missing", f"required field '{name}' was not found"))
    return issues


def validate(doc_type: str, values: dict[str, str | None],
             evidence_text: dict[str, str] | None = None) -> list[ValidationIssue]:
    """`values`: field name -> canonical value (None = missing). `evidence_text`: field name ->
    evidence line, used for the currency-consistency check."""
    if doc_type == "contract":
        return _required(doc_type, values) + validate_contract(values)
    if doc_type == "invoice":
        return _required(doc_type, values) + validate_invoice(values, evidence_text or {})
    return []


def validate_contract(v: dict) -> list[ValidationIssue]:
    issues = []
    start, end = _date(v, "effective_date"), _date(v, "expiration_date")
    if start and end and end <= start:
        issues.append(_issue("expiration_date", "expiration_not_after_effective",
                             "expiration date must be after the effective date",
                             expected=f"> {start.isoformat()}", actual=end.isoformat()))
    value = _dec(v, "contract_value")
    if value is not None and value < 0:
        issues.append(_issue("contract_value", "negative_amount", "contract value is negative", actual=value))
    if value is not None and v.get("currency") is None:
        issues.append(_issue("currency", "currency_missing", "contract value has no currency", "warning"))
    for name, low, high in (("notice_period", 1, 730), ("payment_terms", 0, 180)):
        days = nz.duration_days(v.get(name) or "")
        if v.get(name) and (days is None or not low <= days <= high):
            issues.append(_issue(name, f"implausible_{name}", f"{name} outside {low}-{high} days",
                                 "warning", expected=f"{low}-{high} days", actual=v.get(name)))
    return issues


def _line_indices(v: dict) -> list[int]:
    return sorted({int(m.group(1)) for k in v if (m := re.match(r"line_items\[(\d+)\]\.", k))})


def validate_invoice(v: dict, evidence_text: dict[str, str]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    lines = _line_indices(v)
    amounts: list[Decimal] = []
    taxes: list[Decimal | None] = []
    for i in lines:
        q, u = _dec(v, f"line_items[{i}].quantity"), _dec(v, f"line_items[{i}].unit_price")
        a, r = _dec(v, f"line_items[{i}].amount"), _dec(v, f"line_items[{i}].tax_rate")
        if q is not None and u is not None and a is not None:
            expected = nz.money2(q * u)
            if abs(expected - nz.money2(a)) > CENT:
                issues.append(_issue(f"line_items[{i}].amount", "line_amount_mismatch",
                                     f"line {i + 1}: quantity x unit price != amount",
                                     expected=expected, actual=a))
        if a is not None:
            amounts.append(a)
            taxes.append(a * r / 100 if r is not None else None)
        if r is not None and not Decimal(0) <= r <= Decimal(30):
            issues.append(_issue(f"line_items[{i}].tax_rate", "implausible_tax_rate",
                                 "tax rate outside 0-30 %", "warning", actual=r))
    subtotal, tax, total = _dec(v, "subtotal"), _dec(v, "tax_amount"), _dec(v, "total_amount")
    tolerance = CENT * max(1, len(amounts))
    if subtotal is not None and amounts and len(amounts) == len(lines):
        expected = nz.money2(sum(amounts))
        if abs(expected - subtotal) > tolerance:
            issues.append(_issue("subtotal", "subtotal_mismatch", "subtotal != sum of line amounts",
                                 expected=expected, actual=subtotal))
    if tax is not None and taxes and all(t is not None for t in taxes):
        expected = nz.money2(sum(taxes))  # type: ignore[arg-type]
        if abs(expected - tax) > tolerance:
            issues.append(_issue("tax_amount", "tax_mismatch", "tax amount != sum of line amount x tax rate",
                                 expected=expected, actual=tax))
    base = subtotal if subtotal is not None else (nz.money2(sum(amounts)) if amounts and len(amounts) == len(lines) else None)
    if total is not None and base is not None and tax is not None:
        expected = nz.money2(base + tax)
        if abs(expected - total) > CENT:
            issues.append(_issue("total_amount", "total_mismatch", "total != subtotal + tax",
                                 expected=expected, actual=total))
    elif total is not None and base is not None and tax is None and abs(base - total) > CENT:
        issues.append(_issue("total_amount", "tax_amount_missing",
                             "total differs from subtotal but no tax amount was found", "warning",
                             expected=base, actual=total))
    for name in ("subtotal", "tax_amount", "total_amount"):
        d = _dec(v, name)
        if d is not None and d < 0:
            issues.append(_issue(name, "negative_amount", f"{name} is negative (credit note?)", "warning", actual=d))
    currency = v.get("currency")
    if currency:
        for name, text in evidence_text.items():
            if name in ("subtotal", "tax_amount", "total_amount") or name.endswith((".amount", ".unit_price")):
                found = nz.find_currency(text)
                if found and found != currency:
                    issues.append(_issue(name, "currency_inconsistent",
                                         f"{name} is in {found}, the invoice currency is {currency}",
                                         expected=currency, actual=found))
    issued, due = _date(v, "invoice_date"), _date(v, "due_date")
    if issued and due:
        if due < issued:
            issues.append(_issue("due_date", "due_before_invoice_date", "due date is before the invoice date",
                                 expected=f">= {issued.isoformat()}", actual=due.isoformat()))
        elif (due - issued).days > 365:
            issues.append(_issue("due_date", "implausible_due_date", "due date more than a year after issue",
                                 "warning", actual=due.isoformat()))
    iban = v.get("iban")
    if iban and not nz.iban_checksum_ok(iban):
        issues.append(_issue("iban", "iban_checksum", "IBAN check digits are invalid (ISO 13616 mod 97)",
                             actual=iban))
    return issues
