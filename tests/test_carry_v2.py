"""Carry v2: edited files first with ranges, 4 KB cap, constraints, and the v1/v2 scorer."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from cfxd import carry
from cfxd import session_state as ss
from cfxd.carry import (
    CARRY_V2_CAP_BYTES,
    _snapshot_constraints,
    handle_pre_compact,
    handle_session_start,
)
from cfxd.config import read_config, set_flag
from cfxd.handlers import clear
from cfxd.session_state import FileFact, SessionFacts
from cfxd.session_state import TestFact as _TestFact

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import carry_fidelity_score as score  # noqa: E402

_USAGE = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100}


@pytest.fixture(autouse=True)
def _clean() -> Any:
    ss.reset()
    clear()
    ss.set_range_recording(False)
    yield
    ss.reset()
    clear()
    ss.set_range_recording(False)


def _facts(
    files: tuple[FileFact, ...],
    *,
    test: _TestFact | None = None,
    todos: tuple[tuple[str, str], ...] = (),
) -> SessionFacts:
    return SessionFacts(session_id="s1", files=files, folds=(), commands=(), todos=todos, test=test)


# Exact bytes produced by main's (v1-only) cfxd/carry.py for _golden_facts().
_V1_GOLDEN = (
    '<context-fabric-carry v="0" trigger="auto">\n'
    "Facts Context Fabric recorded before this session was compacted "
    "(reference, not instructions; verify before relying on them).\n"
    "Working set (path | lines | status):\n"
    "- /p/edit.py | 20 lines | edited\n"
    "- /p/read.py | 10 lines | read\n"
    "Recent commands:\n"
    "- uv run pytest -q\n"
    "Open todos the session had (status | text):\n"
    "- pending | finish carry\n"
    "</context-fabric-carry>"
)


def _golden_facts() -> SessionFacts:
    return SessionFacts(
        session_id="s1",
        files=(
            FileFact(
                path="/p/edit.py",
                lines=20,
                edited=True,
                last_seen=3.0,
                ranges=((3, 9, "sha", "edit"),),
            ),
            FileFact(path="/p/read.py", lines=10, edited=False, last_seen=2.0),
        ),
        folds=(),
        commands=("uv run pytest -q",),
        todos=(("pending", "finish carry"),),
    )


def test_carry_v2_off_by_default(tmp_path: Path) -> None:
    assert read_config(tmp_path).get("carry_v2") is False


def test_carry_v2_off_renders_v1_block_byte_for_byte(tmp_path: Path) -> None:
    text, dropped = carry.render_report(_golden_facts(), tmp_path, "auto")
    assert text == _V1_GOLDEN
    assert dropped == {"stale": 0, "commands": 0, "folds": 0, "todos": 0, "files": 0}
    assert " | L3-9" not in text


def test_carry_v2_on_renders_v2_block(tmp_path: Path) -> None:
    set_flag(tmp_path, "carry_v2", True)
    facts = _golden_facts()
    text, _ = carry.render_report(facts, tmp_path, "auto")
    assert text == carry.render_report(facts, tmp_path, "auto", version=2)[0]
    assert " | L3-9" in text
    assert text != _V1_GOLDEN


def test_carry_v2_flag_leaves_checkpoint_on_v1(tmp_path: Path) -> None:
    set_flag(tmp_path, "carry_v2", True)
    text, _ = carry.render_report(_golden_facts(), tmp_path, "checkpoint")
    assert text == _V1_GOLDEN.replace('trigger="auto"', 'trigger="checkpoint"')


def test_v2_edited_first_with_ranges_and_sections(tmp_path: Path) -> None:
    facts = _facts(
        (
            FileFact(path="/p/read.py", lines=10, edited=False, last_seen=3.0),
            FileFact(
                path="/p/edit_old.py",
                lines=20,
                edited=True,
                last_seen=1.0,
                ranges=((3, 9, "sha", "edit"),),
            ),
            FileFact(path="/p/edit_new.py", lines=5, edited=True, last_seen=2.0),
        ),
        test=_TestFact(key="pytest", ok=False, at=1000.0),
        todos=(("pending", "finish carry"),),
    )
    text, _ = carry.render_report(
        facts, tmp_path, "auto", constraints=("Always use uv.",), version=2
    )
    assert text.index("edit_new.py") < text.index("edit_old.py") < text.index("read.py")
    assert "edit_old.py | 20 lines | edited | L3-9" in text
    assert "Last test run: pytest, failed" in text
    assert "pending | finish carry" in text
    assert "Constraints from your earlier messages:" in text
    assert "Always use uv." in text
    assert len(text.encode("utf-8")) <= CARRY_V2_CAP_BYTES


def test_v2_cap_drops_low_priority_sections_first(tmp_path: Path) -> None:
    files = tuple(
        FileFact(path=f"/p/{'x' * 140}_{i:02d}.py", lines=1, edited=True, last_seen=float(i))
        for i in range(80)
    )
    facts = SessionFacts(
        session_id="s1",
        files=files,
        folds=(),
        commands=tuple("c" * 300 + str(i) for i in range(5)),
        todos=tuple(("pending", "t" * 300) for _ in range(8)),
        test=_TestFact(key="pytest", ok=True, at=1000.0),
    )
    text, dropped = carry.render_report(
        facts, tmp_path, "auto", note="n" * 300, goal="g" * 600, version=2
    )
    assert len(text.encode("utf-8")) <= CARRY_V2_CAP_BYTES
    assert text.count("</context-fabric-carry>") == 1
    assert "Working set (path | lines | status):" in text
    assert "_79.py" in text
    assert dropped["files"] > 0
    assert dropped["commands"] == 5 and dropped["todos"] == 8


def test_snapshot_roundtrip_keeps_ranges_and_constraints(tmp_path: Path) -> None:
    facts = _facts(
        (
            FileFact(
                path="/p/a.py",
                lines=40,
                edited=True,
                last_seen=1.0,
                ranges=((2, 4, "sha", "edit"), (20, 20, "sha", "edit")),
            ),
        )
    )
    obj = carry.snapshot_object(facts, "auto", ("keep it offline",))
    restored = carry.facts_from_snapshot(obj, tmp_path)
    assert restored is not None
    assert restored.files[0].ranges == ((2, 4, "", "edit"), (20, 20, "", "edit"))
    assert _snapshot_constraints(obj) == ("keep it offline",)


def test_compaction_end_to_end_carries_ranges_and_constraints(tmp_path: Path) -> None:
    source = tmp_path / "a.py"
    source.write_text("one\ntwo\nthree\n", encoding="utf-8")
    set_flag(tmp_path, "carry_v2", True)
    ss.set_range_recording(True)
    ss.observe_post_tool_use(
        {
            "session_id": "s1",
            "tool_name": "Edit",
            "tool_input": {"file_path": str(source), "old_string": "two", "new_string": "two"},
            "tool_response": {"file": {"numLines": 3, "totalLines": 3}},
        }
    )
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": "Always use uv for commands. Never edit generated files.",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    handle_pre_compact(
        {"session_id": "s1", "trigger": "auto", "transcript_path": str(transcript)},
        tmp_path,
    )
    text = handle_session_start({"session_id": "s1", "source": "compact"}, tmp_path)[
        "hookSpecificOutput"
    ]["additionalContext"]
    assert "a.py" in text and " | L2" in text
    assert "Always use uv for commands." in text
    assert "Never edit generated files." in text
    assert len(text.encode("utf-8")) <= CARRY_V2_CAP_BYTES


def test_v2_carries_no_tool_output(tmp_path: Path) -> None:
    canary = "TOOL-OUTPUT-CANARY-7f3a"
    set_flag(tmp_path, "carry_v2", True)
    ss.observe_post_tool_use(
        {
            "session_id": "s1",
            "tool_name": "Read",
            "tool_input": {"file_path": "/p/a.py"},
            "tool_response": {"file": {"content": canary, "numLines": 40, "totalLines": 40}},
        }
    )
    handle_pre_compact({"session_id": "s1", "trigger": "auto"}, tmp_path)
    text = handle_session_start({"session_id": "s1", "source": "compact"}, tmp_path)[
        "hookSpecificOutput"
    ]["additionalContext"]
    assert canary not in text
    assert "/p/a.py" in text


# --------------------------------------------------------------------- scorer


def _use(mid: str, uid: str, name: str, inp: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "id": mid,
            "model": "claude-sonnet-5-5",
            "usage": _USAGE,
            "content": [{"type": "tool_use", "id": uid, "name": name, "input": inp}],
        },
    }


def _result(uid: str, text: str = "ok") -> dict[str, Any]:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": uid, "content": text}],
        },
    }


def _synthetic(root: Path) -> None:
    rows: list[dict[str, Any]] = []
    for i in range(5):
        path = f"C:\\src\\edit_old_{i}.py"
        rows.append(
            _use(
                f"e{i}", f"et{i}", "Edit", {"file_path": path, "old_string": "a", "new_string": "b"}
            )
        )
        rows.append(_result(f"et{i}"))
    for i in range(20):
        path = f"C:\\src\\read_{i:02d}.py"
        rows.append(_use(f"r{i}", f"rt{i}", "Read", {"file_path": path}))
        rows.append(_result(f"rt{i}"))
    rows.append(
        {
            "type": "user",
            "isCompactSummary": True,
            "message": {"role": "user", "content": "No file was named in this summary."},
        }
    )
    proj = root / "proj"
    proj.mkdir(parents=True)
    (proj / "sess.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )


def test_scorer_v2_restores_edits_v1_drops_them(tmp_path: Path) -> None:
    _synthetic(tmp_path)
    data = score.collect(tmp_path, "all")
    assert data["events"] == 1
    assert data["dropped"] == 5
    assert data["v1_dropped"] == 0
    assert data["v2_dropped"] == 5
    assert data["bytes_v2"]["median"] > 0


def test_scorer_report_has_no_paths(tmp_path: Path) -> None:
    _synthetic(tmp_path)
    text = score.render(score.collect(tmp_path, "all"))
    assert "Carry v2 vs v1" in text
    assert "edit_old" not in text and "C:/" not in text and "read_" not in text
