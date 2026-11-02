"""Coach RCT instrumentation (P11): install arm, one assignment line, action and cost.

The experiment is only interpretable if the arm cannot move with cost, so the falsifying
test here is that assignment is a pure function of the install: no receipt, code path or
replay may let an expensive session drift into treatment.
"""

from __future__ import annotations

import json
import statistics
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from cfxd import coach
from cfxd.config import set_flag
from cfxd.handlers import clear, dispatch

OPUS = "claude-opus-5-5"


@pytest.fixture(autouse=True)
def _clean() -> Any:
    for reset in (clear, coach._STATE.clear, coach._ASSIGNED.clear, coach._INSTALL.clear):
        reset()
    yield
    for reset in (clear, coach._STATE.clear, coach._ASSIGNED.clear, coach._INSTALL.clear):
        reset()


def _on(state_dir: Path, control: bool = True) -> None:
    set_flag(state_dir, "coach", True)
    set_flag(state_dir, "coach_control", control)


def _usage(context: int, model: str = OPUS) -> str:
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


def _transcript(path: Path, context: int) -> Path:
    path.write_text(_usage(context) + "\n", encoding="utf-8")
    return path


def _payload(path: Path, session: str = "s1") -> dict[str, Any]:
    return {"session_id": session, "transcript_path": str(path)}


def _receipts(state_dir: Path, event: str | None = None) -> list[dict[str, Any]]:
    text = (state_dir / coach.COACH_LOG).read_text(encoding="utf-8")
    entries = [json.loads(line) for line in text.splitlines()]
    return [e for e in entries if event is None or e["event"] == event]


def _install_for(base: Path, arm: str) -> Path:
    """A state dir whose install hash lands in ``arm``."""
    for i in range(10_000):
        state_dir = base / f"install-{i}"
        if coach._arm(coach._install_id(state_dir)) == arm:
            return state_dir
    raise AssertionError(f"no install found for arm {arm}")


def _nudge(state_dir: Path, path: Path, session: str = "s1", turns: int = 30) -> None:
    for _ in range(turns):
        coach.handle_stop(_payload(path, session), state_dir)


def test_arm_is_a_pure_function_of_the_install(tmp_path: Path) -> None:
    install_id = coach._install_id(tmp_path)
    expected = "warned" if sha256(install_id.encode()).digest()[0] % 2 == 0 else "control"
    assert coach._arm(install_id) == expected
    coach._INSTALL.clear()
    assert coach._install_id(tmp_path) == install_id
    assert coach._arm(coach._install_id(tmp_path)) == expected


def test_cheap_and_expensive_sessions_of_one_install_share_the_arm(tmp_path: Path) -> None:
    state_dir = _install_for(tmp_path, "control")
    _on(state_dir)
    cheap = _transcript(tmp_path / "cheap.jsonl", 150_000)
    dear = _transcript(tmp_path / "dear.jsonl", 400_000)
    _nudge(state_dir, cheap, "cheap")
    _nudge(state_dir, dear, "dear")
    arms = {r["arm"] for r in _receipts(state_dir, "coach_trigger")}
    assert arms == {"control"}
    assert [r["arm"] for r in _receipts(state_dir, "coach_assignment")] == ["control"]


def test_assignment_stays_independent_of_session_cost_across_installs(
    tmp_path: Path,
) -> None:
    costs: list[float] = []
    arms: list[float] = []
    context = 182_345
    for i in range(40):
        state_dir = tmp_path / f"install-{i}"
        # Fixed install ids and a permuted replay keep the fixture deterministic; the arm
        # is still the real hash, and session cost is still measured from the replay.
        coach._INSTALL[str(state_dir)] = f"install-{i}"
        _on(state_dir)
        turns = 31 + 3 * ((i * 17) % 40)
        path = _transcript(tmp_path / f"session-{i}.jsonl", context)
        _nudge(state_dir, path, f"session-{i}", turns=turns)
        trigger = _receipts(state_dir, "coach_trigger")[0]
        assert trigger["arm"] == coach._arm(f"install-{i}")
        arms.append(1.0 if trigger["arm"] == "warned" else 0.0)
        costs.append((coach._turn_cost(OPUS, context) or 0.0) * turns)
    assert len(set(arms)) == 2, "the fixture must contain both arms"
    assert abs(statistics.correlation(arms, costs)) < 0.2


def test_assignment_is_logged_once_per_install(tmp_path: Path) -> None:
    state_dir = _install_for(tmp_path, "warned")
    _on(state_dir)
    path = _transcript(tmp_path / "s.jsonl", 182_345)
    _nudge(state_dir, path, turns=60)
    assignments = _receipts(state_dir, "coach_assignment")
    assert len(assignments) == 1
    assert set(assignments[0]) == {"ts", "event", "arm"}
    assert assignments[0]["arm"] == coach._arm(coach._install_id(state_dir))


