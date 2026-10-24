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

from cfxd.config import read_config, set_flag
from cfxd.daemon import (
    DEFAULT_PORT,
    STATE_FILE,
    daemon_alive,
    get_health,
    percentile,
    post_hook,
    read_state,
    resolve_state_dir,
    run_serve,
)
from cfxd.stats import collect_stats, format_human

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

    setp = subparsers.add_parser("set", help="toggle brief or carry on/off")
    setp.add_argument("flag", choices=["brief", "carry"], help="feature to toggle")
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
    pid = state.get("pid")
    if isinstance(pid, int) and pid != os.getpid():
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    path.unlink(missing_ok=True)
    print("cfxd stop: stopped")
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
    for _ in range(200):
        start = time.perf_counter()
        try:
            status, _ = post_hook(port, "PreToolUse", b"{}")
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
    print(f"flags:       brief={brief} carry={carry}")
    print(f"health:      {'ok' if healthy else 'FAIL'}")
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


def main(argv: list[str] | None = None) -> int:
    parsed = _build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    result: Any = parsed.func(parsed)
    return int(result) if result is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())
