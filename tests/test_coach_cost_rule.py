"""Coach fires on the frozen cost rule (W4-01): turn 15 and $0.50 of cache-read dollars.

The falsifying cases: a rich session one turn early, a long session a few cents short,
a priced session at the bar (fires once, then cools down), and an unpriced model that
must stay on the old token+turn rule byte-for-byte.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from cfxd import coach
from cfxd.config import set_flag
from cfxd.handlers import clear

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import coach_sweep  # noqa: E402

OPUS = "claude-opus-5-5"
UNPRICED = "claude-made-up-9"
EXACT_182K_UNPRICED = (
    "Context Fabric: this session is at ~182K tokens; each turn now re-reads it. "
    "If this is a natural break: /context-fabric:checkpoint, then /clear. "
    "Turn this off: cfxd set coach off"
)


@pytest.fixture(autouse=True)
def _clean() -> Any:
    clear()
    coach._STATE.clear()
    yield
    clear()
    coach._STATE.clear()


def _on(state_dir: Path) -> None:
    set_flag(state_dir, "coach", True)
    set_flag(state_dir, "coach_control", False)


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


def _write(path: Path, context: int, model: str = OPUS) -> Path:
    path.write_text(_usage(context, model) + "\n", encoding="utf-8")
    return path


def _payload(path: Path, session: str = "s1") -> dict[str, Any]:
    return {"session_id": session, "transcript_path": str(path)}


def _receipts(state_dir: Path) -> list[dict[str, Any]]:
    text = (state_dir / coach.COACH_LOG).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def _play(
    state_dir: Path,
    path: Path,
    contexts: list[int],
    model: str = OPUS,
    session: str = "s1",
) -> list[int]:
    """Feed one context per turn; return the turn numbers that showed a nudge."""
    fired: list[int] = []
    for number, context in enumerate(contexts, 1):
        _write(path, context, model)
        if coach.handle_stop(_payload(path, session), state_dir):
            fired.append(number)
    return fired


def test_frozen_setting_is_turn_15_and_50_cents() -> None:
    assert (coach.COACH_FIRE_TURN, coach.COACH_FIRE_COST_USD) == (15, 0.50)
    assert coach._turn_cost(OPUS, 100_000) == pytest.approx(0.02)


def test_turn_14_over_the_cost_bar_does_not_fire(tmp_path: Path) -> None:
    _on(tmp_path)
    assert 14 * (coach._turn_cost(OPUS, 300_000) or 0.0) >= coach.COACH_FIRE_COST_USD
    assert _play(tmp_path, tmp_path / "s.jsonl", [300_000] * 14) == []
    assert _receipts(tmp_path)[-1]["outcome"] == "below_threshold"


def test_turn_15_just_under_the_cost_bar_does_not_fire(tmp_path: Path) -> None:
    _on(tmp_path)
    spend = 15 * (coach._turn_cost(OPUS, 160_000) or 0.0)
    assert 0.45 < spend < coach.COACH_FIRE_COST_USD
    assert _play(tmp_path, tmp_path / "s.jsonl", [160_000] * 16) == [16]


def test_turn_15_at_the_cost_bar_fires_once_and_cools_down(tmp_path: Path) -> None:
    _on(tmp_path)
    context = 170_000
    assert 15 * (coach._turn_cost(OPUS, context) or 0.0) >= coach.COACH_FIRE_COST_USD
    assert _play(tmp_path, tmp_path / "s.jsonl", [context] * 26) == [15]
    outcomes = [r["outcome"] for r in _receipts(tmp_path)]
    assert outcomes.count("shown") == 1
    assert outcomes[0] == "below_threshold" and "suppressed_recent" in outcomes


def test_unpriced_model_keeps_the_old_rule_and_message(tmp_path: Path) -> None:
    _on(tmp_path)
    path = tmp_path / "unpriced.jsonl"
    # Below the 150K token bar, even far past the turn threshold: still nothing.
    assert _play(tmp_path, path, [149_999] * 40, UNPRICED, session="s-low") == []
    _write(path, 182_345, UNPRICED)
    last: dict[str, Any] = {}
    for _ in range(30):
        last = coach.handle_stop(_payload(path, "s-high"), tmp_path)
    assert last == {"systemMessage": EXACT_182K_UNPRICED}


def test_coach_off_emits_nothing_on_a_costly_session(tmp_path: Path) -> None:
    set_flag(tmp_path, "coach", False)
    assert _play(tmp_path, tmp_path / "s.jsonl", [300_000] * 20) == []
    assert not (tmp_path / coach.COACH_LOG).exists()


def test_live_rule_agrees_with_the_sweep_on_the_same_session(tmp_path: Path) -> None:
    _on(tmp_path)
    turns = [(60_000, OPUS)] * 14 + [(300_000, OPUS)] * 6
    expected = coach_sweep.first_fire(turns, coach.COACH_FIRE_TURN, coach.COACH_FIRE_COST_USD)
    assert expected is not None
    assert _play(tmp_path, tmp_path / "s.jsonl", [context for context, _ in turns]) == [expected]
