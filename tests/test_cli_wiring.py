"""W2-01: the CLI leads with the limit metric, --share goes through ``cfxd/share.py``.

Every number here is computed from a hand-built transcript, so the assertions are exact.
The limit share is ``n/a`` for every tier (decision D-3: no official token denominator),
so the tests hold the fallback in place rather than a number that was never published.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from cfxd import __version__
from cfxd.brief_receipts import path_sha16
from cfxd.handoff_stats import format_report, summarise
from cfxd.limit_metric import limit_weighted_new_tokens, load_weights
from cfxd.share import render_share_text, validate_share
from cfxd.week import format_week, week

ROOT = Path(__file__).resolve().parents[1]
_MODEL = "claude-sonnet-4-5-20250929"


def _usage(input_tokens: int = 1000, output_tokens: int = 100) -> dict[str, int]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }


def _assistant(usage: dict[str, int], model: str = _MODEL) -> dict[str, Any]:
    return {"type": "assistant", "message": {"model": model, "content": [], "usage": usage}}


def _write(path: Path, lines: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def _session(root: Path, session_id: str, turns: int = 1) -> Path:
    return _write(root / "proj" / f"{session_id}.jsonl", [_assistant(_usage())] * turns)


def _run(args: list[str], state: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "CFX_HOME": str(state)}
    return subprocess.run(
        [sys.executable, "-m", "cfxd", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


# ------------------------------------------------------------------ limit metric lead


def test_week_leads_with_the_limit_weighted_metric_and_the_d3_fallback(tmp_path: Path) -> None:
    _session(tmp_path, "s1")

    report = week(tmp_path, 7)
    expected = limit_weighted_new_tokens(
        {
            "input_tokens": 1000,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 100,
        },
        load_weights(),
    )
    assert report["limit_weighted_new_tokens"] == pytest.approx(expected)
    assert report["limit_share"] is None

    text = format_week(report)
    lines = text.splitlines()
    assert lines[1].startswith("Limit-weighted new tokens: ")
    assert lines[2] == (
        "Limit share: n/a (no official 5-hour or weekly token limit for any plan tier; "
        "decision D-3)"
    )


def test_week_raw_tokens_are_behind_verbose(tmp_path: Path) -> None:
    _session(tmp_path, "s1")
    report = week(tmp_path, 7)

    plain = format_week(report)
    verbose = format_week(report, verbose=True)

    assert "Raw tokens" not in plain
    assert "First-turn context" not in plain
    assert "Sensitivity (w_read)" not in plain
    assert "Raw tokens (appendix): input 1,000" in verbose
    assert "Sensitivity (w_read): 1/30 -> " in verbose


def test_handoff_stats_leads_with_the_same_two_lines() -> None:
    data = {
        "sessions": 1,
        "children": [],
        "costed": [],
        "excluded": 0,
        "budgets": [],
        "turns": 4,
        "usage": {
            "input_tokens": 1000,
            "cache_creation_input_tokens": 2000,
            "cache_read_input_tokens": 0,
            "output_tokens": 100,
        },
    }

    report = summarise(data)
    expected = limit_weighted_new_tokens(data["usage"], load_weights())
    assert report["limit_weighted_new_tokens"] == pytest.approx(expected)
    assert report["limit_share"] is None

    text = format_report(report)
    lines = text.splitlines()
    assert lines[0] == f"cfxd handoff-stats {__version__} (aggregates only, one machine)"
    assert lines[1].startswith("Limit-weighted new tokens: ")
    assert lines[2].startswith("Limit share: n/a (no official 5-hour or weekly token limit")
    assert "Raw tokens" not in text
    assert "Raw tokens (appendix)" in format_report(report, verbose=True)


# ------------------------------------------------------------------ --share payloads


def test_week_share_text_and_json_carry_the_same_validated_payload(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    state = tmp_path / "state"
    _session(root, "s1")
    child = _write(
        root / "proj" / "s1" / "subagents" / "agent-kid.jsonl",
        [_assistant(_usage(input_tokens=200, output_tokens=20))],
    )
    assert child.is_file()

    text = _run(["week", "--root", str(root), "--days", "7", "--share"], state)
    as_json = _run(["week", "--root", str(root), "--days", "7", "--share", "--json"], state)
    assert text.returncode == 0, text.stderr
    assert as_json.returncode == 0, as_json.stderr
    payload = validate_share(json.loads(as_json.stdout)["cf_share"])

    assert payload["schema"] == "cf-share/2"
    assert payload["plugin_version"]
    assert payload["metric"] == "limit-weighted/1"
    assert payload["period_days"] == 7
    assert payload["sessions"] == 1
    assert payload["children"] == 1
    assert payload["children_by_model_family"] == {"opus": 0, "sonnet": 1, "haiku": 0}
    # Parent (1,000 in / 100 out) plus the child (200 in / 20 out): 1,200 + 5 * 120.
    assert payload["new_token_equivalents"] == pytest.approx(
        limit_weighted_new_tokens(
            {
                "input_tokens": 1200,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "output_tokens": 120,
            },
            load_weights(),
        )
    )
    assert payload["consent_aggregate_publish"] is False
    assert render_share_text(payload).strip() in text.stdout


def test_week_share_payload_is_absent_without_the_flag(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    _session(root, "s1")

    as_json = _run(["week", "--root", str(root), "--days", "7", "--json"], tmp_path / "state")

    assert as_json.returncode == 0, as_json.stderr
    assert "cf_share" not in json.loads(as_json.stdout)


# ------------------------------------------------------------------ brief paste-safe


def test_brief_paste_safe_replaces_paths_with_stable_indexes(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    state = tmp_path / "state"
    canary = "CANARYWIRE-FILE-1"
    target = str(tmp_path / canary / "main.py")
    sid = "sid-paste-safe"
    _write(
        root / "proj" / f"{sid}.jsonl",
        [
            {
                "type": "assistant",
                "message": {
                    "model": _MODEL,
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_canary",
                            "name": "Agent",
                            "input": {"subagent_type": "explore", "prompt": "look"},
                        }
                    ],
                    "usage": _usage(),
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_canary", "content": "done"}
                    ]
                },
                "toolUseResult": {"status": "completed", "agentId": "kid1"},
            },
        ],
    )
    packet = (
        '<context-fabric-brief v="0">\n'
        "Files the parent session already opened:\n"
        f"- {target} | 10 lines | read\n"
        "</context-fabric-brief>"
    )
    _write(
        root / "proj" / sid / "subagents" / "agent-kid1.jsonl",
        [
            {
                "type": "user",
                "message": {"content": [{"type": "text", "text": "look\n\n" + packet}]},
            },
            {
                "type": "assistant",
                "message": {
                    "model": _MODEL,
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "childread1",
                            "name": "Read",
                            "input": {"file_path": target},
                        }
                    ],
                    "usage": _usage(200, 20),
                },
            },
        ],
    )
    state.mkdir(parents=True, exist_ok=True)
    _write(
        state / "briefs.jsonl",
        [
            {
                "session_id": sid,
                "tool_use_id": "toolu_canary",
                "subagent_type": "explore",
                "decision": "briefed",
                "skip_reason": None,
                "prompt_bytes": 4,
                "packet_bytes": len(packet),
                "counts": {"files": 1},
                "budget_bytes": 2000,
                "target_model": _MODEL,
                "items": [{"p": path_sha16(target), "r": ["read"], "x": [[1, 10]]}],
            }
        ],
    )

    plain = _run(["brief", "--root", str(root), "--state-dir", str(state)], state)
    safe = _run(["brief", "--root", str(root), "--state-dir", str(state), "--paste-safe"], state)
    safe_json = _run(
        [
            "brief",
            "--root",
            str(root),
            "--state-dir",
            str(state),
            "--paste-safe",
            "--json",
        ],
        state,
    )

    assert plain.returncode == safe.returncode == safe_json.returncode == 0, safe.stderr
    assert plain.stdout.startswith(f"cfxd brief {__version__}: 1 handoffs")
    assert canary in plain.stdout  # the default view keeps relative paths
    assert "file-1" in safe.stdout
    assert canary not in safe.stdout
    details = json.loads(safe_json.stdout)["details"]
    assert [item["path"] for item in details[0]["items"]] == ["file-1"]


# ------------------------------------------------------------------ digest flag


def test_include_name_digests_replaces_the_old_flag_and_warns(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    state = tmp_path / "state"
    _session(root, "s1")
    state.mkdir(parents=True, exist_ok=True)
    _write(
        state / "briefs.jsonl",
        [
            {
                "session_id": "s1",
                "tool_use_id": "toolu_canary",
                "subagent_type": "explore",
                "decision": "skipped",
                "skip_reason": "no files",
                "prompt_bytes": 4,
                "packet_bytes": 0,
                "budget_bytes": 2000,
                "target_model": _MODEL,
            }
        ],
    )

    plain = _run(
        ["brief", "--root", str(root), "--state-dir", str(state), "--share", "--json"], state
    )
    flagged = _run(
        [
            "brief",
            "--root",
            str(root),
            "--state-dir",
            str(state),
            "--share",
            "--include-name-digests",
            "--json",
        ],
        state,
    )
    old = _run(
        [
            "brief",
            "--root",
            str(root),
            "--state-dir",
            str(state),
            "--share-digests",
            "--json",
        ],
        state,
    )

    assert plain.returncode == flagged.returncode == old.returncode == 0
    assert "digests" not in json.loads(plain.stdout)
    assert "digests" in json.loads(flagged.stdout)
    assert "pseudonymous, not anonymous" in flagged.stderr
    assert "pseudonymous, not anonymous" in old.stderr
    assert "pseudonymous" not in plain.stderr


@pytest.mark.parametrize(
    "command",
    [["week"], ["handoff-stats"], ["brief"], ["diet"], ["audit"], ["checkup"], ["doctor"]],
)
def test_every_command_answers_version_with_the_plugin_version(command: list[str]) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "cfxd", *command, "--version"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert __version__ in result.stdout


# ------------------------------------------------------------------ retired features

_GOLDEN = ROOT / "tests" / "golden"
_RETIRED_RE = re.compile(r"\b(?:fold|bound|checkpoint)\b", re.IGNORECASE)
_RETIRED_CASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("week", ("week", "--root", str(_GOLDEN), "--days", "36500", "--state-dir", "{state}")),
    (
        "week-share",
        ("week", "--root", str(_GOLDEN), "--days", "36500", "--state-dir", "{state}", "--share"),
    ),
    ("ledger", ("ledger", "--root", str(_GOLDEN), "--days", "36500", "--state-dir", "{state}")),
    ("checkup", ("checkup", "--root", str(_GOLDEN), "--since", "all")),
    ("doctor", ("doctor", "--state-dir", "{state}")),
    ("handoff-stats", ("handoff-stats", "--root", str(_GOLDEN))),
)


@pytest.mark.parametrize(("name", "argv"), _RETIRED_CASES, ids=[c[0] for c in _RETIRED_CASES])
def test_tester_output_never_names_a_retired_feature(
    name: str, argv: tuple[str, ...], tmp_path: Path
) -> None:
    state = tmp_path / "state"
    result = _run([part.format(state=state) for part in argv], state)
    output = result.stdout + result.stderr

    assert not _RETIRED_RE.findall(output), f"{name}: {_RETIRED_RE.findall(output)}"
    assert "scripts/state_audit.py" not in output
    if name != "doctor":  # doctor exits 1 without a running daemon; its text still matters
        assert result.returncode == 0, result.stderr
