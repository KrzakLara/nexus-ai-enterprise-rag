"""The Phase 3 document-intelligence corpus: 32 fictional contracts, invoices and other documents.

GROUND TRUTH comes from the authored parameters of each document (the values the document is
written to state). It is NEVER read back from an extractor. Rendering formats those values into
text: European / English number styles, numeric / long / Croatian dates, label wording variants.
Each labelled field is tagged on the element that prints it, so its ground-truth page is a
layout fact. scripts/build_docintel_dataset.py renders the corpus once. The files, labels and
SHA-256 hashes are committed and frozen (eval/docintel/).

Honest caveats: the corpus is synthetic and was written by the same team that wrote the rule
extractor, so results are optimistic. Test-split documents deliberately use some label wordings
and layouts that the rules were not written for ('novel' below); those are not fixed afterwards.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

PERSONAS = {  # key -> (email, tenant, role, groups)
    "ns_procurement": ("procurement@northstar.demo", "northstar", "member", ["procurement"]),
    "ns_finance": ("finance@northstar.demo", "northstar", "member", ["finance"]),
    "ns_legal": ("legal@northstar.demo", "northstar", "member", ["legal"]),
    "ns_reviewer": ("reviewer@northstar.demo", "northstar", "reviewer", ["procurement", "finance"]),
    "ns_admin": ("admin@northstar.demo", "northstar", "admin", []),
    "oc_procurement": ("procurement@otherco.demo", "otherco", "member", ["procurement"]),
    "oc_reviewer": ("reviewer@otherco.demo", "otherco", "reviewer", ["procurement"]),
}
OWNER = {"northstar": "ns_admin", "otherco": "oc_reviewer"}
HR_MONTHS = ["siječnja", "veljače", "ožujka", "travnja", "svibnja", "lipnja", "srpnja", "kolovoza",
             "rujna", "listopada", "studenoga", "prosinca"]
EN_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
             "October", "November", "December"]
CENT = Decimal("0.01")


@dataclass
class DocSpec:
    id: str
    split: str                 # dev | test
    tenant: str
    groups: list[str]
    format: str                # pdf | scan | mixed | docx | xlsx | txt | md
    language: str              # en | hr
    doc_type: str              # contract | invoice | unknown
    pages: list[list[tuple]]
    fields: dict[str, str | None] = field(default_factory=dict)   # canonical ground truth
    parties: list[str] = field(default_factory=list)
    line_items: list[dict] = field(default_factory=list)
    clauses: list[str] = field(default_factory=list)              # fields expected to be present (text clauses)
    expected_errors: list[str] = field(default_factory=list)      # validation codes the document should trigger
    expected_warnings: list[str] = field(default_factory=list)
    scanned_pages: list[int] = field(default_factory=list)
    notes: str = ""
    xlsx_numbers: dict = field(default_factory=dict)


# ------------------------------------------------------------------------------ formatting

def money(v: Decimal, style: str) -> str:
    q = v.quantize(CENT, rounding=ROUND_HALF_UP)
    s = f"{q:,.2f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".") if style == "eu" else s


def fdate(d: date, style: str) -> str:
    return {"iso": d.isoformat(), "eu": f"{d.day:02d}.{d.month:02d}.{d.year}.",
            "eu_slash": f"{d.day:02d}/{d.month:02d}/{d.year}", "hr_long": f"{d.day}. {HR_MONTHS[d.month - 1]} {d.year}.",
            "en_long": f"{d.day} {EN_MONTHS[d.month - 1]} {d.year}",
            "en_us_long": f"{EN_MONTHS[d.month - 1]} {d.day}, {d.year}"}[style]


def hr_iban(bban: str) -> str:
    """A Croatian IBAN with correct ISO 13616 check digits, printed in groups of four."""
    digits = "".join(str(int(ch, 36)) for ch in bban + "HR00")
    compact = f"HR{98 - int(digits) % 97:02d}{bban}"
    return " ".join(compact[i:i + 4] for i in range(0, len(compact), 4))


def c2(v: Decimal) -> str:
    return str(v.quantize(CENT, rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------------- invoices

INV_LABELS = {
    "en": {"title": "INVOICE", "number": "Invoice number", "date": "Invoice date", "due": "Due date",
           "supplier": "Supplier", "vat_id": "VAT ID", "customer": "Customer", "currency": "Currency",
           "subtotal": "Subtotal", "tax": "VAT", "total": "Total due", "ref": "Payment reference", "iban": "IBAN",
           "cols": ["Description", "Qty", "Unit price", "VAT %", "Amount"]},
    "hr": {"title": "RAČUN", "number": "Broj računa", "date": "Datum računa", "due": "Datum dospijeća",
           "supplier": "Dobavljač", "vat_id": "OIB", "customer": "Kupac", "currency": "Valuta",
           "subtotal": "Osnovica", "tax": "PDV", "total": "Ukupno za platiti", "ref": "Poziv na broj",
           "iban": "IBAN", "cols": ["Opis", "Količina", "Jed. cijena", "PDV %", "Iznos"]},
}


def invoice(id, split, lang, fmt, tenant, groups, number, supplier, tax_id, customer, issued, due, currency,
            items, iban, ref, num_style="eu", date_style="eu", labels=None, perturb=None, omit=(),
            expected_errors=(), expected_warnings=(), notes="", currency_in_amounts=True) -> DocSpec:
    L = dict(INV_LABELS[lang])
    L.update(labels or {})
    perturb = perturb or {}
    rows, gt_items, subtotal, tax = [], [], Decimal(0), Decimal(0)
    for i, (desc, qty, price, rate) in enumerate(items):
        amount = (Decimal(qty) * Decimal(price)).quantize(CENT, rounding=ROUND_HALF_UP)
        printed = amount + perturb.get("line", {}).get(i, Decimal(0))
        subtotal += printed
        tax += printed * Decimal(rate) / 100
        qty_text = str(qty) if Decimal(qty) == int(Decimal(qty)) else money(Decimal(qty), num_style)
        rows.append([desc, qty_text, money(Decimal(price), num_style), f"{rate}%", money(printed, num_style)])
        gt_items.append({"description": desc, "quantity": str(Decimal(qty).normalize()), "unit_price": c2(Decimal(price)),
                         "tax_rate": str(Decimal(rate).normalize()), "amount": c2(printed)})
    tax = tax.quantize(CENT, rounding=ROUND_HALF_UP)
    subtotal_p = subtotal + perturb.get("subtotal", Decimal(0))
    total_p = subtotal_p + tax + perturb.get("total", Decimal(0))
    cur = (lambda v: f"{money(v, num_style)} {currency}") if currency_in_amounts else (lambda v: money(v, num_style))
    head = [("title", f"{L['title']} {number}" if fmt != "xlsx" else L["title"]),
            ("kv", L["number"], number, "invoice_number"),
            ("kv", L["date"], fdate(issued, date_style), "invoice_date")]
    if "due_date" not in omit:
        head.append(("kv", L["due"], fdate(due, date_style), "due_date"))
    head += [("kv", L["supplier"], supplier, "supplier_name")]
    if "supplier_tax_id" not in omit:
        head.append(("kv", L["vat_id"], tax_id, "supplier_tax_id"))
    head += [("kv", L["customer"], customer, "customer_name"), ("kv", L["currency"], currency, "currency")]
    table = ("table", L["cols"], rows + [[L["subtotal"], "", "", "", cur(subtotal_p)],
                                         [L["tax"], "", "", "", cur(tax)],
                                         [L["total"], "", "", "", cur(total_p)]],
             {"tag": "line_items", "widths": [3.2, 1, 1.4, 1, 1.6], "numeric": [1, 2, 3, 4]})
    tail = []
    if "iban" not in omit:
        tail.append(("kv", L["iban"], iban, "iban"))
    if "payment_reference" not in omit:
        tail.append(("kv", L["ref"], ref, "payment_reference"))
    pages = [head + [table] + tail]
    fields = {"invoice_number": number, "supplier_name": supplier,
              "supplier_tax_id": None if "supplier_tax_id" in omit else tax_id.replace(" ", ""),
              "customer_name": customer, "invoice_date": issued.isoformat(),
              "due_date": None if "due_date" in omit else due.isoformat(), "currency": currency,
              "subtotal": c2(subtotal_p), "tax_amount": c2(tax), "total_amount": c2(total_p),
              "payment_reference": None if "payment_reference" in omit else ref,
              "iban": None if "iban" in omit else iban.replace(" ", "")}
    numbers = {}
    if fmt == "xlsx":  # an accounting export stores numbers, not formatted strings
        for row in rows:
            for k in (1, 2, 4):
                numbers[row[k]] = float(Decimal(row[k].replace(".", "").replace(",", ".")) if num_style == "eu"
                                        else Decimal(row[k].replace(",", "")))
    return DocSpec(id=id, split=split, tenant=tenant, groups=groups, format=fmt, language=lang, doc_type="invoice",
                   pages=pages, fields=fields, line_items=gt_items, expected_errors=list(expected_errors),
                   expected_warnings=list(expected_warnings),
                   scanned_pages=[1] if fmt == "scan" else [], notes=notes, xlsx_numbers=numbers)


# -------------------------------------------------------------------------------- contracts

CON_TEXT = {
    "en": {"id": "Agreement ID", "start": "Effective date", "end": "End date", "value": "Annual value",
           "notice": "Notice period", "pay": "Payment terms", "law": "Governing law",
           "party_a": "Customer", "party_b": "Supplier"},
    "hr": {"id": "Broj ugovora", "start": "Datum početka", "end": "Datum isteka", "value": "Vrijednost ugovora",
           "notice": "Otkazni rok", "pay": "Rok plaćanja", "law": "Mjerodavno pravo",
           "party_a": "Naručitelj", "party_b": "Dobavljač"},
}


def contract(id, split, lang, fmt, tenant, groups, title, ctype, cid, party_a, party_b, start, end, value,
             currency, pay_days, notice_text, notice_iso, auto_renew, law_text, law_gt, obligations, extra=(),
             num_style="eu", date_style="eu", labels=None, two_pages=True, schedule=None, omit=(),
             expected_errors=(), expected_warnings=(), notes="", scanned_pages=(), sentence_terms=False):
    T = dict(CON_TEXT[lang])
    T.update(labels or {})
    p1 = [("title", title), ("kv", T["id"], cid, "contract_id"),
          ("kv", T["party_a"], party_a, "parties"), ("kv", T["party_b"], party_b, "parties"),
          ("kv", T["start"], fdate(start, date_style), "effective_date")]
    if end is not None:
        p1.append(("kv", T["end"], fdate(end, date_style), "expiration_date"))
    if value is not None:
        p1.append(("kv", T["value"], f"{currency} {money(value, num_style)}" if lang == "en"
                   else f"{money(value, num_style)} {currency}", "contract_value"))
    if not sentence_terms:
        if pay_days:
            p1.append(("kv", T["pay"], f"{pay_days} days" if lang == "en" else f"{pay_days} dana", "payment_terms"))
        if notice_text:
            p1.append(("kv", T["notice"], notice_text, "notice_period"))
    body = []
    sec = {"en": ["1. Scope", "2. Commercial terms", "3. Term and termination", "4. Final provisions"],
           "hr": ["1. Predmet ugovora", "2. Komercijalni uvjeti", "3. Trajanje i raskid", "4. Završne odredbe"]}[lang]
    body.append(("h", sec[0]))
    for o in obligations:
        body.append(("p", o, ["obligations"]))
    body.append(("h", sec[1]))
    if sentence_terms and pay_days:
        body.append(("p", f"Invoices are payable within {pay_days} days of receipt." if lang == "en"
                     else f"Računi se plaćaju u roku od {pay_days} dana od primitka.", ["payment_terms"]))
    if schedule:
        body.append(("table", schedule[0], schedule[1], {"widths": schedule[2] if len(schedule) > 2 else None,
                                                         "merge": schedule[3] if len(schedule) > 3 else []}))
    body.append(("h", sec[2]))
    if auto_renew is True:
        body.append(("p", "This Agreement automatically renews for successive one-year terms unless either party "
                          "gives written notice of non-renewal." if lang == "en" else
                     "Ugovor se automatski produžuje za razdoblje od godinu dana ako ga nijedna strana ne otkaže.",
                     ["auto_renewal", "renewal_conditions"]))
    elif auto_renew is False:
        body.append(("p", "This Agreement will not renew automatically; any extension requires a written amendment."
                     if lang == "en" else "Ugovor se ne produžuje automatski; produljenje zahtijeva pisani aneks.",
                     ["auto_renewal", "renewal_conditions"]))
    if sentence_terms and notice_text:
        body.append(("p", f"Either party may terminate this Agreement with {notice_text} prior written notice."
                     if lang == "en" else f"Svaka strana može raskinuti ugovor uz otkazni rok od {notice_text}.",
                     ["notice_period", "termination_conditions"]))
    else:
        body.append(("p", "Either party may terminate this Agreement for material breach that is not cured within "
                          "30 days of written notice." if lang == "en" else
                     "Svaka strana može raskinuti ugovor zbog bitne povrede koja nije otklonjena u roku od 30 dana "
                     "od pisane obavijesti.", ["termination_conditions"]))
    body.append(("h", sec[3]))
    if law_text:
        body.append(("p", law_text, ["governing_law"]))
    body += list(extra)
    pages = [p1, body] if two_pages else [p1 + body]
    fields = {"contract_id": cid, "contract_type": ctype, "effective_date": start.isoformat(),
              "expiration_date": end.isoformat() if end else None,
              "contract_value": c2(value) if value is not None else None,
              "currency": currency if value is not None else None,
              "payment_terms": f"P{pay_days}D" if pay_days else None, "notice_period": notice_iso,
              "auto_renewal": None if auto_renew is None else ("true" if auto_renew else "false"),
              "governing_law": law_gt}
    clauses = ["termination_conditions"] + (["renewal_conditions"] if auto_renew is not None else []) + \
              (["obligations"] if obligations else [])
    return DocSpec(id=id, split=split, tenant=tenant, groups=groups, format=fmt, language=lang, doc_type="contract",
                   pages=pages, fields=fields, parties=[party_a, party_b], clauses=clauses,
                   expected_errors=list(expected_errors), expected_warnings=list(expected_warnings),
                   scanned_pages=list(scanned_pages) or ([1, 2] if fmt == "scan" and two_pages else [1] if fmt == "scan" else []),
                   notes=notes)


EN_OBL = ["The Supplier shall deliver all goods to the Customer's plants in Zagreb and Rijeka.",
          "The Supplier shall maintain insurance cover of at least EUR 1,000,000."]
HR_OBL = ["Dobavljač se obvezuje isporučiti robu u skladišta Naručitelja u Zagrebu.",
          "Naručitelj se obvezuje platiti ispostavljene račune u ugovorenom roku."]
LAW_HR_EN = "This Agreement is governed by the laws of the Republic of Croatia."
LAW_HR_HR = "Za ovaj ugovor mjerodavno je pravo Republike Hrvatske."
SLA = (["Service", "Availability", "Response time"],
       [["Cloud hosting", "99.9%", "4 hours"], ["Backup service", "99.5%", "8 hours"]], [2, 1, 1])
ITEMS_A = [("Freight Zagreb-Rijeka", 12, "450.00", 25), ("Warehouse handling", 40, "18.50", 25),
           ("Customs documentation", 3, "95.00", 25)]


def build() -> list[DocSpec]:
    D = date
    specs: list[DocSpec] = [
        # ------------------------------------------------------------------ contracts (dev)
        contract("di-c01", "dev", "en", "pdf", "northstar", ["procurement", "legal"],
                 "Atlas Logistics Supply Agreement", "supply", "NS-SUP-2026-101", "Northstar Manufacturing d.o.o.",
                 "Atlas Logistics GmbH", D(2026, 1, 15), D(2027, 1, 14), Decimal("184000"), "EUR", 45,
                 "90 days", "P90D", True, LAW_HR_EN, "Republic of Croatia", EN_OBL, num_style="en",
                 date_style="en_long",
                 schedule=(["Milestone", "Due date", "Amount"],
                           [["Mobilisation", "2026-02-01", "EUR 20,000.00"], ["Quarterly fee", "2026-04-01", "EUR 41,000.00"]]),
                 notes="multi-page text PDF with a schedule table"),
        contract("di-c02", "dev", "hr", "pdf", "northstar", ["procurement"],
                 "Ugovor o održavanju opreme", "services", "NS-ODR-2026-014", "Northstar Manufacturing d.o.o.",
                 "Servis Plus d.o.o.", D(2026, 3, 1), D(2027, 2, 28), Decimal("48000"), "EUR", 30,
                 "60 dana", "P60D", False, LAW_HR_HR, "Croatia", HR_OBL, date_style="hr_long"),
        contract("di-c03", "dev", "en", "docx", "northstar", ["procurement"],
                 "Cygnus Cloud Services Agreement", "services", "NS-IT-2026-117", "Northstar Manufacturing d.o.o.",
                 "Cygnus Cloud Services Ltd", D(2026, 4, 1), D(2028, 3, 31), Decimal("62400"), "EUR", 30,
                 "3 months", "P3M", True, "This Agreement is governed by the laws of Ireland.", "Ireland",
                 ["The Supplier shall provide 99.9% monthly availability for the hosting service."],
                 num_style="en", date_style="iso",
                 schedule=(["Service level", "Target", "Credit"],
                           [["Availability", "99.9%", "5% of monthly fee"], ["", "99.5%", "10% of monthly fee"]],
                           None, [(1, 0, 2, 0)]),
                 notes="DOCX; SLA table with a vertically merged cell"),
        contract("di-c04", "dev", "en", "txt", "northstar", ["legal"],
                 "Mutual Non-Disclosure Agreement", "nda", "NS-NDA-2026-007", "Northstar Manufacturing d.o.o.",
                 "Helios Analytics Ltd", D(2026, 2, 10), D(2029, 2, 9), None, "EUR", None, None, None, None,
                 "This Agreement is governed by the laws of England and Wales.", "England and Wales",
                 ["Each party shall keep the other party's confidential information strictly confidential."],
                 date_style="eu_slash", labels={"party_a": "Party A", "party_b": "Party B"},
                 notes="missing value, payment terms, notice period and renewal clause"),
        contract("di-c05", "dev", "hr", "md", "northstar", ["procurement"],
                 "Ugovor o licenci za softver", "license", "NS-LIC-2026-022", "Northstar Manufacturing d.o.o.",
                 "Datanova d.o.o.", D(2026, 5, 1), D(2027, 4, 30), Decimal("12500"), "EUR", 15, "30 dana", "P30D",
                 True, LAW_HR_HR, "Croatia", HR_OBL[:1], date_style="eu"),
        contract("di-c06", "dev", "en", "scan", "northstar", ["procurement"],
                 "Borealis Packaging Supply Agreement", "supply", "NS-SUP-2026-131", "Northstar Manufacturing d.o.o.",
                 "Borealis Packaging AB", D(2026, 6, 1), D(2027, 5, 31), Decimal("96500"), "EUR", 60, "90 days",
                 "P90D", False, LAW_HR_EN, "Republic of Croatia", EN_OBL[:1], num_style="en", date_style="iso",
                 notes="two scanned pages"),
        contract("di-c07", "dev", "hr", "scan", "northstar", ["procurement"],
                 "Ugovor o pružanju usluga čišćenja", "services", "NS-USL-2026-045", "Northstar Manufacturing d.o.o.",
                 "Čisto i Sjajno d.o.o.", D(2026, 2, 1), D(2027, 1, 31), Decimal("21600"), "EUR", 30, "30 dana",
                 "P30D", True, LAW_HR_HR, "Croatia", HR_OBL[:1], date_style="eu", two_pages=False,
                 notes="one scanned page, Croatian diacritics"),
        contract("di-c08", "dev", "en", "mixed", "northstar", ["procurement", "finance"],
                 "Warehouse Lease Agreement", "lease", "NS-LEA-2026-003", "Northstar Manufacturing d.o.o.",
                 "Adriatic Property Holdings d.d.", D(2026, 1, 1), D(2030, 12, 31), Decimal("240000"), "EUR", 15,
                 "6 months", "P6M", True, LAW_HR_EN, "Republic of Croatia",
                 ["The Tenant shall keep the premises insured against fire and flood."], num_style="en",
                 date_style="en_us_long", labels={"party_a": "Tenant", "party_b": "Landlord"}, scanned_pages=[2],
                 notes="mixed PDF: page 1 text layer, page 2 scanned"),
        contract("di-c09", "dev", "en", "pdf", "northstar", ["procurement", "legal"],
                 "Amendment No. 1 to the Atlas Logistics Supply Agreement", "amendment", "NS-SUP-2026-101",
                 "Northstar Manufacturing d.o.o.", "Atlas Logistics GmbH", D(2026, 9, 1), D(2027, 12, 31),
                 Decimal("199000"), "EUR", 45, "120 days", "P120D", True, LAW_HR_EN, "Republic of Croatia", [],
                 num_style="en", date_style="en_long", two_pages=False,
                 notes="same contract ID as di-c01 with different end date, value and notice: a cross-document conflict"),
        # ------------------------------------------------------------------ contracts (test)
        contract("di-c10", "test", "en", "pdf", "northstar", ["procurement"],
                 "Framework Agreement for Maintenance Services", "framework", "NS-FWK-2026-009",
                 "Northstar Manufacturing d.o.o.", "TechServ Solutions Ltd", D(2026, 7, 1), D(2028, 6, 30),
                 Decimal("150000"), "EUR", 30, "60 days", "P60D", None, LAW_HR_EN, "Republic of Croatia", EN_OBL[1:],
                 num_style="en", date_style="eu",
                 extra=[("kv", "End date", fdate(D(2029, 6, 30), "eu"), None)],
                 expected_warnings=["conflicting_values_in_document"],
                 notes="ambiguous: a second, different 'End date' line in the final provisions"),
        contract("di-c11", "test", "hr", "docx", "northstar", ["procurement"],
                 "Ugovor o isporuci sirovina", "supply", "NS-ISP-2026-058", "Northstar Manufacturing d.o.o.",
                 "Panonija Metal d.d.", D(2026, 10, 1), D(2027, 9, 30), Decimal("312750.50"), "EUR", 60, "90 dana",
                 "P90D", True, LAW_HR_HR, "Croatia", HR_OBL, date_style="hr_long"),
        contract("di-c12", "test", "en", "scan", "northstar", ["procurement"],
                 "Security Services Agreement", "services", "NS-SEC-2026-021", "Northstar Manufacturing d.o.o.",
                 "Sentinel Guard Services d.o.o.", D(2026, 11, 1), D(2027, 10, 31), Decimal("54000"), "EUR", 30,
                 "30 days", "P30D", False, LAW_HR_EN, "Republic of Croatia", EN_OBL[:1], num_style="en",
                 date_style="en_long", labels={"start": "Commencement date", "end": "Expiry date",
                                               "value": "Contract value", "id": "Contract number"},
                 sentence_terms=True, notes="scanned; payment terms and notice only in sentences"),
        contract("di-c13", "test", "hr", "txt", "northstar", ["finance"],
                 "Ugovor o najmu poslovnog prostora", "lease", "NS-NAJ-2026-002", "Northstar Manufacturing d.o.o.",
                 "Gradski Centar Nekretnine d.o.o.", D(2026, 4, 1), None, Decimal("3600"), "EUR", 10, "3 mjeseca",
                 "P3M", None, LAW_HR_HR, "Croatia", HR_OBL[1:], date_style="eu",
                 labels={"party_a": "Najmoprimac", "party_b": "Najmodavac", "value": "Mjesečna najamnina"},
                 notes="indefinite lease: no expiration date; novel labels (Najmoprimac / Najmodavac / Mjesečna najamnina)"),
        contract("di-c14", "test", "en", "md", "northstar", ["procurement", "finance"],
                 "Consulting Services Agreement", "services", "NS-CON-2026-033", "Northstar Manufacturing d.o.o.",
                 "Meridian Advisory LLP", D(2026, 8, 15), D(2027, 2, 14), Decimal("38750"), "EUR", 30, "30 days",
                 "P30D", False, "This Agreement is governed by the laws of the Republic of Croatia.",
                 "Republic of Croatia", ["The Consultant shall deliver a written report every month."], num_style="en",
                 date_style="iso", labels={"party_b": "Consultant", "pay": "Payment"},
                 notes="novel label 'Payment'"),
        contract("di-c15", "test", "en", "pdf", "otherco", ["procurement"],
                 "Otherco Raw Materials Supply Agreement", "supply", "OC-SUP-2026-004", "Otherco Industries Ltd",
                 "Atlas Logistics GmbH", D(2026, 3, 1), D(2027, 2, 28), Decimal("88000"), "EUR", 30, "90 days",
                 "P90D", True, LAW_HR_EN, "Republic of Croatia", EN_OBL[:1], num_style="en", date_style="en_long",
                 notes="other tenant"),
        contract("di-c16", "test", "hr", "scan", "otherco", ["procurement"],
                 "Ugovor o prijevozu robe", "services", "OC-PRI-2026-011", "Otherco Industries d.o.o.",
                 "Brzi Prijevoz d.o.o.", D(2026, 5, 15), D(2026, 12, 31), Decimal("27300"), "EUR", 30, "15 dana",
                 "P15D", False, LAW_HR_HR, "Croatia", HR_OBL[:1], date_style="eu", two_pages=False,
                 notes="other tenant, scanned"),
        # ------------------------------------------------------------------ invoices (dev)
        invoice("di-i01", "dev", "en", "pdf", "northstar", ["finance"], "INV-2026-0412", "Atlas Logistics GmbH",
                "DE 812 345 671", "Northstar Manufacturing d.o.o.", D(2026, 3, 31), D(2026, 4, 30), "EUR", ITEMS_A,
                "DE89 3704 0044 0532 0130 00", "NS-PO-7781", num_style="en", date_style="iso"),
        invoice("di-i02", "dev", "hr", "pdf", "northstar", ["finance"], "R-2026-0153", "Servis Plus d.o.o.",
                "HR 12345678903", "Northstar Manufacturing d.o.o.", D(2026, 4, 15), D(2026, 5, 15), "EUR",
                [("Servis kompresora", 2, "380.00", 25), ("Rezervni dijelovi", 5, "64.20", 25),
                 ("Stručna literatura", 3, "42.00", 5)], "HR12 1001 0051 8630 0016 0", "HR00 2026-153",
                date_style="eu"),
        invoice("di-i03", "dev", "en", "xlsx", "northstar", ["finance"], "CY-INV-88213", "Cygnus Cloud Services Ltd",
                "IE 6388047V", "Northstar Manufacturing d.o.o.", D(2026, 5, 1), D(2026, 5, 31), "EUR",
                [("Cloud hosting May 2026", 1, "5200.00", 23), ("Backup storage 2 TB", 2, "75.00", 23)],
                "IE29 AIBK 9311 5212 3456 78", "CY-88213", num_style="en", date_style="iso"),
        invoice("di-i04", "dev", "hr", "xlsx", "northstar", ["finance"], "R-2026-0377", "Datanova d.o.o.",
                "HR 98765432106", "Northstar Manufacturing d.o.o.", D(2026, 5, 20), D(2026, 6, 19), "EUR",
                [("Licenca Datanova ERP", 10, "120.00", 25), ("Implementacija", 16, "55.00", 25)],
                hr_iban("23600001101234565"), "HR00 2026-377", date_style="eu",
                perturb={"subtotal": Decimal("100.00")}, expected_errors=["subtotal_mismatch"],
                notes="invalid: subtotal does not equal the sum of the lines (total = printed subtotal + VAT)"),
        invoice("di-i05", "dev", "en", "docx", "northstar", ["finance"], "HEL-2026-019", "Helios Analytics Ltd",
                "GB 123 4567 89", "Northstar Manufacturing d.o.o.", D(2026, 6, 5), D(2026, 7, 5), "GBP",
                [("Data analysis workshop", 2, "1450.00", 20), ("Dashboard licence", 12, "35.00", 20)],
                "GB33 BUKB 2020 1555 5555 55", "HEL-019", num_style="en", date_style="en_long"),
        invoice("di-i06", "dev", "en", "scan", "northstar", ["finance"], "BOR-55120", "Borealis Packaging AB",
                "SE 556677889901", "Northstar Manufacturing d.o.o.", D(2026, 7, 10), D(2026, 8, 9), "EUR",
                [("Carton boxes type A", 500, "1.20", 25), ("Pallet wrap film", 20, "14.90", 25)],
                "SE45 5000 0000 0583 9825 7466", "BOR-55120", num_style="en", date_style="iso"),
        invoice("di-i07", "dev", "hr", "scan", "northstar", ["finance"], "R-2026-0911", "Čisto i Sjajno d.o.o.",
                "HR 33445566770", "Northstar Manufacturing d.o.o.", D(2026, 8, 31), D(2026, 9, 30), "EUR",
                [("Čišćenje hale", 4, "450.00", 25), ("Sredstva za čišćenje", 1, "86.40", 25)],
                hr_iban("23400091110001234"), "HR00 2026-911", date_style="eu",
                perturb={"total": Decimal("50.00")}, expected_errors=["total_mismatch"],
                notes="scanned; invalid total"),
        invoice("di-i08", "dev", "en", "txt", "northstar", ["finance"], "MER-2026-77", "Meridian Advisory LLP",
                "US 98-7654321", "Northstar Manufacturing d.o.o.", D(2026, 9, 15), D(2026, 10, 15), "USD",
                [("Strategy consulting (hours)", 24, "210.00", 0), ("Travel expenses", 1, "640.00", 0)],
                "GB29 NWBK 6016 1331 9268 19", "MER-77", num_style="en", date_style="iso",
                notes="plain text with aligned columns; 0% VAT"),
        # ------------------------------------------------------------------ invoices (test)
        invoice("di-i09", "test", "en", "pdf", "northstar", ["finance"], "TS-2026-1188", "TechServ Solutions Ltd",
                "GB 987 6543 21", "Northstar Manufacturing d.o.o.", D(2026, 10, 2), D(2026, 11, 1), "EUR",
                [("Preventive maintenance visit", 3, "320.00", 25), ("Replacement filters", 12, "18.75", 25)],
                "GB82 WEST 1234 5698 7654 32", "TS-1188", num_style="en", date_style="en_long",
                perturb={"line": {1: Decimal("25.00")}},
                expected_errors=["line_amount_mismatch"],
                notes="invalid line amount (12 x 18.75 printed as 250.00); subtotal/total consistent with printed lines"),
        invoice("di-i10", "test", "en", "xlsx", "northstar", ["finance"], "INV-2026-0533", "Atlas Logistics GmbH",
                "DE 812 345 671", "Northstar Manufacturing d.o.o.", D(2026, 10, 31), D(2026, 11, 30), "EUR",
                ITEMS_A[:2], "DE44 5001 0517 5407 3249 31", "NS-PO-7905", num_style="en", date_style="iso",
                notes="same supplier as di-i01 with a DIFFERENT IBAN: conflicting supplier information"),
        invoice("di-i11", "test", "hr", "md", "northstar", ["finance"], "R-2026-1020", "Panonija Metal d.d.",
                "HR 55667788992", "Northstar Manufacturing d.o.o.", D(2026, 11, 3), D(2026, 12, 3), "EUR",
                [("Čelični limovi 2 mm", 150, "38.40", 25), ("Prijevoz", 1, "210.00", 25)],
                "", "HR00 2026-1020", date_style="hr_long", omit=("iban",), notes="no IBAN printed (missing field)"),
        invoice("di-i12", "test", "en", "scan", "northstar", ["finance"], "SEN-2026-4410", "Sentinel Guard Services d.o.o.",
                "HR 11223344556", "Northstar Manufacturing d.o.o.", D(2026, 11, 30), D(2026, 11, 15), "EUR",
                [("Security guard services November", 1, "4500.00", 25)], hr_iban("24020061100001234"),
                "SEN-4410", num_style="en", date_style="iso", expected_errors=["due_before_invoice_date"],
                notes="scanned; due date before the invoice date"),
        invoice("di-i13", "test", "hr", "docx", "northstar", ["finance"], "GCN-2026-118",
                "Gradski Centar Nekretnine d.o.o.", "HR 66778899005", "Northstar Manufacturing d.o.o.",
                D(2026, 12, 1), D(2026, 12, 15), "EUR", [("Najamnina prosinac 2026", 1, "3600.00", 25)],
                hr_iban("24840081107654321"), "HR01 118-2026", date_style="eu",
                labels={"total": "Iznos za uplatu", "number": "Račun br."},
                notes="novel total label 'Iznos za uplatu'"),
        invoice("di-i14", "test", "en", "pdf", "otherco", ["procurement"], "ATL-OC-2026-31", "Atlas Logistics GmbH",
                "DE 812 345 671", "Otherco Industries Ltd", D(2026, 6, 30), D(2026, 7, 30), "EUR", ITEMS_A[:1],
                "DE89 3704 0044 0532 0130 00", "OC-PO-31", num_style="en", date_style="iso", notes="other tenant"),
        # ------------------------------------------------------------------ other documents
        DocSpec(id="di-o01", split="dev", tenant="northstar", groups=["procurement"], format="pdf", language="en",
                doc_type="unknown", pages=[[
                    ("title", "Minutes of the Procurement Steering Meeting"),
                    ("kv", "Date", "12 March 2026", None),
                    ("p", "Attendees discussed the supplier onboarding backlog and the new packaging standard.", []),
                    ("p", "Action: the category manager will prepare a shortlist of three logistics providers.", []),
                    ("p", "Next meeting is planned for April.", [])]],
                notes="meeting minutes: neither a contract nor an invoice"),
        DocSpec(id="di-o02", split="test", tenant="northstar", groups=["procurement"], format="txt", language="hr",
                doc_type="unknown", pages=[[
                    ("title", "Pravilnik o korištenju službenih vozila"),
                    ("p", "Službena vozila koriste se isključivo za poslovne potrebe.", []),
                    ("p", "Zaposlenik je dužan prijaviti svaku štetu na vozilu u roku od 24 sata.", []),
                    ("p", "Ovaj pravilnik stupa na snagu 1. siječnja 2027.", [])]],
                notes="internal policy: unknown type"),
    ]
    return specs


# Multi-document questions with expected answers (expected document IDs are computed from the
# GROUND TRUTH above and the ACL oracle in docintel_eval.py, not from the system under test).
QUERIES = [
    {"id": "q-expiring-90", "persona": "ns_reviewer", "type": "expiring_within", "days": 90, "as_of": "2026-12-01"},
    {"id": "q-expiring-365", "persona": "ns_reviewer", "type": "expiring_within", "days": 365, "as_of": "2026-12-01"},
    {"id": "q-autorenew", "persona": "ns_reviewer", "type": "auto_renewal"},
    {"id": "q-invalid", "persona": "ns_reviewer", "type": "invalid_invoices"},
    {"id": "q-conflicts", "persona": "ns_reviewer", "type": "conflicts"},
    {"id": "q-oc-autorenew", "persona": "oc_reviewer", "type": "auto_renewal"},
]
