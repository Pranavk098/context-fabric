"""``python -m cfxd`` CLI: serve, stop, status, doctor."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from cfxd import checkpoint
from cfxd.agents import READERS, build_report, default_root, since_cutoff
from cfxd.agents import format_human as format_agent_report
from cfxd.agents.opencode import OpenCodeDBError
from cfxd.audit import DEFAULT_ROOT, audit
from cfxd.audit import format_human as format_audit
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
from cfxd.waste import format_waste, waste
from cfxd.week import format_week, week

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
    _add_state_dir(weekp)
    weekp.set_defaults(func=_cmd_week)

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
        "checkpoint", help="experimental: save, show or clear a restart checkpoint"
    )
    checkpoint_parser.add_argument("action", choices=["save", "show", "clear"])
    checkpoint_parser.add_argument("--cwd", default=None, help="directory (default: current)")
    checkpoint_parser.add_argument(
        "--note", default=None, help="optional goal note (up to 300 characters)"
    )
    _add_state_dir(checkpoint_parser)
    checkpoint_parser.set_defaults(func=_cmd_checkpoint)

    setp = subparsers.add_parser(
        "set", help="toggle brief, carry, fold, fold_read, bound, coach or idle_notice on/off"
    )
    setp.add_argument(
        "flag",
        choices=["brief", "carry", "fold", "fold_read", "bound", "coach", "idle_notice"],
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


def _cmd_doctor(args: argparse.Namespace) -> int:
    state_dir = resolve_state_dir(args.state_dir)
    _warn_outside_profile(state_dir)
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
    print(f"flags:       brief={brief} carry={carry} fold={fold}")
    print(f"health:      {'ok' if healthy else 'FAIL'}")
    print(f"identity:    {'verified' if verify_identity(state_dir, port) else 'NOT verified'}")
    print(
        f"round trips: n={len(samples)} p50={p50:.2f} ms p95={p95:.2f} ms "
        f"({ok_trips}/{len(samples)} ok)"
    )
    print(
        f"shim cold:   n={len(shim_samples)} p50={shim_p50:.2f} ms "
        f"p95={shim_p95:.2f} ms ({shim_failures} failed)"
    )

    if not healthy or ok_trips != len(samples) or p95 > 50.0:
        print("cfxd doctor: unhealthy")
        return 1
    print("cfxd doctor: healthy")
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
    report = week(root, args.days, resolve_state_dir(args.state_dir), share=args.share)
    print(json.dumps(report) if args.json else format_week(report, args.share))
    return 0


def _cmd_ledger(args: argparse.Namespace) -> int:
    if args.days < 1:
        print("cfxd ledger: --days must be at least 1", file=sys.stderr)
        return 2
    root = Path(args.root) if args.root else DEFAULT_ROOT
    report = ledger(root, args.days, resolve_state_dir(args.state_dir), share=args.share)
    print(json.dumps(report) if args.json else format_ledger(report, args.share))
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
