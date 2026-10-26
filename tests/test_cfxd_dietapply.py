"""Tests for ``cfxd diet --apply`` / ``--undo`` against a stand-in ``claude``."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

FAKE_CLAUDE = """
import json, os, sys
from pathlib import Path

state = Path(os.environ["FAKE_CLAUDE_STATE"])
argv = sys.argv[1:]
record_dir = Path(os.environ["CFX_HOME"]) / "diet"
entry = {"argv": argv, "record_exists": any(record_dir.glob("applied-*.json"))}
with open(state / "calls.log", "a", encoding="utf-8") as fh:
    fh.write(json.dumps(entry) + "\\n")
plugins = state / "plugins.json"

def load():
    return json.loads(plugins.read_text(encoding="utf-8"))

def save(data):
    plugins.write_text(json.dumps(data), encoding="utf-8")

if argv[:3] == ["plugin", "list", "--json"]:
    print(json.dumps(load()))
    sys.exit(0)
if argv[:2] in (["plugin", "disable"], ["plugin", "enable"]):
    pid = argv[2]
    fail = os.environ.get("FAKE_CLAUDE_FAIL_ID", "")
    if fail and pid == fail:
        print("cannot change " + pid, file=sys.stderr)
        sys.exit(1)
    data = load()
    for p in data:
        scoped = "--scope" not in argv or p.get("scope") == argv[argv.index("--scope") + 1]
        if p.get("id") == pid and scoped:
            p["enabled"] = argv[1] == "enable"
    save(data)
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


PLUGINS = [
    {"id": "acme@market", "version": "1.0", "scope": "user", "enabled": True},
    {"id": "mix@market", "version": "1.0", "scope": "user", "enabled": True},
    {"id": "context-fabric@market", "version": "1.0", "scope": "user", "enabled": True},
    {"id": "connectoronly@market", "version": "1.0", "scope": "user", "enabled": True},
    {"id": "other@market", "version": "1.0", "scope": "user", "enabled": True},
]


def _assistant(*blocks: dict) -> dict:
    return {
        "type": "assistant",
        "message": {"content": list(blocks), "usage": {"input_tokens": 10}},
    }


def _tool(id_: str, name: str, **inp: object) -> dict:
    return {"type": "tool_use", "id": id_, "name": name, "input": inp}


def _session(root: Path) -> None:
    names = ["acme:a", "acme:b", "mix:used", "mix:unused", "context-fabric:skill", "synced:thing"]
    content = "".join(f"- {name}: description of {name}\n" for name in names)
    lines = [
        {
            "type": "attachment",
            "attachment": {"type": "skill_listing", "names": names, "content": content},
        },
        {
            "type": "attachment",
            "attachment": {
                "type": "deferred_tools_delta",
                "addedNames": ["mcp__plugin_connectoronly_srv__t1", "mcp__remote__t1"],
            },
        },
        _assistant(_tool("s1", "Skill", skill="mix:used")),
        _assistant(),
    ]
    path = root / "proj" / "s1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


class Env:
    def __init__(self, tmp_path: Path, plugins: list[dict] | None = None) -> None:
        self.tmp = tmp_path
        self.root = tmp_path / "transcripts"
        self.root.mkdir()
        _session(self.root)
        self.state = tmp_path / "state"
        self.state.mkdir()
        self.plugins_file = self.state / "plugins.json"
        self.plugins_file.write_text(json.dumps(plugins or PLUGINS), encoding="utf-8")
        self.cfx = tmp_path / "cfxhome"
        self.config = tmp_path / "claude-config"
        self.config.mkdir()
        (self.config / "settings.json").write_text('{"theme":"dark"}', encoding="utf-8")
        self.exe = _write_claude(tmp_path)
        self.env = {
            **os.environ,
            "CFX_HOME": str(self.cfx),
            "CFX_CLAUDE": str(self.exe),
            "FAKE_CLAUDE_STATE": str(self.state),
            "CLAUDE_CONFIG_DIR": str(self.config),
        }

    def run(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "cfxd", "diet", "--root", str(self.root), *args],
            cwd=ROOT,
            env=env or self.env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def calls(self) -> list[dict]:
        path = self.state / "calls.log"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def enables(self) -> list[list[str]]:
        return [c["argv"] for c in self.calls() if c["argv"][:2] == ["plugin", "enable"]]

    def disables(self) -> list[list[str]]:
        return [c["argv"] for c in self.calls() if c["argv"][:2] == ["plugin", "disable"]]

    def records(self) -> list[Path]:
        return sorted((self.cfx / "diet").glob("applied-*.json"))

    def enabled(self, plugin_id: str) -> bool:
        data = json.loads(self.plugins_file.read_text(encoding="utf-8"))
        return next(p["enabled"] for p in data if p["id"] == plugin_id)


