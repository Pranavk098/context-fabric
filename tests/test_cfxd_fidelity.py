"""cfxd fidelity tests: summary recall and mention precision, exact numbers only."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from cfxd.fidelity import fidelity, format_fidelity

ROOT = Path(__file__).resolve().parents[1]


def _usage(input_tokens: int = 0) -> dict:
    return {
        "input_tokens": input_tokens,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


def _assistant(*blocks: dict, usage: dict | None = None) -> dict:
    return {"type": "assistant", "message": {"content": list(blocks), "usage": usage or {}}}


def _user(*blocks: dict) -> dict:
    return {"type": "user", "message": {"content": list(blocks)}}


def _tool_use(tool_id: str, name: str, inp: dict) -> dict:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": inp}


def _tool_result(tool_id: str, content: object) -> dict:
    return {"type": "tool_result", "tool_use_id": tool_id, "content": content}


def _summary(text: str, trigger: str | None = "auto") -> dict:
    obj = {
        "type": "user",
        "isCompactSummary": True,
        "message": {"content": text},
    }
    if trigger is not None:
        obj["compactMetadata"] = {"trigger": trigger}
    return obj


def _session(root: Path, project: str, session_id: str, lines: list[dict]) -> Path:
    path = root / project / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def _reads(*paths: str) -> list[dict]:
    lines: list[dict] = []
    for i, path in enumerate(paths):
        lines.append(
            _assistant(_tool_use(f"r{i}", "Read", {"file_path": path}), usage=_usage(1000))
        )
        lines.append(_user(_tool_result(f"r{i}", "x")))
    return lines


def _two_reads(summary_text: str) -> list[dict]:
    return [*_reads("/repo/a.py", "/repo/b.py"), _summary(summary_text)]


def test_summary_naming_all_held_files(tmp_path: Path) -> None:
    text = "Read `/repo/a.py` and `/repo/b.py`."
    _session(tmp_path, "proj", "s1", _two_reads(text))
    report = fidelity(tmp_path, "all")

    assert report["events_analyzed"] == 1
    event = report["events"][0]
    assert event["held_files"] == 2
    assert event["mentions"] == 2
    assert event["recall"] == 1.0
    assert event["recall_recent20"] == 1.0
    assert event["precision"] == 1.0
    assert event["unseen_mentions"] == 0
    assert event["checkpoint_recall_bound"] == 1.0
    assert event["checkpoint_precision"] == 1.0
    assert event["summary_tokens"] == pytest.approx(round(len(text.encode("utf-8")) / 4.0, 1))
    assert report["recall"]["median"] == 1.0
    assert report["share_with_unseen_mentions"] == 0.0


def test_summary_naming_half(tmp_path: Path) -> None:
    _session(tmp_path, "proj", "s1", _two_reads("Only `/repo/a.py` changed."))
    report = fidelity(tmp_path, "all")

    event = report["events"][0]
    assert event["recall"] == 0.5
    assert event["recall_recent20"] == 0.5
    assert event["precision"] == 1.0
    assert event["unseen_mentions"] == 0


def test_summary_naming_one_unseen_path(tmp_path: Path) -> None:
    _session(tmp_path, "proj", "s1", _two_reads("Read `/repo/a.py` and `/repo/c.py`."))
    report = fidelity(tmp_path, "all")

    event = report["events"][0]
    assert event["recall"] == 0.5
    assert event["precision"] == 0.5
    assert event["unseen_mentions"] == 1
    assert report["events_with_unseen_mentions"] == 1
    assert report["share_with_unseen_mentions"] == 1.0
    assert report["unseen_mentions"]["median"] == 1.0


def test_summary_naming_no_path(tmp_path: Path) -> None:
    _session(tmp_path, "proj", "s1", _two_reads("Nothing changed."))
    report = fidelity(tmp_path, "all")

    event = report["events"][0]
    assert event["mentions"] == 0
    assert event["recall"] == 0.0
    assert event["recall_recent20"] == 0.0
    assert event["precision"] == 1.0
    assert event["unseen_mentions"] == 0
    assert report["events_with_no_mentions"] == 1


def test_basename_and_suffix_matching(tmp_path: Path) -> None:
    lines = [
        *_reads("/repo/src/app.py", "/repo/a/b.py", "/repo/x/util.py"),
        _summary("Touched app.py, a/b.py and src/util.py."),
    ]
    _session(tmp_path, "proj", "s1", lines)
    report = fidelity(tmp_path, "all")

    event = report["events"][0]
    assert event["held_files"] == 3
    assert event["mentions"] == 3
    assert event["recall"] == 1.0
    assert event["precision"] == 1.0
    assert event["unseen_mentions"] == 0


def test_windows_backslash_paths(tmp_path: Path) -> None:
    lines = [
        *_reads("C:\\repo\\src\\X.py"),
        _summary("Read `C:\\repo\\src\\x.py` and SRC/X.PY."),
    ]
    _session(tmp_path, "proj", "s1", lines)
    report = fidelity(tmp_path, "all")

    event = report["events"][0]
    assert event["held_files"] == 1
    assert event["mentions"] == 2
    assert event["recall"] == 1.0
    assert event["precision"] == 1.0
    assert event["unseen_mentions"] == 0


def test_recent_twenty_subset_with_thirty_held_files(tmp_path: Path) -> None:
    paths = [f"/repo/f{i:02d}.py" for i in range(1, 31)]
    text = " ".join(f"`/repo/f{i:02d}.py`" for i in range(21, 31))
    _session(tmp_path, "proj", "s1", [*_reads(*paths), _summary(text)])
    report = fidelity(tmp_path, "all")

    event = report["events"][0]
    assert event["held_files"] == 30
    assert event["mentions"] == 10
    assert event["recall"] == pytest.approx(10 / 30, abs=1e-4)
    assert event["recall_recent20"] == pytest.approx(10 / 20, abs=1e-4)
    assert event["checkpoint_recall_bound"] == pytest.approx(20 / 30, abs=1e-4)
    assert event["precision"] == 1.0


def test_missing_summary_text_and_no_held_files_skipped(tmp_path: Path) -> None:
    _session(
        tmp_path,
        "proj",
        "empty",
        [
            *_reads("/repo/a.py"),
            _summary(""),
            {"type": "system", "subtype": "compact_boundary"},
        ],
    )
    _session(tmp_path, "proj", "noheld", [_summary("Read /repo/a.py.")])
    report = fidelity(tmp_path, "all")

    assert report["events_analyzed"] == 0
    assert report["events_skipped"] == {"no_summary_text": 2, "no_held_files": 1}
    assert report["what_this_means"] == [
        "No compaction summary with held files was analysed, so there is nothing to compare."
    ]


def test_trigger_split(tmp_path: Path) -> None:
    _session(tmp_path, "proj", "auto", [*_reads("/repo/a.py"), _summary("`/repo/a.py`", "auto")])
    _session(
        tmp_path, "proj", "manual", [*_reads("/repo/a.py"), _summary("`/repo/a.py`", "manual")]
    )
    _session(
        tmp_path,
        "proj",
        "boundary",
        [
            *_reads("/repo/a.py"),
            {
                "type": "system",
                "subtype": "compact_boundary",
                "compactMetadata": {"trigger": "manual"},
            },
            _summary("`/repo/a.py`", None),
        ],
    )
    report = fidelity(tmp_path, "all")

    assert report["by_trigger"]["auto"]["events"] == 1
    assert report["by_trigger"]["manual"]["events"] == 2
    assert report["by_trigger"]["auto"]["recall_median"] == 1.0


def test_canary_strings_never_leak_and_cli_shape(tmp_path: Path) -> None:
    summary_canary = "CANARYSUMMARY-abcd1234"
    path_canary = "CANARYPATH-efgh5678"
    out_canary = "CANARYOUT-ijkl9012"
    lines = [
        _assistant(
            _tool_use("r0", "Read", {"file_path": f"/repo/{path_canary}.py"}), usage=_usage(1000)
        ),
        _user(_tool_result("r0", out_canary * 10)),
        _summary(f"Read `/repo/{path_canary}.py`. {summary_canary}"),
    ]
    _session(tmp_path, "proj", "s1", lines)

    for extra in ([], ["--json"]):
        result = subprocess.run(
            [sys.executable, "-m", "cfxd", "fidelity", "--root", str(tmp_path), *extra],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        for canary in (summary_canary, path_canary, out_canary):
            assert canary not in result.stdout
            assert canary not in result.stderr

    result = subprocess.run(
        [sys.executable, "-m", "cfxd", "fidelity", "--root", str(tmp_path), "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    data = json.loads(result.stdout)
    assert data["events_analyzed"] == 1
    assert data["recall"]["median"] == 1.0
    assert data["checkpoint"]["precision"] == 1.0
    assert set(data["events"][0]) == {
        "trigger",
        "summary_tokens",
        "mentions",
        "held_files",
        "recall",
        "recall_recent20",
        "precision",
        "unseen_mentions",
        "checkpoint_recall_bound",
        "checkpoint_precision",
    }

    human = subprocess.run(
        [sys.executable, "-m", "cfxd", "fidelity", "--root", str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert human.returncode == 0, human.stderr
    assert "What this means:" in human.stdout
    assert "Checkpoint comparison" in human.stdout
    assert "counts and numbers only" in human.stdout


def test_empty_root(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    report = fidelity(empty, "all")
    assert report["events_analyzed"] == 0
    assert format_fidelity(report, empty.name) == "No transcripts found under empty."
