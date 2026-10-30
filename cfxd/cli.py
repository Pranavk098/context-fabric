"""``python -m cfxd`` CLI: serve, stop, status, doctor."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from cfxd import checkpoint, handoff, packet
from cfxd.agents import READERS, build_report, default_root, since_cutoff
from cfxd.agents import format_human as format_agent_report
from cfxd.agents.opencode import OpenCodeDBError
from cfxd.audit import DEFAULT_ROOT, audit, coverage
from cfxd.audit import format_coverage as format_coverage_report
from cfxd.audit import format_human as format_audit
from cfxd.brief_receipts import BriefReceipt, join_receipts, path_sha16, receipts_to_json
from cfxd.config import read_config, set_flag
from cfxd.console import ensure_utf8_output
from cfxd.daemon import (
    DEFAULT_PORT,
    STATE_FILE,
    daemon_alive,
    get_health,
    percentile,
    post_hook,
    read_state,
    read_token,
    resolve_state_dir,
    run_serve,
    verify_identity,
)
from cfxd.diet import _anonymize, diet
from cfxd.diet import format_human as format_diet
from cfxd.dietapply import apply_report, undo_records
from cfxd.fidelity import fidelity
from cfxd.fidelity import format_fidelity as format_fidelity_report
from cfxd.ledger import format_ledger, ledger
from cfxd.refetch import format_refetch, refetch
from cfxd.replay import format_replay, replay
from cfxd.routing import format_human as format_routing
from cfxd.routing import routing
from cfxd.stats import collect_stats, format_human
from cfxd.tour import run as run_tour
from cfxd.trend import format_trend, trend
from cfxd.waste import format_waste, waste
from cfxd.week import format_week, save_snapshot, week

_SHIM_PATH = Path(__file__).with_name("shim.py")
_IS_WINDOWS = os.name == "nt"


def _add_state_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--state-dir",
        default=None,
        help="daemon state directory (default: CFX_HOME or ~/.cfx/daemon)",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cfxd", description="Context Fabric daemon")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="run the hook daemon")
    serve.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"listen port (default: CFX_PORT or {DEFAULT_PORT})",
    )
    _add_state_dir(serve)
    serve.set_defaults(func=_cmd_serve)

    stop = subparsers.add_parser("stop", help="stop a running daemon")
    _add_state_dir(stop)
    stop.set_defaults(func=_cmd_stop)

    status = subparsers.add_parser("status", help="report daemon status")
    _add_state_dir(status)
    status.set_defaults(func=_cmd_status)

    doctor = subparsers.add_parser("doctor", help="health, auth, and latency checks")
    _add_state_dir(doctor)
    doctor.set_defaults(func=_cmd_doctor)

    stats = subparsers.add_parser("stats", help="report fold and hook savings from the logs")
    stats.add_argument("--session", default=None, help="filter both logs to a session id")
    stats.add_argument(
        "--since",
        choices=("7d", "24h", "all"),
        default="all",
        help="time window (default: all)",
    )
    stats.add_argument("--json", action="store_true", help="emit a single JSON object")
    _add_state_dir(stats)
    stats.set_defaults(func=_cmd_stats)

    auditp = subparsers.add_parser("audit", help="counts-only analysis of Claude Code transcripts")
    auditp.add_argument("--root", default=None, help=f"transcripts root (default: {DEFAULT_ROOT})")
    auditp.add_argument(
        "--since",
        choices=("7d", "30d", "all"),
        default="30d",
        help="time window by file mtime (default: 30d)",
    )
    auditp.add_argument("--json", action="store_true", help="emit a single JSON object")
    auditp.set_defaults(func=_cmd_audit)

    covp = subparsers.add_parser(
        "coverage", help="experimental: how many tool calls the hooks observed"
    )
    covp.add_argument("--root", default=None, help=f"transcripts root (default: {DEFAULT_ROOT})")
    covp.add_argument("--days", type=int, default=7, help="time window in days (default: 7)")
    covp.add_argument("--json", action="store_true", help="emit a single JSON object")
    _add_state_dir(covp)
    covp.set_defaults(func=_cmd_coverage)

    tourp = subparsers.add_parser(
        "tour", help="guided first run: five steps over your own transcripts"
    )
    tourp.add_argument("--root", default=None, help=f"transcripts root (default: {DEFAULT_ROOT})")
    tourp.add_argument("--yes", action="store_true", help="print every step without pausing")
    _add_state_dir(tourp)
    tourp.set_defaults(func=_cmd_tour)

    agentp = subparsers.add_parser(
        "agent-audit", help="counts-only analysis of non-Claude agent transcripts"
    )
    agentp.add_argument(
        "--agent", choices=("codex", "opencode"), required=True, help="agent to audit"
    )
    agentp.add_argument("--root", default=None, help="transcripts root (default: agent default)")
    agentp.add_argument(
        "--since",
        choices=("7d", "30d", "all"),
        default="30d",
        help="time window by file mtime (default: 30d)",
    )
    agentp.add_argument("--json", action="store_true", help="emit a single JSON object")
    agentp.set_defaults(func=_cmd_agent_audit)

    dietp = subparsers.add_parser(
        "diet", help="starting-prefix items never used in the window, and their cost"
    )
    dietp.add_argument("--root", default=None, help=f"transcripts root (default: {DEFAULT_ROOT})")
    dietp.add_argument(
        "--since",
        choices=("7d", "30d", "all"),
        default="30d",
        help="time window by file mtime (default: 30d)",
    )
    dietp.add_argument("--share", action="store_true", help="replace names with hash labels")
    dietp.add_argument("--json", action="store_true", help="emit a single JSON object")
    diet_action = dietp.add_mutually_exclusive_group()
    diet_action.add_argument(
        "--apply",
        action="store_true",
        help="disable fully-unused local plugins (dry run unless --yes)",
    )
    diet_action.add_argument(
        "--undo", action="store_true", help="re-enable plugins from the last record"
    )
    dietp.add_argument("--yes", action="store_true", help="with --apply: run the disable commands")
    dietp.add_argument("--all", action="store_true", help="with --undo: undo every pending record")
    dietp.set_defaults(func=_cmd_diet)

    replayp = subparsers.add_parser(
        "replay", help="offline replay: upper bound on shrinking old tool results"
    )
    replayp.add_argument("--root", default=None, help=f"transcripts root (default: {DEFAULT_ROOT})")
    replayp.add_argument(
        "--since",
        choices=("7d", "30d", "all"),
        default="30d",
        help="time window by file mtime (default: 30d)",
    )
    replayp.add_argument("--k", default="3,5,10", help="comma-separated k values (default: 3,5,10)")
    replayp.add_argument(
        "--min-bytes",
        type=int,
        default=2048,
        help="min tool-result bytes to shrink (default: 2048)",
    )
    replayp.add_argument(
        "--stub-bytes", type=int, default=300, help="stub size in bytes (default: 300)"
    )
    replayp.add_argument(
        "--batch", default="1,5", help="comma-separated batch values (default: 1,5)"
    )
    replayp.add_argument(
        "--reset-at",
        default="40,80,120",
        help="comma-separated reset turn counts for reset policies (default: 40,80,120)",
    )
    replayp.add_argument(
        "--summary-tokens",
        default="2000,5000",
        help="comma-separated summary token sizes for reset policies (default: 2000,5000)",
    )
    replayp.add_argument(
        "--refetch",
        default="0,0.1,0.25,0.5",
        help="comma-separated re-fetch fractions for reset policies (default: 0,0.1,0.25,0.5)",
    )
    replayp.add_argument("--json", action="store_true", help="emit a single JSON object")
    replayp.set_defaults(func=_cmd_replay)

    refetchp = subparsers.add_parser(
        "refetch", help="how much of the context dropped by a compaction is re-read"
    )
    refetchp.add_argument(
        "--root", default=None, help=f"transcripts root (default: {DEFAULT_ROOT})"
    )
    refetchp.add_argument(
        "--since",
        choices=("7d", "30d", "all"),
        default="30d",
        help="time window by file mtime (default: 30d)",
    )
    refetchp.add_argument(
        "--window",
        type=int,
        default=20,
        help="assistant turns after a boundary to count re-reads (default: 20)",
    )
    refetchp.add_argument("--json", action="store_true", help="emit a single JSON object")
    refetchp.set_defaults(func=_cmd_refetch)

    fidelityp = subparsers.add_parser(
        "fidelity", help="how faithful a compaction summary is to the session"
    )
    fidelityp.add_argument(
        "--root", default=None, help=f"transcripts root (default: {DEFAULT_ROOT})"
    )
    fidelityp.add_argument(
        "--since",
        choices=("7d", "30d", "all"),
        default="30d",
        help="time window by file mtime (default: 30d)",
    )
    fidelityp.add_argument("--json", action="store_true", help="emit a single JSON object")
    fidelityp.set_defaults(func=_cmd_fidelity)

    wastep = subparsers.add_parser(
        "waste", help="loop, retry and rediscovery waste in Claude Code transcripts"
    )
    wastep.add_argument("--root", default=None, help=f"transcripts root (default: {DEFAULT_ROOT})")
    wastep.add_argument(
        "--since",
        choices=("7d", "30d", "all"),
        default="30d",
        help="time window by file mtime (default: 30d)",
    )
    wastep.add_argument("--json", action="store_true", help="emit a single JSON object")
    wastep.set_defaults(func=_cmd_waste)

    routingp = subparsers.add_parser(
        "routing", help="tokens and API-equivalent dollars by model and role"
    )
    routingp.add_argument(
        "--root", default=None, help=f"transcripts root (default: {DEFAULT_ROOT})"
    )
    routingp.add_argument(
        "--since",
        choices=("7d", "30d", "all"),
        default="30d",
        help="time window by file mtime (default: 30d)",
    )
    routingp.add_argument(
        "--prices", default=None, help="price table JSON (default: shipped table)"
    )
    routingp.add_argument(
        "--whatif",
        choices=("haiku", "sonnet", "both"),
        default="both",
        help="cheaper sub-agent tier to re-price against (default: both)",
    )
    routingp.add_argument("--json", action="store_true", help="emit a single JSON object")
    routingp.set_defaults(func=_cmd_routing)

    weekp = subparsers.add_parser(
        "week", help="experimental: one shareable weekly summary of all the analyzers"
    )
    weekp.add_argument("--root", default=None, help=f"transcripts root (default: {DEFAULT_ROOT})")
    weekp.add_argument("--days", type=int, default=7, help="time window in days (default: 7)")
    weekp.add_argument(
        "--share", action="store_true", help="append the version and the safe-to-paste line"
    )
    weekp.add_argument("--json", action="store_true", help="emit a single JSON object")
    weekp.add_argument(
        "--save",
        action="store_true",
        help="also write one numbers-only snapshot per day for cfxd trend",
    )
    _add_state_dir(weekp)
    weekp.set_defaults(func=_cmd_week)

    trendp = subparsers.add_parser(
        "trend", help="experimental: week-over-week snapshots, always labelled OBSERVED"
    )
    trendp.add_argument(
        "--weeks", type=int, default=8, help="how many snapshots to read (default: 8)"
    )
    trendp.add_argument(
        "--share", action="store_true", help="append the safe-to-paste line (numbers only)"
    )
    trendp.add_argument("--json", action="store_true", help="emit a single JSON object")
    _add_state_dir(trendp)
    trendp.set_defaults(func=_cmd_trend)

    ledgerp = subparsers.add_parser(
        "ledger", help="experimental: what the plugin did and what changed, with evidence tiers"
    )
    ledgerp.add_argument("--root", default=None, help=f"transcripts root (default: {DEFAULT_ROOT})")
    ledgerp.add_argument("--days", type=int, default=30, help="time window in days (default: 30)")
    ledgerp.add_argument(
        "--share", action="store_true", help="append the safe-to-paste line (numbers only)"
    )
    ledgerp.add_argument("--json", action="store_true", help="emit a single JSON object")
    _add_state_dir(ledgerp)
    ledgerp.set_defaults(func=_cmd_ledger)

    checkpoint_parser = subparsers.add_parser(
        "checkpoint", help="experimental: save, show, import or clear a restart checkpoint"
    )
    checkpoint_parser.add_argument("action", choices=["save", "show", "clear", "import"])
    checkpoint_parser.add_argument("file", nargs="?", help="packet file (import only)")
    checkpoint_parser.add_argument("--cwd", default=None, help="directory (default: current)")
    checkpoint_parser.add_argument(
        "--note", default=None, help="optional goal note (up to 300 characters)"
    )
    checkpoint_parser.add_argument(
        "--force", action="store_true", help="import a packet older than 24 hours"
    )
    _add_state_dir(checkpoint_parser)
    checkpoint_parser.set_defaults(func=_cmd_checkpoint)

    packet_parser = subparsers.add_parser(
        "packet", help="experimental: validate, build, render or install a Context Packet v0 file"
    )
    packet_sub = packet_parser.add_subparsers(dest="action", required=True)
    packet_validate = packet_sub.add_parser("validate", help="check a packet against the v0 rules")
    packet_validate.add_argument("file", help="packet JSON file")
    packet_validate.add_argument(
        "--cwd", default=None, help="also stat listed files against this directory"
    )
    packet_validate.add_argument("--json", action="store_true", help="emit a single JSON object")
    packet_validate.set_defaults(func=_cmd_packet)
    packet_build = packet_sub.add_parser(
        "build", help="build a packet from an agent session log (observed facts only)"
    )
    packet_build.add_argument("--agent", required=True, choices=list(handoff.AGENTS))
    packet_build.add_argument(
        "--root", default=None, help="session log root (default: the agent's own)"
    )
    packet_build.add_argument("--session", default="latest", help="session id, or latest (default)")
    packet_build.add_argument("--cwd", default=None, help="directory (default: current)")
    packet_build.add_argument("--out", default=None, help="write the packet here (default: stdout)")
    packet_build.set_defaults(func=_cmd_packet)
    packet_render = packet_sub.add_parser("render", help="print a packet for another agent")
    packet_render.add_argument("file", help="packet JSON file")
    packet_render.add_argument(
        "--format", choices=["agents-md", "plain"], default="agents-md", help="output form"
    )
    packet_render.add_argument(
        "--cwd", default=None, help="stat listed files against this directory"
    )
    packet_render.set_defaults(func=_cmd_packet)
    packet_install = packet_sub.add_parser(
        "install-agents-md", help="write or remove the marked packet block in AGENTS.md"
    )
    packet_install.add_argument("--cwd", default=None, help="directory (default: current)")
    packet_install.add_argument("--packet", default=None, help="packet JSON file to install")
    packet_install.add_argument("--remove", action="store_true", help="remove the marked block")
    packet_install.set_defaults(func=_cmd_packet)

    briefp = subparsers.add_parser(
        "brief", help="show what each sub-agent handoff carried and what the child did with it"
    )
    briefp.add_argument("--session", default=None, help="only handoffs from this session id")
    briefp.add_argument("--root", default=None, help=f"transcripts root (default: {DEFAULT_ROOT})")
    briefp.add_argument("--json", action="store_true", help="emit a single JSON object")
    _add_state_dir(briefp)
    briefp.set_defaults(func=_cmd_brief)

    setp = subparsers.add_parser(
        "set", help="toggle brief, carry, fold, fold_read, bound, coach or idle_notice on/off"
    )
    setp.add_argument(
        "flag",
        choices=[
            "brief",
            "carry",
            "fold",
            "fold_read",
            "bound",
            "coach",
            "idle_notice",
            "brief_v1",
            "carry_back",
        ],
        help="feature to toggle",
    )
    setp.add_argument("value", choices=["on", "off"], help="new state")
    _add_state_dir(setp)
    setp.set_defaults(func=_cmd_set)

    return parser


def _cmd_serve(args: argparse.Namespace) -> int:
    port = args.port
    if port is None:
        env = os.environ.get("CFX_PORT")
        port = int(env) if env else DEFAULT_PORT
    return run_serve(resolve_state_dir(args.state_dir), port)


def _cmd_stop(args: argparse.Namespace) -> int:
    state_dir = resolve_state_dir(args.state_dir)
    path = state_dir / STATE_FILE
    state = read_state(path)
    if state is None:
        print("cfxd stop: not running")
        return 0
    port = state.get("port")
    pid = state.get("pid")
    confirmed = False
    if (
        isinstance(port, int)
        and isinstance(pid, int)
        and pid != os.getpid()
        and verify_identity(state_dir, port)
    ):
        confirmed = True
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    path.unlink(missing_ok=True)
    if confirmed:
        print("cfxd stop: stopped")
    else:
        print("cfxd stop: stale state (removed; did not kill pid)")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    state_dir = resolve_state_dir(args.state_dir)
    state = read_state(state_dir / STATE_FILE)
    if state is None:
        print("cfxd status: not running")
        return 1
    if not daemon_alive(state):
        print("cfxd status: stale state (not answering /health)")
        return 1
    print(
        f"cfxd status: running (pid {state.get('pid')}, port {state.get('port')}, "
        f"version {state.get('version', '?')})"
    )
    return 0


def _cmd_set(args: argparse.Namespace) -> int:
    state_dir = resolve_state_dir(args.state_dir)
    set_flag(state_dir, args.flag, args.value == "on")
    if args.flag in ("bound", "coach"):
        # launch.sh reads this flag file to skip Python until the feature is on.
        flag = state_dir / f"{args.flag}.on"
        if args.value == "on":
            flag.touch()
        else:
            flag.unlink(missing_ok=True)
    print(f"cfxd set: {args.flag} {args.value}")
    return 0


def _cmd_checkpoint(args: argparse.Namespace) -> int:
    state_dir = resolve_state_dir(args.state_dir)
    cwd = args.cwd if args.cwd else os.getcwd()
    if args.action == "import":
        return _cmd_checkpoint_import(args, state_dir, cwd)
    if args.action == "clear":
        cleared = checkpoint.clear(state_dir, cwd)
        print("cfxd checkpoint: cleared" if cleared else "cfxd checkpoint: no checkpoint to clear.")
        return 0
    if args.action == "show":
        text = checkpoint.show_text(state_dir, cwd)
        if text is None:
            print("cfxd checkpoint: no checkpoint for this directory.", file=sys.stderr)
            return 1
        print(text)
        print()
        print("local only; not sent anywhere except into your next session")
        return 0
    state = read_state(state_dir / STATE_FILE)
    port = state.get("port") if state is not None else None
    token = read_token(state_dir)
    if not isinstance(port, int) or state is None or not daemon_alive(state) or token is None:
        print("cfxd checkpoint: the local helper is not running; start Claude Code first.")
        return 1
    payload = json.dumps({"cwd": cwd, "note": args.note}).encode("utf-8")
    try:
        status, body = post_hook(port, "checkpoint-save", payload, 5.0, token=token)
        obj = json.loads(body) if status == 200 else {}
    except (OSError, ValueError):
        obj = {}
    summary = obj.get("summary") if isinstance(obj, dict) else None
    if not isinstance(summary, str):
        print("cfxd checkpoint: save failed (no response from the local helper).")
        return 1
    print(summary)
    return 0


def _cmd_checkpoint_import(args: argparse.Namespace, state_dir: Path, cwd: str) -> int:
    if not args.file:
        print("cfxd checkpoint import: give a packet file.", file=sys.stderr)
        return 2
    try:
        obj = packet.load(Path(args.file))
        counts = handoff.import_packet(obj, state_dir, cwd, force=args.force)
    except (OSError, ValueError) as exc:
        print(f"cfxd checkpoint import: {exc}", file=sys.stderr)
        return 1
    print(
        f"cfxd checkpoint import: {counts['files']} files, {counts['folds']} folds, "
        f"{counts['commands']} commands, {counts['todos']} todos"
    )
    return 0


def _cmd_packet(args: argparse.Namespace) -> int:
    if args.action == "build":
        return _cmd_packet_build(args)
    if args.action == "render":
        return _cmd_packet_render(args)
    if args.action == "install-agents-md":
        return _cmd_packet_install(args)
    try:
        obj = packet.load(Path(args.file))
    except (OSError, ValueError) as exc:
        print(f"cfxd packet validate: cannot read {args.file}: {exc}", file=sys.stderr)
        return 2
    checks = packet.validate(obj, args.cwd)
    ok = packet.passed(checks)
    if args.json:
        print(
            json.dumps(
                {"file": args.file, "ok": ok, "checks": [check.as_dict() for check in checks]}
            )
        )
    else:
        for check in checks:
            print(f"{check.status} {check.name}: {check.reason}")
    return 0 if ok else 1


def _cmd_packet_build(args: argparse.Namespace) -> int:
    cwd = args.cwd if args.cwd else os.getcwd()
    root = Path(args.root) if args.root else None
    try:
        obj = handoff.build_packet(args.agent, root, args.session, cwd)
    except (LookupError, OSError) as exc:
        print(f"cfxd packet build: {exc}", file=sys.stderr)
        return 1
    text = json.dumps(obj, indent=2)
    if args.out:
        try:
            Path(args.out).write_text(text + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"cfxd packet build: cannot write {args.out}: {exc}", file=sys.stderr)
            return 2
    else:
        print(text)
    counts = checkpoint._counts(obj)
    test = 1 if "test" in obj else 0
    print(
        f"cfxd packet build: {counts['files']} files, {counts['folds']} folds, "
        f"{counts['commands']} commands, {counts['todos']} todos, {test} test",
        file=sys.stderr,
    )
    return 0


def _cmd_packet_render(args: argparse.Namespace) -> int:
    try:
        obj = packet.load(Path(args.file))
        text, _dropped = handoff.render_packet(obj, args.format, args.cwd)
    except (OSError, ValueError) as exc:
        print(f"cfxd packet render: {exc}", file=sys.stderr)
        return 2
    print(text)
    return 0


def _cmd_packet_install(args: argparse.Namespace) -> int:
    cwd = args.cwd if args.cwd else os.getcwd()
    block: str | None = None
    if not args.remove:
        if not args.packet:
            print("cfxd packet install-agents-md: give --packet or --remove.", file=sys.stderr)
            return 2
        try:
            obj = packet.load(Path(args.packet))
            block, _dropped = handoff.render_packet(obj, "agents-md", cwd)
        except (OSError, ValueError) as exc:
            print(f"cfxd packet install-agents-md: {exc}", file=sys.stderr)
            return 2
    try:
        action, size = handoff.install_agents_md(cwd, block)
    except (OSError, ValueError) as exc:
        print(f"cfxd packet install-agents-md: {exc}", file=sys.stderr)
        return 1
    print(f"cfxd packet install-agents-md: {action} ({size} bytes)")
    return 0


def _measure_shim(state_dir: Path, runs: int) -> tuple[list[float], int]:
    env = {**os.environ, "CFX_HOME": str(state_dir)}
    samples: list[float] = []
    failures = 0
    for _ in range(runs):
        start = time.perf_counter()
        try:
            proc = subprocess.run(
                [sys.executable, "-I", "-S", str(_SHIM_PATH), "PreToolUse"],
                input=b"{}",
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired:
            failures += 1
            continue
        samples.append((time.perf_counter() - start) * 1000.0)
        if proc.returncode != 0:
            failures += 1
    return samples, failures


def _warn_outside_profile(state_dir: Path) -> None:
    """On Windows, warn once when the state dir (and its token) is not under the user profile."""
    if not _IS_WINDOWS:
        return
    profile = os.environ.get("USERPROFILE")
    if not profile:
        return
    try:
        state_dir.resolve().relative_to(Path(profile).resolve())
    except (OSError, ValueError):
        print(
            "cfxd doctor: warning: state directory is outside your user profile; "
            "on Windows other users may be able to read its token. Move CFX_HOME inside it."
        )


def _hook_delivery_warning(report: dict[str, Any], threshold: float = 0.70) -> str | None:
    """Warning when the helper saw too few tool calls; counts only, no paths."""
    delivery = report["delivery"]
    if delivery is None or delivery["overall"] >= threshold:
        return None
    return (
        f"cfxd doctor: warning: the helper saw {delivery['overall'] * 100:.0f}% of tool calls "
        f"in the last {report['days']} days ({report['sessions_compared']} sessions); "
        "checkpoints are completed from the transcript. Close heavy processes to reduce load."
    )


def _sh_check() -> tuple[bool, str]:
    """The hooks run ``sh launch.sh``; Windows gets that from Git for Windows."""
    found = shutil.which("sh")
    if found:
        return True, f"sh:          found ({found})"
    return False, (
        "sh:          NOT found. Windows needs Git for Windows: "
        + "https://git-scm.com/download/win\n"
        + '             after installing, open a new terminal; check: sh -c "echo ok"'
    )


def _cmd_doctor(args: argparse.Namespace) -> int:
    state_dir = resolve_state_dir(args.state_dir)
    _warn_outside_profile(state_dir)
    sh_ok, sh_line = _sh_check()
    state_file = state_dir / STATE_FILE
    state = read_state(state_file)
    if state is None:
        print(f"cfxd doctor: no state file at {state_file}; is the daemon running?")
        return 1
    port = state.get("port")
    if not isinstance(port, int):
        print("cfxd doctor: state file is malformed")
        return 1

    try:
        health_status, health_body = get_health(port)
    except OSError as exc:
        print(f"cfxd doctor: /health unreachable: {exc}")
        return 1
    try:
        health = json.loads(health_body)
    except ValueError:
        health = {}
    healthy = health_status == 200 and isinstance(health, dict) and health.get("ok") is True

    samples: list[float] = []
    ok_trips = 0
    token = read_token(state_dir)
    for _ in range(200):
        start = time.perf_counter()
        try:
            status, _ = post_hook(port, "PreToolUse", b"{}", token=token)
        except OSError:
            status = -1
        samples.append((time.perf_counter() - start) * 1000.0)
        if status == 200:
            ok_trips += 1
    p50 = percentile(samples, 50)
    p95 = percentile(samples, 95)

    shim_samples, shim_failures = _measure_shim(state_dir, runs=20)
    shim_p50 = percentile(shim_samples, 50)
    shim_p95 = percentile(shim_samples, 95)

    print(
        f"cfxd doctor: http://127.0.0.1:{port} (pid {state.get('pid')}, "
        f"version {state.get('version', '?')})"
    )
    cfg = read_config(state_dir)
    brief = "on" if cfg["brief"] else "off"
    carry = "on" if cfg["carry"] else "off"
    fold = "on" if cfg["fold"] else "off"
    brief_v1 = "on" if cfg.get("brief_v1") else "off"
    carry_back = "on" if cfg.get("carry_back") else "off"
    print(
        f"flags:       brief={brief} carry={carry} fold={fold} "
        f"brief_v1={brief_v1} carry_back={carry_back}"
    )
    print(f"health:      {'ok' if healthy else 'FAIL'}")
    print(f"identity:    {'verified' if verify_identity(state_dir, port) else 'NOT verified'}")
    print(sh_line)
    print(
        f"round trips: n={len(samples)} p50={p50:.2f} ms p95={p95:.2f} ms "
        f"({ok_trips}/{len(samples)} ok)"
    )
    print(
        f"shim cold:   n={len(shim_samples)} p50={shim_p50:.2f} ms "
        f"p95={shim_p95:.2f} ms ({shim_failures} failed)"
    )

    if not healthy or not sh_ok or ok_trips != len(samples) or p95 > 50.0:
        print("cfxd doctor: unhealthy")
        return 1
    print("cfxd doctor: healthy")
    warning = _hook_delivery_warning(coverage(DEFAULT_ROOT, state_dir, 7))
    if warning is not None:
        print(warning)
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    stats = collect_stats(resolve_state_dir(args.state_dir), args.since, args.session)
    if args.json:
        print(json.dumps(stats))
    else:
        print(format_human(stats))
    return 0


def _cmd_audit(args: argparse.Namespace) -> int:
    root = Path(args.root) if args.root else DEFAULT_ROOT
    report = audit(root, args.since)
    if args.json:
        print(json.dumps(report))
    else:
        print(format_audit(report, root.name))
    return 0


def _cmd_coverage(args: argparse.Namespace) -> int:
    if args.days < 1:
        print("cfxd coverage: --days must be at least 1", file=sys.stderr)
        return 2
    root = Path(args.root) if args.root else DEFAULT_ROOT
    report = coverage(root, resolve_state_dir(args.state_dir), args.days)
    if args.json:
        print(json.dumps(report))
    else:
        print(format_coverage_report(report))
    return 0


def _cmd_tour(args: argparse.Namespace) -> int:
    root = Path(args.root) if args.root else DEFAULT_ROOT
    return run_tour(root, args.yes, args.state_dir)


def _cmd_agent_audit(args: argparse.Namespace) -> int:
    root = Path(args.root) if args.root else default_root(args.agent)
    try:
        report = build_report(args.agent, READERS[args.agent](root, since_cutoff(args.since)))
    except OpenCodeDBError as exc:
        print(f"cfxd agent-audit: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report))
    else:
        print(format_agent_report(report, root.name))
    return 0


def _cmd_diet(args: argparse.Namespace) -> int:
    if (args.apply or args.undo) and (args.share or args.json):
        print(
            "cfxd diet: --apply/--undo cannot be combined with --share or --json", file=sys.stderr
        )
        return 2
    if args.yes and not args.apply:
        print("cfxd diet: --yes needs --apply", file=sys.stderr)
        return 2
    if args.all and not args.undo:
        print("cfxd diet: --all needs --undo", file=sys.stderr)
        return 2
    if args.undo:
        return undo_records(args.all)
    root = Path(args.root) if args.root else DEFAULT_ROOT
    report = _anonymize(diet(root, args.since)) if args.share else diet(root, args.since)
    if args.apply:
        return apply_report(report, yes=args.yes)
    if args.json:
        print(json.dumps(report))
    else:
        print(format_diet(report, share=args.share, dir_name=root.name))
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    root = Path(args.root) if args.root else DEFAULT_ROOT
    ks = _parse_ints(args.k)
    batches = _parse_ints(args.batch)
    reset_ks = _parse_ints(args.reset_at)
    summary_tokens = _parse_ints(args.summary_tokens)
    refetch = _parse_floats(args.refetch)
    if any(not 0.0 <= f <= 1.0 for f in refetch):
        print("cfxd replay: --refetch values must be between 0 and 1", file=sys.stderr)
        return 2
    report = replay(
        root,
        args.since,
        ks,
        batches,
        args.min_bytes,
        args.stub_bytes,
        reset_ks,
        summary_tokens,
        refetch,
    )
    if args.json:
        print(json.dumps(report))
    else:
        print(format_replay(report, root.name))
    return 0


def _cmd_refetch(args: argparse.Namespace) -> int:
    root = Path(args.root) if args.root else DEFAULT_ROOT
    report = refetch(root, args.since, args.window)
    if args.json:
        print(json.dumps(report))
    else:
        print(format_refetch(report, root.name))
    return 0


def _cmd_fidelity(args: argparse.Namespace) -> int:
    root = Path(args.root) if args.root else DEFAULT_ROOT
    report = fidelity(root, args.since)
    if args.json:
        print(json.dumps(report))
    else:
        print(format_fidelity_report(report, root.name))
    return 0


def _cmd_waste(args: argparse.Namespace) -> int:
    root = Path(args.root) if args.root else DEFAULT_ROOT
    report = waste(root, args.since)
    if args.json:
        print(json.dumps(report))
    else:
        print(format_waste(report, root.name))
    return 0


def _cmd_routing(args: argparse.Namespace) -> int:
    root = Path(args.root) if args.root else DEFAULT_ROOT
    prices = Path(args.prices) if args.prices else None
    report = routing(root, args.since, prices, args.whatif)
    if args.json:
        print(json.dumps(report))
    else:
        print(format_routing(report, root.name))
    return 0


def _cmd_week(args: argparse.Namespace) -> int:
    if args.days < 1:
        print("cfxd week: --days must be at least 1", file=sys.stderr)
        return 2
    root = Path(args.root) if args.root else DEFAULT_ROOT
    state_dir = resolve_state_dir(args.state_dir)
    report = week(root, args.days, state_dir, share=args.share)
    print(json.dumps(report) if args.json else format_week(report, args.share))
    if args.save:
        save_snapshot(state_dir, report)
    return 0


def _cmd_trend(args: argparse.Namespace) -> int:
    if args.weeks < 1:
        print("cfxd trend: --weeks must be at least 1", file=sys.stderr)
        return 2
    report = trend(resolve_state_dir(args.state_dir), args.weeks, share=args.share)
    print(json.dumps(report) if args.json else format_trend(report, args.share))
    return 0


def _cmd_ledger(args: argparse.Namespace) -> int:
    if args.days < 1:
        print("cfxd ledger: --days must be at least 1", file=sys.stderr)
        return 2
    root = Path(args.root) if args.root else DEFAULT_ROOT
    report = ledger(root, args.days, resolve_state_dir(args.state_dir), share=args.share)
    print(json.dumps(report) if args.json else format_ledger(report, args.share))
    return 0


_BRIEF_EMPTY_OFF = (
    "No briefs have been logged yet.\n"
    "Brief v1 is off. Turn it on with: cfxd set brief on && cfxd set brief_v1 on\n"
    "Then delegate work to a sub-agent and run `cfxd brief` again."
)
_BRIEF_EMPTY_ON = (
    "No briefs have been logged yet.\n"
    "Brief v1 is on. Delegate work to a sub-agent and run `cfxd brief` again."
)


def _read_briefs(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return entries
    for line in lines:
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            entries.append(obj)
    return entries


def _rel(path: str) -> str:
    try:
        return Path(path).resolve().relative_to(Path.cwd().resolve()).as_posix()
    except (ValueError, OSError):
        return path


def _brief_details(entry: dict[str, Any], r: BriefReceipt) -> list[dict[str, Any]]:
    """v1 items (path hash + reasons) with the path recovered from what the child saw."""
    known = {path_sha16(p): p for p in [*r.briefed_files, *r.child_files_read]}
    out: list[dict[str, Any]] = []
    items = entry.get("items")
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        reasons = it.get("r")
        out.append(
            {
                "path": known.get(str(it.get("p"))),
                "reasons": [x for x in reasons if isinstance(x, str)]
                if isinstance(reasons, list)
                else [],
                "excerpt_ranges": it.get("x") if isinstance(it.get("x"), list) else [],
            }
        )
    return out


def _format_brief(entries: list[dict[str, Any]], receipts: list[BriefReceipt]) -> str:
    out = [f"Brief handoffs: {len(receipts)}", ""]
    for n, (entry, r) in enumerate(zip(entries, receipts, strict=True), 1):
        agent = r.subagent_type or "agent"
        sid = (r.session_id or "?")[:8]
        out.append(f"{n}. {agent}  session {sid}  {r.ts or ''}".rstrip())
        budget = entry.get("budget_bytes")
        size = f"{r.packet_bytes:,} bytes"
        if isinstance(budget, int) and budget > 0:
            size += f" of {budget:,} budget ({r.packet_bytes * 100 // budget}%)"
        if r.packet_items is not None:
            size += f", {r.packet_items} items"
        rows: list[tuple[str, str]] = [
            ("decision", r.decision + (f" ({r.skip_reason})" if r.skip_reason else ""))
            if r.decision
            else ("decision", "unknown"),
            ("target model", r.target_model or str(entry.get("target_model") or "unknown")),
            ("packet", size),
        ]
        if r.matched:
            rows.append(
                (
                    "re-read anyway",
                    f"{len(r.rereads_of_briefed)} of {len(r.briefed_files)} briefed files"
                    + "".join(f"\n    {_rel(p)}" for p in r.rereads_of_briefed),
                )
            )
            if r.parent_read_not_briefed:
                rows.append(
                    (
                        "left out, re-read",
                        f"{len(r.parent_read_not_briefed)} files the parent had read"
                        + "".join(f"\n    {_rel(p)}" for p in r.parent_read_not_briefed),
                    )
                )
            t = r.child_tokens
            rows.append(
                (
                    "child tokens",
                    f"{t.get('total', 0):,} total over {r.child_turns} turns "
                    f"(input {t.get('input', 0):,}, output {t.get('output', 0):,}, "
                    f"cache read {t.get('cache_read', 0):,}, "
                    f"cache write {t.get('cache_creation', 0):,})",
                )
            )
            rows.append(("compactions", f"{r.compactions}, context overflows {r.overflows}"))
        rows.append(("outcome", r.outcome or "unknown"))
        if not r.matched:
            rows.append(("unmatched", r.unmatched_reason or "unknown"))
        for label, value in rows:
            out.append(f"   {label:<18} {value}")
        details = _brief_details(entry, r)
        if details:
            out.append("   top items and why:")
            for d in details[:5]:
                where = _rel(d["path"]) if d["path"] else "(path not recoverable)"
                out.append(f"    {where}  [{', '.join(d['reasons']) or 'no reason recorded'}]")
            if len(details) > 5:
                out.append(f"    ... {len(details) - 5} more")
        out.append("")
    return "\n".join(out).rstrip()


def _cmd_brief(args: argparse.Namespace) -> int:
    state_dir = resolve_state_dir(args.state_dir)
    root = Path(args.root) if args.root else DEFAULT_ROOT
    entries = _read_briefs(state_dir / "briefs.jsonl")
    if args.session:
        entries = [e for e in entries if e.get("session_id") == args.session]
    if not entries:
        state = "on" if read_config(state_dir).get("brief_v1") else "off"
        if args.json:
            print(json.dumps({"receipts": [], "details": [], "brief_v1": state}))
        elif args.session:
            print(f"No briefs logged for session {args.session}.")
        else:
            print(_BRIEF_EMPTY_ON if state == "on" else _BRIEF_EMPTY_OFF)
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "briefs.jsonl"
        log.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
        receipts = join_receipts(log, root)
    if args.json:
        details = [
            {
                "tool_use_id": r.tool_use_id,
                "budget_bytes": e.get("budget_bytes"),
                "items": _brief_details(e, r),
            }
            for e, r in zip(entries, receipts, strict=True)
        ]
        print(json.dumps({"receipts": receipts_to_json(receipts), "details": details}))
    else:
        print(_format_brief(entries, receipts))
    return 0


def _parse_ints(value: str) -> tuple[int, ...]:
    parts = [p.strip() for p in value.split(",") if p.strip()]
    return tuple(int(p) for p in parts)


def _parse_floats(value: str) -> tuple[float, ...]:
    parts = [p.strip() for p in value.split(",") if p.strip()]
    return tuple(float(p) for p in parts)


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_output()
    parsed = _build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    result: Any = parsed.func(parsed)
    return int(result) if result is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())
