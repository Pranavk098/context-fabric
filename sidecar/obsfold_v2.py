"""Content-aware observation folding — ``obsfold_v2`` (offline lab, not wired into the sidecar).

A variant of :mod:`sidecar.obsfold` that, instead of replacing an older tool observation
with a whole-content ``ctx://obs/`` stub, keeps the parts a coding agent still needs and
folds only the removed remainder into a *reversible* stub:

* **test output** — keep failing-test names, assertion lines (``assert`` / pytest ``E ``)
  and the last traceback frames;
* **diffs** — keep ``diff --git`` / hunk headers (``@@``) and the changed (``+``/``-``)
  lines, dropping unchanged context lines;
* **long file reads** — keep head + tail plus any line matching a task identifier; a read
  with *no* task-identifier match is low-value and folds whole, exactly like ``obsfold``;
* **anything else** — fold the whole content into the same reversible ``ctx://obs/`` stub
  as :mod:`sidecar.obsfold`.

The mask/chunk scheduling (keep the last ``n`` observations verbatim, advance the fold
boundary every ``k`` new observations) is identical to ``obsfold``; only the *action* taken
on a folded message differs. Stubs are content-addressed and stored with
``block_id = obs_<id>`` exactly as in ``obsfold``, so ``obsfold.expand`` reverses them.

Deterministic and model-free: classification and line selection are pure functions of the
content and the task identifiers. Partial folds are content-preserving but position-lossy:
the kept lines stay verbatim and in order, while the removed remainder is collapsed to one
stub (for the whole-content "other" case, reversibility is exact).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from kernel.cartridge.serializer import count_tokens
from kernel.store import SqliteStore
from protocol.canonical import canonicalize, sha256_hex
from protocol.models import BlockKind, ContextBlock, Creator, CreatorType, Fidelity, MemoryTier
from sidecar.obsfold import (
    DEFAULT_K,
    DEFAULT_N,
    FoldEvent,
    FoldResult,
    _foldable_indices,
    _tool_name,
    make_stub,
)

_FILE_MIN_LINES = 40  # a read longer than this is classified "file"
_FILE_HEAD = 5  # leading lines kept for a file read (context), not the whole header
_FILE_TAIL = 5  # trailing lines kept for a file read
_TRACEBACK_TAIL = 15

_PATH_RE = re.compile(r"[\w./-]+\.py")
_IDENTIFIER_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")


def extract_identifiers(text: str) -> tuple[str, ...]:
    """Code-like tokens from the task: ``.py`` paths and CamelCase/snake_case words."""
    tokens: set[str] = set(_PATH_RE.findall(text))
    tokens.update(
        token
        for token in _IDENTIFIER_RE.findall(text)
        if "_" in token or (len(token) >= 3 and any(char.isupper() for char in token[1:]))
    )
    return tuple(sorted(tokens))


def _classify(content: str) -> str:
    """Classify one observation as ``diff`` / ``test`` / ``file`` / ``other``."""
    lines = content.split("\n")
    has_hunk = any(line.startswith("@@") for line in lines)
    has_change = any(line[:1] in "+-" and not line.startswith(("+++", "---")) for line in lines)
    if has_hunk and has_change:
        return "diff"
    if any(
        marker in content
        for marker in (
            "= FAILURES =",
            "= ERRORS =",
            "short test summary info",
            "AssertionError",
            "Traceback (most recent call last)",
        )
    ) or any("::" in line and ("FAILED" in line or "ERROR" in line) for line in lines):
        return "test"
    if len(lines) > _FILE_MIN_LINES:
        return "file"
    return "other"


def _diff_keep(line: str) -> bool:
    if line.startswith("@@") or line.startswith("diff --git"):
        return True
    return line[:1] in "+-" and not line.startswith(("+++", "---"))


def _failure_header(stripped: str) -> bool:
    """Pytest ``____ test_name ____`` failure-section header."""
    return (
        stripped.startswith("_")
        and stripped.endswith("_")
        and "test" in stripped
        and len(stripped) >= 8
    )


def _test_kept(lines: Sequence[str]) -> set[int]:
    kept: set[int] = set()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if (
            "FAILED" in line
            or "ERROR" in line
            or stripped.startswith("E ")
            or line.startswith("assert")
            or "AssertionError" in line
            or "Error:" in line
            or _failure_header(stripped)
        ):
            kept.add(index)
    # the last traceback frames are the deepest + the exception line, at the tail
    tail_start = max(0, len(lines) - _TRACEBACK_TAIL)
    kept.update(range(tail_start, len(lines)))
    return kept


def _file_kept(lines: Sequence[str], task_identifiers: Sequence[str]) -> set[int]:
    total = len(lines)
    id_indices = {
        index
        for identifier in task_identifiers
        if identifier
        for index, line in enumerate(lines)
        if identifier in line
    }
    if not id_indices:
        # low-value read (no task symbol): fold the whole content, as aggressive as obsfold.
        return set()
    kept: set[int] = set(range(min(_FILE_HEAD, total)))
    kept.update(range(max(0, total - _FILE_TAIL), total))
    kept.update(id_indices)
    return kept


def fold_content(content: str, *, task_identifiers: Sequence[str] = ()) -> tuple[str, str]:
    """Fold one observation's content; return ``(kept, removed)``.

    ``removed == ""`` means nothing worth folding: the content is returned unchanged as
    ``kept``. Otherwise ``kept`` is the retained lines (verbatim, in order) and ``removed``
    is the folded-away remainder (the whole content for the "other" case).
    """
    kind = _classify(content)
    lines = content.split("\n")
    if kind == "other":
        return "", content
    if kind == "diff":
        kept_indices = {i for i, line in enumerate(lines) if _diff_keep(line)}
    elif kind == "test":
        kept_indices = _test_kept(lines)
    else:  # file
        kept_indices = _file_kept(lines, task_identifiers)
        if not kept_indices:
            # no task symbol -> low-value read, fold the whole content like obsfold
            return "", content
    removed_lines = [line for i, line in enumerate(lines) if i not in kept_indices]
    if not removed_lines:
        return content, ""
    kept_lines = [line for i, line in enumerate(lines) if i in kept_indices]
    return "\n".join(kept_lines), "\n".join(removed_lines)


def fold_messages(
    messages: Sequence[dict[str, Any]],
    *,
    n: int = DEFAULT_N,
    k: int = DEFAULT_K,
    task_identifiers: Sequence[str] = (),
    store: SqliteStore | None = None,
    session_id: str = "default",
    created_at: datetime | None = None,
) -> FoldResult:
    """Content-aware fold of older tool observations; return the folded list + receipt.

    The mask/chunk scheduling is identical to :func:`sidecar.obsfold.fold_messages` (keep
    the last ``n`` observations verbatim, advance the fold boundary every ``k``); the action
    on each folded message is :func:`fold_content`. Removed content is persisted (keyed by
    stub hash, ``block_id = obs_<id>``) so :func:`sidecar.obsfold.expand` reverses it.
    """
    if n < 0:
        raise ValueError("n must be >= 0")
    if k < 1:
        raise ValueError("k must be >= 1")

    foldable = _foldable_indices(messages)
    total = len(foldable)
    folded_by_mask = 0
    if total > n:
        folded_by_mask = ((total - n) // k) * k
    fold_positions: set[int] = set(foldable[:folded_by_mask])

    plans: dict[int, tuple[str, str]] = {}
    for index in foldable:
        content = messages[index]["content"]
        assert isinstance(content, str)
        plans[index] = fold_content(content, task_identifiers=task_identifiers)

    output = [dict(message) for message in messages]
    events: list[FoldEvent] = []
    blocks: list[ContextBlock] = []
    base_seq = store.max_source_event_seq() if store is not None else 0
    for index in sorted(fold_positions):
        kept, removed = plans[index]
        if not removed:
            continue
        tool_name = _tool_name(messages, index)
        stub, stub_id = make_stub(removed, tool_name)
        output[index]["content"] = stub + ("\n" + kept if kept else "")
        events.append(
            FoldEvent(
                message_index=index,
                stub_id=stub_id,
                content_hash="sha256:" + sha256_hex(removed.encode("utf-8")),
                tool_name=tool_name,
                content_bytes=len(removed.encode("utf-8")),
            )
        )
        blocks.append(
            ContextBlock(
                block_id="obs_" + stub_id,
                block_kind=BlockKind.observation,
                memory_tier=MemoryTier.register,
                content=removed,
                content_hash="sha256:" + sha256_hex(removed.encode("utf-8")),
                token_estimate=count_tokens(removed),
                source_event_seq=base_seq + len(blocks) + 1,
                creator=Creator(type=CreatorType.tool, id=tool_name),
                fidelity=Fidelity.raw,
                trust_domain="default",
            )
        )

    if store is not None and events:
        store.append_many(blocks)
        store.log_folds(
            session_id,
            [
                {
                    "message_index": event.message_index,
                    "stub_id": event.stub_id,
                    "content_hash": event.content_hash,
                    "tool_name": event.tool_name,
                    "content_bytes": event.content_bytes,
                }
                for event in events
            ],
            "sha256:" + sha256_hex(canonicalize(output)),
            created_at or datetime.now(tz=UTC),
        )

    return FoldResult(
        messages=output,
        folded=len(events),
        events=tuple(events),
        seen_hash="sha256:" + sha256_hex(canonicalize(output)),
    )


if __name__ == "__main__":
    # Self-check: test/diff/file folding keeps the important lines; "other" is reversible.
    pytest_out = "\n".join(f"collecting {i}" for i in range(20))
    pytest_out += "\n____ test_bad ____\n    assert 1 == 2\nE   assert 1 == 2\n"
    pytest_out += "FAILED tests/test_x.py::test_bad - AssertionError: assert 1 == 2"
    kept, removed = fold_content(pytest_out)
    assert "test_bad" in kept and "assert 1 == 2" in kept and removed != ""

    diff = "diff --git a/x.py b/x.py\n@@ -1,3 +1,3 @@\n ctx\n-a\n+b\n ctx2"
    kept, removed = fold_content(diff)
    assert "@@ -1,3 +1,3 @@" in kept and "+b" in kept and "-a" in kept
    assert " ctx\n" in removed

    long_file = "\n".join(f"line {i}" for i in range(60))
    kept, removed = fold_content(long_file, task_identifiers=("line 33",))
    assert "line 0" in kept and "line 59" in kept and "line 33" in kept and removed != ""

    # low-value file read (no task identifier) folds whole, like obsfold
    kept, removed = fold_content(long_file, task_identifiers=("no_such_symbol",))
    assert kept == "" and removed != ""

    stub, stub_id = make_stub("plain output", "bash")
    assert stub_id == sha256_hex(b"plain output")[:12]

    # stable prefix (k=3 batching): 6 -> 7 -> 8 obs is append-only leading bytes
    def _messages(count: int) -> list[dict[str, Any]]:
        msgs: list[dict[str, Any]] = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "fix compute_cached in mod.py"},
        ]
        for i in range(count):
            msgs.append({"role": "assistant", "content": f"call {i}"})
            msgs.append(
                {
                    "role": "tool",
                    "tool_call_id": f"call_{i}",
                    "name": "bash",
                    "content": f"plain output {i}",
                }
            )
        return msgs

    def _render(msgs: Sequence[dict[str, Any]]) -> bytes:
        return b"\n".join(canonicalize(m) for m in msgs)

    r6 = _render(fold_messages(_messages(6)).messages)
    r7 = _render(fold_messages(_messages(7)).messages)
    r8 = _render(fold_messages(_messages(8)).messages)
    assert r7.startswith(r6) and r8.startswith(r7)
    assert fold_messages(_messages(6)).folded == 3 and fold_messages(_messages(9)).folded == 6
    print("obsfold_v2.py self-check: OK (test/diff/file folding, low-value fold, stable prefix)")
