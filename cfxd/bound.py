"""PreToolUse bound handler: keep only the tail of noisy Bash output.

Experimental and off by default (``cfxd set bound on``): the saving is not yet
proven. Only a single simple, allowlisted command is rewritten, so pipes, chains,
redirects and background runs behave exactly as before. The rewritten command
keeps stderr and the original exit status (``pipefail``).
"""

from __future__ import annotations

import copy
import json
import os
import re
import shlex
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cfxd.config import read_config
from cfxd.fsutil import ensure_dir, open_log
from cfxd.handlers import register_after

TAIL_LINES = 150
BOUND_LOG = "bound.jsonl"

# ">" also covers ">>" and "2>&1"; "&" covers "&&" and "&".
_META = re.compile(r"[|;&<>`\n]|\$\(")
_PREFIXES = (("uv", "run"), ("python", "-m"), ("npx",))
_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("pytest", ("pytest",)),
    ("npm run test", ("npm", "run", "test")),
    ("npm test", ("npm", "test")),
    ("yarn test", ("yarn", "test")),
    ("pnpm test", ("pnpm", "test")),
    ("go test", ("go", "test")),
    ("cargo test", ("cargo", "test")),
    ("dotnet test", ("dotnet", "test")),
    ("mvn test", ("mvn", "test")),
    ("gradle test", ("gradle", "test")),
    ("make test", ("make", "test")),
    ("git log", ("git", "log")),
    ("docker logs", ("docker", "logs")),
    ("kubectl logs", ("kubectl", "logs")),
    ("journalctl", ("journalctl",)),
)
_STOP = {"head", "tail", "grep", "-q", "--quiet"}
_COUNT_FLAG = re.compile(r"^-n\d*$|^-\d+$|^--max-count(=.*)?$")
# Test runners only: the checkpoint observer reuses these keys, not the log keys.
_TEST_KEYS = frozenset(
    {
        "pytest",
        "npm run test",
        "npm test",
        "yarn test",
        "pnpm test",
        "go test",
        "cargo test",
        "dotnet test",
        "mvn test",
        "gradle test",
        "make test",
    }
)

_receipt_lock = threading.Lock()


def install(state_dir: Path) -> None:
    """Register the ``PreToolUse`` handler after the brief handler."""
    register_after("PreToolUse", lambda payload: handle_pre_tool_use(payload, state_dir))


def handle_pre_tool_use(payload: dict[str, Any], state_dir: Path) -> dict[str, Any]:
    try:
        if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
            return {}
        if os.environ.get("CFX_BOUND") == "0":
            return {}
        if not read_config(state_dir).get("bound", False):
            return {}
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            return {}
        if tool_input.get("run_in_background") is True:
            return {}
        command = tool_input.get("command")
        if not isinstance(command, str):
            return {}
        key = _command_key(command)
        if key is None:
            return {}
        session_id = payload.get("session_id")
        if not isinstance(session_id, str):
            session_id = None
        _receipt(state_dir, key, len(command), session_id)
        updated = copy.deepcopy(tool_input)
        updated["command"] = wrap_command(command)
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": updated}}
    except Exception:
        # Fail open: a hook must never break the session.
        return {}


def wrap_command(command: str) -> str:
    """Return the bounded form of ``command`` (stderr merged, exit status kept)."""
    return f"set -o pipefail; ( {command} ) 2>&1 | tail -n {TAIL_LINES}"


def _command_key(command: str) -> str | None:
    if _META.search(command):
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    lower = [token.lower() for token in tokens]
    if not lower or _STOP.intersection(lower):
        return None
    start = 0
    for prefix in _PREFIXES:
        if tuple(lower[: len(prefix)]) == prefix:
            start = len(prefix)
            break
    words = lower[start:]
    for key, pattern in _PATTERNS:
        if tuple(words[: len(pattern)]) != pattern:
            continue
        if key == "git log" and any(_COUNT_FLAG.match(token) for token in words[2:]):
            return None
        return key
    return None


def test_key(command: str) -> str | None:
    """The test-runner key whose words start ``command``, or ``None``. Never raises."""
    if not isinstance(command, str):
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    lower = [token.lower() for token in tokens]
    if not lower:
        return None
    start = 0
    for prefix in _PREFIXES:
        if tuple(lower[: len(prefix)]) == prefix:
            start = len(prefix)
            break
    words = lower[start:]
    for key, pattern in _PATTERNS:
        if key in _TEST_KEYS and tuple(words[: len(pattern)]) == pattern:
            return key
    return None


def _receipt(state_dir: Path, key: str, command_len: int, session_id: str | None) -> None:
    entry = {
        "ts": datetime.now(UTC).isoformat(),
        "event": "bound",
        "key": key,
        "command_len": command_len,
        "tail_lines": TAIL_LINES,
        "session_id": session_id,
    }
    ensure_dir(state_dir)
    with _receipt_lock:
        with open_log(state_dir / BOUND_LOG) as fh:
            fh.write(json.dumps(entry) + "\n")
