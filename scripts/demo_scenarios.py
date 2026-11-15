"""Run the eight NEXUS AI demo scenarios against a RUNNING stack (docker compose up) and check them.

    python scripts/demo_scenarios.py                       # http://localhost:8000
    python scripts/demo_scenarios.py --api http://host:8000

Read-only: it only logs in as the seeded demo personas and calls /api/ask and
/api/evaluation/summary (each /api/ask writes one audit event, like any UI query).
Exit code 1 if any scenario does not behave as documented. KNOWN = documented limitation
(reported, not a failure).
"""
import argparse
import json
import sys
import urllib.request

PASSWORD = "DemoPass123!"


class Api:
    def __init__(self, base: str):
        self.base, self.tokens = base.rstrip("/"), {}

    def _call(self, path, body=None, token=None):
        req = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": "Bearer " + token} if token else {})})
        with urllib.request.urlopen(req, timeout=120) as response:
            return json.loads(response.read())

    def token(self, email):
        if email not in self.tokens:
            self.tokens[email] = self._call("/api/auth/login", {"email": email, "password": PASSWORD})["access_token"]
        return self.tokens[email]

    def ask(self, email, question, mode, **extra):
        return self._call("/api/ask", {"question": question, "mode": mode, "top_k": 3, **extra}, self.token(email))

    def summary(self, email):
        return self._call("/api/evaluation/summary", token=self.token(email))


def show(title, body):
    ev = body["diagnostics"].get("evidence", {})
    print(f"\n=== {title}\n  retrieval={body['retrieval']} answer_mode={body['mode']} "
          f"evidence={ev.get('reasons') if ev.get('applied') else 'n/a'}")
    for s in body["sources"]:
        ranks = f"lex={s.get('lexical_rank')} sem={s.get('semantic_rank')}" if s["method"] == "hybrid" else ""
        print(f"  [{s['id']}] {s['title']} ({s['method']} {ranks})")
        for q in s.get("selected_sentences", [])[:2]:
            print(f"       “{q}”")


def main() -> int:
    # Quotes contain Croatian letters (č, ž, ...): a Windows console defaults to cp1252 and would
    # raise UnicodeEncodeError mid-run, so force UTF-8 output.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", default="http://localhost:8000")
    api = Api(parser.parse_args().api)
    fin, hr = "finance@northstar.demo", "hr@northstar.demo"
    checks = []

    def check(name, ok):
        checks.append((name, "PASS" if ok else "FAIL"))

    def known(name, still_present):
        """A documented limitation: reported, does not fail the run; flagged if it disappears."""
        checks.append((name, "KNOWN" if still_present else "CHANGED (update the docs)"))

    b = api.ask(fin, "NS-IT-2026-017", "lexical")
    show("1. Exact keyword (PostgreSQL FTS): NS-IT-2026-017", b)
    check("1 exact identifier -> Cygnus first", b["sources"] and "Cygnus" in b["sources"][0]["title"])

    q = "What uptime does the cloud provider promise?"
    raw = api.ask(fin, q, "semantic", apply_evidence_policy=False)
    show("2a. Semantic paraphrase, raw retrieval (policy off; no shared keyword 'availability')", raw)
    check("2a paraphrase retrieval -> Cygnus first", raw["sources"] and "Cygnus" in raw["sources"][0]["title"])
    b = api.ask(fin, q, "semantic")
    show("2b. Same paraphrase with the shipped evidence policy", b)
    sim = b["diagnostics"]["evidence"].get("signals", {}).get("sentence_top_similarity")
    known(f"2b documented false abstention (best sentence cos {sim} < 0.835)", b["mode"] == "no_evidence")

    b = api.ask(fin, "NS-IT-2026-017", "hybrid")
    show("3. Hybrid fusion (RRF over FTS + semantic ranks)", b)
    top = b["sources"][0] if b["sources"] else {}
    check("3 hybrid -> Cygnus first with both branch ranks", "Cygnus" in top.get("title", "") and top.get("lexical_rank") == 1)

    b = api.ask(fin, "Kolika je dnevnica za službeno putovanje u Hrvatskoj dulje od 12 sati?", "hybrid")
    show("4. Croatian question over a Croatian document", b)
    check("4 Croatian -> 30 EUR quoted", b["mode"] == "extractive" and "30 EUR" in b["answer"])

    b = api.ask(fin, "Koliko dana unaprijed moramo najaviti raskid ugovora s dobavljačem?", "hybrid")
    show("5. Cross-language: Croatian question, English Atlas contract", b)
    check("5 cross-language -> '90 days' quoted from Atlas", b["mode"] == "extractive" and "90 days" in b["answer"]
          and any("Atlas" in s["title"] for s in b["sources"]))

    b = api.ask(fin, "What is the parking policy for visitors at the Graz plant?", "hybrid")
    show("6. Abstention: the corpus does not contain the answer", b)
    check("6 abstains with no sources", b["mode"] == "no_evidence" and not b["sources"])

    b = api.ask(fin, "What is the capital expenditure in OC-FIN-2027-001?", "hybrid")
    show("7a. Other tenant's document identifier (Northstar finance user)", b)
    check("7a identifier_not_found, no Otherco text", b["mode"] == "no_evidence"
          and "identifier_not_found" in b["diagnostics"]["evidence"]["reasons"] and "9,800,000" not in json.dumps(b))
    b = api.ask(hr, "Koliko dana unaprijed moramo najaviti raskid ugovora s dobavljačem?", "hybrid")
    show("7b. Same Atlas question as the HR user (no Finance/Legal grant)", b)
    check("7b HR user never sees Atlas", not any("Atlas" in s["title"] for s in b["sources"]) and "90 days" not in b["answer"])

    s = api.summary(fin)
    hyb = s["modes"]["hybrid"]["test"]
    print(f"\n=== 8. Stored evaluation (generated {s['generated_at']}): hybrid test nDCG@5={hyb['ndcg@5']} "
          f"Recall@5={hyb['recall@5']}; authorization violations={s['security'].get('total_violations')}")
    check("8 evaluation summary served, zero violations", s["security"].get("total_violations") == 0)

    print("\n" + "\n".join(f"  {status:<5}  {name}" for name, status in checks))
    return 1 if any(status == "FAIL" for _, status in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
