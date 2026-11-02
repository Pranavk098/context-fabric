"""W3-04: the Codex (beta) log reader, its golden reports and the log interface.

Every fixture here is synthetic. The real ``~/.codex`` log was only inspected with a
read-only script for key names and integer totals; no real session file is in the repo.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cfxd.cli import main as cli_main
from cfxd.logs import get_source
from cfxd.logs.codex import format_report, report, sessions

GOLDEN = Path(__file__).with_name("golden") / "codex"
REPORTS = GOLDEN / "reports"

_TS = "2026-01-01T00:00:00Z"


def _line(ordinal: int, type_: str, payload: dict) -> dict:
    return {"timestamp": _TS, "ordinal": ordinal, "type": type_, "payload": payload}


def _usage(input_tokens: int, cached: int, output: int) -> dict:
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": 0,
        "output_tokens": output,
        "reasoning_output_tokens": 0,
        "total_tokens": input_tokens + output,
    }


def _write(root: Path, name: str, lines: list) -> Path:
    path = root / "2026" / "01" / "01" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


_CASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("audit", ("audit", "--source", "codex", "--since", "all")),
    ("week", ("week", "--source", "codex", "--days", "36500", "--state-dir", "{state}")),
)


def _stage(dst: Path) -> Path:
    """Copy the fixtures to ``dst`` as ``.jsonl``.

    They are stored as ``.jsonl.fixture`` so ``tests/golden/reports`` (which audits the
    whole ``tests/golden`` root) never picks up a Codex rollout as a Claude transcript.
    """
    root = dst / "codex"
    for path in GOLDEN.rglob("*.jsonl.fixture"):
        target = root / path.relative_to(GOLDEN).with_suffix("")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
    return root


@pytest.mark.parametrize(("name", "argv"), _CASES, ids=[case[0] for case in _CASES])
def test_codex_golden_report_is_exact(
    name: str,
    argv: tuple[str, ...],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CFX_HOME", str(tmp_path))
    root = _stage(tmp_path)
    args = [arg.format(state=tmp_path) for arg in (*argv, "--root", str(root))]
    assert cli_main(args) == 0
    actual = capsys.readouterr().out.replace("\r\n", "\n")
    assert actual == (REPORTS / f"{name}.txt").read_text(encoding="utf-8")


def test_codex_golden_fixtures_are_the_only_expected_reports() -> None:
    assert {path.stem for path in REPORTS.glob("*.txt")} == {name for name, _ in _CASES}


def test_repeated_token_count_snapshot_adds_nothing(tmp_path: Path) -> None:
    first = _usage(1000, 600, 50)
    second = _usage(1500, 900, 80)
    _write(
        tmp_path,
        "rollout-dedupe.jsonl",
        [
            _line(0, "turn_context", {"model": "gpt-5"}),
            _line(1, "event_msg", {"type": "task_started"}),
            _line(
                2,
                "event_msg",
                {
                    "type": "token_count",
                    "info": {"total_token_usage": first, "last_token_usage": first},
                },
            ),
            _line(
                3,
                "event_msg",
                {
                    "type": "token_count",
                    "info": {"total_token_usage": second, "last_token_usage": _usage(500, 300, 30)},
                },
            ),
            _line(
                4,
                "event_msg",
                {
                    "type": "token_count",
                    "info": {"total_token_usage": second, "last_token_usage": _usage(500, 300, 30)},
                },
            ),
        ],
    )
    (session,) = sessions(tmp_path)
    (turn,) = session.turns
    assert turn.usage == {"input": 600, "cache_read": 900, "cache_write": 0, "output": 80}


def test_unknown_model_reports_counts_only(tmp_path: Path) -> None:
    usage = _usage(1000, 0, 100)
    _write(
        tmp_path,
        "rollout-unpriced.jsonl",
        [
            _line(0, "turn_context", {"model": "gpt-nonexistent"}),
            _line(1, "event_msg", {"type": "task_started"}),
            _line(
                2,
                "event_msg",
                {
                    "type": "token_count",
                    "info": {"total_token_usage": usage, "last_token_usage": usage},
                },
            ),
        ],
    )
    result = report(tmp_path)
    assert result["dollars"] is None
    assert result["unpriced_models"] == ["gpt-nonexistent"]
    text = format_report(result, "audit")
    assert "no price for this model" in text
    assert "$" not in text


def test_priced_model_uses_the_price_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    usage = _usage(1000, 0, 100)
    _write(
        tmp_path,
        "rollout-priced.jsonl",
        [
            _line(0, "turn_context", {"model": "gpt-5"}),
            _line(1, "event_msg", {"type": "task_started"}),
            _line(
                2,
                "event_msg",
                {
                    "type": "token_count",
                    "info": {"total_token_usage": usage, "last_token_usage": usage},
                },
            ),
        ],
    )
    families = {"gpt-5": {"input": 1.0, "cache_read": 0.1, "output": 2.0}}
    monkeypatch.setattr("cfxd.routing._load_prices", lambda _path: {"families": families})
    result = report(tmp_path)
    assert result["dollars"] == 0.0012
    assert "OpenAI list prices" in format_report(result, "audit")


@pytest.mark.parametrize("command", ("audit", "week"))
def test_codex_empty_root_prints_one_line_and_exits_zero(
    command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli_main([command, "--source", "codex", "--root", str(tmp_path)]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("Codex (beta)")


def test_codex_week_refuses_share_and_save(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["week", "--source", "codex", "--save", "--state-dir", str(tmp_path)]
    assert cli_main([*args, "--root", str(tmp_path)]) == 2
    assert "Claude Code only" in capsys.readouterr().err


def test_source_registry_and_default_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    codex = get_source("codex")
    claude = get_source("claude")
    assert (codex.name, claude.name) == ("codex", "claude")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert codex.default_root() == Path.home() / ".codex" / "sessions"
    monkeypatch.setenv("CODEX_HOME", str(Path("C:/fake/codex")))
    assert codex.default_root() == Path("C:/fake/codex") / "sessions"
    assert claude.default_root() == Path.home() / ".claude" / "projects"
    with pytest.raises(ValueError):
        get_source("nope")


def test_claude_adapter_matches_the_golden_transcript() -> None:
    claude = get_source("claude")
    (parent,) = claude.sessions(GOLDEN.parent / "proj")
    assert len(parent.turns) == 5  # p1's two lines count once
    assert sum(turn.usage["output"] for turn in parent.turns) == 290
    assert sum(turn.usage["input"] for turn in parent.turns) == 2600
    assert sum(turn.tool_calls for turn in parent.turns) == 6
    assert {turn.model for turn in parent.turns} == {"claude-haiku-4-5"}
