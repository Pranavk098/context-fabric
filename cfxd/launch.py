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

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# This file lives at <plugin>/cfxd/launch.py; the plugin root is its parent's
# parent. Put it on sys.path so `import cfxd` works from any cwd.
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from cfxd.fsutil import ensure_dir, open_log  # noqa: E402
from cfxd.hookclient import (  # noqa: E402
    DEFAULT_PORT,
    _health_ok,
    post_hook,
    read_token,
    resolve_state_dir,
    verify_identity,
)

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


_PY_CACHE_FILE = "python"


def _write_python_cache(state_dir: Path) -> None:
    """Cache this interpreter's path so launch.sh forward can skip ``uv run`` startup."""
    try:
        ensure_dir(state_dir)
        fd, tmp = tempfile.mkstemp(dir=str(state_dir), prefix=".python.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(sys.executable)
                fh.flush()
            os.chmod(tmp, 0o600)
            os.replace(tmp, state_dir / _PY_CACHE_FILE)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except OSError:
        pass


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


def _post(event: str, payload: bytes, timeout: float, token: str | None) -> None:
    try:
        status, body = post_hook(_port(), event, payload, timeout, token=token)
    except Exception:
        status, body = -1, b"{}"
    if status == 200:
        print(body.decode("utf-8"))
    else:
        print("{}")


_WELCOME_MARKER = "welcomed"
_WELCOME = json.dumps(
    {
        "systemMessage": (
            "Context Fabric (alpha) is active. It records which files and outputs this session "
            "touches so sub-agents and /compact can reuse them. Nothing leaves your machine. "
            "Check it with `cfxd doctor`; what to try and what to send back: "
            "https://github.com/Pranavk098/context-fabric/blob/main/docs/TESTING-GUIDE.md"
        )
    }
)


def _first_run(state_dir: Path) -> bool:
    """True exactly once per install: creates the marker so the welcome is shown a single time."""
    marker = state_dir / _WELCOME_MARKER
    try:
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError:
        return False
    os.close(fd)
    return True


def _session_start() -> int:
    payload = sys.stdin.buffer.read()
    port = _port()
    state_dir = resolve_state_dir()
    _write_python_cache(state_dir)
    if not _health_ok(port):
        _ensure_daemon(port)
    token = read_token(state_dir)
    verified = verify_identity(state_dir, port)
    if not verified:
        if _health_ok(port):
            # Something answers /health but cannot prove it is our daemon.
            print(_SQUATTER_WARNING)
            return 0
        # Nothing answers: fall through to the normal fail-open forward.
    if verified and token is not None and _first_run(state_dir):
        try:
            post_hook(port, "SessionStart", payload, 1.0, token)
        except Exception:
            pass
        print(_WELCOME)
        return 0
    _post("SessionStart", payload, 1.0, token)
    return 0


def _forward(event: str) -> int:
    # Never spawn here (only session-start does). If the daemon is down or its
    # identity cannot be proven, send nothing so a squatter never sees the token.
    payload = sys.stdin.buffer.read()
    state_dir = resolve_state_dir()
    port = _port()
    token = read_token(state_dir)
    if token is None or not verify_identity(state_dir, port):
        print("{}")
        return 0
    _post(event, payload, 1.0, token)
    return 0


def _checkpoint_note(args: list[str]) -> str | None:
    """Read ``--note`` or ``--note-file`` from argv; the file is deleted after reading."""
    note: str | None = None
    index = 0
    while index < len(args):
        if args[index] == "--note" and index + 1 < len(args):
            note = args[index + 1]
            index += 2
        elif args[index] == "--note-file" and index + 1 < len(args):
            path = Path(args[index + 1])
            try:
                if path.stat().st_size <= 4096:
                    note = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                note = None
            finally:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            index += 2
        else:
            index += 1
    return note


def _checkpoint_save(args: list[str] | None = None) -> int:
    """Ask the daemon to save a checkpoint for this cwd; print one human line."""
    state_dir = resolve_state_dir()
    port = _port()
    token = read_token(state_dir)
    if token is None or not verify_identity(state_dir, port):
        print("Checkpoint not saved: the local helper is not running.")
        return 0
    body: dict[str, object] = {"cwd": os.getcwd()}
    note = _checkpoint_note(args or [])
    if note is not None:
        body["note"] = note
    payload = json.dumps(body).encode("utf-8")
    try:
        status, response = post_hook(port, "checkpoint-save", payload, 2.0, token=token)
        obj = json.loads(response) if status == 200 else {}
    except Exception:
        obj = {}
    summary = obj.get("summary") if isinstance(obj, dict) else None
    print(
        summary
        if isinstance(summary, str)
        else "Checkpoint not saved: no response from the local helper."
    )
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
    if args and args[0] == "checkpoint-save":
        return _checkpoint_save(args[1:])
    if args and args[0] == "ensure":
        return _ensure()
    print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
