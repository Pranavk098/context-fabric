"""Shared helpers for the cfxd hook-transport tests."""

from __future__ import annotations

from cfxd.daemon import Daemon


def hook_headers(daemon: Daemon) -> dict[str, str]:
    """Content-Type plus the daemon's per-install token, as a hook client sends them."""
    return {"Content-Type": "application/json", "X-Cfx-Token": daemon.token}