def test_dry_run_executes_nothing_and_prints_commands(tmp_path: Path) -> None:
    env = Env(tmp_path)
    result = env.run("--apply")
    assert result.returncode == 0, result.stderr
    assert env.disables() == []
    assert "claude plugin disable acme@market --scope user" in result.stdout
    assert "connectoronly@market" in result.stdout
    assert "Re-run with --yes to apply" in result.stdout
    # A plugin with a used skill, the plugin itself, and unused-for-this-report plugins are safe.
    assert "mix@market" not in result.stdout
    assert "context-fabric@market" not in result.stdout
    assert env.records() == []


def test_yes_disables_only_fully_unused_local_plugins(tmp_path: Path) -> None:
    env = Env(tmp_path)
    result = env.run("--apply", "--yes")
    assert result.returncode == 0, result.stderr
    assert env.disables() == [
        ["plugin", "disable", "acme@market", "--scope", "user"],
        ["plugin", "disable", "connectoronly@market", "--scope", "user"],
    ]
    assert env.enabled("acme@market") is False
    assert env.enabled("connectoronly@market") is False
    assert env.enabled("mix@market") is True
    assert env.enabled("context-fabric@market") is True
    assert env.enabled("other@market") is True
    assert "takes effect in NEW sessions" in result.stdout
    assert "cfxd diet --undo" in result.stdout

    (record,) = env.records()
    data = json.loads(record.read_text(encoding="utf-8"))
    assert {p["id"] for p in data["plugins"]} == {"acme@market", "connectoronly@market"}
    assert all(p["applied"] is True and p["enabled_after"] is False for p in data["plugins"])
    text = record.read_text(encoding="utf-8")
    for skill in ("acme:a", "acme:b", "synced:thing", "mix:used"):
        assert skill not in text
    assert str(tmp_path) not in text
    if os.name != "nt":
        assert record.stat().st_mode & 0o777 == 0o600


def test_record_written_before_any_disable(tmp_path: Path) -> None:
    env = Env(tmp_path)
    assert env.run("--apply", "--yes").returncode == 0
    disables = [c for c in env.calls() if c["argv"][:2] == ["plugin", "disable"]]
    assert disables
    assert all(c["record_exists"] for c in disables)


def test_account_synced_items_listed_but_never_passed_to_a_command(tmp_path: Path) -> None:
    env = Env(tmp_path)
    result = env.run("--apply")
    assert "Manage these in claude.ai (Settings, Connectors / Plugins)" in result.stdout
    assert "synced" in result.stdout
    assert "remote" in result.stdout
    for call in env.calls():
        assert all("synced" not in arg and "remote" not in arg for arg in call["argv"])


