"""Fail-open hook shim for command-hook clients (OpenCode/Codex).

Usage::

    python -I -S cfxd/shim.py <EventName>

Reads the hook event JSON from stdin, forwards it to the local cfxd daemon
(discovered via ``state.json``), and writes the response body to stdout. On
any failure — no state file, dead daemon, timeout, bad response — it prints
``{}`` and exits 0 so the hook never blocks the agent.

Only stdlib modules are imported so this file stays runnable under
``python -I -S`` (no site-packages, no ``cfxd`` package import).
"""

import json
import os
import sys
import urllib.request

_TIMEOUT = 0.2


def _state_dir() -> str:
    env = os.environ.get("CFX_HOME")
    if env:
        return env
    profile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return os.path.join(profile, ".cfx", "daemon")


def main() -> int:
    try:
        if len(sys.argv) < 2:
            raise ValueError("missing event name")
        event = sys.argv[1]
        with open(os.path.join(_state_dir(), "state.json"), encoding="utf-8") as fh:
            state = json.load(fh)
        port = state["port"]
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/hooks/{event}",
            data=sys.stdin.read().encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            body = response.read()
        sys.stdout.write(body.decode("utf-8"))
        return 0
    except Exception:
        sys.stdout.write("{}\n")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
