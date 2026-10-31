"""Coach A/B control: deterministic arm, balanced split, silent withheld arm, counts-only."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from cfxd import coach
from cfxd.config import set_flag
from cfxd.handlers import clear

EXACT_182K_OPUS = (
    "Context Fabric: this session is at ~182K tokens; each turn now re-reads it "
    "(about $0.04 at Opus 5.5 cache-read prices, API-equivalent). If this is a natural "
    "break: /context-fabric:checkpoint, then /clear. Turn this off: cfxd set coach off"
)


@pytest.fixture(autouse=True)
def _clean() -> Any:
    clear()
    coach._STATE.clear()
    yield
    clear()
    coach._STATE.clear()


def _on(tmp_path: Path, control: bool = True) -> None:
    set_flag(tmp_path, "coach", True)
    set_flag(tmp_path, "coach_control", control)


def _usage(context: int, model: str = "claude-opus-5-5") -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "model": model,
                "usage": {
                    "input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": context,
                },
            },
        }
    )


def _transcript(tmp_path: Path, context: int = 150_000) -> Path:
    path = tmp_path / "session.jsonl"
    path.write_text(_usage(context) + "\n", encoding="utf-8")
    return path


def _payload(path: Path, session: str) -> dict[str, Any]:
    return {"session_id": session, "transcript_path": str(path)}


def _receipts(tmp_path: Path) -> list[dict[str, Any]]:
    text = (tmp_path / coach.COACH_LOG).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def _session_for(arm: str) -> str:
    for i in range(10_000):
        session = f"synthetic-{i}"
        if coach._arm(session) == arm:
            return session
    raise AssertionError(f"no session found for arm {arm}")


def _run(path: Path, session: str, state_dir: Path, turns: int = 30) -> list[dict[str, Any]]:
    return [coach.handle_stop(_payload(path, session), state_dir) for _ in range(turns)]


def test_arm_is_deterministic_per_session_id() -> None:
    for session in ("s1", "synthetic-7", "CANARY-1a2b3c"):
        expected = "warned" if sha256(session.encode("utf-8")).digest()[0] % 2 == 0 else "control"
        assert coach._arm(session) == expected
        assert coach._arm(session) == expected


def test_arm_split_over_200_synthetic_ids_is_roughly_balanced() -> None:
    warned = sum(coach._arm(f"synthetic-{i}") == "warned" for i in range(200))
    assert 80 <= warned <= 120


def test_control_arm_withholds_and_still_logs(tmp_path: Path) -> None:
    _on(tmp_path)
    session = _session_for("control")
    path = _transcript(tmp_path)
    assert _run(path, session, tmp_path) == [{}] * 30
    receipts = _receipts(tmp_path)
    trigger = [r for r in receipts if r["event"] == "coach_trigger"]
    assert trigger == [
        {
            "ts": trigger[0]["ts"],
            "event": "coach_trigger",
            "arm": "control",
            "turn": 30,
            "context_tokens": 150_000,
        }
    ]


def test_warned_arm_warns_and_still_logs(tmp_path: Path) -> None:
    _on(tmp_path)
    session = _session_for("warned")
    path = _transcript(tmp_path, 182_345)
    results = _run(path, session, tmp_path)
    assert results[-1] == {"systemMessage": EXACT_182K_OPUS}
    trigger = [r for r in _receipts(tmp_path) if r["event"] == "coach_trigger"]
    assert [(r["arm"], r["turn"], r["context_tokens"]) for r in trigger] == [
        ("warned", 30, 182_345)
    ]


def test_receipts_contain_no_canary_id_or_path(tmp_path: Path) -> None:
    _on(tmp_path)
    canary = "CANARY-1a2b3c4d5e6f-NEVER-STORE"
    session = f"{canary}-control"
    if coach._arm(session) != "control":
        session = f"{canary}-warned"
    path = tmp_path / f"{canary}.jsonl"
    path.write_text(_usage(150_000) + "\n", encoding="utf-8")
    _run(path, session, tmp_path)
    text = (tmp_path / coach.COACH_LOG).read_text(encoding="utf-8")
    assert canary not in text
    assert str(path) not in text
    assert all("session_id" not in r or r["session_id"] is None for r in _receipts(tmp_path))


def test_flag_off_output_and_receipts_are_unchanged(tmp_path: Path) -> None:
    _on(tmp_path, control=False)
    path = _transcript(tmp_path, 182_345)
    results = _run(path, "s1", tmp_path)
    assert results[-1] == {"systemMessage": EXACT_182K_OPUS}
    receipts = _receipts(tmp_path)
    assert [r["event"] for r in receipts] == ["coach_nudge"] * 30
    last = receipts[-1]
    assert set(last) == {
        "ts",
        "event",
        "session_id",
        "context_tokens",
        "turns",
        "model",
        "outcome",
    }
    assert last["session_id"] == "s1"
    assert (last["context_tokens"], last["turns"], last["outcome"]) == (182_345, 30, "shown")


def test_coach_off_emits_nothing_in_either_mode(tmp_path: Path) -> None:
    for control in (False, True):
        set_flag(tmp_path, "coach", False)
        set_flag(tmp_path, "coach_control", control)
        path = _transcript(tmp_path, 182_345)
        assert _run(path, "s1", tmp_path) == [{}] * 30
        assert not (tmp_path / coach.COACH_LOG).exists()
