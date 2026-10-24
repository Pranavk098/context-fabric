"""Fail-open launcher for command-hook clients (SessionStart / PreCompact).

Run directly, from any cwd, with no ``cfxd`` package on the path::

    uv run --no-project --quiet --python ">=3.12" <root>/cfxd/launch.py session-start
    uv run --no-project --quiet --python ">=3.12" <root>/cfxd/launch.py forward PreCompact
    uv run --no-project --quiet --python ">=3.12" <root>/cfxd/launch.py ensure

``session-start`` ensures the resident daemon is up (spawning a detached
``cfxd serve`` if /health fails), forwards the hook event JSON from stdin, and
prints the daemon's response body — or ``{}`` on any failure — always exiting 0.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

# This file lives at <plugin>/cfxd/launch.py; the plugin root is its parent's
# parent. Put it on sys.path so `import cfxd` works from any cwd.
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from cfxd.daemon import DEFAULT_PORT, _health_ok, post_hook, resolve_state_dir, verify_identity  # noqa: E402
from cfxd.fsutil import ensure_dir, open_log  # noqa: E402

HEALTH_POLL_SECONDS = 3.0

_SQUATTER_WARNING = (
    '{"systemMessage":"Context Fabric: something else is using port 47831, '
    "or an old Context Fabric daemon is running. Hooks are not protected. "
    'Restart your computer or run: python -m cfxd stop"}'
)

# Windows process creation flags: detached, own process group, no console window.
_DETACHED = 0x00000008
_NEW_PROCESS_GROUP = 0x00000200
_NO_WINDOW = 0x08000000


def _port() -> int:
    env = os.environ.get("CFX_PORT")
    return int(env) if env else DEFAULT_PORT


def _spawn(state_dir: Path, port: int) -> None:
    ensure_dir(state_dir)
    cmd = [
        sys.executable,
        "-m",
        "cfxd",
        "serve",
        "--state-dir",
        str(state_dir),
        "--port",
        str(port),
    ]
    old_umask = os.umask(0o077)
    try:
        with open_log(state_dir / "daemon.log") as log:
            if os.name == "nt":
                subprocess.Popen(
                    cmd,
                    cwd=str(_PLUGIN_ROOT),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    creationflags=_DETACHED | _NEW_PROCESS_GROUP | _NO_WINDOW,
                )
            else:
                subprocess.Popen(
                    cmd,
                    cwd=str(_PLUGIN_ROOT),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
    finally:
        os.umask(old_umask)


def _wait_healthy(port: int, timeout: float = HEALTH_POLL_SECONDS) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _health_ok(port):
            return True
        time.sleep(0.05)
    return _health_ok(port)


def _ensure_daemon(port: int) -> bool:
    _spawn(resolve_state_dir(), port)
    return _wait_healthy(port)


def _post(event: str, payload: bytes, timeout: float) -> None:
    try:
        status, body = post_hook(_port(), event, payload, timeout)
    except Exception:
        status, body = -1, b"{}"
    if status == 200:
        print(body.decode("utf-8"))
    else:
        print("{}")


def _session_start() -> int:
    payload = sys.stdin.buffer.read()
    port = _port()
    state_dir = resolve_state_dir()
    if not _health_ok(port):
        _ensure_daemon(port)
    if not verify_identity(state_dir, port):
        if _health_ok(port):
            # Something answers /health but cannot prove it is our daemon.
            print(_SQUATTER_WARNING)
            return 0
        # Nothing answers: fall through to the normal fail-open forward.
    _post("SessionStart", payload, 1.0)
    return 0


def _forward(event: str) -> int:
    _post(event, sys.stdin.buffer.read(), 1.0)
    return 0


def _ensure() -> int:
    port = _port()
    state_dir = resolve_state_dir()
    if _health_ok(port):
        print("already running" if verify_identity(state_dir, port) else _SQUATTER_WARNING)
    elif _ensure_daemon(port):
        print("started" if verify_identity(state_dir, port) else _SQUATTER_WARNING)
    else:
        print("start failed")
    return 0


def main(argv: list[str] | None = None) -> int:
    # A hook must never fail: any unexpected error degrades to an empty answer.
    try:
        return _dispatch(sys.argv[1:] if argv is None else argv)
    except Exception:
        print("{}")
        return 0


def _dispatch(args: list[str]) -> int:
    if args and args[0] == "session-start":
        return _session_start()
    if len(args) >= 2 and args[0] == "forward":
        return _forward(args[1])
    if args and args[0] == "ensure":
        return _ensure()
    print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
