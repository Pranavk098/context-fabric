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

from cfxd.agents import READERS, build_report, default_root, since_cutoff
from cfxd.agents import format_human as format_agent_report
from cfxd.audit import DEFAULT_ROOT, audit
from cfxd.audit import format_human as format_audit
from cfxd.config import read_config, set_flag
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
from cfxd.replay import format_replay, replay
from cfxd.stats import collect_stats, format_human
from cfxd.waste import format_waste, waste

_SHIM_PATH = Path(__file__).with_name("shim.py")


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
    replayp.add_argument("--json", action="store_true", help="emit a single JSON object")
    replayp.set_defaults(func=_cmd_replay)

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

    setp = subparsers.add_parser("set", help="toggle brief, carry, fold or fold_read on/off")
    setp.add_argument(
        "flag", choices=["brief", "carry", "fold", "fold_read"], help="feature to toggle"
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
    if not isinstance(port, int) or not isinstance(pid, int):
        path.unlink(missing_ok=True)
        print("cfxd stop: stale state (removed)")
        return 0
    confirmed = False
    try:
        status, body = get_health(port)
        health = json.loads(body)
        confirmed = status == 200 and isinstance(health, dict) and health.get("pid") == pid
    except (OSError, ValueError):
        confirmed = False
    if confirmed and pid != os.getpid():
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
    set_flag(resolve_state_dir(args.state_dir), args.flag, args.value == "on")
    print(f"cfxd set: {args.flag} {args.value}")
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


def _cmd_doctor(args: argparse.Namespace) -> int:
    state_dir = resolve_state_dir(args.state_dir)
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


def _cmd_agent_audit(args: argparse.Namespace) -> int:
    if args.agent == "opencode":
        print("agent 'opencode' not supported yet", file=sys.stderr)
        return 2
    root = Path(args.root) if args.root else default_root(args.agent)
    report = build_report(args.agent, READERS[args.agent](root, since_cutoff(args.since)))
    if args.json:
        print(json.dumps(report))
    else:
        print(format_agent_report(report, root.name))
    return 0


def _cmd_diet(args: argparse.Namespace) -> int:
    root = Path(args.root) if args.root else DEFAULT_ROOT
    report = _anonymize(diet(root, args.since)) if args.share else diet(root, args.since)
    if args.json:
        print(json.dumps(report))
    else:
        print(format_diet(report, share=args.share, dir_name=root.name))
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    root = Path(args.root) if args.root else DEFAULT_ROOT
    ks = _parse_ints(args.k)
    batches = _parse_ints(args.batch)
    report = replay(root, args.since, ks, batches, args.min_bytes, args.stub_bytes)
    if args.json:
        print(json.dumps(report))
    else:
        print(format_replay(report, root.name))
    return 0


def _cmd_waste(args: argparse.Namespace) -> int:
    root = Path(args.root) if args.root else DEFAULT_ROOT
    report = waste(root, args.since)
    if args.json:
        print(json.dumps(report))
    else:
        print(format_waste(report, root.name))
    return 0


def _parse_ints(value: str) -> tuple[int, ...]:
    parts = [p.strip() for p in value.split(",") if p.strip()]
    return tuple(int(p) for p in parts)


def main(argv: list[str] | None = None) -> int:
    parsed = _build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    result: Any = parsed.func(parsed)
    return int(result) if result is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())
