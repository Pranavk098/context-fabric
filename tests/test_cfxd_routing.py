"""``cfxd routing`` tests: tokens and API-equivalent dollars by model and role."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cfxd.routing import (
    _match_family,
    _normalize,
    _price_for,
    _tokens,
    _turn_dollars,
    format_human,
    routing,
)

ROOT = Path(__file__).resolve().parents[1]


def _usage(
    input_tokens: int = 0, cc5m: int = 0, cc1h: int = 0, cr: int = 0, output: int = 0
) -> dict:
    return {
        "input_tokens": input_tokens,
        "cache_creation_input_tokens": cc5m + cc1h,
        "cache_read_input_tokens": cr,
        "output_tokens": output,
        "cache_creation": {
            "ephemeral_5m_input_tokens": cc5m,
            "ephemeral_1h_input_tokens": cc1h,
        },
    }


def _turn(
    model: str, *, input_tokens: int = 0, cc5m: int = 0, cc1h: int = 0, cr: int = 0, output: int = 0
) -> dict:
    return {
        "type": "assistant",
        "message": {
            "model": model,
            "content": [],
            "usage": _usage(input_tokens, cc5m, cc1h, cr, output),
        },
    }


def _write(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


def _session(root: Path, project: str, session_id: str, lines: list[dict]) -> Path:
    path = root / project / f"{session_id}.jsonl"
    _write(path, lines)
    return path


def _subagent(root: Path, project: str, session_id: str, agent_id: str, lines: list[dict]) -> Path:
    path = root / project / session_id / "subagents" / f"{agent_id}.jsonl"
    _write(path, lines)
    return path


def _model_entry(role_report: dict, name: str) -> dict:
    return next(m for m in role_report["models"] if m["model"] == name)


def test_exact_dollars_per_model_and_role(tmp_path: Path) -> None:
    _session(
        tmp_path,
        "proj",
        "p1",
        [
            _turn("claude-sonnet-5", input_tokens=1_000_000, output=1_000_000),
            _turn("claude-sonnet-5", cc5m=1_000_000),
        ],
    )
    _subagent(
        tmp_path,
        "proj",
        "p1",
        "agent-1",
        [_turn("claude-haiku-4-5", input_tokens=1_000_000, output=1_000_000)],
    )
    report = routing(tmp_path, "all")

    assert report["totals"]["sessions"] == 2
    assert report["totals"]["turns"] == 3
    assert report["totals"]["tokens"]["input"] == 2_000_000
    assert report["totals"]["tokens"]["cache_creation_5m"] == 1_000_000
    assert report["totals"]["tokens"]["cache_creation_1h"] == 0
    assert report["totals"]["tokens"]["output"] == 2_000_000
    assert report["totals"]["weighted_tokens"] == pytest.approx(13_250_000.0)
    assert report["totals"]["dollars"] == pytest.approx(20.5)

    parent = report["roles"]["parent"]
    assert parent["sessions"] == 1
    assert parent["turns"] == 2
    assert parent["dollars"] == pytest.approx(14.5)
    sonnet = _model_entry(parent, "sonnet-5")
    assert sonnet["turns"] == 2
    assert sonnet["tokens"]["input"] == 1_000_000
    assert sonnet["tokens"]["cache_creation_5m"] == 1_000_000
    assert sonnet["tokens"]["output"] == 1_000_000
    assert sonnet["dollars"] == pytest.approx(14.5)

    sub = report["roles"]["subagent"]
    assert sub["turns"] == 1
    assert sub["dollars"] == pytest.approx(6.0)
    haiku = _model_entry(sub, "haiku-4-5")
    assert haiku["tokens"]["input"] == 1_000_000
    assert haiku["tokens"]["output"] == 1_000_000
    assert haiku["dollars"] == pytest.approx(6.0)


def test_longest_prefix_matching(tmp_path: Path) -> None:
    _session(
        tmp_path,
        "proj",
        "p1",
        [
            _turn("claude-sonnet-5-5", input_tokens=1_000_000),
            _turn("claude-sonnet-5", input_tokens=2_000_000),
            _turn("claude-haiku-4-5-20251001", input_tokens=1_000_000),
            _turn("claude-opus-5-5", input_tokens=1_000_000),
        ],
    )
    parent = routing(tmp_path, "all")["roles"]["parent"]
    names = {m["model"] for m in parent["models"]}
    assert names == {"sonnet-5-5", "sonnet-5", "haiku-4-5", "opus-5-5"}

    assert _model_entry(parent, "sonnet-5-5")["tokens"]["input"] == 1_000_000
    assert _model_entry(parent, "sonnet-5")["tokens"]["input"] == 2_000_000
    # the date suffix is stripped; each Opus version has its own price row
    assert _model_entry(parent, "haiku-4-5")["tokens"]["input"] == 1_000_000
    assert _model_entry(parent, "opus-5-5")["tokens"]["input"] == 1_000_000

    assert _normalize("claude-sonnet-5-5") == "sonnet-5-5"
    assert _normalize("claude-haiku-4-5-20251001") == "haiku-4-5"
    assert _match_family("sonnet-5-5", {"sonnet-5": {}, "sonnet-5-5": {}}) == "sonnet-5-5"
    assert _match_family("sonnet-5", {"sonnet-5": {}, "sonnet-5-5": {}}) == "sonnet-5"


def test_unpriced_model(tmp_path: Path) -> None:
    _session(
        tmp_path,
        "proj",
        "p1",
        [_turn("claude-zeta-9", input_tokens=1_000_000, output=1_000_000)],
    )
    report = routing(tmp_path, "all")
    assert report["totals"]["dollars"] == pytest.approx(0.0)
    assert report["roles"]["parent"]["models"] == []
    u = report["unpriced"]
    assert u["turns"] == 1
    assert u["sessions"] == 1
    assert u["tokens"]["input"] == 1_000_000
    assert u["models"][0]["model"] == "claude-zeta-9"
    assert u["models"][0]["turns"] == 1


def test_haiku_55_tiering_boundary() -> None:
    haiku = {
        "input": 0.10,
        "cache_write_5m": 0.125,
        "cache_write_1h": 0.20,
        "cache_read": 0.01,
        "output": 0.50,
        "over_100k": {
            "input": 0.50,
            "cache_write_5m": 0.625,
            "cache_write_1h": 1.0,
            "cache_read": 0.05,
            "output": 2.50,
        },
    }
    low = {
        "input": 100_000,
        "cache_creation_5m": 0,
        "cache_creation_1h": 0,
        "cache_read": 0,
        "output": 0,
    }
    high = {
        "input": 100_001,
        "cache_creation_5m": 0,
        "cache_creation_1h": 0,
        "cache_read": 0,
        "output": 0,
    }
    assert _turn_dollars(haiku, low, _prompt(low)) == pytest.approx(0.01)
    assert _turn_dollars(haiku, high, _prompt(high)) == pytest.approx(0.0500005)
    assert _price_for(haiku, 100_000) is haiku
    assert _price_for(haiku, 100_001) is haiku["over_100k"]


def _prompt(tokens: dict[str, int]) -> int:
    return (
        tokens["input"]
        + tokens["cache_creation_5m"]
        + tokens["cache_creation_1h"]
        + tokens["cache_read"]
    )


def test_haiku_55_tiering_report(tmp_path: Path) -> None:
    _session(
        tmp_path,
        "proj",
        "p1",
        [
            _turn("claude-haiku-5-5", input_tokens=100_000),
            _turn("claude-haiku-5-5", input_tokens=200_000),
        ],
    )
    haiku = _model_entry(routing(tmp_path, "all")["roles"]["parent"], "haiku-5-5")
    # 100K -> low tier ($0.10/MTok = $0.01), 200K -> high tier ($0.50/MTok = $0.10)
    assert haiku["dollars"] == pytest.approx(0.11)


def test_whatif_reprice_only_pricier(tmp_path: Path) -> None:
    _subagent(
        tmp_path,
        "proj",
        "p1",
        "agent-1",
        [
            _turn("claude-opus-5", input_tokens=1_000_000),
            _turn("claude-haiku-4-5", input_tokens=1_000_000),
        ],
    )
    report = routing(tmp_path, "all")
    total = report["totals"]["dollars"]  # opus $5 + haiku $1 = $6

    haiku = report["whatif"]["haiku"]
    assert haiku["turns"] == 1  # only the opus turn is re-priced
    assert haiku["dollars_before"] == pytest.approx(5.0)
    # prompt is 1M > 100K, so the Haiku 5.5 target uses its high tier ($0.50/MTok)
    assert haiku["dollars_after"] == pytest.approx(0.50)
    assert haiku["change"] == pytest.approx(-4.50)
    assert haiku["percent_of_total"] == pytest.approx(-4.50 / total * 100.0)

    sonnet = report["whatif"]["sonnet"]
    assert sonnet["turns"] == 1
    assert sonnet["dollars_before"] == pytest.approx(5.0)
    assert sonnet["dollars_after"] == pytest.approx(2.0)
    assert sonnet["change"] == pytest.approx(-3.0)


def test_whatif_haiku_target_leaves_haiku_unchanged(tmp_path: Path) -> None:
    # A turn already on Haiku is not re-priced by --whatif haiku (not pricier).
    _subagent(
        tmp_path,
        "proj",
        "p1",
        "agent-1",
        [_turn("claude-haiku-4-5", input_tokens=1_000_000)],
    )
    report = routing(tmp_path, "all")
    assert report["whatif"]["haiku"]["turns"] == 0
    assert report["whatif"]["haiku"]["dollars_before"] == pytest.approx(0.0)
    assert report["whatif"]["sonnet"]["turns"] == 0


def test_prices_override(tmp_path: Path) -> None:
    prices = tmp_path / "prices.json"
    prices.write_text(
        json.dumps(
            {
                "as_of": "test",
                "models": [
                    {
                        "family": "sonnet-5",
                        "input": 1.0,
                        "cache_write_5m": 1.0,
                        "cache_write_1h": 1.0,
                        "cache_read": 1.0,
                        "output": 1.0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    _session(
        tmp_path,
        "proj",
        "p1",
        [
            _turn("claude-sonnet-5", input_tokens=1_000_000),
            _turn("claude-opus-5", input_tokens=1_000_000),
        ],
    )
    report = routing(tmp_path, "all", prices_path=prices)
    parent = report["roles"]["parent"]
    sonnet = _model_entry(parent, "sonnet-5")
    assert sonnet["dollars"] == pytest.approx(1.0)  # overridden, not the shipped $2
    assert "opus" not in {m["model"] for m in parent["models"]}
    assert report["unpriced"]["models"][0]["model"] == "claude-opus-5"


def test_since_filter_by_mtime(tmp_path: Path) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    new = _session(tmp_path, "proj", "new", [_turn("claude-sonnet-5", input_tokens=1_000_000)])
    old = _session(tmp_path, "proj", "old", [_turn("claude-sonnet-5", input_tokens=1_000_000)])
    os.utime(new, ((now - timedelta(days=2)).timestamp(),) * 2)
    os.utime(old, ((now - timedelta(days=40)).timestamp(),) * 2)

    assert routing(tmp_path, "7d", now=now)["totals"]["sessions"] == 1
    assert routing(tmp_path, "30d", now=now)["totals"]["sessions"] == 1
    assert routing(tmp_path, "all", now=now)["totals"]["sessions"] == 2


def test_empty_root(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    report = routing(empty, "all")
    assert report["totals"]["sessions"] == 0
    assert report["totals"]["turns"] == 0
    assert report["totals"]["dollars"] == 0.0
    assert format_human(report, empty.name) == "No transcripts found under empty."


def test_tokens_split_fallback() -> None:
    usage = {"input_tokens": 10, "cache_creation_input_tokens": 7, "output_tokens": 3}
    t = _tokens(usage)
    assert t["input"] == 10
    assert t["cache_creation_5m"] == 0
    assert t["cache_creation_1h"] == 7
    assert t["output"] == 3


def test_json_shape(tmp_path: Path) -> None:
    _session(tmp_path, "proj", "p1", [_turn("claude-sonnet-5", input_tokens=1_000_000)])
    result = subprocess.run(
        [sys.executable, "-m", "cfxd", "routing", "--root", str(tmp_path), "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert set(data["totals"]) >= {"sessions", "turns", "tokens", "weighted_tokens", "dollars"}
    assert set(data["roles"]) == {"parent", "subagent"}
    assert isinstance(data["roles"]["parent"]["models"], list)
    assert "unpriced" in data
    assert set(data["whatif"]) == {"haiku", "sonnet"}
    assert isinstance(data["limits"], list)


def test_canary_strings_never_leak(tmp_path: Path) -> None:
    path_canary = "CANARYPATH-abcd1234"
    cmd_canary = "CANARYCMD-efgh5678"
    prompt_canary = "CANARYPROMPT-mnop3456"
    lines = [
        {
            "type": "assistant",
            "message": {
                "model": "claude-sonnet-5",
                "content": [{"type": "text", "text": prompt_canary}],
                "usage": _usage(1),
            },
        },
        {
            "type": "assistant",
            "message": {
                "model": "claude-sonnet-5",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Bash",
                        "input": {"command": cmd_canary},
                    }
                ],
                "usage": _usage(1),
            },
        },
    ]
    _session(tmp_path, "proj", "p1", lines)

    for extra in ([], ["--json"]):
        result = subprocess.run(
            [sys.executable, "-m", "cfxd", "routing", "--root", str(tmp_path), *extra],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        for canary in (path_canary, cmd_canary, prompt_canary):
            assert canary not in result.stdout
            assert canary not in result.stderr


def test_cli_human_output(tmp_path: Path) -> None:
    _session(tmp_path, "proj", "p1", [_turn("claude-sonnet-5", input_tokens=1_000_000)])
    result = subprocess.run(
        [sys.executable, "-m", "cfxd", "routing", "--root", str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "Claude Code routing" in result.stdout
    assert "sonnet-5" in result.stdout
    assert "not a bill" in result.stdout
    assert "Limits:" in result.stdout


def test_each_model_version_resolves_to_its_own_price_row() -> None:
    # Opus 5.5 is priced below Opus 5 (input $4 vs $5, cache read $0.20 vs $0.50 per MTok); a
    # generic "opus" row overstated it. Longest-prefix matching must pick the exact version.
    from cfxd.routing import _load_prices, _match_family, _normalize

    prices = _load_prices(Path(__file__).resolve().parents[1] / "cfxd" / "data" / "prices.json")
    fams = prices["families"]
    assert _match_family(_normalize("claude-opus-5-5"), fams) == "opus-5-5"
    assert _match_family(_normalize("claude-opus-5"), fams) == "opus-5"
    assert _match_family(_normalize("claude-opus-4-8"), fams) == "opus-4-8"
    assert _match_family(_normalize("claude-fable-5"), fams) == "fable-5"
    assert fams["opus-5-5"]["cache_read"] == 0.20
    assert fams["opus-5"]["cache_read"] == 0.50
    assert fams["fable-5"]["output"] == 50
