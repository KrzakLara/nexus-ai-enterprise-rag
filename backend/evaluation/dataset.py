"""Ground-truth dataset: hand-written questions + evidence quotes -> frozen chunk-level labels.

Relevance is derived ONLY from the source documents: a chunk is relevant to a question when it
contains one of the question's verbatim evidence quotes (grade 2 = primary, 1 = supporting).
Retrieval output is never used to label anything. Unauthorized evidence (authorization cases) is
stored separately and never counted as relevant.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .corpus import EVAL_DIR, Corpus

QUESTIONS_PATH = EVAL_DIR / "questions.jsonl"
FROZEN_PATH = EVAL_DIR / "retrieval.jsonl"
CATEGORIES = {
    "exact_identifier", "keyword", "paraphrase", "croatian", "cross_language", "numeric_date",
    "multi_document", "distractor", "unanswerable", "authorization",
}
LANGUAGES = {"en", "hr"}


class DatasetError(ValueError):
    pass


@dataclass(frozen=True)
class EvalCase:
    id: str
    question: str
    persona: str
    tenant: str
    category: str
    language: str
    answerable: bool
    split: str
    relevance: dict[str, int]                  # authorized relevant chunk id -> grade (1|2)
    unauthorized_relevant_chunk_ids: tuple[str, ...]
    forbidden_document_ids: tuple[str, ...]
    expected_evidence: tuple[str, ...]
    expected_facts: tuple[str, ...]

    @property
    def relevant_ids(self) -> set[str]:
        return set(self.relevance)


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise DatasetError(f"{path.name}:{n}: invalid JSON ({exc})") from None
    return rows


def assign_splits(questions: list[dict]) -> dict[str, str]:
    """Deterministic stratified split: within each category (sorted by id) alternate dev/test."""
    by_cat: dict[str, list[str]] = defaultdict(list)
    for q in questions:
        by_cat[q["category"]].append(q["id"])
    return {qid: ("dev" if i % 2 == 0 else "test")
            for ids in by_cat.values() for i, qid in enumerate(sorted(ids))}


def build_cases(questions: list[dict], corpus: Corpus) -> list[EvalCase]:
    errors: list[str] = []
    seen: set[str] = set()
    splits = assign_splits(questions)
    cases = []
    for q in questions:
        qid = q.get("id", "?")
        def err(msg: str) -> None:
            errors.append(f"{qid}: {msg}")  # noqa: B023 - evaluated immediately
        if qid in seen:
            err("duplicate id")
        seen.add(qid)
        for key in ("question", "persona", "category", "language", "answerable", "evidence"):
            if key not in q:
                err(f"missing field {key!r}")
        if errors and errors[-1].startswith(f"{qid}: missing"):
            continue
        if q["persona"] not in corpus.personas:
            err(f"unknown persona {q['persona']}")
            continue
        if q["category"] not in CATEGORIES:
            err(f"unknown category {q['category']}")
        if q["language"] not in LANGUAGES:
            err(f"unknown language {q['language']}")
        relevance: dict[str, int] = {}
        unauthorized: set[str] = set()
        forbidden_docs: set[str] = set()
        for ev in q["evidence"]:
            doc = ev.get("doc")
            if doc not in corpus.documents:
                err(f"evidence references unknown document {doc}")
                continue
            if ev.get("grade") not in (1, 2):
                err(f"evidence grade must be 1 or 2 ({doc})")
                continue
            hits = [c for c in corpus.chunks_of(doc) if ev["text"] in c.content]
            if not hits:
                err(f"evidence text not found verbatim in any chunk of {doc}: {ev['text']!r}")
                continue
            if corpus.authorized(q["persona"], doc):
                for c in hits:
                    relevance[c.id] = max(relevance.get(c.id, 0), ev["grade"])
            else:
                unauthorized.update(c.id for c in hits)
                forbidden_docs.add(doc)
        answerable = bool(q["answerable"])
        if answerable and not relevance:
            err("answerable question has no authorized relevant chunk")
        if answerable and unauthorized:
            err("answerable question cites evidence the persona may not read")
        if not answerable and relevance:
            err("unanswerable question has authorized relevant chunks")
        if q["category"] == "authorization" and not forbidden_docs:
            err("authorization case must reference a forbidden document")
        if q["category"] == "unanswerable" and q["evidence"]:
            err("unanswerable case must not have evidence")
        if answerable and q["category"] in ("unanswerable", "authorization"):
            err("category implies answerable=false")
        for fact in q.get("expected_facts", []):
            if not any(fact in corpus.chunk_by_id[cid].content for cid in relevance):
                err(f"expected fact {fact!r} is not contained in any relevant chunk")
        cases.append(EvalCase(
            id=qid, question=q["question"], persona=q["persona"],
            tenant=corpus.personas[q["persona"]].tenant, category=q["category"],
            language=q["language"], answerable=answerable, split=splits[qid],
            relevance=dict(sorted(relevance.items())),
            unauthorized_relevant_chunk_ids=tuple(sorted(unauthorized)),
            forbidden_document_ids=tuple(sorted(forbidden_docs)),
            expected_evidence=tuple(ev["text"] for ev in q["evidence"]),
            expected_facts=tuple(q.get("expected_facts", [])),
        ))
    if errors:
        raise DatasetError("Invalid evaluation dataset:\n  " + "\n  ".join(errors))
    return cases


def case_to_row(case: EvalCase) -> dict:
    return {
        "id": case.id, "split": case.split, "persona": case.persona, "tenant": case.tenant,
        "category": case.category, "language": case.language, "answerable": case.answerable,
        "question": case.question,
        "relevant_chunk_ids": sorted(case.relevance),
        "relevance": case.relevance,
        "unauthorized_relevant_chunk_ids": list(case.unauthorized_relevant_chunk_ids),
        "forbidden_document_ids": list(case.forbidden_document_ids),
        "expected_evidence": list(case.expected_evidence),
        "expected_facts": list(case.expected_facts),
    }


def row_to_case(row: dict) -> EvalCase:
    return EvalCase(
        id=row["id"], question=row["question"], persona=row["persona"], tenant=row["tenant"],
        category=row["category"], language=row["language"], answerable=row["answerable"],
        split=row["split"], relevance={k: int(v) for k, v in row["relevance"].items()},
        unauthorized_relevant_chunk_ids=tuple(row["unauthorized_relevant_chunk_ids"]),
        forbidden_document_ids=tuple(row["forbidden_document_ids"]),
        expected_evidence=tuple(row["expected_evidence"]),
        expected_facts=tuple(row["expected_facts"]),
    )


def render_frozen(cases: list[EvalCase]) -> str:
    return "".join(json.dumps(case_to_row(c), ensure_ascii=False) + "\n" for c in cases)


def load_frozen(corpus: Corpus, path: Path = FROZEN_PATH,
                questions_path: Path = QUESTIONS_PATH) -> list[EvalCase]:
    """Load the frozen dataset and verify it is byte-identical to a fresh rebuild."""
    rebuilt = build_cases(read_jsonl(questions_path), corpus)
    frozen = path.read_text(encoding="utf-8")
    if frozen != render_frozen(rebuilt):
        raise DatasetError(
            f"{path.name} is stale or was edited by hand; run python scripts/build_eval_dataset.py")
    return [row_to_case(r) for r in read_jsonl(path)]
