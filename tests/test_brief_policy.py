"""Brief policy (R-2): stay silent where a brief cannot pay for itself."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from cfxd import session_state as ss
from cfxd.brief import handle_pre_tool_use
from cfxd.cli import main
from cfxd.config import read_config, set_flag
from cfxd.handlers import clear


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    ss.reset()
    clear()
    yield
    ss.reset()
    clear()


def _agent(prompt: str, session_id: str = "s1", **tool_input: Any) -> dict[str, Any]:
    return {
        "tool_name": "Agent",
        "session_id": session_id,
        "tool_use_id": "tu-1",
        "tool_input": {"prompt": prompt, **tool_input},
    }


def _seed(session_id: str, *paths: str) -> None:
    for path in paths:
        ss.observe_post_tool_use(
            {
                "session_id": session_id,
                "tool_name": "Read",
                "tool_input": {"file_path": path},
                "tool_response": {"file": {"numLines": 40, "totalLines": 40}},
            }
        )


def _last(state: Path) -> dict[str, Any]:
    return json.loads((state / "briefs.jsonl").read_text(encoding="utf-8").splitlines()[-1])


def _packet(result: dict[str, Any]) -> str:
    return result["hookSpecificOutput"]["updatedInput"]["prompt"]


def test_haiku_is_skipped_even_with_knowledge(tmp_path: Path) -> None:
    _seed("s1", "/a/one.py", "/a/two.py", "/a/three.py")
    assert handle_pre_tool_use(_agent("P" * 300, model="haiku"), tmp_path) == {}
    receipt = _last(tmp_path)
    assert (receipt["decision"], receipt["skip_reason"]) == ("skipped", "policy_haiku")
    assert receipt["target_model"] == "haiku"


def test_fewer_than_three_known_files_is_skipped(tmp_path: Path) -> None:
    _seed("s1", "/a/one.py", "/a/two.py")
    assert handle_pre_tool_use(_agent("P" * 300, model="sonnet"), tmp_path) == {}
    receipt = _last(tmp_path)
    assert (receipt["decision"], receipt["skip_reason"]) == ("skipped", "policy_low_knowledge")


def test_no_facts_is_low_knowledge(tmp_path: Path) -> None:
    assert handle_pre_tool_use(_agent("P" * 300, model="sonnet"), tmp_path) == {}
    assert _last(tmp_path)["skip_reason"] == "policy_low_knowledge"


def test_three_known_files_still_briefs(tmp_path: Path) -> None:
    _seed("s1", "/a/one.py", "/a/two.py", "/a/three.py")
    out = handle_pre_tool_use(_agent("P" * 300, model="sonnet"), tmp_path)
    assert '<context-fabric-brief v="0">' in _packet(out)
    assert _last(tmp_path)["decision"] == "briefed"


def test_policy_off_leaves_v0_unchanged(tmp_path: Path) -> None:
    _seed("s1", "/a/one.py")
    set_flag(tmp_path, "brief_policy", False)
    out = handle_pre_tool_use(_agent("P" * 300, model="haiku"), tmp_path)
    assert '<context-fabric-brief v="0">' in _packet(out)
    assert _last(tmp_path)["decision"] == "briefed"


def test_policy_off_leaves_v1_unchanged(tmp_path: Path) -> None:
    _seed("s1", "/a/one.py")
    set_flag(tmp_path, "brief_v1", True)
    set_flag(tmp_path, "brief_policy", False)
    out = handle_pre_tool_use(_agent("P" * 300, model="haiku"), tmp_path)
    assert '<context-fabric-brief v="1" model="haiku">' in _packet(out)
    receipt = _last(tmp_path)
    assert (receipt["decision"], receipt["brief_v"], receipt["skip_reason"]) == ("briefed", 1, None)


def test_policy_is_on_by_default(tmp_path: Path) -> None:
    assert read_config(tmp_path)["brief_policy"] is True
    set_flag(tmp_path, "brief_policy", False)
    assert read_config(tmp_path)["brief_policy"] is False


def test_brief_cli_shows_policy_skip_reasons(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed("s1", "/a/one.py", "/a/two.py", "/a/three.py")
    handle_pre_tool_use(_agent("P" * 300, model="haiku", session_id="s1"), tmp_path)
    _seed("s2", "/a/one.py", "/a/two.py")
    handle_pre_tool_use(_agent("P" * 300, model="sonnet", session_id="s2"), tmp_path)

    root = tmp_path / "no-projects"
    assert main(["brief", "--root", str(root), "--state-dir", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert "skipped (policy_haiku)" in text
    assert "skipped (policy_low_knowledge)" in text
