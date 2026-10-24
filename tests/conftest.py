"""Hermetic test environment.

A corporate proxy in the developer's environment must not capture loopback test traffic (bare
``httpx.Client()`` and ``urllib`` both read ``HTTP(S)_PROXY``). Clear them and exempt loopback for
every test; the product code already ignores proxies for its own loopback calls.
"""

from __future__ import annotations

import pytest

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
