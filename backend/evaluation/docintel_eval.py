"""Phase 3 document-intelligence evaluation, end to end through the production API.

Every frozen corpus document (eval/docintel) is uploaded by its owner into a disposable
PostgreSQL database (Alembic migrations, real parser, real Tesseract OCR, offline rules provider).
The persisted extractions are then measured against the ground truth:

  classification · field-level exact match / precision / recall / F1 (per split, type, format,
  field) · missing-field and invalid-field detection · evidence validity, page attribution and
  support · line items (row / column associations, exact rows) and numeric reconciliation ·
  OCR CER / WER / key-token recall · simulated human review through the review API (correction
  rate, approval consistency, validation false-accept rate, blocked invalid approvals,
  optimistic-lock conflict) · multi-document queries against an independent oracle · a security
  matrix of every persona x document x endpoint (unauthorized access, cross-tenant leakage,
  unauthorized review, unauthorized comparison).

Only the deterministic offline provider is measured. The optional LLM provider is NOT evaluated
(no API key); this is stated in the report.
"""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.app.auth import hash_password
from backend.app.docintel.compare import ARITHMETIC_CODES, supplier_key
from backend.app.models import User

from .docintel_corpus import PERSONAS
from .docintel_metrics import (cer, equal, key_token_recall, prf, rate, score_slots, set_scores, wer)
from .safety import assert_disposable_database

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "eval" / "docintel"
PASSWORD = "EvalPass123!"
SCALAR = {
    "contract": {"contract_id": "text", "contract_type": "text", "effective_date": "date",
                 "expiration_date": "date", "contract_value": "money", "value_period": "text", "currency": "currency",
                 "payment_terms": "duration", "notice_period": "duration", "auto_renewal": "bool",
                 "governing_law": "text"},
    "invoice": {"invoice_number": "text", "supplier_name": "text", "supplier_tax_id": "tax_id",
                "customer_name": "text", "invoice_date": "date", "due_date": "date", "currency": "currency",
                "subtotal": "money", "tax_amount": "money", "total_amount": "money",
                "payment_reference": "text", "iban": "iban"},
}
ITEM_COLS = {"description": "text", "quantity": "decimal", "unit_price": "money", "tax_rate": "percent",
             "amount": "money"}


# ------------------------------------------------------------------------------- loading

