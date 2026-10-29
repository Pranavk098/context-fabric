"""Security regressions: bounded JSONL readers (F7) and the bound filter (F8)."""

from __future__ import annotations

import importlib
import json
import tracemalloc
from pathlib import Path
from typing import Any

import pytest

from cfxd.agents import codex
from cfxd.audit import _iter_objects
from cfxd.bound import _command_key

VALID = {"type": "assistant", "n": 1}
PEAK_LIMIT = 64 * 1024 * 1024


def _planted(tmp_path: Path, name: str = "session.jsonl") -> Path:
    """A 25 MB single line, a deeply nested line, then one valid object."""
    path = tmp_path / name
    with path.open("wb") as fh:
        fh.write(b'{"pad":"' + b"x" * (25 * 1024 * 1024) + b'"}\n')
        fh.write(b"[" * 200_000 + b"\n")
        fh.write(json.dumps(VALID).encode() + b"\n")
    return path


def test_audit_reader_skips_huge_and_nested_lines_within_memory(tmp_path: Path) -> None:
    path = _planted(tmp_path)
    tracemalloc.start()
    try:
        objs = list(_iter_objects(path))
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert objs == [VALID]
    assert peak < PEAK_LIMIT


def test_codex_reader_skips_huge_and_nested_lines_within_memory(tmp_path: Path) -> None:
    path = _planted(tmp_path, "rollout-s1.jsonl")
    tracemalloc.start()
    try:
        objs = list(codex._iter_lines(path, False, None))
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert objs == [VALID]
    assert peak < PEAK_LIMIT


def _zstd() -> Any:
    for name in ("compression.zstd", "zstandard"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    return None


ZSTD = _zstd()


@pytest.mark.skipif(ZSTD is None, reason="no zstd decoder installed")
def test_zstd_decompressed_size_is_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex, "_MAX_ZSTD_BYTES", 50_000)
    line = (json.dumps({"type": "event_msg"}) + "\n").encode()
    path = tmp_path / "rollout-s1.jsonl.zst"
    path.write_bytes(ZSTD.ZstdCompressor().compress(line * 5_000))
    objs = list(codex._iter_lines(path, True, ZSTD))
    assert 0 < len(objs) < 5_000


@pytest.mark.parametrize(
    "command",
    [
        "docker logs -f web",
        "docker logs --follow web",
        "kubectl logs --follow pod",
        "kubectl logs -F pod",
        "journalctl -f",
        "journalctl --follow",
        "pytest # note",
        "pytest )",
        "pytest (",
        "pytest\r",
    ],
)
def test_bound_rejects_unsafe_commands(command: str) -> None:
    assert _command_key(command) is None


def test_bound_still_rewrites_plain_test_command() -> None:
    assert _command_key("pytest -k a") == "pytest"
