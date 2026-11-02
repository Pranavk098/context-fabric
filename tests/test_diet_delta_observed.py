"""`cfxd diet --apply` reversibility, and the observed before/after it leaves behind.

The observed figure is per project and labelled *observed* (R9): it compares the first-turn
prefix of sessions that started after an apply with the same project's pre-apply baseline.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from cfxd.daemon import percentile

ROOT = Path(__file__).resolve().parents[1]

FAKE_CLAUDE = """
import json, os, sys, time
from pathlib import Path

state = Path(os.environ["FAKE_CLAUDE_STATE"])
argv = sys.argv[1:]
plugins = state / "plugins.json"
calls = state / "calls.log"
with open(calls, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\\n")

def load():
    return json.loads(plugins.read_text(encoding="utf-8"))

def save(data):
    plugins.write_text(json.dumps(data), encoding="utf-8")

def flip(pid, enabled):
    data = load()
    for p in data:
        if p.get("id") == pid:
            p["enabled"] = enabled
    save(data)

if argv[:3] == ["plugin", "list", "--json"]:
    print(json.dumps(load()))
    sys.exit(0)
if argv[:2] in (["plugin", "disable"], ["plugin", "enable"]):
    flip(argv[2], argv[1] == "enable")
    done = state / "command-count"
    n = int(done.read_text()) if done.exists() else 0
    done.write_text(str(n + 1))
    if os.environ.get("FAKE_CLAUDE_STALL_AFTER_FIRST") == "1" and n == 0:
        # The CLI change landed; hold here so the caller can kill cfxd before it records it.
        (state / "stalled").write_text("1")
        time.sleep(10)
    sys.exit(0)
sys.exit(2)
"""


def _write_claude(tmp_path: Path) -> Path:
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE, encoding="utf-8")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    if os.name == "nt":
        exe = bindir / "claude.cmd"
        exe.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        exe = bindir / "claude"
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        exe.chmod(0o755)
    return exe


def _assistant(input_tokens: int) -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [],
            "usage": {"input_tokens": input_tokens, "cache_creation_input_tokens": 0},
        },
    }


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.root = tmp_path / "transcripts"
        self.root.mkdir()
        self.cfx = tmp_path / "cfxhome"
        self.state = tmp_path / "state"
        self.state.mkdir()
        self.plugins_file = self.state / "plugins.json"
        self.plugins_file.write_text(
            json.dumps([{"id": "acme@market", "scope": "user", "enabled": True}]),
            encoding="utf-8",
        )
        self.env = {
            **os.environ,
            "CFX_HOME": str(self.cfx),
            "CFX_CLAUDE": str(_write_claude(tmp_path)),
            "FAKE_CLAUDE_STATE": str(self.state),
        }

    def session(self, project: str, name: str, first_turn: int, mtime: float) -> Path:
        lines = [
            {
                "type": "attachment",
                "attachment": {
                    "type": "skill_listing",
                    "names": ["acme:a"],
                    "content": "- acme:a: description of acme:a\n",
                },
            },
            _assistant(first_turn),
        ]
        path = self.root / project / f"{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
        os.utime(path, (mtime, mtime))
        return path

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "cfxd", "diet", "--root", str(self.root), *args],
            cwd=ROOT,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def baseline(self) -> dict:
        path = self.cfx / "diet" / "baseline.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def records(self) -> list[Path]:
        return sorted((self.cfx / "diet").glob("applied-*.json"))

    def calls(self) -> list[list[str]]:
        path = self.state / "calls.log"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def enables(self) -> list[list[str]]:
        return [c for c in self.calls() if c[:2] == ["plugin", "enable"]]

    def enabled(self, plugin_id: str) -> bool:
        data = json.loads(self.plugins_file.read_text(encoding="utf-8"))
        return next(p["enabled"] for p in data if p["id"] == plugin_id)


def _seed(env: Env, project: str, count: int, first_turn: int, built_at: float) -> None:
    for i in range(count):
        env.session(project, f"{project}{i}", first_turn, built_at + i)


# --- revert first: apply and undo stay safe ------------------------------------------------


def test_apply_confirmation_prints_the_undo_command(tmp_path: Path) -> None:
    env = Env(tmp_path)
    _seed(env, "proj-a", 4, 1200, time.time() - 3600)
    result = env.run("--apply", "--yes")
    assert result.returncode == 0, result.stderr
    assert env.enabled("acme@market") is False
    assert "cfxd diet --undo" in result.stdout


def test_undo_with_no_record_changes_nothing(tmp_path: Path) -> None:
    env = Env(tmp_path)
    result = env.run("--undo")
    assert result.returncode == 0, result.stderr
    assert "nothing to undo" in result.stdout
    assert env.enabled("acme@market") is True
    assert env.enables() == []


def test_interrupted_apply_is_undoable(tmp_path: Path) -> None:
    env = Env(tmp_path)
    _seed(env, "proj-a", 4, 1200, time.time() - 3600)
    env.env["FAKE_CLAUDE_STALL_AFTER_FIRST"] = "1"

    proc = subprocess.Popen(
        [sys.executable, "-m", "cfxd", "diet", "--root", str(env.root), "--apply", "--yes"],
        cwd=ROOT,
        env=env.env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    marker = env.state / "stalled"
    deadline = time.time() + 30
    while not marker.exists() and proc.poll() is None and time.time() < deadline:
        time.sleep(0.05)
    assert marker.exists(), "apply never reached the first disable"
    proc.kill()
    proc.wait(timeout=30)

    (record,) = env.records()
    data = json.loads(record.read_text(encoding="utf-8"))
    # The disable landed but the record never learned about it: the record must still undo it.
    assert env.enabled("acme@market") is False
    assert all(entry["applied"] is None for entry in data["plugins"])

    result = env.run("--undo")
    assert result.returncode == 0, result.stderr
    assert env.enabled("acme@market") is True
    assert len(env.enables()) == 1


def test_tester_guide_shows_the_undo_command_beside_every_apply_step() -> None:
    lines = (ROOT / "docs" / "testers" / "diet.md").read_text(encoding="utf-8").splitlines()
    command = "uv run cfxd diet --apply"
    applies = [i for i, line in enumerate(lines) if command in line]
    assert len(applies) >= 2
    for index in applies:
        window = "\n".join(lines[index : index + 3])
        assert "diet --undo" in window, lines[index]


# --- the observed before/after ---------------------------------------------------------------


def test_apply_stores_the_last_sessions_first_turn_per_project_as_a_hash(tmp_path: Path) -> None:
    env = Env(tmp_path)
    built_at = time.time() - 3600
    for i in range(12):
        env.session("proj-a", f"a{i}", (i + 1) * 100, built_at + i)
    result = env.run("--apply", "--yes")
    assert result.returncode == 0, result.stderr

    data = env.baseline()["projects"]
    assert len(data) == 1
    ((key, entry),) = data.items()
    assert len(key) == 16 and all(c in "0123456789abcdef" for c in key)
    assert entry["n"] == 10
    # The last ten sessions only: 300..1200, not the oldest two.
    last_ten = [float(v) for v in range(300, 1300, 100)]
    assert entry["first_turn_tokens_median"] == round(percentile(last_ten, 50))
    text = (env.cfx / "diet" / "baseline.json").read_text(encoding="utf-8")
    assert "proj-a" not in text
    assert str(tmp_path) not in text


def _applied_env(tmp_path: Path) -> tuple[Env, float]:
    env = Env(tmp_path)
    _seed(env, "proj-a", 4, 1000, time.time() - 3600)
    result = env.run("--apply", "--yes")
    assert result.returncode == 0, result.stderr
    (entry,) = env.baseline()["projects"].values()
    return env, float(entry["applied_at"])


def test_below_three_sessions_after_apply_prints_no_number(tmp_path: Path) -> None:
    env, applied_at = _applied_env(tmp_path)
    for i in range(2):
        env.session("proj-a", f"after{i}", 400, applied_at + 60 + i)

    result = env.run()
    assert result.returncode == 0, result.stderr
    assert "observed (this machine, this project, 4 sessions)" in result.stdout
    assert "not enough sessions yet" in result.stdout
    assert "tokens after" not in result.stdout
    assert "smaller" not in result.stdout and "larger" not in result.stdout


def test_observed_compares_the_same_project_only_and_never_says_saved(tmp_path: Path) -> None:
    env = Env(tmp_path)
    built_at = time.time() - 3600
    _seed(env, "proj-a", 4, 1000, built_at)
    _seed(env, "proj-b", 3, 9000, built_at)
    assert env.run("--apply", "--yes").returncode == 0
    (entry,) = [
        e for e in env.baseline()["projects"].values() if e["first_turn_tokens_median"] == 1000
    ]
    applied_at = float(entry["applied_at"])

    for i in range(3):
        env.session("proj-a", f"after{i}", 400, applied_at + 60 + i)

    result = env.run()
    assert result.returncode == 0, result.stderr
    out = result.stdout
    # Project A: 1,000 before, 400 after, and project B's 9,000 never pools in.
    assert "first-turn prefix: 1,000 tokens before, 400 tokens after" in out
    assert "600 tokens smaller over 3 sessions since apply" in out
    assert "9,000" not in out
    # Project B has no session after the apply.
    assert "not enough sessions yet" in out
    assert "saved" not in out.lower() and "saves" not in out.lower()
