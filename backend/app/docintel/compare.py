"""Multi-document intelligence over STRUCTURED, evidence-backed fields of authorized documents.

* Every document is authorized individually through security.authorized_documents (the RAG ACL).
  If any requested ID is not readable, the request fails with one generic 404 that does not say
  which ID, so the existence of forbidden documents is not revealed.
* Dates, durations and amounts are compared deterministically (Decimal / date arithmetic, no LLM).
* Every value in a result carries its document, page and evidence text. Rejected extractions are
  excluded. Approved ones use reviewer-corrected values. Missing values are listed as missing, not
  guessed. A difference between two contracts is a 'difference'; a 'conflict' is reported only
  for documents that describe the same contract ID or the same supplier.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Document, Extraction, ExtractionField, User
from ..security import authorized_documents
from .normalize import duration_days
from .schemas import base_name
from .service import effective, field_state

COMPARE_FIELDS = {
    "contract": ["contract_id", "parties", "effective_date", "expiration_date", "contract_value", "currency",
                 "payment_terms", "notice_period", "auto_renewal", "governing_law", "termination_conditions",
                 "obligations"],
    "invoice": ["supplier_name", "supplier_tax_id", "customer_name", "invoice_date", "due_date", "currency",
                "subtotal", "tax_amount", "total_amount", "iban"],
}
_LEGAL_SUFFIX = re.compile(r"\b(d\.?\s?o\.?\s?o\.?|j\.?\s?d\.?\s?o\.?\s?o\.?|d\.?\s?d\.?|gmbh|ltd\.?|limited|"
                           r"inc\.?|llc|s\.?r\.?l\.?|ag|plc|bv|oy)\b", re.I)


class ComparisonError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def supplier_key(name: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", _LEGAL_SUFFIX.sub(" ", name.lower())).split())


def _load(db: Session, user: User, document_ids: list[str] | None):
    allowed = authorized_documents(user).with_only_columns(Document.id)
    q = (select(Document, Extraction).join(Extraction, Extraction.document_id == Document.id)
         .where(Document.id.in_(allowed)))
    if document_ids is not None:
        wanted = list(dict.fromkeys(document_ids))
        rows = db.execute(q.where(Document.id.in_(wanted))).all()
        if len(rows) != len(wanted):
            # unreadable, non-existent and unprocessed documents are indistinguishable on purpose
            raise ComparisonError(404, "One or more documents were not found or have no extraction")
    else:
        rows = db.execute(q.order_by(Document.title)).all()
    out = []
    for doc, ext in rows:
        fields = list(db.scalars(select(ExtractionField).where(ExtractionField.extraction_id == ext.id)
                                 .order_by(ExtractionField.position)))
        out.append((doc, ext, fields))
    return out


def _values(fields: list[ExtractionField]) -> dict[str, list[dict]]:
    """base field name -> list of {value, evidence, state} (lists: one entry per item)."""
    out: dict[str, list[dict]] = {}
    for f in fields:
        value, evidence, ground = effective(f)
        if value is None:
            continue
        out.setdefault(base_name(f.name), []).append({
            "field": f.name, "value": value, "value_type": f.value_type, "grounding": ground,
            "state": field_state(f, f.issues or []),
            "evidence": {"page": evidence.get("page"), "text": evidence.get("text")} if evidence else None})
    return out


def _doc_ref(doc: Document, ext: Extraction) -> dict:
    return {"document_id": doc.id, "title": doc.title, "doc_type": ext.doc_type, "review_status": ext.status}


def _sort_key(value_type: str, value: str):
    if value_type in ("money", "decimal", "percent"):
        return Decimal(value)
    if value_type == "duration":
        return duration_days(value) or 0
    return value  # ISO dates sort lexicographically


def compare(db: Session, user: User, document_ids: list[str], fields: list[str] | None = None) -> dict:
    if not 2 <= len(set(document_ids)) <= 10:
        raise ComparisonError(422, "Select between 2 and 10 different documents")
    rows = _load(db, user, document_ids)
    used = [(d, e, f) for d, e, f in rows if e.status != "rejected"]
    excluded = [_doc_ref(d, e) for d, e, _ in rows if e.status == "rejected"]
    types = {e.doc_type for _, e, _ in used}
    names = fields or list(dict.fromkeys(n for t in sorted(types) for n in COMPARE_FIELDS.get(t, [])))
    result_fields = []
    for name in names:
        entries, present = [], []
        for doc, ext, fs in used:
            vals = _values(fs).get(name, [])
            entry = _doc_ref(doc, ext) | {"values": vals}
            entries.append(entry)
            if vals:
                present.append((doc, vals))
        distinct = {tuple(sorted(v["value"].lower() for v in vals)) for _, vals in present}
        analysis: dict = {"documents_with_value": len(present),
                          "missing_in": [e["document_id"] for e in entries if not e["values"]],
                          "all_equal": len(distinct) == 1 and len(present) == len(entries),
                          "differs": len(distinct) > 1}
        scalar = [(doc, vals[0]) for doc, vals in present if len(vals) == 1]
        if scalar and scalar[0][1]["value_type"] in ("date", "money", "decimal", "percent", "duration"):
            vt = scalar[0][1]["value_type"]
            ordered = sorted(scalar, key=lambda dv: _sort_key(vt, dv[1]["value"]))
            analysis["lowest"] = {"document_id": ordered[0][0].id, "value": ordered[0][1]["value"]}
            analysis["highest"] = {"document_id": ordered[-1][0].id, "value": ordered[-1][1]["value"]}
        result_fields.append({"field": name, "documents": entries, "analysis": analysis})
    return {"documents": [_doc_ref(d, e) for d, e, _ in used], "excluded_rejected": excluded,
            "fields": result_fields,
            "note": "Values are structured extractions with their evidence; approved extractions "
                    "use reviewer-corrected values. 'differs' is a difference, not a conflict."}


# ----------------------------------------------------------------------- structured queries

def _contracts(db, user, ids=None):
    return [(d, e, _values(f)) for d, e, f in _load(db, user, ids) if e.doc_type == "contract" and e.status != "rejected"]


def _invoices(db, user, ids=None):
    return [(d, e, f) for d, e, f in _load(db, user, ids) if e.doc_type == "invoice" and e.status != "rejected"]


def expiring_within(db: Session, user: User, days: int, as_of: date, ids=None) -> dict:
    if not 0 < days <= 3650:
        raise ComparisonError(422, "days must be between 1 and 3650")
    end = as_of + timedelta(days=days)
    matches, no_date, expired = [], [], []
    for doc, ext, vals in _contracts(db, user, ids):
        exp = (vals.get("expiration_date") or [None])[0]
        if exp is None:
            no_date.append(_doc_ref(doc, ext))
            continue
        d = date.fromisoformat(exp["value"])
        item = _doc_ref(doc, ext) | {"expiration_date": exp["value"], "days_left": (d - as_of).days,
                                     "evidence": exp["evidence"], "state": exp["state"]}
        if as_of <= d <= end:
            matches.append(item)
        elif d < as_of:
            expired.append(item)
    matches.sort(key=lambda m: m["days_left"])
    return {"query": "expiring_within", "as_of": as_of.isoformat(), "days": days, "window_end": end.isoformat(),
            "matches": matches, "already_expired": expired, "without_expiration_date": no_date}


def auto_renewal(db: Session, user: User, ids=None) -> dict:
    yes, no, unknown = [], [], []
    for doc, ext, vals in _contracts(db, user, ids):
        v = (vals.get("auto_renewal") or [None])[0]
        target = unknown if v is None else (yes if v["value"] == "true" else no)
        target.append(_doc_ref(doc, ext) | ({"evidence": v["evidence"], "state": v["state"]} if v else {}))
    return {"query": "auto_renewal", "matches": yes, "no_automatic_renewal": no, "not_stated": unknown}


ARITHMETIC_CODES = {"line_amount_mismatch", "subtotal_mismatch", "tax_mismatch", "total_mismatch",
                    "currency_inconsistent", "due_before_invoice_date", "iban_checksum"}


def invalid_invoices(db: Session, user: User, ids=None) -> dict:
    failing, passing = [], []
    for doc, ext, fields in _invoices(db, user, ids):
        issues = [i for f in fields for i in (f.issues or []) if i.get("code") in ARITHMETIC_CODES]
        ref = _doc_ref(doc, ext)
        if issues:
            failing.append(ref | {"issues": [{k: i.get(k) for k in ("field", "code", "message", "expected", "actual")}
                                             for i in issues]})
        else:
            passing.append(ref)
    return {"query": "invalid_invoices", "matches": failing, "valid": passing,
            "note": "Checks: quantity x unit price, subtotal, tax, total, currency, dates, IBAN checksum."}


def conflicts(db: Session, user: User, ids=None) -> dict:
    rows = _load(db, user, ids)
    found = []
    # (a) the same contract ID in several documents with different values (e.g. an amendment)
    by_id: dict[str, list] = {}
    for doc, ext, fs in rows:
        if ext.status == "rejected" or ext.doc_type != "contract":
            continue
        vals = _values(fs)
        cid = (vals.get("contract_id") or [None])[0]
        if cid:
            by_id.setdefault(cid["value"].upper(), []).append((doc, ext, vals))
    for cid, docs in by_id.items():
        if len(docs) < 2:
            continue
        for name in ("expiration_date", "contract_value", "notice_period", "payment_terms", "auto_renewal"):
            seen = {(vals.get(name) or [{}])[0].get("value") for _, _, vals in docs} - {None}
            if len(seen) > 1:
                found.append({"kind": "same_contract_id", "key": cid, "field": name,
                              "values": [_doc_ref(d, e) | {"value": (v.get(name) or [{}])[0].get("value"),
                                                           "evidence": (v.get(name) or [{}])[0].get("evidence")}
                                         for d, e, v in docs],
                              "note": "Same contract ID with different values (possibly an amendment): review."})
    # (b) the same supplier with different tax IDs or bank accounts across invoices
    by_supplier: dict[str, list] = {}
    for doc, ext, fs in rows:
        if ext.status == "rejected" or ext.doc_type != "invoice":
            continue
        vals = _values(fs)
        name = (vals.get("supplier_name") or [None])[0]
        if name:
            by_supplier.setdefault(supplier_key(name["value"]), []).append((doc, ext, vals))
    for key, docs in by_supplier.items():
        if len(docs) < 2:
            continue
        for name in ("supplier_tax_id", "iban"):
            seen = {(vals.get(name) or [{}])[0].get("value") for _, _, vals in docs} - {None}
            if len(seen) > 1:
                found.append({"kind": "same_supplier", "key": key, "field": name,
                              "values": [_doc_ref(d, e) | {"value": (v.get(name) or [{}])[0].get("value"),
                                                           "evidence": (v.get(name) or [{}])[0].get("evidence")}
                                         for d, e, v in docs],
                              "note": "Same supplier name with different identifiers: possible fraud or data error."})
    return {"query": "conflicts", "matches": found}


# ------------------------------------------------------------ natural-language question router

INTENTS = [
    ("expiring_within", re.compile(r"expir\w*\s+(?:with)?in\s+(?:the\s+next\s+)?(\d+)\s+days|"
                                   r"ističu?\s+(?:u\s+)?(?:sljedećih\s+|idućih\s+)?(\d+)\s+dana", re.I)),
    ("auto_renewal", re.compile(r"automatic(?:ally)?\s+renew|auto-?renew|automatsk\w*\s+(?:produž|obnav|produlj)", re.I)),
    ("invalid_invoices", re.compile(r"invoice\w*\b.*\b(fail|invalid|incorrect|wrong|do not add up|don't add up)|"
                                    r"račun\w*\b.*\b(neispravn|pogrešn|ne slaž)", re.I)),
    ("conflicts", re.compile(r"conflict|inconsisten|contradict|proturječ|nesklad|sukobljen", re.I)),
    ("compare_payment_terms", re.compile(r"compar\w*.*payment\s+terms|usporedi\w*.*(uvjet\w*|rok\w*)\s+plaćanja", re.I)),
    ("compare", re.compile(r"\bcompar\w*|\busporedi", re.I)),
]


def interpret(question: str) -> tuple[str, dict] | None:
    for intent, pattern in INTENTS:
        m = pattern.search(question)
        if m:
            params = {}
            if intent == "expiring_within":
                params["days"] = int(next(g for g in m.groups() if g))
            return intent, params
    return None


def answer_question(db: Session, user: User, question: str, as_of: date, ids: list[str] | None) -> dict:
    parsed = interpret(question)
    if parsed is None:
        raise ComparisonError(422, "Unsupported question. Supported: contracts expiring within N days, "
                                   "automatic renewal clauses, invoices failing validation, conflicting "
                                   "supplier information, comparing (payment terms of) selected documents.")
    intent, params = parsed
    if intent == "expiring_within":
        result = expiring_within(db, user, params["days"], as_of, ids)
    elif intent == "auto_renewal":
        result = auto_renewal(db, user, ids)
    elif intent == "invalid_invoices":
        result = invalid_invoices(db, user, ids)
    elif intent == "conflicts":
        result = conflicts(db, user, ids)
    else:
        if not ids:
            raise ComparisonError(422, "Select the documents to compare")
        result = compare(db, user, ids, ["payment_terms"] if intent == "compare_payment_terms" else None)
    return {"interpreted_as": intent, "parameters": params, "result": result,
            "method": "deterministic intent rules + structured fields (no LLM)"}
