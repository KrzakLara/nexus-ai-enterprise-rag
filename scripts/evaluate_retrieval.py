"""Reproducible small retrieval + authorization baseline, no API keys required."""
from pathlib import Path
import json
import sys
from sqlalchemy import select
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.database import SessionLocal
from backend.app.models import User
from backend.app.retrieval import search


def main():
    samples = json.loads((Path(__file__).resolve().parents[1] / "demo_documents" / "eval.json").read_text())
    n_correct = 0
    with SessionLocal() as db:
        for example in samples:
            user = db.scalar(select(User).where(User.email == example["user"]))
            hits = search(db, user, example["query"], top_k=5)
            names = [hit.document_title.lower().replace(" ", "_") for hit in hits]
            if example["expected"] is None:
                passed = not any(example["forbidden"].lower() in hit.content.lower() for hit in hits)
            else:
                passed = any(example["expected"] in name for name in names)
            n_correct += passed
            print(("PASS" if passed else "FAIL"), example["user"], example["query"], names[:3])
    print(f"Retrieval/ACL examples passed: {n_correct}/{len(samples)}")
    if n_correct != len(samples):
        raise SystemExit(1)

if __name__ == "__main__":
    main()
