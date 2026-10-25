"""Minimal stdlib hook client: token read, identity proof, and hook POST.

Importable without pulling in the daemon (``ThreadPoolExecutor``, ``http.server``,
handlers, …) so the hot-path ``launch.py forward`` stays fast. These functions are
the single source of truth for the client side; ``cfxd.daemon`` re-exports them so
its public API is unchanged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

DEFAULT_PORT = 47831
DEFAULT_STATE_DIR = Path.home() / ".cfx" / "daemon"
TOKEN_FILE = "token"
HEALTH_TIMEOUT_S = 0.5

# Our own calls are loopback-only: never route them through a proxy set in the environment.
_OPENER = build_opener(ProxyHandler({}))


def resolve_state_dir(explicit: str | None = None) -> Path:
    """``--state-dir`` > ``CFX_HOME`` > ``%USERPROFILE%/.cfx/daemon``."""
    if explicit:
        return Path(explicit)
    env = os.environ.get("CFX_HOME")
    if env:
        return Path(env)
    return DEFAULT_STATE_DIR


def read_token(state_dir: Path) -> str | None:
    try:
        token = (state_dir / TOKEN_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


def get_health(
    port: int, timeout: float = HEALTH_TIMEOUT_S, nonce: str | None = None
) -> tuple[int, bytes]:
    url = f"http://127.0.0.1:{port}/health"
    if nonce is not None:
        url += f"?nonce={nonce}"
    try:
        with _OPENER.open(url, timeout=timeout) as resp:
            return resp.status, resp.read()
    except HTTPError as exc:
        return exc.code, exc.read()


def verify_identity(state_dir: Path, port: int) -> bool:
    """Prove ``/health`` on ``port`` is answered by the daemon that owns ``state_dir``."""
    token = read_token(state_dir)
    if token is None:
        return False
    nonce = secrets.token_hex(16)
    try:
        status, body = get_health(port, nonce=nonce)
    except OSError:
        return False
    if status != 200:
        return False
    try:
        health = json.loads(body)
    except ValueError:
        return False
    if not isinstance(health, dict):
        return False
    proof = health.get("proof")
    expected = hmac.new(token.encode("utf-8"), nonce.encode("utf-8"), hashlib.sha256).hexdigest()
    return isinstance(proof, str) and hmac.compare_digest(proof, expected)


def post_hook(
    port: int,
    event: str,
    payload: bytes,
    timeout: float = 1.0,
    token: str | None = None,
) -> tuple[int, bytes]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-Cfx-Token"] = token
    req = Request(
        f"http://127.0.0.1:{port}/hooks/{event}",
        data=payload,
        headers=headers,
        method="POST",
    )
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except HTTPError as exc:
        return exc.code, exc.read()


def _health_ok(port: int) -> bool:
    try:
        status, body = get_health(port)
    except OSError:
        return False
    if status != 200:
        return False
    try:
        health = json.loads(body)
    except ValueError:
        return False
    return isinstance(health, dict) and health.get("ok") is True
