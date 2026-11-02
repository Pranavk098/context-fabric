"""Restart break-even simulator (P4 M3) on synthetic transcripts.

Every answer here is known by construction: turn counts, boundary candidates, the
measured prefix, and the direction of each cost-to-finish comparison. No real logs, no
network, no model call.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import restart_breakeven as rb  # noqa: E402

_MODEL = "claude-sonnet-4-5"
_FAMILIES = rb._load_prices(rb._DEFAULT_PRICES)["families"]
_ENTRY = _FAMILIES["sonnet-4-5"]
_SESSION = "11111111-2222-4333-8444-555555555555"


def _usage(prompt: int) -> dict[str, int]:
    return {
        "input_tokens": 10,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": max(0, prompt - 10),
        "output_tokens": 100,
    }


def _row(mid: str, *, read: int, cc5m: int = 0, output: int = 0) -> dict[str, Any]:
    """An assistant turn whose prompt is exactly ``read + cc5m`` tokens."""
    message = {
        "id": mid,
        "model": _MODEL,
        "usage": {
            "input_tokens": 0,
            "cache_creation": {"ephemeral_5m_input_tokens": cc5m, "ephemeral_1h_input_tokens": 0},
            "cache_read_input_tokens": read,
            "output_tokens": output,
        },
        "stop_reason": "end_turn",
        "content": [],
    }
    return {"type": "assistant", "message": message}


def _turn(mid: str, prompt: int, blocks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    message = {
        "id": mid,
        "model": _MODEL,
        "usage": _usage(prompt),
        "stop_reason": "end_turn",
        "content": list(blocks or []),
    }
    return {"type": "assistant", "message": message}


def _bash(uid: str, command: str) -> dict[str, Any]:
    return {"type": "tool_use", "id": uid, "name": "Bash", "input": {"command": command}}


def _result(uid: str, text: str) -> dict[str, Any]:
    block = {"type": "tool_result", "tool_use_id": uid, "content": text}
    return {"type": "user", "message": {"content": [block]}}


def _said(text: str) -> dict[str, Any]:
    return {"type": "user", "message": {"content": text}}


def _long(
    key: str,
    *,
    turns: int = 45,
    prefix: int = 2000,
    growth: int = 800,
    commit_at: int | None = None,
    model: str = _MODEL,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for i in range(turns):
        blocks = []
        if commit_at is not None and i == commit_at:
            blocks.append(_bash(f"c{i}", 'git commit -m "CFX_SECRET_MESSAGE"'))
        row = _turn(f"{key}-{i}", prefix + i * growth, blocks)
        row["message"]["model"] = model
        rows.append(row)
    return rows


def _write(root: Path, project: str, stem: str, rows: list[dict[str, Any]]) -> None:
    path = root / project / f"{stem}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _run(root: Path, *extra: str) -> None:
    assert rb.main(["--root", str(root), "--bars-commit", "b6f1c26", *extra]) == 0


# ------------------------------------------------------------------ CLI contract


def test_refuses_to_run_without_bars_commit(tmp_path: Path) -> None:
    _write(tmp_path, "proj", _SESSION, _long("a", turns=3))
    with pytest.raises(SystemExit):
        rb.main(["--root", str(tmp_path)])


def test_header_prints_the_bars_commit_and_packet(tmp_path: Path, capsys: Any) -> None:
    _write(tmp_path, "proj", _SESSION, _long("a", turns=3))
    _run(tmp_path, "--packet-kb", "3")
    out = capsys.readouterr().out
    assert "b6f1c26" in out
    assert "768 tokens (3 KB target)" in out


# ------------------------------------------------------------------ turn and boundary scan


def test_one_turn_per_message_id_with_its_final_usage() -> None:
    rows = [
        _turn("m0", 300),
        _turn("m0", 900),
        _turn("m0", 900),
        _turn("m1", 1200),
    ]
    turns, index = rb.scan_turns(rows, _FAMILIES)
    assert [t.prompt for t in turns] == [900, 1200]
    assert index == {"m0": 0, "m1": 1}


def test_sidechain_lines_are_not_turns() -> None:
    side = _turn("worker", 50_000)
    side["isSidechain"] = True
    turns, index = rb.scan_turns([_turn("m0", 1000), side], _FAMILIES)
    assert [t.prompt for t in turns] == [1000]
    assert "worker" not in index


def test_boundaries_are_the_three_deterministic_kinds_only() -> None:
    rows = [
        _turn("m0", 5000, [_bash("t0", "uv run pytest -q")]),
        _result("t0", "1 failed, 1 passed"),
        _turn("m1", 6000, [_bash("t1", "uv run pytest -q")]),
        _result("t1", "3 passed"),
        _turn("m2", 7000, [_bash("c2", "git commit -am x")]),
        _turn("m3", 8000),
        _said("Next task: fix the parser"),
    ]
    turns, index = rb.scan_turns(rows, _FAMILIES)
    assert len(turns) == 4
    cands, kinds = rb.boundary_candidates(rows, index)
    # The pass after the failure is a boundary at turn 2, the commit at 3, the
    # task switch at 4. A mixed "1 failed, 1 passed" line counts as a failure.
    assert cands == [2, 3, 4]
    assert kinds == {"test_pass": 1, "commit": 1, "task_switch": 1}


def test_a_pass_before_any_failure_is_not_a_boundary() -> None:
    rows = [
        _turn("m0", 5000, [_bash("t0", "uv run pytest -q")]),
        _result("t0", "3 passed"),
        _turn("m1", 6000, [_bash("t1", "uv run pytest -q")]),
        _result("t1", "3 passed"),
    ]
    _turns, index = rb.scan_turns(rows, _FAMILIES)
    assert rb.boundary_candidates(rows, index)[0] == []


# ------------------------------------------------------------------ the model


def test_prefix_is_measured_from_the_session_not_hardcoded() -> None:
    small = _long("s", turns=45, prefix=500, commit_at=5)
    large = _long("l", turns=45, prefix=9000, commit_at=5)
    turns_small, index_small = rb.scan_turns(small, _FAMILIES)
    turns_large, index_large = rb.scan_turns(large, _FAMILIES)
    assert turns_small[0].prompt == 500
    assert turns_large[0].prompt == 9000
    packet = 768
    head_small = rb.simulate(turns_small, [6], 500, packet, _ENTRY)["rows"]
    head_large = rb.simulate(turns_large, [6], 9000, packet, _ENTRY)["rows"]
    # A larger measured prefix is a larger cache write, so the same boundary has less headroom.
    assert head_small[0][1] > head_large[0][1]


def test_restart_opening_cost_is_prefix_plus_packet_at_the_write_price() -> None:
    turns = [_turn(f"m{i}", 2000 + 800 * i) for i in range(4)]
    scanned, _index = rb.scan_turns(turns, _FAMILIES)
    packet = 768
    rows = rb.simulate(scanned, [3], scanned[0].prompt, packet, _ENTRY)["rows"]
    _t, headroom, cont = rows[0]
    # The last turn: continuing pays its logged prompt, restarting pays the write of
    # prefix + packet instead, so a prefix this close to the context is a net loss.
    assert cont == pytest.approx((10 * 3 + 4390 * 0.3 + 100 * 15) / 1e6)
    opening = (2000 + packet) * 3.75 / 1e6
    # The restarted turn pays its own new tokens (here 10 input) and reads only the growth.
    anticipated = opening + (10 * 3 + 800 * 0.3 + 100 * 15) / 1e6
    assert anticipated == pytest.approx(cont - headroom)


def test_headroom_comes_from_the_context_a_restart_discards() -> None:
    turns, _index = rb.scan_turns(_long("a", turns=45), _FAMILIES)
    simulated = rb.simulate(turns, [2, 20], turns[0].prompt, 768, _ENTRY)
    early, late = (h for _t, h, _c in simulated["rows"])
    # At turn 2 the prefix is nearly the whole context, so the write premium wins.
    assert early < 0
    # At turn 20 a restart discards far more than it re-pays.
    assert late > 0
    assert simulated["crossed"] is not None
    assert simulated["crossed"][0] == 20


def test_a_bigger_packet_lowers_headroom() -> None:
    turns, _index = rb.scan_turns(_long("a", turns=45), _FAMILIES)
    thin = rb.simulate(turns, [6], turns[0].prompt, 0, _ENTRY)["rows"][0][1]
    fat = rb.simulate(turns, [6], turns[0].prompt, 3000, _ENTRY)["rows"][0][1]
    assert thin > fat


def test_new_tokens_on_the_restarted_path_are_not_free() -> None:
    # 50 turns, each writing 2,000 new cache tokens over an 83,000-token prefix, so the
    # measured prefix (85,000) is exactly the context at the turn-1 boundary.
    rows = [_row(f"m{i}", read=83_000 + 2_000 * i, cc5m=2_000, output=100) for i in range(50)]
    turns, _index = rb.scan_turns(rows, _FAMILIES)
    assert turns[0].prompt == 85_000
    packet = 768
    simulated = rb.simulate(turns, [1, 2], turns[0].prompt, packet, _ENTRY)
    assert [t for t, _h, _c in simulated["rows"]] == [1, 2]
    assert all(headroom <= 0 for _t, headroom, _c in simulated["rows"])

    def pre_fix(t: int) -> float:
        # The reviewed bug: cache read of the whole context plus output, new tokens free.
        restart = (turns[0].prompt + packet) * 3.75 / 1e6
        cont = sum(
            rb._turn_dollars(_ENTRY, turns[i].tokens, turns[i].prompt) for i in range(t, len(turns))
        )
        for i in range(t, len(turns)):
            ctx = turns[0].prompt + packet + (turns[i].prompt - turns[t - 1].prompt)
            read = max(0, ctx - (turns[0].prompt + packet)) if i == t else ctx
            restart += (read * 0.3 + turns[i].tokens["output"] * 15) / 1e6
        return cont - restart

    # 49 turns of 2,000 tokens read at 0.3 instead of written at 3.75: a fake $0.031 win.
    assert pre_fix(1) == pytest.approx(0.0309, abs=1e-3)


def test_a_long_grown_context_makes_a_restart_genuinely_win() -> None:
    # 45 turns over a 2,000-token prefix: each adds 2,000 cache-write tokens, 100 output,
    # packet 0. sonnet-4-5 prices per Mtok: input 3, write 3.75, read 0.3, output 15.
    rows = [_row(f"m{i}", read=2_000 * i, cc5m=2_000, output=100) for i in range(45)]
    turns, _index = rb.scan_turns(rows, _FAMILIES)
    _t, headroom, cont = rb.simulate(turns, [20], turns[0].prompt, 0, _ENTRY)["rows"][0]
    # Continuing, turns 20..44: 0.3 * 2,000i + 2,000 * 3.75 + 100 * 15 = 9,000 + 600i
    # micro-dollars; 25 turns = 225,000 + 600 * 800 = 705,000 = $0.705.
    # Restart: write the 2,000-token prefix at 3.75 = 7,500. Turn 20 reads the 2,000-token
    # growth = 600, writes its 2,000 new tokens = 7,500, outputs 1,500 -> 9,600. Turns
    # 21..44 read 2,000j (j = 2..25) = 0.3 * 2,000 * 324 = 194,400, write 24 * 7,500 =
    # 180,000, output 24 * 1,500 = 36,000 -> 410,400. Restarted total = 427,500 = $0.4275,
    # so the headroom is 0.705 - 0.4275 = $0.2775.
    assert cont == pytest.approx(0.705)
    assert headroom == pytest.approx(0.2775)


def test_a_session_that_only_ties_is_context_and_never_the_bar_figure() -> None:
    def session(turns: int = 45, crossed: Any = None, exists: bool = False) -> dict[str, Any]:
        return {
            "turns": turns,
            "prefix": 2000,
            "candidates": 1,
            "exists": exists,
            "crossed": crossed,
        }

    tie = session(exists=True)
    cleared = [session(crossed=(6, 0.03, 0.4), exists=True) for _ in range(2)]
    sessions = [*cleared, tie, *(session(exists=False) for _ in range(6))]
    summary = rb.summarise(sessions)
    assert summary["n_long"] == 9
    assert summary["n_cleared"] == 2
    assert summary["share_cleared"] == pytest.approx(2 / 9)
    assert summary["n_exists"] == 3
    assert summary["n_tie_only"] == 1
    assert summary["headroom_q"] == (0.03, 0.03, 0.03)


def test_the_population_is_40_turns_and_up() -> None:
    short = {"turns": 39, "prefix": 1, "candidates": 1, "exists": True, "crossed": (5, 0.1, 1.0)}
    full = {"turns": 40, "prefix": 1, "candidates": 1, "exists": True, "crossed": (5, 0.1, 1.0)}
    summary = rb.summarise([short, full])
    assert summary["n_long"] == 1
    assert summary["n_cleared"] == 1


# ------------------------------------------------------------------ end to end


def test_two_long_sessions_one_past_the_bar(tmp_path: Path, capsys: Any) -> None:
    _write(tmp_path, "proj-a", _SESSION, _long("a", turns=45, commit_at=5))
    _write(tmp_path, "proj-b", "22222222-3333-4444-8555-666666666666", _long("b", turns=45))
    _run(tmp_path)
    out = capsys.readouterr().out
    assert "40+ turn sessions (the D-2 population): 2" in out
    assert "of those with no boundary candidate: 1" in out
    assert "1 of 2 sessions = **50.0%** -> bar cleared" in out


def test_a_prefix_that_dominates_the_context_never_breaks_even(tmp_path: Path, capsys: Any) -> None:
    _write(
        tmp_path, "proj", _SESSION, _long("a", turns=45, prefix=20_000, growth=100, commit_at=43)
    )
    _run(tmp_path)
    out = capsys.readouterr().out
    assert "0 of 1 sessions = **0.0%** -> bar not cleared" in out


def test_output_is_aggregates_only(tmp_path: Path, capsys: Any) -> None:
    rows = _long("a", turns=45, commit_at=5)
    rows.insert(0, _said("Please switch to the CFX_SECRET_PHRASE repo"))
    _write(tmp_path, "proj-CFX_SECRET_REPO", _SESSION, rows)
    _run(tmp_path)
    out = capsys.readouterr().out
    for secret in ("CFX_SECRET", _SESSION, "proj-CFX", ".jsonl", "git commit"):
        assert secret not in out
    assert "saved" not in out.lower()


def test_duplicate_copies_and_unpriced_models_are_counted_not_modelled(
    tmp_path: Path, capsys: Any
) -> None:
    rows = _long("a", turns=45, commit_at=5)
    _write(tmp_path, "proj-a", _SESSION, rows)
    _write(tmp_path, "proj-b", _SESSION, rows)
    _write(tmp_path, "proj-c", "33333333-4444-4555-8666-777777777777", _long("u", model="mystery"))
    _run(tmp_path)
    out = capsys.readouterr().out
    assert "duplicate copies skipped: 1" in out
    assert "unpriced model: 1" in out
    assert "40+ turn sessions (the D-2 population): 1" in out
