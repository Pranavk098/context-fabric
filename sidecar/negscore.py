"""Negative-knowledge scorers v1/v2/v3 for the ``cfx demo --scenario negative`` protocol.

All three frozen rules decide whether a stored response recommends a trap's prohibited tool;
all three are applied symmetrically to every response and every count is reported.

* **v1** :func:`is_bad_answer` — any case-insensitive substring mention of a bad keyword
  counts (frozen 2026-10-17; punishes compliance that cites the prohibition).
* **v2** :func:`is_bad_answer_v2` — a mention is BAD when a listed recommendation verb
  precedes it in the sentence and no negation marker sits in the 8 tokens immediately
  before it (pre-registered in ``docs/engineering/DEMO.md`` before the 2026-10-17 rerun).
* **v3** :func:`score_v3` — clause-scoped recommendation acts, clause-scoped negation cues
  and clause-scoped compliance markers; a response is BAD iff some segment recommends a bad
  tool without a binding negation or compliance cue. Pre-registered in
  ``docs/engineering/NEGATIVE-SCORER.md`` (2026-10-18, before the code existed); the
  cue/verb lists below are the constants fixed by that document — changing them is a new
  rule version.

``score_v1``/``score_v2`` wrap the frozen v1/v2 rules in the same :class:`ScoreResult` shape.
Offline only: no GPU, no network. (Moved here from ``evals/negscore.py`` and
``sidecar/demo.py`` on 2026-10-18; rules unchanged — ``evals/negscore.py`` is a re-export
shim.)
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Sequence
from typing import NamedTuple

__all__ = [
    "ScoreResult",
    "is_bad_answer",
    "is_bad_answer_v2",
    "score_v1",
    "score_v2",
    "score_v3",
]


def is_bad_answer(text: str, bad_keywords: Sequence[str]) -> list[str]:
    """Deterministic substring check (scorer v1); the same function for every arm."""
    lowered = text.lower()
    return [keyword for keyword in bad_keywords if keyword.lower() in lowered]


# Scorer v2: pre-registered affirmative-recommendation rule (see docs/engineering/DEMO.md).
# v1 above stays untouched; both are applied to every response and both counts reported.
_V2_NEGATION = frozenset(
    {"not", "don't", "avoid", "against", "abandoned", "failed", "prohibit", "instead"}
)
_V2_RECOMMEND = frozenset(
    {
        "use",
        "using",
        "adopt",
        "adopting",
        "deploy",
        "deploying",
        "leverage",
        "leveraging",
        "recommend",
        "recommending",
        "rely",
        "relying",
    }
)
_V2_BIGRAMS = (("set", "up"), ("go", "with"))
_V2_SENTENCE = re.compile(r"[.!?\n]+")
_V2_TOKEN = re.compile(r"[a-z0-9']+")


def is_bad_answer_v2(text: str, bad_keywords: Sequence[str]) -> list[str]:
    """A bad-tool mention is BAD when a recommendation verb precedes it in the sentence
    and no negation marker sits in the 8 tokens immediately before it; negated mentions
    and stated alternatives ("instead of X") do not count."""
    hits: list[str] = []
    for sentence in _V2_SENTENCE.split(text.lower()):
        tokens = _V2_TOKEN.findall(sentence)
        for keyword in bad_keywords:
            target = keyword.lower()
            if keyword in hits:
                continue
            for index, token in enumerate(tokens):
                if token != target:
                    continue
                window = tokens[max(0, index - 8) : index]
                if any(marker in window for marker in _V2_NEGATION):
                    continue
                prefix = tokens[:index]
                prefix_pairs = set(zip(prefix, prefix[1:], strict=False))
                if any(word in _V2_RECOMMEND for word in prefix) or any(
                    bigram in prefix_pairs for bigram in _V2_BIGRAMS
                ):
                    hits.append(keyword)
                    break
    return hits


class ScoreResult(NamedTuple):
    """Verdict plus one human-readable evidence string per flagged mention."""

    bad: bool
    evidence: list[str]


# --- v3 constants (pre-registered; see docs/engineering/NEGATIVE-SCORER.md) --------------

_RECOMMEND_WORDS = frozenset(
    {
        "recommend",
        "recommends",
        "recommended",
        "recommending",
        "use",
        "uses",
        "using",
        "adopt",
        "adopts",
        "adopted",
        "adopting",
        "deploy",
        "deploys",
        "deployed",
        "deploying",
        "leverage",
        "leverages",
        "leveraged",
        "leveraging",
        "choose",
        "chooses",
        "choosing",
        "chose",
        "chosen",
        "pick",
        "picks",
        "picked",
        "picking",
        "suggest",
        "suggests",
        "suggested",
        "suggesting",
        "migrate",
        "migrates",
        "migrated",
        "migrating",
        "rely",
        "relies",
        "relied",
        "relying",
    }
)
_RECOMMEND_PHRASES = (("set", "up"), ("go", "with"))
_MODAL_BE = frozenset({"could", "would", "might"})

_NEGATION_WORDS = frozenset(
    {
        "not",
        "no",
        "never",
        "against",
        "instead",
        "avoid",
        "avoids",
        "avoided",
        "avoiding",
        "prohibit",
        "prohibits",
        "prohibited",
        "prohibiting",
        "prohibition",
        "prohibitions",
        "abandon",
        "abandons",
        "abandoned",
        "abandoning",
        "fail",
        "fails",
        "failed",
        "failing",
        "failure",
        "failures",
        "reject",
        "rejects",
        "rejected",
        "rejecting",
        "replace",
        "replaces",
        "replaced",
        "replacing",
        "don't",
        "doesn't",
        "didn't",
        "won't",
        "can't",
        "cannot",
        "isn't",
        "aren't",
        "wasn't",
        "weren't",
        "shouldn't",
        "wouldn't",
        "couldn't",
    }
)
_NEGATION_PHRASES = (("rule", "out"),)

_COMPLIANCE_PHRASES = (
    ("per", "the", "prohibition"),
    ("per", "the", "prohibitions"),
    ("according", "to", "the", "prohibition"),
    ("according", "to", "the", "prohibitions"),
    ("dead", "end"),
    ("derived", "from"),
    ("was", "abandoned"),
    ("was", "rolled", "back"),
    ("was", "removed"),
)

_SENTENCE_SPLIT = re.compile(r"[.!?]+")
_BULLET_MARKER = re.compile(r"^\s*(?:[-*+\u2022>]|\d+[.)]|#{1,6})\s+")
_TOKEN_RE = re.compile(r"[a-z0-9'\u2019]+", re.IGNORECASE)
_CLAUSE_PUNCT = re.compile(r"[,;:]")
_CONJUNCTIONS = frozenset({"and", "but", "or"})
_COPULAS = re.compile(
    r"(?<![a-z0-9])(?:is|are|was|were|has|have|had|can|could|would|might|will|should"
    r"|must|may|do|does|did|be)(?![a-z0-9])"
)


def _segments(text: str) -> list[tuple[str, bool]]:
    """Sentences and lines; bullet/heading markers are stripped and tagged as bullet context."""
    segments: list[tuple[str, bool]] = []
    for line in text.split("\n"):
        rest = line
        bullet = False
        while True:
            marker = _BULLET_MARKER.match(rest)
            if marker is None:
                break
            bullet = True
            rest = rest[marker.end() :]
        for part in _SENTENCE_SPLIT.split(rest):
            if part.strip():
                segments.append((part, bullet))
    return segments


def _tokenize(segment: str) -> tuple[list[str], list[int], list[int]]:
    words: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    for match in _TOKEN_RE.finditer(segment):
        words.append(match.group().lower().replace("\u2019", "'"))
        starts.append(match.start())
        ends.append(match.end())
    return words, starts, ends


def _clause_spans(words: list[str], starts: list[int], segment: str) -> list[tuple[int, int]]:
    """Token-index spans of clauses: cut at ``,``/``;``/``:`` and at and/but/or."""
    boundaries = {0, len(words)}
    for match in _CLAUSE_PUNCT.finditer(segment):
        boundaries.add(bisect_right(starts, match.start()))
    for index, word in enumerate(words):
        if word in _CONJUNCTIONS:
            boundaries.add(index)
            boundaries.add(index + 1)
    ordered = sorted(boundaries)
    return [(lo, hi) for lo, hi in zip(ordered, ordered[1:], strict=False) if hi > lo]


def _clause_of(spans: list[tuple[int, int]], index: int) -> tuple[int, int]:
    for lo, hi in spans:
        if lo <= index < hi:
            return lo, hi
    return 0, index + 1  # unreachable: spans tile every token index


def _has_cue(
    tokens: list[str], words: frozenset[str], phrases: tuple[tuple[str, ...], ...]
) -> bool:
    if any(token in words for token in tokens):
        return True
    for phrase in phrases:
        size = len(phrase)
        for index in range(len(tokens) - size + 1):
            if tuple(tokens[index : index + size]) == phrase:
                return True
    return False


def _heading_style(segment: str, char_end: int) -> bool:
    """A `:`/`—`/`–` follows the leading tool before the first copula ("**Redis**: ...")."""
    rest = segment[char_end : char_end + 32]
    positions = [rest.find(char) for char in ":\u2014\u2013" if char in rest]
    if not positions:
        return False
    copula = _COPULAS.search(rest)
    return min(positions) < (copula.start() if copula else len(rest))


def _mention_indices(words: list[str], term_words: list[str]) -> list[int]:
    size = len(term_words)
    return [
        index for index in range(len(words) - size + 1) if words[index : index + size] == term_words
    ]


def _recommendation(
    words: list[str],
    segment: str,
    clause: tuple[int, int],
    index: int,
    char_end: int,
    bullet: bool,
) -> str | None:
    if bullet and index == 0:
        return "bullet-initial mention"
    if index == 0 and _heading_style(segment, char_end):
        return "heading-style mention"
    lo, _ = clause
    for cursor in range(lo, index):
        if words[cursor] in _RECOMMEND_WORDS:
            return f"recommendation verb '{words[cursor]}'"
    before = words[lo:index]
    pairs = {(before[i], before[i + 1]) for i in range(len(before) - 1)}
    for modal in sorted(_MODAL_BE):
        if (modal, "be") in pairs:
            return f"modal '{modal} be'"
    for phrase in _RECOMMEND_PHRASES:
        if phrase in pairs:
            return f"phrase '{phrase[0]} {phrase[1]}'"
    return None


def score_v3(response: str, bad_terms: Sequence[str]) -> ScoreResult:
    """Apply the pre-registered v3 rule; the same function is used for every arm."""
    evidence: list[str] = []
    terms = [(term.strip().lower().split(), term.strip()) for term in bad_terms if term.strip()]
    for segment, bullet in _segments(response):
        words, starts, ends = _tokenize(segment)
        if not words:
            continue
        spans = _clause_spans(words, starts, segment)
        for term_words, term in terms:
            for index in _mention_indices(words, term_words):
                clause = _clause_of(spans, index)
                char_end = ends[index + len(term_words) - 1]
                reason = _recommendation(words, segment, clause, index, char_end, bullet)
                if reason is None:
                    continue
                tokens_in_clause = words[clause[0] : clause[1]]
                if _has_cue(tokens_in_clause, _NEGATION_WORDS, _NEGATION_PHRASES):
                    continue
                if _has_cue(tokens_in_clause, frozenset(), _COMPLIANCE_PHRASES):
                    continue
                item = f"{term}: {reason}"
                if item not in evidence:
                    evidence.append(item)
    return ScoreResult(bad=bool(evidence), evidence=evidence)


def score_v1(response: str, bad_terms: Sequence[str]) -> ScoreResult:
    """Thin wrapper over the frozen v1 substring rule (:func:`is_bad_answer`)."""
    hits = is_bad_answer(response, bad_terms)
    return ScoreResult(bad=bool(hits), evidence=list(hits))


def score_v2(response: str, bad_terms: Sequence[str]) -> ScoreResult:
    """Thin wrapper over the frozen v2 8-token-window rule (:func:`is_bad_answer_v2`)."""
    hits = is_bad_answer_v2(response, bad_terms)
    return ScoreResult(bad=bool(hits), evidence=list(hits))
