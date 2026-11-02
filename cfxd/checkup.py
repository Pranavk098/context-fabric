"""``cfxd checkup``: one numbers-only report a tester can run and paste back.

Fixed order, nothing else: diet (the numeric ``--share`` view), compaction-summary
fidelity (frozen metrics M1/M2/M3), then sub-agent re-discovery (``handoff-stats``
numbers). Each section reuses the shipped analyzer; a section with no data prints one
line saying so and the report continues. Exit status is always 0.

Privacy: counts and percentages only. No skill, connector, model, session, agent or
file names, no paths, prompts or text, and the report ends with the safe-to-paste line.

The module also holds the deep ``cfxd doctor`` checks (P9): daemon reachable, token
present and permissioned, hooks registered, state dir writable with clean content
classes, Claude Code version, and the Windows launcher fallback. Each check returns
pass/fail plus the exact fix command, and prints no path (``$CFX_HOME`` stands in),
so the output is paste-safe. ``cfxd/cli.py`` (owned by the wave's CLI wiring WP)
calls :func:`doctor`; this module only exposes the checks and their rendering.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cfxd.audit import DEFAULT_ROOT
from cfxd.config import read_config
from cfxd.content_class import audit_state_dir
from cfxd.daemon import (
    STATE_FILE,
    get_health,
    read_state,
    resolve_state_dir,
    token_permission_warning,
    verify_identity,
)
from cfxd.diet import _numeric, diet
from cfxd.fidelity import fidelity
from cfxd.handoff_stats import collect, summarise
from cfxd.hookclient import TOKEN_FILE
from cfxd.routing import _DEFAULT_PRICES, _load_prices

_SAFE = "safe to paste: counts and percentages only"
_ESTIMATE = "an estimate, not a bill"

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_REQUIRED_HOOKS = ("SessionStart", "PreCompact", "PreToolUse", "PostToolUse", "Stop")
_PLUGIN_REINSTALL = "claude plugin install context-fabric@context-fabric"
_CLI_ABSENT = "not found on PATH (fine if you use the desktop app or an IDE extension)"


def _diet_section(root: Path, since: str, now: datetime) -> tuple[dict[str, Any] | None, list[str]]:
    report = _numeric(diet(root, since, now=now))
    if report["sessions"] == 0 or report["listing_not_found"] >= report["sessions"]:
        return None, ["Diet: no data (no skill or connector listing in this window)."]
    unused_skills = sum(g["count"] for g in report["never_used"]["plugins"])
    unused_connectors = len(report["never_used"]["connectors"])
    data = {
        "sessions": report["sessions"],
        "unused_skills": unused_skills,
        "unused_connectors": unused_connectors,
        "prefix_tokens_estimated_median": report["prefix_tokens_estimated_median"],
        "tokens_per_turn": report["effect"]["per_turn_tokens"],
        "estimate": _ESTIMATE,
    }
    lines = [
        f"Diet (sessions: {report['sessions']:,}; {_ESTIMATE}):",
        f"  unused skills: {unused_skills:,}; unused connectors: {unused_connectors:,}",
        f"  setup prefix: ~{report['prefix_tokens_estimated_median']:,.0f} est. tokens; "
        f"unused prefix: ~{report['effect']['per_turn_tokens']:,.0f} est. tokens per turn",
    ]
    return data, lines


def _fidelity_section(
    root: Path, since: str, now: datetime
) -> tuple[dict[str, Any] | None, list[str]]:
    report = fidelity(root, since, now=now)
    if report["events_analyzed"] == 0:
        return None, ["Fidelity: no data (no compaction summary with held files in this window)."]
    frozen = report["frozen_metrics"]
    m1, m2, m3 = frozen["M1"], frozen["M2"], frozen["M3"]
    data = {"events_analyzed": report["events_analyzed"], "M1": m1, "M2": m2, "M3": m3}
    lines = [
        f"Fidelity (frozen metrics v1, compactions: {report['events_analyzed']:,}):",
        f"  M1 unsupported path rate: {m1['rate']:.4f} "
        f"({m1['unsupported']:,} of {m1['mentions']:,} mentions)",
        f"  M2 dropped edit rate: {m2['rate']:.4f} "
        f"({m2['dropped']:,} of {m2['edited_files']:,} edited files)",
        f"  M3 unsupported pass claims: {m3['unsupported']:,} of "
        f"{m3['events_with_pass_claim']:,} events with a pass claim",
    ]
    return data, lines


def _subagent_section(root: Path) -> tuple[dict[str, Any] | None, list[str]]:
    report = summarise(collect(root, _load_prices(_DEFAULT_PRICES)))
    if report["children"] == 0:
        return None, ["Sub-agents: no data (no child transcript joined to a parent here)."]
    brief_on = read_config(resolve_state_dir(None)).get("brief_v1", False)
    cost = report["cost_share"]
    data = {
        "brief_on": brief_on,
        "sessions": report["sessions"],
        "children": report["children"],
        "families": report["families"],
        "cost_share": cost,
    }
    lines = [
        "Sub-agents (cfxd handoff-stats numbers):",
        f"  Brief on: {'yes' if brief_on else 'no'}; sessions with child transcripts: "
        f"{report['sessions']:,}; joined children: {report['children']:,}",
        "  by child model family (children, parent knew 3+ files, re-reads of those files, "
        "re-discovery share):",
    ]
    for row in report["families"]:
        share = (
            "n/a" if row["rediscovery_share"] is None else f"{100 * row['rediscovery_share']:.1f}%"
        )
        lines.append(
            f"    {row['family']}: {row['children']:,}, {row['parent_knew_3']:,}, "
            f"{row['rereads']:,} of {row['read_calls']:,} read calls, {share}"
        )
    if cost["lower"] is None:
        lines.append("  re-discovery cost share: n/a (no priced session)")
    else:
        lines.append(
            "  re-discovery cost share of total session cost: "
            f"{100 * cost['lower']:.1f}% (lower) to {100 * cost['upper']:.1f}% (upper)"
        )
    return data, lines


@dataclass(frozen=True)
class DoctorCheck:
    """One diagnostic line: what was checked, its detail, and the exact fix on failure.

    ``warn`` marks a line that is worth telling the tester about but is not a failure:
    ``ok`` stays true, so no ``doctor()`` consumer fails on it.
    """

    name: str
    ok: bool
    detail: str
    fix: str | None = None
    warn: bool = False

    def line(self) -> str:
        status = "WARN" if self.warn else "PASS" if self.ok else "FAIL"
        text = f"  {self.name:<12} {status}  {self.detail}"
        if self.fix is None:
            return text
        return f"{text}; {'install the CLI' if self.warn else 'fix'}: {self.fix}"


def _state_dir_fix() -> str:
    if os.name == "nt":
        return 'mkdir "$env:CFX_HOME"'
    return 'mkdir -p "$CFX_HOME" && chmod 700 "$CFX_HOME"'


def _token_fix() -> str:
    if os.name == "nt":
        return 'icacls "$env:CFX_HOME\\token" /inheritance:r /grant:r "$env:USERNAME:F"'
    return 'chmod 600 "$CFX_HOME/token"'


def _claude_install() -> str:
    if os.name == "nt":
        return "irm https://claude.ai/install.ps1 | iex"
    return "curl -fsSL https://claude.ai/install.sh | bash"


def _daemon_check(state_dir: Path) -> DoctorCheck:
    """The helper answers on its recorded port and can prove it owns this state dir."""
    state = read_state(state_dir / STATE_FILE)
    if state is None:
        return DoctorCheck(
            "daemon",
            False,
            "no state file; the local helper is not running",
            "python -m cfxd serve",
        )
    port = state.get("port")
    if not isinstance(port, int):
        return DoctorCheck(
            "daemon",
            False,
            "state file is malformed",
            "python -m cfxd stop && python -m cfxd serve",
        )
    try:
        status, body = get_health(port)
    except OSError:
        return DoctorCheck(
            "daemon", False, f"no answer on 127.0.0.1:{port}", "python -m cfxd serve"
        )
    try:
        health = json.loads(body)
    except ValueError:
        health = {}
    if status != 200 or not (isinstance(health, dict) and health.get("ok") is True):
        return DoctorCheck(
            "daemon",
            False,
            f"unhealthy on 127.0.0.1:{port}",
            "python -m cfxd stop && python -m cfxd serve",
        )
    if not verify_identity(state_dir, port):
        return DoctorCheck(
            "daemon",
            False,
            f"answering on 127.0.0.1:{port} but identity is NOT verified",
            "python -m cfxd stop && python -m cfxd serve",
        )
    version = state.get("version", "?")
    return DoctorCheck(
        "daemon", True, f"reachable on 127.0.0.1:{port} (version {version}), identity verified"
    )


def _token_check(state_dir: Path) -> DoctorCheck:
    """The per-install token exists, is non-empty, and no other user can read it."""
    path = state_dir / TOKEN_FILE
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        return DoctorCheck("token", False, "missing", "python -m cfxd serve")
    if not token:
        return DoctorCheck("token", False, "empty", "python -m cfxd serve")
    if token_permission_warning(path) is not None:
        return DoctorCheck("token", False, "readable by other users", _token_fix())
    return DoctorCheck("token", True, "present; readable only by you")


def _hooks_check(plugin_root: Path = _PLUGIN_ROOT) -> DoctorCheck:
    """Every hook the plugin relies on is declared in its hooks file."""
    try:
        data = json.loads((plugin_root / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return DoctorCheck(
            "hooks", False, "plugin hook file missing or unreadable", _PLUGIN_REINSTALL
        )
    missing = [name for name in _REQUIRED_HOOKS if name not in hooks]
    if missing:
        return DoctorCheck(
            "hooks", False, "not registered: " + ", ".join(missing), _PLUGIN_REINSTALL
        )
    return DoctorCheck(
        "hooks", True, f"{len(_REQUIRED_HOOKS)} of {len(_REQUIRED_HOOKS)} events registered"
    )


def _state_dir_check(state_dir: Path) -> DoctorCheck:
    """The state dir accepts a write, and every file in it holds only its content class."""
    try:
        fd, tmp = tempfile.mkstemp(dir=str(state_dir), prefix=".doctor.", suffix=".tmp")
    except OSError:
        return DoctorCheck("state dir", False, "not writable", _state_dir_fix())
    os.close(fd)
    os.unlink(tmp)
    violations = audit_state_dir(state_dir)
    if violations:
        return DoctorCheck(
            "state dir",
            False,
            f"writable, but {len(violations)} file(s) exceed their content class",
            "python scripts/state_audit.py",
        )
    return DoctorCheck("state dir", True, "writable; 0 content-class violations")


def _claude_code_check(runner: Callable[..., Any] | None = None) -> DoctorCheck:
    """The host agent is installed and reports a version.

    A missing CLI is a WARN: the desktop app and IDE extensions need no ``claude`` on
    PATH, so their users must not be told the install is unhealthy.
    """
    run = runner or subprocess.run
    try:
        proc = run(["claude", "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return DoctorCheck("claude code", True, _CLI_ABSENT, _claude_install(), warn=True)
    out = proc.stdout if isinstance(proc.stdout, str) else ""
    version = out.strip().splitlines()[0][:60] if out.strip() else ""
    if proc.returncode != 0 or not version:
        return DoctorCheck("claude code", True, _CLI_ABSENT, _claude_install(), warn=True)
    return DoctorCheck("claude code", True, f"version {version}")


def _launcher_check(which: Callable[[str], str | None] = shutil.which) -> DoctorCheck:
    """Windows needs ``sh`` for the hooks and ``python -m cfxd`` as the launcher fallback."""
    if os.name != "nt":
        return DoctorCheck("launcher", True, "not needed here (hooks run sh launch.sh)")
    if which("sh") is None:
        return DoctorCheck(
            "launcher",
            False,
            "sh not found (hooks cannot start)",
            "install Git for Windows: https://git-scm.com/download/win then open a new terminal",
        )
    if which("python") is None:
        return DoctorCheck(
            "launcher",
            False,
            "python not on PATH (the python -m cfxd fallback cannot run)",
            "add Python to PATH, or run: uv run python -m cfxd doctor",
        )
    return DoctorCheck("launcher", True, "sh found (Git for Windows); python fallback available")


def doctor(
    state_dir: Path,
    *,
    plugin_root: Path | None = None,
    runner: Callable[..., Any] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> list[DoctorCheck]:
    """The six P9 checks in a fixed order; every line is pass/fail plus its fix."""
    return [
        _daemon_check(state_dir),
        _token_check(state_dir),
        _hooks_check(plugin_root or _PLUGIN_ROOT),
        _state_dir_check(state_dir),
        _claude_code_check(runner),
        _launcher_check(which),
    ]


def format_doctor(checks: list[DoctorCheck]) -> str:
    """Header plus one line per check; no paths, so the block is paste-safe."""
    return "\n".join(
        ["checks (paste-safe: no paths, no prompts, no text):"] + [check.line() for check in checks]
    )


def checkup(root: Path, since: str = "30d", now: datetime | None = None) -> dict[str, Any]:
    """Run the three analyzers in order; each returns data or one no-data line."""
    if now is None:
        now = datetime.now(UTC)
    report: dict[str, Any] = {
        "since": since,
        "diet": None,
        "fidelity": None,
        "sub_agents": None,
        "no_data": [],
        "lines": [],
        "safe_to_paste": "counts and percentages only",
    }
    for key, (data, lines) in (
        ("diet", _diet_section(root, since, now)),
        ("fidelity", _fidelity_section(root, since, now)),
        ("sub_agents", _subagent_section(root)),
    ):
        report[key] = data
        report["lines"].extend(lines)
        if data is None:
            report["no_data"].append(key)
    return report


def format_checkup(report: dict[str, Any]) -> str:
    """Header, the three sections in order, then the exact safe-to-paste line."""
    lines = [f"cfxd checkup (numbers only, {report['since']})"]
    lines.extend(report["lines"])
    lines.append(_SAFE)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cfxd checkup",
        description="one numbers-only tester report: diet, fidelity, sub-agents",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help=f"transcripts root (default: {DEFAULT_ROOT})",
    )
    parser.add_argument(
        "--since",
        choices=("7d", "30d", "all"),
        default="30d",
        help="time window by file mtime (default: 30d)",
    )
    parser.add_argument("--json", action="store_true", help="emit a single JSON object")
    args = parser.parse_args(argv)
    report = checkup(args.root, args.since)
    if args.json:
        print(json.dumps({key: value for key, value in report.items() if key != "lines"}))
    else:
        print(format_checkup(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
