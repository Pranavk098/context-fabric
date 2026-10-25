"""``cfxd diet --apply`` / ``--undo``: disable fully-unused local plugins, reversibly.

Only the ``claude plugin`` CLI is called; settings files are never edited. A record
of exactly what was disabled is written before the first command runs, so an
interrupted run can always be undone. Items that live in the Claude account rather
than in a local plugin are listed but never acted on.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cfxd.diet import _connector_line, _plugin_line

PROTECTED = ("context-fabric",)
_TIMEOUT_S = 60


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def _claude() -> str | None:
    override = os.environ.get("CFX_CLAUDE")
    if override:
        return override if Path(override).exists() else shutil.which(override)
    return shutil.which("claude")


def _run(exe: str, args: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            [exe, *args],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            shell=False,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _list_plugins(exe: str) -> list[dict[str, Any]] | None:
    code, out = _run(exe, ["plugin", "list", "--json"])
    if code != 0:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    return [p for p in data if isinstance(p, dict)] if isinstance(data, list) else None


def _name(plugin: dict[str, Any]) -> str | None:
    pid = plugin.get("id")
    return pid.split("@", 1)[0] if isinstance(pid, str) and "@" in pid else None


def _by_name(plugins: list[dict[str, Any]], enabled_only: bool) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for plugin in plugins:
        name = _name(plugin)
        if name is None or (enabled_only and plugin.get("enabled") is not True):
            continue
        cur = out.get(name)
        if cur is None or (plugin.get("enabled") and not cur.get("enabled")):
            out[name] = plugin
    return out


def _connector_plugin(name: str, plugins: set[str]) -> str | None:
    for plugin in plugins:
        if name.startswith(f"plugin_{plugin}_"):
            return plugin
    return None


def _candidates(
    report: dict[str, Any],
    installed: dict[str, dict[str, Any]],
    known: set[str],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (candidates by plugin, account-synced skill groups, account-synced connectors)."""
    never = report["never_used"]
    used = report["used"]
    used_plugins = {g["plugin"] for g in used["plugins"]}
    used_connectors = {c["name"] for c in used["connectors"]}
    names = known

    covered: dict[str, dict[str, Any]] = {}

    def add(plugin: str) -> dict[str, Any]:
        slot = covered.setdefault(
            plugin,
            {
                "id": installed[plugin]["id"],
                "scope": installed[plugin].get("scope"),
                "enabled": installed[plugin].get("enabled"),
                "skills": 0,
                "tokens": 0.0,
                "connectors": [],
            },
        )
        return slot

    for group in never["plugins"]:
        plugin = group["plugin"]
        if plugin not in installed or plugin in PROTECTED or plugin in used_plugins:
            continue
        if any(_connector_plugin(name, names) == plugin for name in used_connectors):
            continue
        slot = add(plugin)
        slot["skills"] = group["count"]
        slot["tokens"] += float(group["per_turn_tokens"])

    account_connectors: list[dict[str, Any]] = []
    for conn in never["connectors"]:
        plugin = _connector_plugin(conn["name"], names)
        if plugin is None:
            account_connectors.append(conn)
            continue
        if plugin in used_plugins or plugin in PROTECTED or plugin not in installed:
            continue
        if any(_connector_plugin(name, names) == plugin for name in used_connectors):
            continue
        slot = add(plugin)
        slot["tokens"] += float(conn["per_turn_tokens"])
        slot["connectors"].append(conn["name"])

    account_skills = [g for g in never["plugins"] if g["plugin"] not in known]
    return covered, account_skills, account_connectors


def _candidate_lines(candidates: dict[str, dict[str, Any]]) -> list[str]:
    lines = []
    for plugin in sorted(candidates, key=lambda p: (-candidates[p]["tokens"], p)):
        c = candidates[plugin]
        scope = c["scope"] or "user"
        skills = f"{c['skills']} skills" if c["skills"] else "no skills"
        lines.append(
            f"  plugin {c['id']} (scope: {c['scope'] or '?'})  {skills}  "
            f"~{c['tokens']:,.0f} est. tokens/turn"
        )
        lines.append(f"    claude plugin disable {c['id']} --scope {scope}")
    return lines


def _account_lines(skills: list[dict[str, Any]], connectors: list[dict[str, Any]]) -> list[str]:
    if not skills and not connectors:
        return []
    lines = [
        "Manage these in claude.ai (Settings, Connectors / Plugins): cannot be changed from here"
    ]
    for group in skills:
        lines.append(_plugin_line(group))
    for conn in connectors:
        lines.append(_connector_line(conn))
    return lines


def _record_dir() -> Path:
    home = os.environ.get("CFX_HOME")
    return (Path(home) if home else Path.home() / ".cfx") / "diet"