def test_shown_and_would_have_shown_are_both_receipted(tmp_path: Path) -> None:
    warned = _install_for(tmp_path / "w", "warned")
    _on(warned)
    path = _transcript(tmp_path / "s.jsonl", 182_345)
    _nudge(warned, path, turns=29)
    assert coach.handle_stop(_payload(path), warned)["systemMessage"].startswith("Context Fabric")
    assert {r["arm"] for r in _receipts(warned, "coach_trigger")} == {"warned"}

    control = _install_for(tmp_path / "c", "control")
    _on(control)
    _nudge(control, path, session="c1", turns=31)
    assert {r["arm"] for r in _receipts(control, "coach_trigger")} == {"control"}


def test_action_is_restart_when_context_is_reset(tmp_path: Path) -> None:
    state_dir = _install_for(tmp_path, "control")
    _on(state_dir)
    path = _transcript(tmp_path / "s.jsonl", 182_345)
    _nudge(state_dir, path, turns=30)
    _transcript(path, 40_000)
    assert coach.handle_stop(_payload(path), state_dir) == {}
    outcome = _receipts(state_dir, "coach_outcome")
    assert len(outcome) == 1
    assert (outcome[0]["arm"], outcome[0]["action"], outcome[0]["turns_after"]) == (
        "control",
        "restart",
        1,
    )
    assert outcome[0]["cost_after_usd"] == pytest.approx(coach._turn_cost(OPUS, 40_000), abs=1e-4)


def test_action_is_none_after_n_turns_and_cost_is_summed(tmp_path: Path) -> None:
    state_dir = _install_for(tmp_path, "control")
    _on(state_dir)
    path = _transcript(tmp_path / "s.jsonl", 182_345)
    _nudge(state_dir, path, turns=30)
    for step in range(1, coach.ACTION_TURNS + 1):
        _transcript(path, 182_345 + step * 1_000)
        assert coach.handle_stop(_payload(path), state_dir) == {}
        if step < coach.ACTION_TURNS:
            assert _receipts(state_dir, "coach_outcome") == []
    outcome = _receipts(state_dir, "coach_outcome")
    assert [(r["action"], r["turns_after"]) for r in outcome] == [("none", coach.ACTION_TURNS)]
    expected = sum(coach._turn_cost(OPUS, 182_345 + step * 1_000) for step in range(1, 6))
    assert outcome[0]["cost_after_usd"] == pytest.approx(expected, abs=1e-4)


def test_action_is_end_when_a_new_session_starts(tmp_path: Path) -> None:
    state_dir = _install_for(tmp_path, "control")
    _on(state_dir)
    path = _transcript(tmp_path / "s.jsonl", 182_345)
    _nudge(state_dir, path, turns=30)
    coach.install(state_dir)
    dispatch("SessionStart", {"session_id": "s1"})  # the nudge's own session may resume
    assert _receipts(state_dir, "coach_outcome") == []
    dispatch("SessionStart", {"session_id": "s2"})
    outcome = _receipts(state_dir, "coach_outcome")
    assert [(r["action"], r["arm"]) for r in outcome] == [("end", "control")]


def test_action_is_dismiss_when_the_coach_is_turned_off(tmp_path: Path) -> None:
    state_dir = _install_for(tmp_path, "control")
    _on(state_dir)
    path = _transcript(tmp_path / "s.jsonl", 182_345)
    _nudge(state_dir, path, turns=30)
    set_flag(state_dir, "coach", False)
    assert coach.handle_stop(_payload(path), state_dir) == {}
    assert coach.handle_stop(_payload(path), state_dir) == {}
    outcome = _receipts(state_dir, "coach_outcome")
    assert [(r["action"], r["arm"]) for r in outcome] == [("dismiss", "control")]


def test_outcome_receipt_is_counts_only(tmp_path: Path) -> None:
    canary = "CANARY-77aa88bb99cc-NEVER-STORE"
    state_dir = _install_for(tmp_path, "control")
    _on(state_dir)
    path = _transcript(tmp_path / f"{canary}.jsonl", 182_345)
    _nudge(state_dir, path, session=f"{canary}-session", turns=30)
    _transcript(path, 30_000)
    coach.handle_stop(_payload(path, f"{canary}-session"), state_dir)
    outcome = _receipts(state_dir, "coach_outcome")[0]
    assert set(outcome) == {"ts", "event", "arm", "action", "turns_after", "cost_after_usd"}
    text = (state_dir / coach.COACH_LOG).read_text(encoding="utf-8")
    assert canary not in text
    assert str(tmp_path) not in text


def test_flag_off_writes_nothing_new(tmp_path: Path) -> None:
    path = _transcript(tmp_path / "s.jsonl", 182_345)
    set_flag(tmp_path, "coach", False)
    set_flag(tmp_path, "coach_control", True)
    _nudge(tmp_path, path, turns=40)
    assert not (tmp_path / coach.COACH_LOG).exists()
