"""Offline, deterministic extraction provider (no API key, no model).

Three rule families, all reading the normalized document (structure.py):
  * label rules     'Label: value', 'Label | value' (table / spreadsheet rows), or 'Label value'
                    when the value parses strictly as the field's type (OCR often drops colons).
                    Labels are matched longest-first, so 'Total excl. VAT' never matches 'Total'.
  * table rules     line items from tables whose header maps to known columns (tables.py).
  * sentence rules  renewal, termination, governing law, obligations, payable-within, notice.
Every value comes with the verbatim line / row / sentence it was read from. If a label occurs
twice with different values, the first is kept and a 'conflicting_values_in_document' issue is
attached. Nothing is inferred from other fields (no computed due dates, no guessed currency).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import normalize as nz
from .schemas import Evidence, ExtractedField, TableRef, ValidationIssue
from .structure import BBox, ParsedDocument
from .tables import TOTAL_LABELS, map_header

PROVIDER = "rules"
VERSION = "rules_v1"

INVOICE_LABELS: dict[str, list[str]] = {
    "invoice_number": ["invoice number", "invoice no.", "invoice no", "invoice #", "invoice nr.",
                       "invoice id", "broj računa", "račun broj", "račun br.", "broj fakture",
                       "invoice"],
    "supplier_name": ["supplier", "seller", "vendor", "issued by", "from", "dobavljač",
                      "prodavatelj", "izdavatelj računa", "izdavatelj"],
    "supplier_tax_id": ["supplier vat id", "vat id", "vat number", "vat no.", "vat reg. no.", "tax id",
                        "tax number", "oib dobavljača", "oib", "pdv id", "porezni broj"],
    "customer_name": ["customer", "bill to", "billed to", "buyer", "client", "kupac", "naručitelj",
                      "primatelj"],
    "invoice_date": ["invoice date", "date of issue", "issue date", "issued on", "datum računa",
                     "datum izdavanja računa", "datum izdavanja", "date"],
    "due_date": ["due date", "payment due", "pay by", "datum dospijeća", "dospijeće",
                 "valuta plaćanja", "rok plaćanja"],
    "currency": ["currency", "valuta"],
    "subtotal": ["subtotal", "sub-total", "net total", "total net", "net amount", "total excl. vat",
                 "total excluding vat", "total before tax", "osnovica", "iznos bez pdv-a",
                 "ukupno bez pdv-a", "međuzbroj"],
    "tax_amount": ["vat amount", "tax amount", "total vat", "vat", "tax", "iznos pdv-a", "pdv",
                   "porez"],
    "total_amount": ["total amount due", "total amount", "total due", "amount due", "grand total",
                     "total payable", "balance due", "total", "ukupno za platiti", "za platiti",
                     "ukupan iznos", "sveukupno", "ukupno s pdv-om", "ukupno"],
    "payment_reference": ["payment reference", "model i poziv na broj", "poziv na broj",
                          "reference", "ref."],
    "iban": ["iban", "iban primatelja", "bank account", "žiro račun"],
}
CONTRACT_LABELS: dict[str, list[str]] = {
    "contract_id": ["agreement id", "agreement no.", "agreement number", "contract id",
                    "contract no.", "contract number", "contract reference", "broj ugovora",
                    "ugovor br.", "ugovor broj", "oznaka ugovora"],
    "parties": ["parties", "party a", "party b", "counterparty", "supplier", "service provider", "tenant",
                "landlord",
                "provider", "customer", "client", "buyer", "licensor", "licensee",
                "ugovorne strane", "dobavljač", "pružatelj usluge", "naručitelj", "kupac",
                "izvršitelj", "isporučitelj"],
    "effective_date": ["effective date", "start date", "commencement date", "date of commencement",
                       "effective from", "valid from", "datum stupanja na snagu", "datum početka",
                       "početak primjene", "vrijedi od"],
    "expiration_date": ["end date", "expiration date", "expiry date", "expires on", "valid until",
                        "valid to", "term ends", "datum isteka", "datum završetka", "vrijedi do",
                        "istek ugovora"],
    "contract_value": ["annual value", "total contract value", "contract value", "total value",
                       "annual fee", "vrijednost ugovora", "godišnja vrijednost",
                       "ukupna vrijednost", "godišnja naknada"],
    "currency": ["currency", "valuta"],
    "payment_terms": ["payment terms", "terms of payment", "uvjeti plaćanja", "rok plaćanja"],
    "notice_period": ["notice period", "termination notice", "otkazni rok", "rok otkaza"],
    "governing_law": ["governing law", "applicable law", "mjerodavno pravo", "primjenjivo pravo"],
}
LABELS = {"invoice": INVOICE_LABELS, "contract": CONTRACT_LABELS}
VALUE_TYPES = {
    "invoice_number": "id", "supplier_name": "text", "supplier_tax_id": "tax_id",
    "customer_name": "text", "invoice_date": "date", "due_date": "date", "currency": "currency",
    "subtotal": "money", "tax_amount": "money", "total_amount": "money", "payment_reference": "text",
    "iban": "iban", "contract_id": "id", "parties": "text", "effective_date": "date",
    "expiration_date": "date", "contract_value": "money", "payment_terms": "duration",
    "notice_period": "duration", "governing_law": "text",
}
CONTRACT_TYPES = [  # (category, title keywords)
    ("amendment", ("amendment", "addendum", "aneks", "dodatak ugovoru")),
    ("nda", ("non-disclosure", "nondisclosure", "confidentiality", "nda", "povjerljivosti")),
    ("license", ("licence", "license", "licenc")),
    ("lease", ("lease", "zakup", "najam", "najmu")),
    ("framework", ("framework", "okvirni")),
    ("supply", ("supply", "supplier", "purchase", "isporuc", "isporuk", "opskrb", "kupoprodaj")),
    ("services", ("service", "services", "maintenance", "hosting", "consulting", "usluga", "usluge",
                  "održavanj", "savjetovanj")),
]
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-/_.]*\d[A-Za-z0-9\-/_.]*")
_TAX_ID = re.compile(r"\b(?:[A-Z]{2}\s?)?\d[0-9A-Z \-]{5,20}")
_PARENTHETICAL = re.compile(r"\s*\((?:the\s+|u daljnjem tekstu:?\s*)?[\"“”'‘’]?[^)]*[\"“”'‘’]?\)\s*$", re.I)
_PERCENT_TOKEN = re.compile(r"\(?\s*\d{1,2}(?:[.,]\d{1,2})?\s*%\s*\)?")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+(?=[A-ZČĆŠĐŽ0-9(\"“])")


@dataclass
class _Line:
    text: str
    page: int
    block_id: str
    bbox: BBox | None
    table_id: str | None = None
    row: int | None = None


def _evidence(doc_id: str, ln: _Line, text: str | None = None, col: int | None = None,
              bbox: BBox | None = None) -> Evidence:
    table = TableRef(table_id=ln.table_id, row=ln.row, col=col) if ln.table_id is not None else None
    return Evidence(document_id=doc_id, page=ln.page, text=text or ln.text, block_id=ln.block_id,
                    bbox=bbox or ln.bbox, table=table)


def _lines(doc: ParsedDocument) -> list[_Line]:
    return [_Line(line.text, page.number, block.id, line.bbox, line.table_id, line.row)
            for page, block, line in doc.lines() if line.text.strip()]


def _sorted_labels(labels: dict[str, list[str]]) -> list[tuple[str, str]]:
    pairs = [(label, field) for field, values in labels.items() for label in values]
    return sorted(pairs, key=lambda p: -len(p[0]))


def _label_hits(text: str, labels: list[tuple[str, str]]):
    """Yield (field, value_text, had_separator) for every 'label value' pair in a line."""
    segments = [s.strip() for s in text.split(" | ")] if " | " in text else [text]
    for k, seg in enumerate(segments):
        seg_c = " ".join(seg.split())
        low = seg_c.lower()
        for label, field in labels:
            if not low.startswith(label):
                continue
            rest = seg_c[len(label):]
            if rest and (rest[0].isalnum() and not label.endswith((".", "#"))):
                continue  # 'Invoices' must not match 'invoice'
            sep = bool(re.match(r"\s*[:;：]", rest)) or rest.startswith("  ")
            value = re.sub(r"^\s*[:;：.\-–—]?\s*", "", rest).strip()
            if not value and k + 1 < len(segments):
                value, sep = segments[k + 1], True
            if value:
                yield field, value, sep
            break


def _parse_value(kind: str, text: str, strict: bool) -> tuple[str, str] | None:
    """(canonical value, raw) or None. `strict`: no separator after the label (OCR), so the value
    must parse as its type; free text is then rejected."""
    if kind == "date":
        d = nz.parse_date(text)
        return (d.isoformat(), text) if d else None
    if kind == "money":
        cleaned = _PERCENT_TOKEN.sub(" ", text)
        m = nz.parse_money(cleaned)
        return (nz.canonical_decimal(m[0]), text) if m else None
    if kind == "duration":
        d = nz.parse_duration(text)
        return (d, text) if d else None
    if kind == "currency":
        c = nz.find_currency(text)
        if not c:
            code = text.strip().upper()[:3]
            c = code if code in nz.ISO_CURRENCIES and len(text.strip()) == 3 else None
        return (c, text) if c else None
    if kind == "iban":
        iban = nz.find_iban(text)
        return (iban, text) if iban else None
    if kind == "tax_id":
        m = _TAX_ID.search(text)
        value = re.sub(r"\s", "", m.group(0)).upper().strip("-") if m else ""
        return (value, text) if sum(ch.isdigit() for ch in value) >= 6 else None
    if kind == "id":
        m = _ID.search(text)
        return (m.group(0).rstrip("."), text) if m else None
    if strict:
        return None
    value = _PARENTHETICAL.sub("", text).strip().rstrip(",;")
    last = value.split()[-1] if value.split() else ""
    if value.endswith(".") and "." not in last[:-1] and len(last) > 3:
        value = value[:-1]  # sentence full stop; abbreviations such as 'd.o.o.' / 'd.d.' keep theirs
    return (value[:200], text) if value else None


def extract(doc: ParsedDocument, document_id: str, doc_type: str) -> list[ExtractedField]:
    if doc_type not in LABELS:
        return []
    lines = _lines(doc)
    labels = _sorted_labels(LABELS[doc_type])
    method = f"{PROVIDER}:label"
    found: dict[str, ExtractedField] = {}
    parties: list[ExtractedField] = []
    line_item_rows = _line_item_rows(doc)
    for ln in lines:
        if ln.table_id is not None and (ln.table_id, ln.row) in line_item_rows:
            continue  # line-item rows are handled by the table rules
        for field, value_text, sep in _label_hits(ln.text, labels):
            kind = VALUE_TYPES[field]
            parsed = _parse_value(kind, value_text, strict=not sep)
            if parsed is None:
                continue
            value, raw = parsed
            vtype = {"id": "text"}.get(kind, kind)
            ev = _evidence(document_id, ln)
            if field == "parties":
                if value.lower() not in {p.value.lower() for p in parties}:
                    parties.append(ExtractedField(name=f"parties[{len(parties)}]", value_type="text",
                                                  value=value, raw=raw, evidence=ev, method=method))
                continue
            if field in found:
                prev = found[field]
                if prev.value != value and not any(i.code == "conflicting_values_in_document"
                                                   and i.actual == value for i in prev.issues):
                    prev.issues.append(ValidationIssue(
                        field=field, code="conflicting_values_in_document", severity="warning",
                        message=f"'{field}' also appears as {value!r} on page {ln.page}",
                        expected=prev.value, actual=value))
                continue
            found[field] = ExtractedField(name=field, value_type=vtype, value=value, raw=raw,
                                          evidence=ev, method=method)
            if field in ("total_amount", "contract_value", "subtotal") and "currency" not in found:
                cur = nz.find_currency(ln.text)
                if cur:
                    found["currency"] = ExtractedField(name="currency", value_type="currency",
                                                       value=cur, raw=cur, evidence=ev,
                                                       method=f"{PROVIDER}:currency_of_amount")
    out = list(found.values()) + parties
    if doc_type == "invoice":
        out += _line_items(doc, document_id)
        if "currency" not in found:
            out += _document_currency(lines, document_id)
    else:
        out += _contract_sentences(doc, document_id, found, has_parties=bool(parties))
        out += _contract_type(doc, document_id)
    return out


# ---------------------------------------------------------------------------------- line items

def _line_item_tables(doc: ParsedDocument):
    for table in doc.tables():
        mapping = map_header(table.header())
        names = set(mapping.values())
        if "description" in names and ({"amount"} <= names or {"quantity", "unit_price"} <= names):
            yield table, mapping


def _line_item_rows(doc: ParsedDocument) -> set[tuple[str, int]]:
    rows = set()
    for table, _ in _line_item_tables(doc):
        for r in range(table.header_rows, table.n_rows):
            first = next((c for c in table.grid()[r] if c), "")
            if not TOTAL_LABELS.match(first):
                rows.add((table.id, r))
    return rows


def _line_items(doc: ParsedDocument, document_id: str) -> list[ExtractedField]:
    out: list[ExtractedField] = []
    index = 0
    kinds = {"description": "text", "quantity": "decimal", "unit_price": "money", "tax_rate": "percent",
             "amount": "money"}
    for table, mapping in _line_item_tables(doc):
        grid = table.grid()
        boxes = {(c.row, c.col): c.bbox for c in table.cells}
        row_lines = {ln.row: ln for ln in table.row_lines()}
        for r in range(table.header_rows, table.n_rows):
            row = grid[r]
            if not any(row) or TOTAL_LABELS.match(next((c for c in row if c), "")):
                continue
            ln = _Line(row_lines[r].text, table.page, f"table:{table.id}", row_lines[r].bbox, table.id, r)
            item: list[ExtractedField] = []
            for col, name in mapping.items():
                cell = row[col] if col < len(row) else ""
                if not cell:
                    continue
                kind = kinds[name]
                if kind == "text":
                    value = " ".join(cell.split())
                elif kind == "percent":
                    p = nz.parse_percent(cell) if "%" in cell else nz.parse_amount_only(cell)
                    value = nz.canonical_decimal(p) if p is not None else None
                else:
                    num = nz.parse_amount_only(re.sub(r"(?i)\b(kom|pcs|h|hrs|sati|x|eur|usd|€)\b|€", " ", cell))
                    value = nz.canonical_decimal(num) if num is not None else None
                if value is None:
                    continue
                item.append(ExtractedField(
                    name=f"line_items[{index}].{name}", value_type=kind, value=value, raw=cell,
                    evidence=_evidence(document_id, ln, text=ln.text, col=col, bbox=boxes.get((r, col))),
                    method=f"{PROVIDER}:table"))
            if any(f.name.endswith(".amount") or f.name.endswith(".unit_price") for f in item):
                out += item
                index += 1
    return out


def _document_currency(lines: list[_Line], document_id: str) -> list[ExtractedField]:
    """Currency only if exactly one currency is mentioned in the whole document."""
    codes = {c for ln in lines if (c := nz.find_currency(ln.text))}
    if len(codes) != 1:
        return []
    code = codes.pop()
    ln = next(ln for ln in lines if nz.find_currency(ln.text) == code)
    return [ExtractedField(name="currency", value_type="currency", value=code, raw=code,
                           evidence=_evidence(document_id, ln), method=f"{PROVIDER}:document_currency")]


# ---------------------------------------------------------------------------- contract sentences

# Croatian legal phrasing names the jurisdiction in the genitive; the canonical value is English.
JURISDICTION_CANONICAL = {"hrvatske": "Croatia", "republike hrvatske": "Croatia"}
RENEWAL_POSITIVE = re.compile(r"automatically renew|renews automatically|shall be renewed automatically|"
                              r"auto-renew|automatically extended|automatski (se )?(produžuje|obnavlja|"
                              r"produljuje)|prešutno (se )?produžuje", re.I)
RENEWAL_NEGATIVE = re.compile(r"(will|shall|does) not (be )?(automatically )?renew|no automatic renewal|"
                              r"not renew automatically|ne produžuje (se )?automatski|nema automatskog "
                              r"(produljenja|produženja)|neće se automatski", re.I)
TERMINATION = re.compile(r"\bterminat|\braskid|\braskinuti|\botkaz", re.I)
GOVERNED = re.compile(r"governed by (?:and construed in accordance with )?the laws? of ([^.,;]+)|"
                      r"mjerodavno je pravo ([^.,;]+)", re.I)
OBLIGATION = re.compile(r"\bshall\b|\bmust\b|\bobvezuje se\b|\bdužan je\b|\bdužna je\b", re.I)
PAYABLE = re.compile(r"(payable|paid|plaća se|platiti|plaćaju se)\b.{0,40}?\b(within|in roku od|u roku od|"
                     r"net)\b.{0,15}?\d+\s*(days?|dana)", re.I)
NOTICE = re.compile(r"\d+\s*(calendar\s+)?days?['’]?\s*(prior\s+)?(written\s+)?notice|"
                    r"otkaznim rokom od \d+|otkazni rok (je|iznosi) \d+|(\d+) dana unaprijed", re.I)
EFFECTIVE = re.compile(r"(effective|enters into force|stupa na snagu)\s+(as of|from|on|dana)?\s*", re.I)
UNTIL = re.compile(r"(remain in force until|valid until|expires on|ostaje na snazi do|vrijedi do)\s*", re.I)


def _sentences(doc: ParsedDocument):
    for page in doc.pages:
        for block in page.blocks:
            if block.kind != "paragraph":
                continue
            text = " ".join(block.text.split())
            for sentence in _SENTENCE_SPLIT.split(text):
                if len(sentence) > 12:
                    yield page.number, block, sentence


def _contract_sentences(doc: ParsedDocument, document_id: str, found: dict,
                        has_parties: bool) -> list[ExtractedField]:
    out: list[ExtractedField] = []
    have = set(found)

    def ev(page, block, sentence):
        return Evidence(document_id=document_id, page=page, text=sentence, block_id=block.id,
                        bbox=block.bbox)

    obligations = 0
    renewal_done = termination_done = False
    for page, block, s in _sentences(doc):
        if not renewal_done and (RENEWAL_NEGATIVE.search(s) or RENEWAL_POSITIVE.search(s)):
            positive = bool(RENEWAL_POSITIVE.search(s)) and not RENEWAL_NEGATIVE.search(s)
            e = ev(page, block, s)
            out.append(ExtractedField(name="auto_renewal", value_type="bool", value="true" if positive else "false",
                                      raw=s, evidence=e, method=f"{PROVIDER}:sentence"))
            out.append(ExtractedField(name="renewal_conditions", value_type="text", value=s, raw=s,
                                      evidence=e, method=f"{PROVIDER}:sentence"))
            renewal_done = True
        if not termination_done and TERMINATION.search(s) and not re.match(r"^\s*\d+(\.\d+)*\.?\s*\w+\s*$", s):
            out.append(ExtractedField(name="termination_conditions", value_type="text", value=s, raw=s,
                                      evidence=ev(page, block, s), method=f"{PROVIDER}:sentence"))
            termination_done = True
        if "governing_law" not in have:
            m = GOVERNED.search(s)
            if m:
                value = re.sub(r"^(the|republike)\s+", "", (m.group(1) or m.group(2)).strip(), flags=re.I)
                value = JURISDICTION_CANONICAL.get(value.lower(), value)
                out.append(ExtractedField(name="governing_law", value_type="text", value=value,
                                          raw=m.group(0), evidence=ev(page, block, s),
                                          method=f"{PROVIDER}:sentence"))
                have.add("governing_law")
        if "payment_terms" not in have and PAYABLE.search(s):
            d = nz.parse_duration(PAYABLE.search(s).group(0))
            if d:
                out.append(ExtractedField(name="payment_terms", value_type="duration", value=d, raw=s,
                                          evidence=ev(page, block, s), method=f"{PROVIDER}:sentence"))
                have.add("payment_terms")
        if "notice_period" not in have and NOTICE.search(s):
            d = nz.parse_duration(NOTICE.search(s).group(0).replace("unaprijed", ""))
            if d:
                out.append(ExtractedField(name="notice_period", value_type="duration", value=d, raw=s,
                                          evidence=ev(page, block, s), method=f"{PROVIDER}:sentence"))
                have.add("notice_period")
        for key, pattern in (("effective_date", EFFECTIVE), ("expiration_date", UNTIL)):
            if key not in have:
                m = pattern.search(s)
                d = nz.parse_date(s[m.end():m.end() + 40]) if m else None
                if d:
                    out.append(ExtractedField(name=key, value_type="date", value=d.isoformat(), raw=s,
                                              evidence=ev(page, block, s), method=f"{PROVIDER}:sentence"))
                    have.add(key)
        if obligations < 3 and OBLIGATION.search(s) and not TERMINATION.search(s):
            out.append(ExtractedField(name=f"obligations[{obligations}]", value_type="text", value=s, raw=s,
                                      evidence=ev(page, block, s), method=f"{PROVIDER}:sentence"))
            obligations += 1
    if not has_parties:
        out += _parties_from_between(doc, document_id)
    return out


_BETWEEN = re.compile(r"\bbetween\s+(.+?)\s*(?:\([^)]*\))?\s*,?\s+and\s+(.+?)\s*(?:\([^)]*\))?\s*[.,;]|"
                      r"\bizmeđu\s+(.+?)\s*(?:\([^)]*\))?\s*,?\s+i\s+(.+?)\s*(?:\([^)]*\))?\s*[.,;]", re.I)


def _parties_from_between(doc: ParsedDocument, document_id: str) -> list[ExtractedField]:
    for page, block, s in _sentences(doc):
        m = _BETWEEN.search(s)
        if m:
            names = [g for g in m.groups() if g]
            return [ExtractedField(name=f"parties[{i}]", value_type="text", value=n.strip(), raw=n,
                                   evidence=Evidence(document_id=document_id, page=page, text=s,
                                                     block_id=block.id, bbox=block.bbox),
                                   method=f"{PROVIDER}:sentence") for i, n in enumerate(names[:2])]
    return []


def _contract_type(doc: ParsedDocument, document_id: str) -> list[ExtractedField]:
    for page in doc.pages:
        for block in page.blocks:
            if block.kind not in ("heading", "paragraph"):
                continue
            title = block.text.split("\n")[0]
            low = title.lower()
            if not re.search(r"agreement|contract|ugovor|aneks|addendum|amendment", low):
                continue
            for category, words in CONTRACT_TYPES:
                if any(w in low for w in words):
                    return [ExtractedField(name="contract_type", value_type="text", value=category, raw=title,
                                           evidence=Evidence(document_id=document_id, page=page.number,
                                                             text=title, block_id=block.id, bbox=block.bbox),
                                           method=f"{PROVIDER}:title_keywords")]
            return []
    return []