def _write_record(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _read_records() -> list[tuple[Path, dict[str, Any]]]:
    records = []
    for path in sorted(_record_dir().glob("applied-*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            records.append((path, data))
    return records


def _status(plugins: list[dict[str, Any]]) -> dict[str, bool]:
    return {str(p["id"]): p.get("enabled") is True for p in plugins if isinstance(p.get("id"), str)}


def apply_report(report: dict[str, Any], yes: bool = False) -> int:
    """Show (and, with ``yes``, disable) fully-unused local plugins."""
    if report["sessions"] == 0:
        print("No transcripts found under the transcripts root.")
        return 0
    exe = _claude()
    if exe is None:
        print("contains names from your setup; do not paste")
        print("cfxd diet: 'claude' is not on PATH; nothing was run.")
        print("Install or expose Claude Code, or run these yourself:")
        for group in report["never_used"]["plugins"]:
            print(
                f"  claude plugin disable {group['plugin']}@<marketplace> "
                f"({group['count']} skills, ~{group['per_turn_tokens']:,.0f} est. tokens/turn)"
            )
        return 1 if yes else 0

    plugins = _list_plugins(exe)
    if plugins is None:
        print("cfxd diet: could not read 'claude plugin list --json'; nothing was run.")
        return 1 if yes else 0

    installed = _by_name(plugins, enabled_only=True)
    candidates, account_skills, account_connectors = _candidates(
        report, installed, set(_by_name(plugins, enabled_only=False))
    )
    total = round(sum(c["tokens"] for c in candidates.values()), 1)

    lines = ["contains names from your setup; do not paste"]
    if candidates:
        verb = "applying" if yes else "dry run, nothing executed"
        lines.append(f"Candidates ({len(candidates)}, {verb}):")
        lines.extend(_candidate_lines(candidates))
        lines.append(f"Estimated ~{total:,.0f} est. tokens/turn removed.")
    else:
        lines.append("No fully-unused local plugins to disable.")
    lines.extend(_account_lines(account_skills, account_connectors))

    if not yes:
        if candidates:
            lines.append("Re-run with --yes to apply.")
        print("\n".join(lines))
        return 0

    print("\n".join(lines))
    if not candidates:
        return 0

    record_path = _record_dir() / f"applied-{_stamp()}.json"
    record: dict[str, Any] = {
        "created_at": _now(),
        "updated_at": _now(),
        "undone_at": None,
        "plugins": [
            {
                "id": c["id"],
                "scope": c["scope"],
                "enabled_before": c["enabled"],
                "enabled_after": None,
                "est_tokens_per_turn": round(c["tokens"], 1),
                "applied": None,
                "disabled_at": None,
            }
            for c in candidates.values()
        ],
    }
    _write_record(record_path, record)

    failures: list[tuple[str, str]] = []
    for entry in record["plugins"]:
        args = ["plugin", "disable", str(entry["id"])]
        if entry.get("scope"):
            args += ["--scope", str(entry["scope"])]
        code, out = _run(exe, args)
        entry["applied"] = code == 0
        entry["disabled_at"] = _now() if code == 0 else None
        if code != 0:
            reason = out.strip().splitlines()[-1][:300] if out.strip() else "failed"
            failures.append((str(entry["id"]), reason))
        record["updated_at"] = _now()
        _write_record(record_path, record)

    after = _list_plugins(exe)
    state = _status(after) if after is not None else {}
    successes: list[str] = []
    for entry in record["plugins"]:
        if entry["applied"] and state.get(str(entry["id"])) is False:
            entry["enabled_after"] = False
            successes.append(str(entry["id"]))
        else:
            if entry["applied"]:
                failures.append((str(entry["id"]), "not reported as disabled by claude"))
            entry["applied"] = False
            entry["enabled_after"] = state.get(str(entry["id"]))
    record["updated_at"] = _now()
    _write_record(record_path, record)

    if successes:
        print(f"Disabled: {', '.join(successes)}")
    for pid, reason in failures:
        print(f"FAILED to disable {pid}: {reason}")
    print(f"Undo record: {record_path.name}")
    print(
        "takes effect in NEW sessions; open one and run /context to see the change; "
        "undo with: cfxd diet --undo"
    )
    return 0


def undo_records(all_records: bool = False) -> int:
    """Re-enable what an apply record disabled."""
    records = [(p, r) for p, r in _read_records() if not r.get("undone_at")]
    if not records:
        print("cfxd diet --undo: nothing to undo.")
        return 0
    if not all_records:
        records = records[:1]

    exe = _claude()
    if exe is None:
        print("cfxd diet: 'claude' is not on PATH; nothing was run.")
        for _path, record in records:
            for entry in record.get("plugins") or []:
                if entry.get("applied"):
                    print(f"  claude plugin enable {entry['id']} --scope {entry['scope']}")
        return 1

    for path, record in records:
        print(f"cfxd diet --undo: {path.name}")
        after = _list_plugins(exe) or []
        state = _status(after)
        for entry in record.get("plugins") or []:
            pid = str(entry.get("id"))
            if not entry.get("applied"):
                continue
            if pid not in state:
                print(f"  {pid}: no longer installed; skipped")
                continue
            args = ["plugin", "enable", pid]
            if entry.get("scope"):
                args += ["--scope", str(entry["scope"])]
            code, out = _run(exe, args)
            if code != 0:
                reason = out.strip().splitlines()[-1][:300] if out.strip() else "failed"
                print(f"  FAILED to enable {pid}: {reason}")
                continue
            state = _status(_list_plugins(exe) or [])
            if state.get(pid) is True:
                entry["enabled_after"] = True
                print(f"  enabled {pid}")
            else:
                print(f"  FAILED to enable {pid}: not reported as enabled by claude")
        record["undone_at"] = _now()
        _write_record(path, record)

    print("takes effect in NEW sessions; open one and run /context to see the change")
    return 0
