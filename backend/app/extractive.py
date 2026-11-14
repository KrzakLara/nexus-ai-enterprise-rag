"""Query-aware, deterministic evidence selection for the offline (no-LLM) answer mode.

For each of the top authorized hits, the chunk is split into sentences / lines and every sentence
is scored against the question:

    score = w_semantic * cos(e5 "query: q", e5 "passage: sentence")      (if an embedder is given)
          + w_lexical  * share of the question's content terms found in the sentence
          + w_exact    * share of the question's identifiers / numbers found verbatim

The answer quotes the best sentence(s) VERBATIM, each with its own [S#] citation, so nothing is
generated, nothing is merged across sources, and every quoted span is a substring of an
authorized chunk. Scores are ranking heuristics, not confidence values.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from .evidence import extract_identifiers, identifier_in
from .retrieval import QUERY_STOP_WORDS, Hit

# Split on line breaks and on sentence ends followed by an upper-case letter.
# "EUR 1.60", "31. ožujka", "2027. godinu" are not split (no space+capital after the dot).
_SPLIT = re.compile(r"\n+|(?<=[.!?])\s+(?=[A-ZČĆŠĐŽ])")
# A SINGLE line break inside running prose (hard-wrapped TXT, text extracted from PDFs) is not a
# sentence boundary: the next line starts in lower case, or with a number while the previous line
# has no terminal punctuation ("Service levels require\n98.5% on-time delivery."). Headings,
# "Key: value" lines, numbered items ("2. Commercial terms") and blank lines stay boundaries.
_LOWER_START = re.compile(r"[a-zčćšđž]")
_DIGIT_START = re.compile(r"\d")
_NUMBERED_ITEM = re.compile(r"\d+[.)]\s+[A-ZČĆŠĐŽ]")
_TERMINAL = re.compile(r"[.!?:;]\s*$")
_WORD = re.compile(r"\w+", re.UNICODE)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


class PassageQueryEmbedder(Protocol):
    def embed_query(self, text: str) -> np.ndarray: ...
    def embed_passages(self, texts: list[str]) -> np.ndarray: ...


@dataclass(frozen=True)
class ExtractionConfig:
    """Defaults = configuration selected on the DEV split (scripts/evaluate.py, extraction study)."""
    max_sources: int = 3
    sentences_per_source: int = 2
    min_sentence_chars: int = 12
    w_semantic: float = 1.0
    w_lexical: float = 0.5
    w_exact: float = 0.0


@dataclass(frozen=True)
class Snippet:
    source_index: int          # 1-based position in the hits list -> [S#]
    hit: Hit
    sentence: str              # verbatim substring of hit.content
    score: float
    semantic_similarity: float | None


def _is_continuation(previous_line: str, rest: str) -> bool:
    nxt = rest.lstrip(" \t")
    if _LOWER_START.match(nxt):
        return True
    return bool(_DIGIT_START.match(nxt) and previous_line.strip() and not _TERMINAL.search(previous_line)
                and not _NUMBERED_ITEM.match(nxt))


def split_sentences(text: str, min_chars: int = 12) -> list[str]:
    """Sentences / lines as VERBATIM substrings of `text` (a joined sentence keeps its newline)."""
    parts, start = [], 0
    for m in _SPLIT.finditer(text):
        if m.group() == "\n":  # single line break: boundary unless it continues the sentence
            line_start = text.rfind("\n", 0, m.start()) + 1
            if _is_continuation(text[line_start:m.start()], text[m.end():]):
                continue
        parts.append(text[start:m.start()])
        start = m.end()
    parts.append(text[start:])
    return [p.strip() for p in parts if len(p.strip()) >= min_chars]


def display(sentence: str) -> str:
    """One-line rendering of a quote (whitespace collapsed; the words are unchanged)."""
    return " ".join(sentence.split())


def _stem(token: str) -> str:
    """Crude, language-agnostic prefix stem: groups ugovor/ugovora/ugovorom, retain/retained."""
    return token[:5] if len(token) > 5 else token


def content_terms(text: str) -> set[str]:
    return {_stem(t) for t in (w.lower() for w in _WORD.findall(text))
            if t not in QUERY_STOP_WORDS and (len(t) > 2 or t.isdigit())}


def _exact_items(question: str) -> list[str]:
    return extract_identifiers(question) + _NUMBER.findall(question)


def _exact_share(items: list[str], sentence: str) -> float:
    if not items:
        return 0.0
    found = sum(1 for i in items if (identifier_in(i, sentence) if "-" in i else i in sentence))
    return found / len(items)


def select_evidence(question: str, hits: list[Hit], embedder: PassageQueryEmbedder | None = None,
                    config: ExtractionConfig = ExtractionConfig()) -> list[Snippet]:
    top = hits[: config.max_sources]
    candidates: list[tuple[int, Hit, str]] = []
    for i, hit in enumerate(top, 1):
        sentences = split_sentences(hit.content, config.min_sentence_chars) or [hit.content.strip()]
        candidates.extend((i, hit, s) for s in sentences)
    if not candidates:
        return []
    q_terms = content_terms(question)
    exact = _exact_items(question)
    sims: list[float | None] = [None] * len(candidates)
    if embedder is not None and config.w_semantic:
        q = embedder.embed_query(question)
        vectors = embedder.embed_passages([s for _, _, s in candidates])
        sims = [float(v @ q) for v in vectors]
    scored = []
    for (i, hit, sentence), sim in zip(candidates, sims):
        lexical = len(q_terms & content_terms(sentence)) / len(q_terms) if q_terms else 0.0
        score = (config.w_semantic * (sim or 0.0) + config.w_lexical * lexical
                 + config.w_exact * _exact_share(exact, sentence))
        scored.append(Snippet(i, hit, sentence, round(score, 4), None if sim is None else round(sim, 4)))
    chosen: list[Snippet] = []
    for i in range(1, len(top) + 1):
        own = sorted((s for s in scored if s.source_index == i), key=lambda s: (-s.score, s.sentence))
        # keep document order within a source so quoted sentences read naturally
        best = own[: config.sentences_per_source]
        chosen.extend(sorted(best, key=lambda s: s.hit.content.find(s.sentence)))
    return chosen


def render_extractive_answer(snippets: list[Snippet]) -> str:
    lines = [f"[S{s.source_index}] {s.hit.document_title}, page {s.hit.page_number}: {display(s.sentence)}"
             for s in snippets]
    return ("Evidence-first offline answer (verbatim quotes, no LLM key configured):\n\n"
            + "\n\n".join(lines)
            + "\n\nAdd OPENAI_API_KEY to generate a synthesized answer from these authorized sources.")
