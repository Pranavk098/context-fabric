"""Content-aware observation folding (``obsfold_v2``) tests — keeps failing-test lines,
traceback frames and diff hunks; reversible stubs; deterministic; ``obsfold`` untouched."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kernel.store import SqliteStore
from protocol.canonical import canonicalize
from sidecar import obsfold
from sidecar.obsfold_v2 import fold_content, fold_messages

AS_OF = datetime(2026, 1, 1, tzinfo=UTC)

PYTEST_OUT = (
    "============================= test session starts ==============================\n"
    "collected 3 items\n\n"
    "test_units.py ...\n\n"
    "=================================== FAILURES ===================================\n"
    "___________________________________ test_bad ___________________________________\n\n"
    "    def test_bad():\n"
    "        assert add(1, 2) == 4\n"
    ">       assert add(1, 2) == 4\n"
    "E       assert 3 == 4\n"
    "E        +  where 3 = add(1, 2)\n\n"
    "test_units.py:42: AssertionError\n"
    "=========================== short test summary info ============================\n"
    "FAILED test_units.py::test_bad - AssertionError: assert 3 == 4\n"
    "========================= 1 failed, 2 passed in 0.10s ==========================\n"
)

DIFF = (
    "diff --git a/mod.py b/mod.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/mod.py\n"
    "+++ b/mod.py\n"
    "@@ -10,6 +10,7 @@ def foo():\n"
    " context line one\n"
    "-    old = compute()\n"
    "+    new = compute(cached=True)\n"
    " context line two\n"
    "@@ -30,4 +30,4 @@ def bar():\n"
    " context line three\n"
    "-    return old\n"
    "+    return new\n"
)


def _fold(content: str) -> tuple[str, str]:
    return fold_content(content)


def _message(content: str, *, name: str = "bash", index: int = 0) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": f"call_{index}", "name": name, "content": content}


def _session(content: str) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": "you are a coding sub-agent"},
        {"role": "user", "content": "fix the bug"},
        {"role": "assistant", "content": None},
        _message(content),
    ]


# -------------------------------------------------------------- test output folding


def test_test_output_keeps_failing_name_assertion_and_traceback() -> None:
    kept, removed = _fold(PYTEST_OUT)
    assert "FAILED test_units.py::test_bad" in kept  # failing test name
    assert "assert 3 == 4" in kept  # assertion line
    assert "AssertionError" in kept  # exception line
    assert "test_units.py:42: AssertionError" in kept  # last traceback frame
    assert removed  # the passing-session preamble is folded away


def test_test_output_removed_is_stubbed_and_reversible(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "obs.db")
    result = fold_messages(_session(PYTEST_OUT), n=0, k=1, store=store, created_at=AS_OF)
    assert result.folded == 1
    folded = result.messages[3]["content"]
    assert folded.startswith("[ctx://obs/") and "FAILED test_units.py::test_bad" in folded
    uri = folded[1 : folded.index(" folded:")]
    expanded = obsfold.expand(store, uri)
    assert expanded is not None and "collected 3 items" in expanded  # removed preamble stored


# -------------------------------------------------------------------- diff folding


def test_diff_keeps_hunks_and_changed_lines_drops_context() -> None:
    kept, removed = _fold(DIFF)
    assert "@@ -10,6 +10,7 @@ def foo():" in kept
    assert "@@ -30,4 +30,4 @@ def bar():" in kept
    assert "+    new = compute(cached=True)" in kept
    assert "-    old = compute()" in kept
    assert " context line one" not in kept
    assert " context line one" in removed


# ------------------------------------------------------------------- file folding


def test_file_keeps_head_tail_and_identifier_lines() -> None:
    body = [f"line {i}" for i in range(60)]
    body[30] = "def compute_cached():"
    content = "\n".join(body)
    kept, removed = fold_content(content, task_identifiers=("compute_cached",))
    assert "line 0" in kept  # head
    assert "line 59" in kept  # tail
    assert "def compute_cached():" in kept  # identifier line (middle)
    assert "line 29" not in kept  # middle non-identifier folded away
    assert "line 29" in removed


def test_file_without_identifier_folds_whole_like_obsfold() -> None:
    # low-value read: no task symbol -> fold the whole content, as aggressive as obsfold
    content = "\n".join(f"line {i}" for i in range(60))
    kept, removed = fold_content(content, task_identifiers=("no_such_symbol",))
    assert kept == ""
    assert removed != ""


def test_fold_messages_stable_prefix_k3() -> None:
    # the k=3 batching keeps the leading bytes append-only between fold events
    def messages(count: int) -> list[dict[str, Any]]:
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

    render = lambda msgs: b"\n".join(canonicalize(m) for m in msgs)  # noqa: E731
    r6 = render(fold_messages(messages(6)).messages)
    r7 = render(fold_messages(messages(7)).messages)
    r8 = render(fold_messages(messages(8)).messages)
    assert r7.startswith(r6) and r8.startswith(r7)
    assert fold_messages(messages(6)).folded == 3
    assert fold_messages(messages(7)).folded == 3
    assert fold_messages(messages(8)).folded == 3
    assert fold_messages(messages(9)).folded == 6  # boundary advances every k=3


# ------------------------------------------------------- "other" -> reversible stub


def test_other_folds_whole_content_to_reversible_stub(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "obs.db")
    content = "some short command output that is neither a test nor a diff"
    result = fold_messages(_session(content), n=0, k=1, store=store, created_at=AS_OF)
    assert result.folded == 1
    folded = result.messages[3]["content"]
    assert folded.startswith("[ctx://obs/")
    assert folded == obsfold.make_stub(content, "bash")[0]  # byte-identical to obsfold stub
    uri = folded[1 : folded.index(" folded:")]
    assert obsfold.expand(store, uri) == content


# ------------------------------------------------------------------- determinism


def test_fold_content_deterministic() -> None:
    assert _fold(PYTEST_OUT) == _fold(PYTEST_OUT)
    assert _fold(DIFF) == _fold(DIFF)


def test_fold_messages_deterministic() -> None:
    messages = _session(PYTEST_OUT) + [_message("trailing output", index=1)]
    first = fold_messages(messages, n=0, k=1)
    second = fold_messages(messages, n=0, k=1)
    assert first.messages == second.messages
    assert first.events == second.events
    assert first.seen_hash == second.seen_hash