def load_corpus() -> tuple[list[dict], dict, list[dict]]:
    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    for name, digest in manifest["files"].items():
        if hashlib.sha256((DATA / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError(f"frozen corpus file modified: {name} (rebuild deliberately and review the diff)")
    labels = [json.loads(line) for line in (DATA / "labels.jsonl").read_text(encoding="utf-8").splitlines()]
    truth = {(r["id"], r["page"]): r["text"] for r in
             map(json.loads, (DATA / "ocr_truth.jsonl").read_text(encoding="utf-8").splitlines())}
    queries = json.loads((DATA / "queries.json").read_text(encoding="utf-8"))
    return labels, truth, queries


def authorized(persona: str, label: dict) -> bool:
    """Independent re-implementation of the ACL (not the application's SQL)."""
    _, tenant, role, groups = PERSONAS[persona]
    if tenant != label["tenant"]:
        return False
    return role == "admin" or label["owner"] == persona or bool(set(groups) & set(label["groups"]))


def can_review(persona: str, label: dict) -> bool:
    return authorized(persona, label) and PERSONAS[persona][2] in ("admin", "reviewer")


def prepare(url: str):
    from backend.app.migrate import migrate

    parsed = assert_disposable_database(url)
    admin = create_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{parsed.database}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{parsed.database}"'))
    admin.dispose()
    real = parsed.render_as_string(hide_password=False)
    migrate(real)
    engine = create_engine(real)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        pwd = hash_password(PASSWORD)
        for key, (email, tenant, role, groups) in PERSONAS.items():
            db.add(User(id=key, email=email, tenant_id=tenant, role=role, groups=groups, password_hash=pwd))
        db.commit()
    return engine, factory


@contextmanager
def api_client(factory, storage: Path):
    from fastapi.testclient import TestClient

    from backend.app import main
    from backend.app.database import get_db

    def override():
        with factory() as db:
            yield db

    original = main.settings
    main.settings = replace(original, storage_dir=storage, embed_on_upload=False)
    main.app.dependency_overrides[get_db] = override
    try:
        yield TestClient(main.app)
    finally:
        main.app.dependency_overrides.clear()
        main.settings = original


# ----------------------------------------------------------------------------- evaluation

def run(database_url: str, ci: bool = False) -> dict:
    labels, truth, queries = load_corpus()
    engine, factory = prepare(database_url)
    started = time.perf_counter()
    with tempfile.TemporaryDirectory() as tmp, api_client(factory, Path(tmp)) as client:
        tokens = {}

        def h(persona):
            if persona not in tokens:
                r = client.post("/api/auth/login", json={"email": PERSONAS[persona][0], "password": PASSWORD})
                tokens[persona] = {"Authorization": "Bearer " + r.json()["access_token"]}
            return tokens[persona]

        ids, uploads = {}, {}
        for lab in labels:
            data = (DATA / lab["file"]).read_bytes()
            t0 = time.perf_counter()
            r = client.post("/api/documents", headers=h(lab["owner"]),
                            files={"file": (Path(lab["file"]).name, data, "application/octet-stream")},
                            data={"groups": ",".join(lab["groups"])})
            ms = (time.perf_counter() - t0) * 1000
            uploads[lab["id"]] = {"status": r.status_code, "ms": round(ms), "body": r.json()}
            if r.status_code == 201:
                ids[lab["id"]] = r.json()["id"]
        before = {lid: client.get(f"/api/documents/{ids[lid]}/extraction", headers=h(lab_owner(labels, lid))).json()
                  for lid in ids}
        structures = {lid: client.get(f"/api/documents/{ids[lid]}/structure", headers=h(lab_owner(labels, lid))).json()
                      for lid in ids}
        report = {"meta": meta(labels, uploads, structures), "uploads": {k: {"status": v["status"], "ms": v["ms"]}
                                                                         for k, v in uploads.items()}}
        report["classification"] = classification(labels, before)
        report["extraction"] = extraction_metrics(labels, before)
        report["evidence"] = evidence_metrics(labels, before, structures)
        report["tables"] = table_metrics(labels, before)
        report["validation"] = validation_metrics(labels, before)
        report["ocr"] = ocr_metrics(labels, truth, structures, uploads)
        report["queries_before_review"] = query_metrics(client, h, labels, ids, queries, rejected=set())
        review, rejected = review_simulation(client, h, labels, ids, before)
        report["review"] = review
        report["queries_after_review"] = query_metrics(client, h, labels, ids, queries, rejected=rejected)
        report["security"] = security_matrix(client, h, labels, ids)
        report["failures"] = failure_analysis(labels, before)
        report["llm_provider"] = {"evaluated": False, "status": "NOT VERIFIED",
                                  "reason": "no OPENAI_API_KEY; the provider contract is covered by mocked tests"}
    engine.dispose()
    report["meta"]["total_seconds"] = round(time.perf_counter() - started, 1)
    report["meta"]["mode"] = "ci" if ci else "full"
    return report


def lab_owner(labels, lid):
    return next(lab["owner"] for lab in labels if lab["id"] == lid)


def meta(labels, uploads, structures) -> dict:
    import platform

    from backend.app.docintel.ocr import ocr_available, tesseract_info

    formats: dict[str, int] = {}
    for lab in labels:
        formats[lab["format"]] = formats.get(lab["format"], 0) + 1
    return {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "documents": len(labels), "splits": {s: sum(1 for x in labels if x["split"] == s) for s in ("dev", "test")},
            "types": {t: sum(1 for x in labels if x["doc_type"] == t) for t in ("contract", "invoice", "unknown")},
            "formats": formats, "languages": {g: sum(1 for x in labels if x["language"] == g) for g in ("en", "hr")},
            "scanned_pages": sum(len(x["scanned_pages"]) for x in labels),
            "tenants": sorted({x["tenant"] for x in labels}),
            "uploads_ok": sum(1 for u in uploads.values() if u["status"] == 201),
            "ocr": tesseract_info() if ocr_available() else None,
            "parser": sorted({s.get("parser", "?") for s in structures.values()}),
            "provider": "rules_v1 (offline, deterministic)",
            "corpus_sha256": hashlib.sha256((DATA / "manifest.json").read_bytes()).hexdigest()[:16],
            "platform": f"{platform.system()} {platform.release()}, Python {platform.python_version()}"}


def _pred(extraction: dict) -> dict[str, dict]:
    return {f["name"]: f for f in extraction.get("fields", [])}


def _slots(lab: dict, pred: dict[str, dict]):
    """(group, field, value_type, prediction, ground truth) for one document."""
    t = lab["doc_type"]
    out = []
    for name, vtype in SCALAR.get(t, {}).items():
        p = pred.get(name, {}).get("value")
        out.append(("scalar", name, vtype, p, lab["fields"].get(name)))
    if t == "contract":
        predicted = [f["value"] for n, f in pred.items() if n.startswith("parties[") and f.get("value")]
        gt = list(lab["parties"])
        for g in gt:  # each ground-truth party: matched (TP) or missed (FN)
            hit = next((p for p in predicted if equal("text", p, g)), None)
            out.append(("parties", "parties", "text", hit, g))
            if hit is not None:
                predicted.remove(hit)
        for p in predicted:  # extra parties: FP
            out.append(("parties", "parties", "text", p, None))
    return out


def extraction_metrics(labels, before) -> dict:
    groups: dict[str, list] = {}
    per_field: dict[str, list] = {}
    missing_gt = missing_ok = hallucinated = 0
    for lab in labels:
        if lab["doc_type"] == "unknown" or lab["id"] not in before:
            continue
        pred = _pred(before[lab["id"]])
        for group, name, vtype, p, g in _slots(lab, pred):
            for key in ("all", f"split:{lab['split']}", f"type:{lab['doc_type']}", f"format:{lab['format']}",
                        f"language:{lab['language']}"):
                groups.setdefault(key, []).append((vtype, p, g))
            per_field.setdefault(f"{lab['doc_type']}.{name}", []).append((vtype, p, g))
            if g is None and group == "scalar":
                missing_gt += 1
                missing_ok += p is None
                hallucinated += p is not None
    unknown_fields = sum(len(before[lab["id"]].get("fields", [])) for lab in labels
                         if lab["doc_type"] == "unknown" and lab["id"] in before)
    return {"definition": "field slots = scalar fields + ground-truth parties (+ extra predicted parties); "
                          "a wrong value counts as FP and FN",
            "groups": {k: score_slots(v) for k, v in sorted(groups.items())},
            "per_field": {k: score_slots(v) for k, v in sorted(per_field.items())},
            "missing_fields": {"ground_truth_missing": missing_gt, "correctly_left_empty": missing_ok,
                               "hallucinated": hallucinated, "detection_rate": rate(missing_ok, missing_gt)},
            "fields_extracted_from_unknown_documents": unknown_fields}


def classification(labels, before) -> dict:
    confusion: dict[str, dict[str, int]] = {}
    correct = 0
    for lab in labels:
        got = before.get(lab["id"], {}).get("doc_type", "missing")
        confusion.setdefault(lab["doc_type"], {}).setdefault(got, 0)
        confusion[lab["doc_type"]][got] += 1
        correct += got == lab["doc_type"]
    return {"accuracy": rate(correct, len(labels)), "confusion": confusion}


def evidence_metrics(labels, before, structures) -> dict:
    n = valid = supported = page_ok = page_known = unsupported = 0
    for lab in labels:
        if lab["id"] not in before or lab["doc_type"] == "unknown":
            continue
        pages = len(structures[lab["id"]].get("pages", []))
        for f in before[lab["id"]].get("fields", []):
            if f.get("value") is None:
                continue
            n += 1
            ev = f.get("evidence") or {}
            ok_ref = bool(ev) and ev.get("document_id") == ids_of(before, lab) and 1 <= ev.get("page", 0) <= pages \
                and not any(i["code"] in ("evidence_not_in_document", "evidence_invalid_page", "evidence_wrong_document")
                            for i in f.get("issues", []))
            valid += ok_ref
            supported += f.get("grounding") in ("exact", "normalized")
            unsupported += f.get("grounding") not in ("exact", "normalized")
            tag = "line_items" if f["name"].startswith("line_items[") else re.sub(r"\[\d+\]$", "", f["name"])
            if tag in lab["pages"] and ev:
                page_known += 1
                page_ok += ev.get("page") == lab["pages"][tag]
    return {"fields_with_value": n, "evidence_reference_valid_rate": rate(valid, n),
            "evidence_support_rate": rate(supported, n), "unsupported_field_rate": rate(unsupported, n),
            "page_attribution_accuracy": rate(page_ok, page_known), "page_attribution_checked": page_known,
            "method": "exact / normalized string matching of the value inside the cited evidence (no model)"}


def ids_of(before, lab):
    return before[lab["id"]].get("document_id")


def table_metrics(labels, before) -> dict:
    cells = cells_ok = rows = rows_ok = extra_rows = 0
    per_doc = {}
    for lab in labels:
        if lab["doc_type"] != "invoice" or lab["id"] not in before:
            continue
        pred = _pred(before[lab["id"]])
        n_pred = len({k.split("]")[0] for k in pred if k.startswith("line_items[")})
        extra_rows += max(0, n_pred - len(lab["line_items"]))
        doc_ok = 0
        for i, item in enumerate(lab["line_items"]):
            row_ok = True
            for col, vtype in ITEM_COLS.items():
                cells += 1
                same = equal(vtype, pred.get(f"line_items[{i}].{col}", {}).get("value"), item[col])
                cells_ok += same
                row_ok &= same
            rows += 1
            rows_ok += row_ok
            doc_ok += row_ok
        per_doc[lab["id"]] = {"gt_rows": len(lab["line_items"]), "predicted_rows": n_pred, "exact_rows": doc_ok}
    return {"cell_association_accuracy": rate(cells_ok, cells), "cells": cells,
            "line_item_exact_match": rate(rows_ok, rows), "rows": rows, "extra_predicted_rows": extra_rows,
            "per_document": per_doc}


def _error_codes(extraction: dict) -> set[str]:
    codes = {i["code"] for f in extraction.get("fields", []) for i in f.get("issues", []) if i["severity"] == "error"}
    return codes | {i["code"] for i in extraction.get("issues", []) if i["severity"] == "error"}


def validation_metrics(labels, before) -> dict:
    tp = fp = fn = 0
    recon_ok = recon_n = 0
    per_doc = {}
    for lab in labels:
        if lab["doc_type"] != "invoice" or lab["id"] not in before:
            continue
        found = _error_codes(before[lab["id"]]) & ARITHMETIC_CODES
        expected = set(lab["expected_errors"]) & ARITHMETIC_CODES
        tp += len(found & expected)
        fp += len(found - expected)
        fn += len(expected - found)
        recon_n += 1
        recon_ok += bool(found) == bool(expected)
        per_doc[lab["id"]] = {"expected": sorted(expected), "found": sorted(found)}
    return {"invalid_field_detection": prf(tp, fp, fn),
            "numeric_reconciliation_accuracy": rate(recon_ok, recon_n),
            "definition": "an invoice is reconciled correctly when its arithmetic / date / IBAN checks fail "
                          "exactly when the ground truth says the document is invalid",
            "per_document": per_doc}


def ocr_metrics(labels, truth, structures, uploads) -> dict:
    pages = []
    for (doc_id, page), ref in sorted(truth.items()):
        st = structures.get(doc_id, {})
        p = next((x for x in st.get("pages", []) if x["number"] == page), None)
        if p is None:
            pages.append({"document": doc_id, "page": page, "error": "not processed"})
            continue
        hyp = page_text(p)
        pages.append({"document": doc_id, "page": page, "method": p["method"], "cer": cer(ref, hyp),
                      "wer": wer(ref, hyp), "key_token_recall": key_token_recall(ref, hyp),
                      "mean_confidence": (p.get("ocr") or {}).get("mean_confidence"),
                      "deskew_degrees": (p.get("ocr") or {}).get("deskew_degrees")})
    ok = [x for x in pages if "cer" in x]
    ocr_docs = [lab["id"] for lab in labels if lab["scanned_pages"]]
    ms = [uploads[d]["ms"] for d in ocr_docs if d in uploads]
    return {"pages": pages, "scanned_pages": len(pages), "processed": len(ok),
            "mean_cer": round(sum(x["cer"] for x in ok) / len(ok), 4) if ok else None,
            "mean_wer": round(sum(x["wer"] for x in ok) / len(ok), 4) if ok else None,
            "mean_key_token_recall": round(sum(x["key_token_recall"] or 0 for x in ok) / len(ok), 4) if ok else None,
            "upload_ms_per_scanned_document_mean": round(sum(ms) / len(ms)) if ms else None,
            "note": "CER/WER on whitespace-normalized page text vs the exact text drawn on the scan; "
                    "Tesseract confidence is an engine score, not a probability"}


def page_text(page: dict) -> str:
    parts = []
    for b in page.get("blocks", []):
        if b.get("table"):
            grid = {}
            for c in b["table"]["cells"]:
                grid.setdefault(c["row"], []).append((c["col"], c["text"]))
            parts += [" ".join(t for _, t in sorted(row) if t) for _, row in sorted(grid.items())]
        else:
            parts.append(b["text"])
    return "\n".join(parts)


# ------------------------------------------------------------------ multi-document queries

def expected_query(q: dict, labels: list[dict], rejected: set[str]):
    vis = [lab for lab in labels if authorized(q["persona"], lab) and lab["id"] not in rejected]
    if q["type"] == "expiring_within":
        start = date.fromisoformat(q["as_of"])
        end = start + timedelta(days=q["days"])
        return {lab["id"] for lab in vis if lab["doc_type"] == "contract" and lab["fields"].get("expiration_date")
                and start <= date.fromisoformat(lab["fields"]["expiration_date"]) <= end}
    if q["type"] == "auto_renewal":
        return {lab["id"] for lab in vis if lab["doc_type"] == "contract" and lab["fields"].get("auto_renewal") == "true"}
    if q["type"] == "invalid_invoices":
        return {lab["id"] for lab in vis if lab["doc_type"] == "invoice" and set(lab["expected_errors"]) & ARITHMETIC_CODES}
    found = set()
    by_id: dict[str, list] = {}
    for lab in vis:
        if lab["doc_type"] == "contract":
            by_id.setdefault(lab["fields"]["contract_id"].upper(), []).append(lab)
    for cid, docs in by_id.items():
        for name in ("expiration_date", "contract_value", "notice_period", "payment_terms", "auto_renewal"):
            if len(docs) > 1 and len({d["fields"].get(name) for d in docs} - {None}) > 1:
                found.add(("same_contract_id", cid, name))
    by_sup: dict[str, list] = {}
    for lab in vis:
        if lab["doc_type"] == "invoice":
            by_sup.setdefault(supplier_key(lab["fields"]["supplier_name"]), []).append(lab)
    for key, docs in by_sup.items():
        for name in ("supplier_tax_id", "iban"):
            if len(docs) > 1 and len({d["fields"].get(name) for d in docs} - {None}) > 1:
                found.add(("same_supplier", key, name))
    return found


def query_metrics(client, h, labels, ids, queries, rejected) -> dict:
    back = {v: k for k, v in ids.items()}
    out = {}
    for q in queries:
        body = {"type": q["type"]} | ({"days": q["days"], "as_of": q["as_of"]} if q["type"] == "expiring_within" else {})
        res = client.post("/api/intelligence/query", headers=h(q["persona"]), json=body).json()
        if q["type"] == "conflicts":
            got = {(m["kind"], m["key"], m["field"]) for m in res.get("matches", [])}
        else:
            got = {back.get(m["document_id"], m["document_id"]) for m in res.get("matches", [])}
        expected = expected_query(q, labels, rejected)
        out[q["id"]] = set_scores(got, expected) | {"expected": sorted(map(str, expected)),
                                                     "got": sorted(map(str, got))}
    exact = sum(1 for v in out.values() if v["exact"])
    return {"queries": out, "exact_answers": exact, "total": len(out)}


# --------------------------------------------------------------------- review simulation

def _corrections(lab: dict, pred: dict[str, dict]) -> list[dict]:
    """What a careful reviewer types to make the extraction equal the document's ground truth."""
    out = []
    for name, vtype in SCALAR[lab["doc_type"]].items():
        p, g = pred.get(name, {}).get("value"), lab["fields"].get(name)
        if not equal(vtype, p, g) and (p is not None or g is not None):
            out.append({"name": name, "value": g})
    if lab["doc_type"] == "contract":
        predicted = [(n, f["value"]) for n, f in pred.items() if n.startswith("parties[") and f.get("value")]
        if sorted(v.casefold() for _, v in predicted) != sorted(v.casefold() for v in lab["parties"]):
            for i, party in enumerate(lab["parties"]):
                out.append({"name": f"parties[{i}]", "value": party})
            for n, _ in predicted:
                if int(n[8:-1]) >= len(lab["parties"]):
                    out.append({"name": n, "value": None})
    if lab["doc_type"] == "invoice":
        n_pred = len({k.split("]")[0] for k in pred if k.startswith("line_items[")})
        for i in range(max(n_pred, len(lab["line_items"]))):
            for col, vtype in ITEM_COLS.items():
                name = f"line_items[{i}].{col}"
                p = pred.get(name, {}).get("value")
                g = lab["line_items"][i][col] if i < len(lab["line_items"]) else None
                if not equal(vtype, p, g) and (p is not None or g is not None):
                    out.append({"name": name, "value": g})
    return out


def review_simulation(client, h, labels, ids, before) -> tuple[dict, set]:
    slots = corrected = approved = rejected_n = consistent = 0
    false_accepts = invalid_docs = blocked = overrides = 0
    extraction_wrong = extraction_wrong_pending = 0
    lock_conflict_ok = None
    rejected: set[str] = set()
    details = {}
    for lab in labels:
        if lab["id"] not in ids:
            continue
        reviewer = next((p for p in ("ns_reviewer", "oc_reviewer", "ns_admin") if can_review(p, lab)), None)
        ext = before[lab["id"]]
        eid, status_before = ext["id"], ext["status"]
        hdr = h(reviewer)
        if lab["doc_type"] == "unknown":
            r = client.post(f"/api/extractions/{eid}/reject", headers=hdr,
                            json={"expected_version": ext["version"], "comment": "not a contract or invoice"})
            rejected.add(lab["id"])
            rejected_n += r.status_code == 200
            continue
        pred = _pred(ext)
        fixes = _corrections(lab, pred)
        n_slots = len(SCALAR[lab["doc_type"]]) + len(lab["parties"]) + len(lab["line_items"]) * len(ITEM_COLS)
        slots += n_slots
        corrected += len(fixes)
        if fixes:
            extraction_wrong += 1
            extraction_wrong_pending += status_before == "pending"
        version = ext["version"]
        if lock_conflict_ok is None and fixes:  # one stale write must be refused (optimistic locking)
            stale = client.patch(f"/api/extractions/{eid}/fields", headers=hdr,
                                 json={"expected_version": version - 1, "corrections": fixes[:1]})
            lock_conflict_ok = stale.status_code == 409
        if fixes:
            r = client.patch(f"/api/extractions/{eid}/fields", headers=hdr,
                             json={"expected_version": version, "corrections": fixes})
            if r.status_code != 200:
                details[lab["id"]] = {"correction_error": r.json()}
                continue
            version = r.json()["version"]
        invalid = bool(set(lab["expected_errors"]))
        r = client.post(f"/api/extractions/{eid}/approve", headers=hdr,
                        json={"expected_version": version, "comment": "matches the document"})
        if invalid:
            invalid_docs += 1
            if r.status_code == 200:
                false_accepts += 1  # validation let an invalid document through
                approved += 1
            else:
                blocked += r.status_code == 409
                rr = client.post(f"/api/extractions/{eid}/reject", headers=hdr,
                                 json={"expected_version": version, "comment": "document totals / dates are invalid"})
                rejected_n += rr.status_code == 200
                rejected.add(lab["id"])
            continue
        if r.status_code == 409:  # remaining (warning-free) errors on a valid document: explicit override
            overrides += 1
            r = client.post(f"/api/extractions/{eid}/approve", headers=hdr,
                            json={"expected_version": version, "comment": "matches the document",
                                  "override_reason": "verified against the original document"})
        if r.status_code == 200:
            approved += 1
            final = _pred(r.json())
            consistent += not _corrections(lab, final)
        details[lab["id"]] = {"corrections": len(fixes), "status_before": status_before,
                              "decision": "approved" if r.status_code == 200 else f"http {r.status_code}"}
    return ({"correction_rate": rate(corrected, slots), "corrected_fields": corrected, "field_slots": slots,
             "approved": approved, "rejected": rejected_n,
             "approval_consistency": rate(consistent, approved),
             "invalid_documents": invalid_docs, "invalid_approvals_blocked": blocked,
             "validation_false_accept_rate": rate(false_accepts, invalid_docs),
             "extractions_with_errors": extraction_wrong,
             "extraction_errors_not_flagged_rate": rate(extraction_wrong_pending, extraction_wrong),
             "approvals_needing_override": overrides, "optimistic_lock_conflict_detected": lock_conflict_ok,
             "policy": "reviewer corrects every field to the ground truth, approves valid documents, "
                       "rejects documents whose ground truth is invalid (after checking that approval is blocked) "
                       "and rejects unknown documents",
             "details": details}, rejected)


# ---------------------------------------------------------------------- security matrix

def security_matrix(client, h, labels, ids) -> dict:
    counts = {"checks": 0, "unauthorized_extraction_access": 0, "cross_tenant_leakage": 0,
              "unauthorized_review_actions": 0, "unauthorized_comparisons": 0, "false_denials": 0}
    titles = {lab["id"]: Path(lab["file"]).stem for lab in labels}
    tenant_of = {lab["id"]: lab["tenant"] for lab in labels}
    leaks = []

    def leaked(persona, payload: str, lab_filter) -> list[str]:
        return [lid for lid in ids if lab_filter(lid) and (ids[lid] in payload)]

    by_id = {lab["id"]: lab for lab in labels}
    extraction_ids = {}
    for lid in ids:
        r = client.get(f"/api/documents/{ids[lid]}/extraction", headers=h(by_id[lid]["owner"]))
        extraction_ids[lid] = r.json()["id"]
    for persona in PERSONAS:
        hdr = h(persona)
        for lid, doc_id in ids.items():
            lab = by_id[lid]
            allowed = authorized(persona, lab)
            eid = extraction_ids[lid]
            for path in (f"/api/documents/{doc_id}/extraction", f"/api/documents/{doc_id}/structure",
                         f"/api/documents/{doc_id}/processing", f"/api/extractions/{eid}/history",
                         f"/api/extractions/{eid}/fields/{'invoice_number' if lab['doc_type'] == 'invoice' else 'contract_id'}/evidence"):
                r = client.get(path, headers=hdr)
                counts["checks"] += 1
                if not allowed and r.status_code != 404:
                    counts["unauthorized_extraction_access"] += 1
                    leaks.append({"persona": persona, "doc": lid, "path": path, "status": r.status_code})
                if allowed and r.status_code == 404 and "evidence" not in path:
                    counts["false_denials"] += 1
            for method, path, body in (("patch", f"/api/extractions/{eid}/fields",
                                        {"expected_version": 1, "corrections": [{"name": "currency", "value": "EUR"}]}),
                                       ("post", f"/api/extractions/{eid}/approve", {"expected_version": 1}),
                                       ("post", f"/api/extractions/{eid}/reject", {"expected_version": 1, "comment": "x" * 5}),
                                       ("post", f"/api/documents/{doc_id}/process", {"provider": "rules"})):
                if allowed and PERSONAS[persona][2] in ("admin", "reviewer"):
                    continue  # legitimate reviewers are exercised by the review simulation
                if allowed and method == "post" and path.endswith("/process") and lab["owner"] == persona:
                    continue
                r = getattr(client, method)(path, headers=hdr, json=body)
                counts["checks"] += 1
                ok_status = 404 if not allowed else 403
                if r.status_code != ok_status:
                    counts["unauthorized_review_actions"] += 1
                    leaks.append({"persona": persona, "doc": lid, "path": path, "status": r.status_code})
        # comparisons: one authorized + one forbidden document must fail without detail
        mine = [lid for lid in ids if authorized(persona, by_id[lid])]
        others = [lid for lid in ids if not authorized(persona, by_id[lid])]
        for forbidden in others:
            if not mine:
                break
            r = client.post("/api/intelligence/compare", headers=hdr,
                            json={"document_ids": [ids[mine[0]], ids[forbidden]]})
            counts["checks"] += 1
            if r.status_code != 404 or ids[forbidden] in r.text or titles[forbidden] in r.text:
                counts["unauthorized_comparisons"] += 1
                leaks.append({"persona": persona, "compare_with": forbidden, "status": r.status_code})
        for qtype in ("expiring_within", "auto_renewal", "invalid_invoices", "conflicts"):
            r = client.post("/api/intelligence/query", headers=hdr, json={"type": qtype, "days": 3650,
                                                                           "as_of": "2026-01-01"})
            counts["checks"] += 1
            bad = leaked(persona, r.text, lambda lid: not authorized(persona, by_id[lid]))
            if bad:
                counts["unauthorized_comparisons"] += 1
                leaks.append({"persona": persona, "query": qtype, "leaked": bad})
        for path in ("/api/intelligence/documents", "/api/documents"):
            r = client.get(path, headers=hdr)
            counts["checks"] += 1
            other_tenant = leaked(persona, r.text, lambda lid: tenant_of[lid] != PERSONAS[persona][1])
            if other_tenant:
                counts["cross_tenant_leakage"] += 1
                leaks.append({"persona": persona, "path": path, "leaked": other_tenant})
            if leaked(persona, r.text, lambda lid: not authorized(persona, by_id[lid])):
                counts["unauthorized_extraction_access"] += 1
    counts["total_violations"] = sum(counts[k] for k in ("unauthorized_extraction_access", "cross_tenant_leakage",
                                                          "unauthorized_review_actions", "unauthorized_comparisons"))
    counts["violations"] = leaks[:50]
    return counts


def failure_analysis(labels, before) -> list[dict]:
    out = []
    for lab in labels:
        ext = before.get(lab["id"])
        if ext is None:
            out.append({"document": lab["id"], "error": "upload / processing failed"})
            continue
        pred = _pred(ext)
        problems = []
        if ext["doc_type"] != lab["doc_type"]:
            problems.append(f"classified as {ext['doc_type']}, ground truth {lab['doc_type']}")
        if lab["doc_type"] != "unknown":
            for group, name, vtype, p, g in _slots(lab, pred):
                if not equal(vtype, p, g):
                    problems.append(f"{name}: predicted {p!r}, expected {g!r}")
        out.append({"document": lab["id"], "split": lab["split"], "format": lab["format"],
                    "language": lab["language"], "status": ext["status"], "notes": lab["notes"],
                    "problems": problems})
    return out