def test_failed_disable_is_reported_and_recorded(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.env["FAKE_CLAUDE_FAIL_ID"] = "acme@market"
    result = env.run("--apply", "--yes")
    assert result.returncode == 0, result.stderr
    assert "FAILED to disable acme@market" in result.stdout
    assert env.enabled("acme@market") is True
    assert env.enabled("connectoronly@market") is False
    (record,) = env.records()
    data = json.loads(record.read_text(encoding="utf-8"))
    by_id = {p["id"]: p for p in data["plugins"]}
    assert by_id["acme@market"]["applied"] is False
    assert by_id["connectoronly@market"]["applied"] is True


def test_undo_reenables_exactly_what_was_disabled_and_marks_record(tmp_path: Path) -> None:
    env = Env(tmp_path)
    assert env.run("--apply", "--yes").returncode == 0
    result = env.run("--undo")
    assert result.returncode == 0, result.stderr
    assert env.enabled("acme@market") is True
    assert env.enabled("connectoronly@market") is True
    assert [argv[2] for argv in env.enables()] == ["acme@market", "connectoronly@market"]
    (record,) = env.records()
    assert json.loads(record.read_text(encoding="utf-8"))["undone_at"]

    again = env.run("--undo")
    assert again.returncode == 0
    assert "nothing to undo" in again.stdout
    assert len(env.enables()) == 2


def test_undo_with_no_record(tmp_path: Path) -> None:
    env = Env(tmp_path)
    result = env.run("--undo")
    assert result.returncode == 0
    assert "nothing to undo" in result.stdout


def _record(cfx: Path, stamp: str, plugin_id: str) -> Path:
    cfx.mkdir(parents=True, exist_ok=True)
    path = cfx / f"applied-{stamp}.json"
    path.write_text(
        json.dumps(
            {
                "created_at": "2026-01-01T00:00:00Z",
                "undone_at": None,
                "plugins": [
                    {
                        "id": plugin_id,
                        "scope": "user",
                        "enabled_before": True,
                        "enabled_after": False,
                        "est_tokens_per_turn": 1.0,
                        "applied": True,
                        "disabled_at": "2026-01-01T00:00:00Z",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_undo_all_goes_newest_first(tmp_path: Path) -> None:
    env = Env(tmp_path)
    cfx = env.cfx / "diet"
    _record(cfx, "20260101T000000000000Z", "acme@market")
    _record(cfx, "20260102T000000000000Z", "connectoronly@market")
    for plugin_id in ("acme@market", "connectoronly@market"):
        data = json.loads(env.plugins_file.read_text(encoding="utf-8"))
        for p in data:
            if p["id"] == plugin_id:
                p["enabled"] = False
        env.plugins_file.write_text(json.dumps(data), encoding="utf-8")

    result = env.run("--undo", "--all")
    assert result.returncode == 0, result.stderr
    assert [argv[2] for argv in env.enables()] == ["connectoronly@market", "acme@market"]
    assert all(json.loads(p.read_text(encoding="utf-8"))["undone_at"] for p in env.records())


def test_undo_reports_a_plugin_that_is_no_longer_installed(tmp_path: Path) -> None:
    env = Env(tmp_path)
    _record(env.cfx / "diet", "20260101T000000000000Z", "gone@market")
    result = env.run("--undo")
    assert result.returncode == 0, result.stderr
    assert "gone@market: no longer installed; skipped" in result.stdout
    assert json.loads(env.records()[0].read_text(encoding="utf-8"))["undone_at"]


def test_claude_missing(tmp_path: Path) -> None:
    env = Env(tmp_path)
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    missing = {k: v for k, v in env.env.items() if k != "CFX_CLAUDE"}
    missing["PATH"] = str(empty)
    dry = env.run("--apply", env=missing)
    assert dry.returncode == 0, dry.stderr
    assert "not on PATH" in dry.stdout
    yes = env.run("--apply", "--yes", env=missing)
    assert yes.returncode == 1


@pytest.mark.parametrize("extra", [("--share",), ("--json",)])
def test_apply_rejects_share_and_json(tmp_path: Path, extra: tuple[str, ...]) -> None:
    env = Env(tmp_path)
    result = env.run("--apply", *extra)
    assert result.returncode == 2
    assert env.disables() == []


def test_nothing_else_is_modified(tmp_path: Path) -> None:
    env = Env(tmp_path)

    def snapshot() -> dict[str, bytes]:
        out = {}
        for path in env.config.rglob("*"):
            if path.is_file():
                out[str(path.relative_to(env.config))] = path.read_bytes()
        out["plugins.json"] = env.plugins_file.read_bytes()
        return out

    before = snapshot()
    assert env.run("--apply", "--yes").returncode == 0
    after = snapshot()
    assert after["plugins.json"] != before["plugins.json"]  # the fake claude flipped enabled
    assert {k: v for k, v in after.items() if k != "plugins.json"} == {
        k: v for k, v in before.items() if k != "plugins.json"
    }


def test_account_synced_scope_is_not_a_local_candidate() -> None:
    from cfxd.dietapply import _by_name

    plugins = [
        {"id": "a@market", "scope": "user", "enabled": True},
        {"id": "b@synced", "scope": "synced", "enabled": True},
    ]
    assert set(_by_name(plugins, enabled_only=True)) == {"a"}
    assert set(_by_name(plugins, enabled_only=False)) == {"a", "b"}
