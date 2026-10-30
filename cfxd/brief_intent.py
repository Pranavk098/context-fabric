"""Brief v1 intent extraction (ADR-0011, WP B3): goal, constraints, decisions, already-tried.

Text comes from exactly three places: the parent transcript's user prompts, the parent's own
assistant text, and the delegation prompt. It never comes from a ``tool_result`` or ``tool_use``
block, ``thinking``, sidechain / meta / compact-summary records, ``<system-reminder>`` and similar
tagged blocks, fenced code or ``>`` quote lines. Extraction is rule-based (sentence patterns); no
model is called. Every item is sanitised with Brief v0's rules, capped, and labelled by source.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any

from cfxd.brief import _cap, _sanitize
from cfxd.brief_types import Intent, IntentItem
from cfxd.session_state import SessionFacts

INTENT_ITEM_MAX_BYTES = 200
GOAL_MIN_BYTES = 40
MAX_PER_SECTION = 20
_MAX_SENTENCES_PER_RECORD = 40
# Share of each section's cap reserved for the opening user turn(s) (ordering amendment).
RESERVED_PER_SECTION = -(-MAX_PER_SECTION // 3)
_STRONG = 3
_TOKEN = re.compile(r"[\w][\w./\-]*")
_STOPWORDS = frozenset(
    "this that with from have will would should must never always only avoid required "
    "does done into when then than them they their there what which while where about "
    "after before also been being both each other some such very your ours".split()
)

_CONSTRAINT = re.compile(
    r"\b(?:must|must not|do not|don't|never|always|only|avoid|required|should not|shouldn't)\b"
)
_CONSTRAINT_START = re.compile(r"^(?:please\s+)?(?:use|name|call|keep|leave|put|prefer|rename)\b")
_DECISION = re.compile(
    r"\b(?:we'll|we will|i'll|i will|decided|we chose|going with|agreed|let's|instead of)\b"
    r"|\bdecision:"
)
_TRIED = re.compile(
    r"\b(?:tried|attempted|didn't work|did not work|doesn't work|failed|reverted|rolled back"
    r"|dead end)\b"
)
_SECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("constraints", _CONSTRAINT),
    ("decisions", _DECISION),
    ("tried", _TRIED),
)

_TAGGED_BLOCK = re.compile(
    r"<\s*(system-reminder|command-[\w-]+|local-command-[\w-]+|bash-(?:input|stdout|stderr)"
    r"|context-fabric-[\w-]*)\b[^>]*>.*?(?:<\s*/\s*\1\s*>|\Z)",
    re.DOTALL | re.IGNORECASE,
)
_MARKER = re.compile(r"<[^a-z]*context[^a-z]*fabric")
_BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+")
_SENTENCE_SPLIT = re.compile(r"(?<=[.?!])\s+")
_WS = re.compile(r"\s+")


def _normalise(text: str) -> str:
    """NFKC, straight quotes, and line breaks for the Unicode separators."""
    out = unicodedata.normalize("NFKC", text)
    out = out.replace("’", "'").replace("‘", "'")
    return out.replace(" ", "\n").replace(" ", "\n").replace("\r", "\n")


def _strip_blocks(text: str) -> str:
    for _ in range(3):
        stripped = _TAGGED_BLOCK.sub(" ", text)
        if stripped == text:
            break
        text = stripped
    return text


def _prose_lines(text: str) -> list[str]:
    """Lines outside fenced code and quote lines, after removing tagged blocks."""
    lines: list[str] = []
    fenced = False
    for line in _strip_blocks(_normalise(text)).split("\n"):
        head = line.lstrip()
        if head.startswith("```") or head.startswith("~~~"):
            # An unclosed fence swallows the rest of the text.
            fenced = not fenced
            continue
        if fenced or head.startswith(">"):
            continue
        lines.append(line)
    return lines


def _sentences(text: str) -> list[str]:
    out: list[str] = []
    for line in _prose_lines(text):
        line = _BULLET.sub("", line).strip()
        for part in _SENTENCE_SPLIT.split(line):
            part = part.strip()
            if len(part) >= 8:
                out.append(part)
    return out[:_MAX_SENTENCES_PER_RECORD]


def _section_of(sentence: str) -> str | None:
    lowered = sentence.casefold()
    if _CONSTRAINT.search(lowered) or _CONSTRAINT_START.match(lowered):
        return "constraints"
    for name, pattern in _SECTION_PATTERNS[1:]:
        if pattern.search(lowered):
            return name
    return None


def _fold_key(text: str) -> str:
    folded = _WS.sub(" ", text.casefold().replace("`", "").replace("*", "")).strip()
    return folded.rstrip(" .!?;:")


def _looks_like_marker(text: str) -> bool:
    folded = unicodedata.normalize("NFKC", text).casefold()
    compact = "".join(ch for ch in folded if unicodedata.category(ch)[0] not in ("Z", "C"))
    return _MARKER.search(compact) is not None


def _tokens(text: str) -> dict[str, int]:
    """Content words of ``text`` mapped to a weight: 3 for identifier-like, 1 for plain words."""
    out: dict[str, int] = {}
    for raw in _TOKEN.findall(unicodedata.normalize("NFKC", text)):
        word = raw.strip("./-_")
        low = word.casefold()
        if len(low) < 4 or low in _STOPWORDS:
            continue
        identifier = any(ch in word for ch in "._/\\") or any(ch.isdigit() for ch in word)
        identifier = identifier or (
            word != low and word != word.capitalize() and word != word.upper()
        )
        out[low] = max(out.get(low, 0), _STRONG if identifier else 1)
    return out


_Cand = tuple[str, str, int, int]  # (sentence, source, turn, arrival order)


def _select(cands: list[_Cand], task: dict[str, int]) -> list[_Cand]:
    """Pick up to ``MAX_PER_SECTION`` candidates (given newest-first), in rendering order.

    Order: (1) items sharing an identifier (file name, symbol) with the delegation prompt, best
    first; (2) the reserved share of the opening user turn(s), chosen oldest first and shown
    newest-first; (3) the rest, items sharing plain words first, then newest-first. The sort is
    stable, so ties stay newest-first.
    """
    scored: list[tuple[int, bool, _Cand]] = []
    for cand in cands:
        shared = [(w, task[t]) for t, w in _tokens(cand[0]).items() if t in task]
        scored.append(
            (sum(max(pair) for pair in shared), any(max(p) >= _STRONG for p in shared), cand)
        )
    ranked = sorted(scored, key=lambda t: -t[0])
    early = sorted((c for _, _, c in scored if c[1] == "user"), key=lambda c: (c[2], c[3]))
    early = early[:RESERVED_PER_SECTION]
    strong = [c for _, is_strong, c in ranked if is_strong]
    strong_ids = {id(c) for c in strong}
    early = [c for c in early if id(c) not in strong_ids]
    early.sort(key=lambda c: (-c[2], c[3]))  # shown newest-first, as before
    picked = strong[: MAX_PER_SECTION - len(early)] + early
    chosen = {id(c) for c in picked}
    picked += [c for _, _, c in ranked if id(c) not in chosen]
    return picked[:MAX_PER_SECTION]


def _make_item(sentence: str, source: str, turn: int) -> IntentItem | None:
    if _looks_like_marker(sentence):
        return None
    text = _cap(_sanitize(sentence), INTENT_ITEM_MAX_BYTES).strip()
    if not text:
        return None
    return IntentItem(text=text, source=source, turn=turn)


def _record_text(obj: dict[str, Any], kind: str) -> str | None:
    """The allowed text of one transcript record, or ``None`` when it is not a source."""
    if obj.get("type") != kind:
        return None
    if obj.get("isSidechain") is True or obj.get("isMeta") is True:
        return None
    if obj.get("isCompactSummary") or obj.get("isApiErrorMessage") or "toolUseResult" in obj:
        return None
    message = obj.get("message")
    if not isinstance(message, dict) or message.get("role") != kind:
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts = [
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block["text"], str)
    ]
    return "\n".join(parts) if parts else None


def _after_last_boundary(records: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
    start = 0
    for index, obj in enumerate(records):
        if obj.get("subtype") == "compact_boundary":
            start = index + 1
    return list(enumerate(records))[start:]


def _read_records(transcript_path: str | None) -> list[dict[str, Any]]:
    if not isinstance(transcript_path, str) or not transcript_path:
        return []
    try:
        from cfxd.checkpoint import _iter_tail

        return list(_iter_tail(Path(transcript_path)))
    except Exception:
        return []


def delegation_items(prompt: str) -> int:
    """How many constraint, decision or already-tried sentences the delegation prompt holds."""
    if not isinstance(prompt, str):
        return 0
    return sum(1 for s in _sentences(prompt) if _section_of(s) is not None)


def extract_intent(transcript_path: str | None, prompt: str, facts: SessionFacts | None) -> Intent:
    """Goal, constraints, decisions and already-tried from the allowed sources only."""
    delegation = _fold_key(_normalise(prompt)) if isinstance(prompt, str) else ""
    seen: set[str] = set()
    deduped = 0
    cands: dict[str, list[_Cand]] = {"constraints": [], "decisions": [], "tried": []}

    def add(name: str, sentence: str, source: str, turn: int) -> None:
        nonlocal deduped
        key = _fold_key(sentence)
        if not key or key in seen:
            return
        if key in delegation:
            deduped += 1
            return
        if _make_item(sentence, source, turn) is None:
            return
        seen.add(key)
        cands[name].append((sentence, source, turn, len(cands[name])))

    records = _after_last_boundary(_read_records(transcript_path))
    users = [
        (turn, text)
        for turn, obj in reversed(records)
        if (text := _record_text(obj, "user")) is not None
    ]
    assistants = [
        (turn, text)
        for turn, obj in reversed(records)
        if (text := _record_text(obj, "assistant")) is not None
    ]

    for source, group in (("user", users), ("assistant", assistants)):
        for turn, text in group:
            for sentence in _sentences(text):
                name = _section_of(sentence)
                if name is not None:
                    add(name, sentence, source, turn)

    task = _tokens(prompt) if isinstance(prompt, str) else {}
    sections: dict[str, list[IntentItem]] = {}
    for name, found in cands.items():
        sections[name] = [
            item
            for sentence, source, turn, _ in _select(found, task)
            if (item := _make_item(sentence, source, turn)) is not None
        ]

    if facts is not None and facts.test is not None and facts.test.ok is False:
        key = _cap(_sanitize(facts.test.key), 80).strip()
        if key:
            line = IntentItem(text=f"Last test run: {key}, failed", source="assistant", turn=-1)
            sections["tried"].insert(0, line)

    # Goal/constraint dedup rule (ADR-0011, B4 note): a sentence shown in constraints, decisions
    # or tried is never repeated in the goal. The goal is built from the newest user prompt that
    # has goal prose left, keeping only sentences that are not constraint/decision/tried-shaped and
    # whose normalised text is neither equal to, contained in, nor containing a kept item.
    kept_keys = [_fold_key(i.text) for name in sections for i in sections[name]]
    goal = None
    for turn, text in users:
        sentences = _sentences(text)
        if _goal_text(sentences) is None:
            continue
        rest: list[str] = []
        for sentence in sentences:
            key = _fold_key(sentence)
            if any(k and (key in k or k in key) for k in kept_keys):
                deduped += 1
            elif _section_of(sentence) is None:
                rest.append(sentence)
        goal_text = _goal_text(rest, 1)
        if goal_text is not None:
            if _fold_key(goal_text) in delegation:
                deduped += 1
            else:
                goal = _make_item(goal_text, "user", turn)
            break

    return Intent(
        goal=goal,
        constraints=tuple(sections["constraints"]),
        decisions=tuple(sections["decisions"]),
        tried=tuple(sections["tried"]),
        deduped=deduped,
    )


def _goal_text(sentences: list[str], min_bytes: int = GOAL_MIN_BYTES) -> str | None:
    """The first sentences of a user prompt, if at least 40 bytes of prose survive stripping."""
    joined = " ".join(sentences).strip()
    if not joined or len(joined.encode("utf-8")) < min_bytes:
        return None
    out: list[str] = []
    size = 0
    for sentence in sentences:
        cost = len(sentence.encode("utf-8")) + 1
        if out and size + cost > INTENT_ITEM_MAX_BYTES:
            break
        out.append(sentence)
        size += cost
    return " ".join(out)
