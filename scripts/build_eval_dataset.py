"""Rebuild eval/retrieval.jsonl (frozen chunk-level labels) from eval/questions.jsonl + corpus."""
from collections import Counter
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.evaluation.corpus import load_corpus
from backend.evaluation.dataset import FROZEN_PATH, QUESTIONS_PATH, build_cases, read_jsonl, render_frozen


def main() -> None:
    corpus = load_corpus()
    cases = build_cases(read_jsonl(QUESTIONS_PATH), corpus)
    FROZEN_PATH.write_text(render_frozen(cases), encoding="utf-8")
    print(f"documents={len(corpus.documents)} chunks={len(corpus.chunks)} cases={len(cases)}")
    print("splits:", dict(Counter(c.split for c in cases)))
    print("categories:", dict(sorted(Counter(c.category for c in cases).items())))
    print("languages:", dict(Counter(c.language for c in cases)))
    print("answerable:", dict(Counter(c.answerable for c in cases)))
    print("relevant chunks per answerable case:",
          dict(sorted(Counter(len(c.relevance) for c in cases if c.answerable).items())))
    for pid in sorted(corpus.personas):
        print(f"  persona {pid:<11} authorized chunks: {len(corpus.authorized_chunk_ids(pid))}")


if __name__ == "__main__":
    main()
