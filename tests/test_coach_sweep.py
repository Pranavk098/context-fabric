"""Coach threshold sweep (P11): precision and recall per setting on synthetic sessions."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import coach_sweep as sweep  # noqa: E402

OPUS = "claude-opus-5-5"


def _session(name: str, turns: int, context: int = 182_345) -> dict[str, Any]:
    return {"session": name, "turns": [(context, OPUS)] * turns}


def test_no_setting_fires_below_every_threshold() -> None:
    data = sweep.sweep([_session("a", 40)], fire_turns=[10], costs=[1e9])
    row = data["rows"][0]
    assert (row["fired"], row["hits"], row["precision"], row["recall"]) == (0, 0, None, 0.0)


def test_precision_and_recall_are_measured_against_the_top_decile() -> None:
    # Ten priced sessions of ten to a hundred turns: the top 10% is the longest.
    sessions = [_session(f"s{i}", 10 * (i + 1)) for i in range(10)]
    data = sweep.sweep(sessions, fire_turns=[10, 50], costs=[0.0])
    assert (data["top_k"], data["n_priced"], data["n_sessions"]) == (1, 10, 10)
    early, late = data["rows"]
    assert (early["fired"], early["hits"]) == (10, 1)
    assert early["precision"] == 0.1
    assert early["recall"] == 1.0
    # At turn 50 six sessions reach it and only the target is in the top decile.
    assert (late["fired"], late["hits"]) == (6, 1)
    assert (late["precision"], late["recall"]) == (1 / 6, 1.0)


def test_cost_threshold_counts_spend_up_to_the_fire_turn() -> None:
    # Three turns of one session cost less than six of the same session.
    turns = 6
    one_turn = sweep.session_cost(_session("a", 1)["turns"])
    data = sweep.sweep(
        [_session("short", 3), _session("long", turns)],
        fire_turns=[1],
        costs=[one_turn * 2, one_turn * 4],
    )
    cheap, dear = data["rows"]
    assert cheap["fired"] == 2
    assert dear["fired"] == 1
    assert sweep.first_fire(_session("a", 3)["turns"], 1, one_turn * 4) is None


def test_render_is_aggregates_only_and_labelled_measured(tmp_path: Path) -> None:
    canary = "CANARY-1f2e3d4c5b6a-NEVER-STORE"
    root = tmp_path / "proj"
    root.mkdir()
    line = f'{{"type":"user","message":{{"content":"{canary}"}}}}'
    (root / "sess.jsonl").write_text(line + "\n", encoding="utf-8")
    data = sweep.sweep(sweep.collect(tmp_path), fire_turns=[10], costs=[0.0])
    text = sweep.render(data)
    assert canary not in text
    assert str(tmp_path) not in text
    assert "*Measured*" in text
    assert "| fire turn | cost |" in text
    assert "No setting fired" in text
