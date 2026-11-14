"""Deterministic document-type classifier (contract / invoice / unknown), English + Croatian.

Each type has weighted cue phrases. A cue found in the first heading / first 300 characters counts
double (titles are strong evidence). The document gets a type only if its score reaches MIN_SCORE
AND beats the other type by MIN_MARGIN; otherwise it is 'unknown' with decision 'ambiguous' or
'insufficient_evidence'. Scores are cue counts, not probabilities. Only contracts and invoices are
supported: anything else is deliberately reported as unknown, never forced into a class.
"""
from __future__ import annotations

import re

from .normalize import fold
from .structure import ParsedDocument

CUES: dict[str, dict[str, float]] = {
    "invoice": {
        "invoice": 2, "račun": 2, "racun": 1, "faktura": 2, "invoice number": 2, "invoice no": 2,
        "broj računa": 2, "due date": 1, "datum dospijeća": 1, "dospijeće": 1, "subtotal": 1,
        "vat": 1, "pdv": 1, "total due": 1, "amount due": 1, "za platiti": 1, "unit price": 1,
        "jed. cijena": 1, "qty": 1, "količina": 1, "iban": 1, "bill to": 1, "poziv na broj": 1,
        "payment reference": 1,
    },
    "contract": {
        "agreement": 2, "contract": 2, "ugovor": 2, "parties": 1, "ugovorne strane": 1,
        "effective date": 1, "termination": 1, "raskid": 1, "governing law": 1,
        "mjerodavno pravo": 1, "notice period": 1, "otkazni rok": 1, "renew": 1, "obnavlja": 1,
        "produžuje": 1, "shall": 1, "obvezuje": 1, "term of": 1, "trajanje": 1, "counterparty": 1,
        "start date": 1, "end date": 1, "datum isteka": 1,
    },
}
MIN_SCORE, MIN_MARGIN = 4.0, 2.0
METHOD = "keyword_cues_v1"


def classify(doc: ParsedDocument) -> dict:
    full = fold("\n".join(doc.page_texts()))
    first_heading = next((b.text for p in doc.pages for b in p.blocks if b.kind == "heading"), "")
    head = fold(first_heading + " " + full[:300])
    scores: dict[str, float] = {}
    cues: dict[str, list[str]] = {}
    for doc_type, table in CUES.items():
        total, found = 0.0, []
        for cue, weight in table.items():
            pattern = rf"(?<![\w]){re.escape(cue)}(?![\w])" if len(cue) <= 4 else re.escape(cue)
            if re.search(pattern, full):
                total += weight * (2 if re.search(pattern, head) else 1)
                found.append(cue)
        scores[doc_type], cues[doc_type] = round(total, 1), found
    best, other = sorted(scores, key=lambda t: -scores[t])
    if scores[best] < MIN_SCORE:
        decision, label = "insufficient_evidence", "unknown"
    elif scores[best] - scores[other] < MIN_MARGIN:
        decision, label = "ambiguous", "unknown"
    else:
        decision, label = "classified", best
    return {"type": label, "decision": decision, "scores": scores, "cues": cues, "method": METHOD,
            "note": "Cue counts, not probabilities. Supported types: contract, invoice."}
