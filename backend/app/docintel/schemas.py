"""Pydantic v2 extraction schemas. Every field carries its value, the verbatim evidence (document,
page, line text, block, bounding box / table cell when available), the extraction method, the
grounding result and its validation issues. A missing value is None; values are never invented.

Canonical value formats (enforced by `ExtractedField`):
    date      ISO 8601 'YYYY-MM-DD'          money / decimal / percent   Decimal string ('1234.50')
    duration  ISO 8601 'P90D' / 'P3M' / 'P1Y'  currency  ISO 4217 code ('EUR')
    bool      'true' / 'false'                 iban      compact upper case ('HR12...')
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .normalize import ISO_CURRENCIES
from .structure import BBox

ValueType = Literal["text", "date", "money", "decimal", "percent", "duration", "currency", "bool",
                    "iban", "tax_id"]
Grounding = Literal["exact", "normalized", "unsupported", "no_evidence", "reviewer_asserted"]
DocType = Literal["contract", "invoice", "unknown"]
SCHEMA_VERSION = "1"


class TableRef(BaseModel):
    table_id: str
    row: int
    col: int | None = None


class Evidence(BaseModel):
    document_id: str
    page: int
    text: str                       # verbatim line / table row from the parsed document
    block_id: str | None = None
    bbox: BBox | None = None        # only when the parser produced geometry
    table: TableRef | None = None


class ValidationIssue(BaseModel):
    field: str
    code: str
    severity: Literal["error", "warning"]
    message: str
    expected: str | None = None
    actual: str | None = None


def check_canonical(value_type: str, value: str) -> None:
    """Raise ValueError if `value` is not in the canonical format of `value_type`."""
    if value_type == "date":
        date.fromisoformat(value)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("date must be YYYY-MM-DD")
    elif value_type in ("money", "decimal", "percent"):
        try:
            Decimal(value)
        except InvalidOperation:
            raise ValueError(f"not a decimal number: {value!r}") from None
        if not re.fullmatch(r"-?\d+(\.\d+)?", value):
            raise ValueError("decimal must be written like 1234.50")
    elif value_type == "duration":
        if not re.fullmatch(r"P\d+[DMY]", value):
            raise ValueError("duration must be ISO 8601, e.g. P90D")
    elif value_type == "currency":
        if value not in ISO_CURRENCIES:
            raise ValueError(f"unsupported currency code {value!r}")
    elif value_type == "bool":
        if value not in ("true", "false"):
            raise ValueError("bool must be 'true' or 'false'")
    elif value_type == "iban":
        if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", value):
            raise ValueError("IBAN must be compact upper case")
    elif not value.strip():
        raise ValueError("empty text")


class ExtractedField(BaseModel):
    name: str                       # 'total_amount', 'line_items[2].quantity'
    value_type: ValueType
    value: str | None = None
    raw: str | None = None          # the surface string the value was read from
    evidence: Evidence | None = None
    method: str                     # 'rules:label', 'rules:table', 'rules:sentence', 'llm:<model>'
    grounding: Grounding = "no_evidence"
    issues: list[ValidationIssue] = Field(default_factory=list)

    @model_validator(mode="after")
    def _canonical(self):
        if self.value is not None:
            check_canonical(self.value_type, self.value)
        return self


# ------------------------------------------------------------------------- field registries

FieldSpec = tuple[str, str, bool]   # (name, value_type, required)
CONTRACT_FIELDS: list[FieldSpec] = [
    ("contract_id", "text", True), ("contract_type", "text", False), ("parties", "text_list", True),
    ("effective_date", "date", True), ("expiration_date", "date", False),
    ("contract_value", "money", False), ("currency", "currency", False),
    ("payment_terms", "duration", False), ("notice_period", "duration", False),
    ("auto_renewal", "bool", False), ("renewal_conditions", "text", False),
    ("termination_conditions", "text", False), ("governing_law", "text", False),
    ("obligations", "text_list", False),
]
INVOICE_FIELDS: list[FieldSpec] = [
    ("invoice_number", "text", True), ("supplier_name", "text", True),
    ("supplier_tax_id", "tax_id", False), ("customer_name", "text", False),
    ("invoice_date", "date", True), ("due_date", "date", False), ("currency", "currency", True),
    ("subtotal", "money", False), ("tax_amount", "money", False), ("total_amount", "money", True),
    ("payment_reference", "text", False), ("iban", "iban", False),
]
LINE_ITEM_FIELDS: list[FieldSpec] = [
    ("description", "text", True), ("quantity", "decimal", False), ("unit_price", "money", False),
    ("tax_rate", "percent", False), ("amount", "money", True),
]
FIELDS_BY_TYPE = {"contract": CONTRACT_FIELDS, "invoice": INVOICE_FIELDS, "unknown": []}
_LINE = re.compile(r"line_items\[(\d+)\]\.(\w+)$")
_LIST = re.compile(r"(\w+)\[(\d+)\]$")   # parties[0], obligations[2]: one evidence per item


def value_type_of(doc_type: str, name: str) -> str:
    m = _LINE.match(name)
    if m:
        key, specs = m.group(2), LINE_ITEM_FIELDS
    else:
        lm = _LIST.match(name)
        key, specs = (lm.group(1) if lm else name), FIELDS_BY_TYPE[doc_type]
    for spec_name, vtype, _ in specs:
        if spec_name == key:
            if vtype == "text_list":
                if not _LIST.match(name):
                    raise KeyError(f"{name!r} is a list field; use {name}[i]")
                return "text"
            return vtype
    raise KeyError(f"unknown field {name!r} for {doc_type}")


def base_name(name: str) -> str:
    """'parties[1]' -> 'parties', 'line_items[0].amount' -> 'line_items', 'iban' -> 'iban'."""
    return name.split("[", 1)[0]


def required_fields(doc_type: str) -> list[str]:
    """Required base names (a list field is satisfied by at least one item)."""
    return [name for name, _, req in FIELDS_BY_TYPE[doc_type] if req]


# --------------------------------------------------------------------------- typed documents

class LineItem(BaseModel):
    description: ExtractedField
    quantity: ExtractedField
    unit_price: ExtractedField
    tax_rate: ExtractedField
    amount: ExtractedField


class ContractExtraction(BaseModel):
    document_type: Literal["contract"] = "contract"
    contract_id: ExtractedField
    contract_type: ExtractedField
    parties: list[ExtractedField]
    effective_date: ExtractedField
    expiration_date: ExtractedField
    contract_value: ExtractedField
    currency: ExtractedField
    payment_terms: ExtractedField
    notice_period: ExtractedField
    auto_renewal: ExtractedField
    renewal_conditions: ExtractedField
    termination_conditions: ExtractedField
    governing_law: ExtractedField
    obligations: list[ExtractedField]


class InvoiceExtraction(BaseModel):
    document_type: Literal["invoice"] = "invoice"
    invoice_number: ExtractedField
    supplier_name: ExtractedField
    supplier_tax_id: ExtractedField
    customer_name: ExtractedField
    invoice_date: ExtractedField
    due_date: ExtractedField
    currency: ExtractedField
    line_items: list[LineItem]
    subtotal: ExtractedField
    tax_amount: ExtractedField
    total_amount: ExtractedField
    payment_reference: ExtractedField
    iban: ExtractedField


def typed(doc_type: str, fields: list[ExtractedField]) -> ContractExtraction | InvoiceExtraction | None:
    """Validate a flat field list against the typed schema (raises pydantic.ValidationError)."""
    if doc_type == "unknown":
        return None
    by_name = {f.name: f for f in fields}
    data: dict = {}
    for name, vt, _ in FIELDS_BY_TYPE[doc_type]:
        if vt == "text_list":
            data[name] = sorted((f for f in fields if _LIST.match(f.name) and base_name(f.name) == name),
                                key=lambda f: int(_LIST.match(f.name).group(2)))
        else:
            data[name] = by_name.get(name) or ExtractedField(name=name, value_type=vt, method="none")
    if doc_type == "contract":
        return ContractExtraction(**data)
    indices = sorted({int(m.group(1)) for f in fields if (m := _LINE.match(f.name))})
    items = []
    for i in indices:
        items.append(LineItem(**{
            col: by_name.get(f"line_items[{i}].{col}")
            or ExtractedField(name=f"line_items[{i}].{col}", value_type=vt, method="none")
            for col, vt, _ in LINE_ITEM_FIELDS}))
    return InvoiceExtraction(**data, line_items=items)
