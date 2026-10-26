"""cfxd refetch tests: re-read after a compaction boundary, exact numbers only."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from cfxd.refetch import format_refetch, refetch
from cfxd.replay import replay

ROOT = Path(__file__).resolve().parents[1]
BOUNDARY = {"type": "system", "subtype": "compact_boundary"}


def _usage(input_tokens: int = 0, output_tokens: int = 0, cc: int = 0, cr: int = 0) -> dict:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": cc,
        "cache_read_input_tokens": cr,
    }


def _assistant(*blocks: dict, usage: dict | None = None, ts: str | None = None) -> dict:
    obj = {"type": "assistant", "message": {"content": list(blocks), "usage": usage or {}}}
    if ts is not None:
        obj["timestamp"] = ts
    return obj


def _user(*blocks: dict) -> dict:
    return {"type": "user", "message": {"content": list(blocks)}}


def _tool_use(tool_id: str, name: str, inp: dict) -> dict:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": inp}


def _tool_result(tool_id: str, content: object) -> dict:
    return {"type": "tool_result", "tool_use_id": tool_id, "content": content}


def _session(root: Path, project: str, session_id: str, lines: list[dict]) -> Path:
    path = root / project / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def _compaction_lines(reread_bytes: int | None) -> list[dict]:
    """C_before=1000 at the last turn before, C_after=300 after: D=700."""
    lines = [
        _assistant(_tool_use("r0", "Read", {"file_path": "/held.txt"}), usage=_usage(1000)),
        _user(_tool_result("r0", "x" * 100)),
        _assistant(usage=_usage(cr=1000)),
        {**BOUNDARY, "compactMetadata": {"trigger": "auto"}},
    ]
    if reread_bytes is None:
        lines.append(_assistant(usage=_usage(cr=300)))
    else:
        lines.append(
            _assistant(_tool_use("r1", "Read", {"file_path": "/held.txt"}), usage=_usage(cr=300))
        )
        lines.append(_user(_tool_result("r1", "x" * reread_bytes)))
    lines.append(_assistant(usage=_usage(cr=300)))
    return lines


@pytest.mark.parametrize(
    ("reread_bytes", "expected_f", "expected_share"),
    [(None, 0.0, 0.0), (1400, 0.5, 0.0), (2800, 1.0, 1.0)],
)
def test_reread_fractions_of_dropped_context(
    tmp_path: Path, reread_bytes: int | None, expected_f: float, expected_share: float
) -> None:
    _session(tmp_path, "proj", "s1", _compaction_lines(reread_bytes))
    report = refetch(tmp_path, "all")

    assert report["events_analyzed"] == 1
    assert report["d_tokens"]["median"] == 700
    assert report["f"]["median"] == pytest.approx(expected_f)
    assert report["share_f_above_0_5"] == pytest.approx(expected_share)
    event = report["events"][0]
    assert event["source"] == "parent"
    assert event["trigger"] == "auto"
    assert event["c_before"] == 1000
    assert event["c_after"] == 300
    assert event["d_tokens"] == 700


def test_reread_outside_window_is_ignored(tmp_path: Path) -> None:
    lines = [
        _assistant(_tool_use("r0", "Read", {"file_path": "/w.txt"}), usage=_usage(1000)),
        _user(_tool_result("r0", "x" * 10)),
        _assistant(usage=_usage(cr=1000)),
        BOUNDARY,
        _assistant(usage=_usage(cr=300)),
        _assistant(usage=_usage(cr=300)),
        _assistant(usage=_usage(cr=300)),
        _assistant(_tool_use("r1", "Read", {"file_path": "/w.txt"}), usage=_usage(cr=300)),
        _user(_tool_result("r1", "x" * 1400)),
        _assistant(usage=_usage(cr=300)),
    ]
    _session(tmp_path, "proj", "s1", lines)

    narrow = refetch(tmp_path, "all", window=3)
    assert narrow["events_analyzed"] == 1
    assert narrow["f"]["median"] == 0.0

    wide = refetch(tmp_path, "all", window=6)
    assert wide["f"]["median"] == pytest.approx(0.5)


def test_edit_then_read_counts_as_reread(tmp_path: Path) -> None:
    lines = [
        _assistant(_tool_use("e0", "Edit", {"file_path": "/e.txt"}), usage=_usage(1000)),
        _user(_tool_result("e0", "done")),
        _assistant(usage=_usage(cr=1000)),
        {**BOUNDARY, "compactMetadata": {"trigger": "manual"}},
        _assistant(_tool_use("r1", "Read", {"file_path": "/e.txt"}), usage=_usage(cr=300)),
        _user(_tool_result("r1", "x" * 1400)),
        _assistant(usage=_usage(cr=300)),
    ]
    _session(tmp_path, "proj", "s1", lines)
    report = refetch(tmp_path, "all")

    assert report["f"]["median"] == pytest.approx(0.5)
    assert report["by_trigger"]["manual"]["events"] == 1


def test_repeated_bash_signature_counts_each_result(tmp_path: Path) -> None:
    command = {"command": "make all"}
    small = {"command": "echo small"}
    lines = [
        _assistant(_tool_use("b0", "Bash", command), usage=_usage(1000)),
        _user(_tool_result("b0", "x" * 3000)),
        _assistant(_tool_use("s0", "Bash", small), usage=_usage(cr=1000)),
        _user(_tool_result("s0", "y" * 100)),
        _assistant(usage=_usage(cr=1000)),
        BOUNDARY,
        _assistant(
            _tool_use("b1", "Bash", command),
            _tool_use("b2", "Bash", command),
            _tool_use("s1", "Bash", small),
            usage=_usage(cr=300),
        ),
        _user(
            _tool_result("b1", "x" * 700),
            _tool_result("b2", "x" * 700),
            _tool_result("s1", "y" * 2800),
        ),
        _assistant(usage=_usage(cr=300)),
    ]
    _session(tmp_path, "proj", "s1", lines)
    report = refetch(tmp_path, "all")

    # 2 * 700 bytes re-read = 350 tokens out of D=700 -> f=0.5.
    # The small original result is not held, so its 2800-byte repeat is ignored.
    assert report["events_analyzed"] == 1
    assert report["f"]["median"] == pytest.approx(0.5)


def test_truncated_window_flagged(tmp_path: Path) -> None:
    _session(tmp_path, "proj", "s1", _compaction_lines(None))
    report = refetch(tmp_path, "all", window=20)

    assert report["window_truncated"] == 1
    event = report["events"][0]
    assert event["truncated"] is True
    assert event["turns_in_window"] == 2


def test_nonpositive_drop_and_missing_usage_skipped(tmp_path: Path) -> None:
    _session(
        tmp_path,
        "proj",
        "zero",
        [_assistant(usage=_usage(1000)), BOUNDARY, _assistant(usage=_usage(1000))],
    )
    _session(
        tmp_path,
        "proj",
        "missing",
        [_assistant(usage=_usage(1000)), BOUNDARY, _assistant(usage={})],
    )
    report = refetch(tmp_path, "all")

    assert report["events_analyzed"] == 0
    assert report["events_skipped"] == {"missing_usage": 1, "nonpositive_drop": 1}
    assert report["saving_at_measured_f"] == {"available": False}


def _long_lines() -> list[dict]:
    lines = [
        _assistant(_tool_use("r0", "Read", {"file_path": "/s.txt"}), usage=_usage(1000)),
        _user(_tool_result("r0", "x" * 10)),
        _assistant(usage=_usage(cr=1000)),
        BOUNDARY,
        _assistant(_tool_use("r1", "Read", {"file_path": "/s.txt"}), usage=_usage(cr=300)),
        _user(_tool_result("r1", "x" * 1400)),
    ]
    for i in range(3, 45):
        lines.append(_assistant(usage=_usage(cr=300 + 10 * (i - 3))))
    return lines


def test_saving_at_measured_f_matches_replay_model(tmp_path: Path) -> None:
    _session(tmp_path, "proj", "s1", _long_lines())
    report = refetch(tmp_path, "all")
    saving = report["saving_at_measured_f"]

    assert saving["f_median"] == pytest.approx(0.5)
    assert saving["f_p75"] == pytest.approx(0.5)
    assert len(saving["rows"]) == 4

    model = replay(
        tmp_path,
        "all",
        ks=(),
        batches=(),
        reset_ks=(40,),
        summary_tokens=(2000, 5000),
        refetch=(saving["f_median"], saving["f_p75"]),
    )
    expected = {
        (policy["summary_tokens"], variant["variant"], variant["f"]): (
            variant["pct_saved"],
            variant["pct_saved_affected"],
        )
        for policy in model["reset_policies"]
        for variant in policy["variants"]
    }
    got = {
        (row["summary_tokens"], row["variant"], row["f"]): (
            row["pct_saved"],
            row["pct_saved_affected"],
        )
        for row in saving["rows"]
    }
    assert got == expected


def test_subagent_boundary_separately_labelled(tmp_path: Path) -> None:
    _session(tmp_path, "proj/subagents", "a1", _compaction_lines(None))
    report = refetch(tmp_path, "all")

    assert report["sessions"] == {"parents": 0, "subagents": 1}
    assert report["by_source"]["subagent"]["events"] == 1
    assert report["events"][0]["source"] == "subagent"


def test_canary_strings_never_leak(tmp_path: Path) -> None:
    path_canary = "CANARYPATH-abcd1234"
    cmd_canary = "CANARYCMD-efgh5678"
    out_canary = "CANARYOUT-ijkl9012"
    prompt_canary = "CANARYPROMPT-mnop3456"
    lines = [
        _assistant(
            {"type": "text", "text": prompt_canary},
            _tool_use("r0", "Read", {"file_path": f"/{path_canary}"}),
            usage=_usage(1000),
        ),
        _user(_tool_result("r0", out_canary * 100)),
        _assistant(_tool_use("b0", "Bash", {"command": cmd_canary}), usage=_usage(cr=1000)),
        _user(_tool_result("b0", cmd_canary * 100)),
        BOUNDARY,
        _assistant(_tool_use("r1", "Read", {"file_path": f"/{path_canary}"}), usage=_usage(cr=300)),
        _user(_tool_result("r1", out_canary * 100)),
        _assistant(usage=_usage(cr=300)),
    ]
    _session(tmp_path, "proj", "s1", lines)

    for extra in ([], ["--json"]):
        result = subprocess.run(
            [sys.executable, "-m", "cfxd", "refetch", "--root", str(tmp_path), *extra],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        for canary in (path_canary, cmd_canary, out_canary, prompt_canary):
            assert canary not in result.stdout
            assert canary not in result.stderr


def test_empty_root(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    report = refetch(empty, "all")
    assert report["events_analyzed"] == 0
    assert format_refetch(report, empty.name) == "No transcripts found under empty."


def test_cli_json_and_human_output(tmp_path: Path) -> None:
    _session(tmp_path, "proj", "s1", _compaction_lines(1400))

    result = subprocess.run(
        [sys.executable, "-m", "cfxd", "refetch", "--root", str(tmp_path), "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["events_analyzed"] == 1
    assert data["f"]["median"] == pytest.approx(0.5)
    assert data["saving_at_measured_f"]["kill_threshold"]["pct"] == 20.0

    human = subprocess.run(
        [sys.executable, "-m", "cfxd", "refetch", "--root", str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert human.returncode == 0, human.stderr
    assert "Kill threshold for the checkpoint feature" in human.stdout
    assert "This report contains counts and numbers only" in human.stdout
    assert "Saving at measured f" in human.stdout
