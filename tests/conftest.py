"""Hermetic test environment, plus shared helpers for the hook-transport tests.

A corporate proxy in the developer's environment must not capture loopback test traffic (bare
``httpx.Client()`` and ``urllib`` both read ``HTTP(S)_PROXY``). Clear them and exempt loopback for
every test; the product code already ignores proxies for its own loopback calls.
"""

from __future__ import annotations

import pytest

from cfxd.daemon import Daemon

_PROXY_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


@pytest.fixture(autouse=True)
def _no_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _PROXY_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost,::1")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost,::1")


def hook_headers(daemon: Daemon) -> dict[str, str]:
    """Content-Type plus the daemon's per-install token, as a hook client sends them."""
    return {"Content-Type": "application/json", "X-Cfx-Token": daemon.token}
