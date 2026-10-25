"""Codex CLI reader: stream ``rollout-*.jsonl[.zst]`` into agent-neutral Sessions.

Each ``rollout-*`` file is one session. Lines are a JSON envelope
``{"timestamp", "ordinal", "type", "payload"}``; token usage comes from
``event_msg`` ``token_count`` events, whose ``total_token_usage`` is cumulative
so we take field-wise deltas (which also dedupes repeated events). Tool results
join ``function_call``/``custom_tool_call`` names to their outputs by
``call_id``. Only counts, sizes, tool names and timestamps are read — never
message text, paths, commands or output.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cfxd.agents.model import Session, ToolResult, Turn, Usage, sanitize_tool_name

_MAX_LINE_BYTES = 20 * 1024 * 1024

# Usage fields on a Codex token_count event, in the cumulative form stored.
_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)

_SKIP_MSG = (
    "{} compressed session(s) skipped; install the optional decoder: "
    "uvx --with zstandard python -m cfxd agent-audit --agent codex"
)


def _zstd_open() -> Any:
    """Return a module whose ``open(path, "rb")`` reads .zst files, else None."""
    for name in ("compression.zstd", "zstandard"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    return None


def read_sessions(root: Path, cutoff: datetime | None = None) -> Iterator[Session]:
    """Yield one Session per Codex rollout file under ``root`` (counts only)."""
    zmod = _zstd_open()
    skipped = 0
    for path, is_zst in _discover(root, cutoff):
        if is_zst and zmod is None:
            skipped += 1
            continue
        yield _read_file(path, is_zst, zmod)
    if skipped:
        print(_SKIP_MSG.format(skipped), file=sys.stderr)


def _discover(root: Path, cutoff: datetime | None) -> list[tuple[Path, bool]]:
    out: list[tuple[Path, bool]] = []
    if not root.is_dir():
        return out
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if not fn.startswith("rollout-"):
                continue
            if fn.endswith(".jsonl"):
                is_zst = False
            elif fn.endswith(".jsonl.zst"):
                is_zst = True
            else:
                continue
            path = Path(dirpath) / fn
            if _in_window(path, cutoff):
                out.append((path, is_zst))
    return out


def _in_window(path: Path, cutoff: datetime | None) -> bool:
    if cutoff is None:
        return True
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    return datetime.fromtimestamp(mtime, UTC) >= cutoff


def _read_file(path: Path, is_zst: bool, zmod: Any) -> Session:
    session_id = path.name  # fallback; overwritten by session_meta
    is_subagent = False
    turns: list[Turn] = []
    tool_results: list[ToolResult] = []
    compactions = 0
    calls: dict[str, str] = {}
    cur_ts: str | None = None
    cur = _zero_usage()
    prev = _zero_usage()

    for obj in _iter_lines(path, is_zst, zmod):
        kind = obj.get("type")
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            continue
        if kind == "session_meta":
            sid = payload.get("session_id")
            if isinstance(sid, str):
                session_id = sid
            source = payload.get("source")
            if isinstance(source, str) and source.startswith("subagent"):
                is_subagent = True
        elif kind == "event_msg":
            etype = payload.get("type")
            if etype == "task_started":
                if cur_ts is not None:
                    turns.append(Turn(cur_ts, _to_usage(cur)))
                cur_ts = _as_str(obj.get("timestamp"))
                cur = _zero_usage()
            elif etype == "token_count":
                info = payload.get("info")
                if isinstance(info, dict):
                    total = info.get("total_token_usage")
                    if isinstance(total, dict):
                        for field in _USAGE_FIELDS:
                            value = total.get(field)
                            if isinstance(value, int):
                                delta = max(0, value - prev[field])
                                cur[field] += delta
                                prev[field] = value
            elif etype == "context_compacted":
                compactions += 1
        elif kind == "response_item":
            rtype = payload.get("type")
            if rtype in ("function_call", "custom_tool_call"):
                cid = payload.get("call_id")
                name = payload.get("name")
                if isinstance(cid, str) and isinstance(name, str):
                    calls[cid] = sanitize_tool_name(name)
            elif rtype in ("function_call_output", "custom_tool_call_output"):
                cid = payload.get("call_id")
                if isinstance(cid, str):
                    name = calls.get(cid)
                    if name is not None:
                        tool_results.append(ToolResult(name, _output_bytes(payload.get("output"))))
        elif kind == "compacted":
            compactions += 1

    if cur_ts is not None:
        turns.append(Turn(cur_ts, _to_usage(cur)))

    return Session(
        agent="codex",
        session_id=session_id,
        is_subagent=is_subagent,
        turns=turns,
        tool_results=tool_results,
        compactions=compactions,
    )


def _iter_lines(path: Path, is_zst: bool, zmod: Any) -> Iterator[dict[str, Any]]:
    if is_zst:
        # Text mode so the stream iterates line-by-line (the raw reader has no readline).
        try:
            fh: Any = zmod.open(str(path), "rt", encoding="utf-8", errors="replace")
        except OSError:
            return
    else:
        try:
            fh = open(path, "rb")
        except OSError:
            return
    with fh:
        try:
            for raw in fh:
                if len(raw) > _MAX_LINE_BYTES:
                    continue
                text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
                try:
                    obj = json.loads(text)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    yield obj
        except Exception:
            # Tolerant of truncated/partial .zst streams mid-file.
            return


def _zero_usage() -> dict[str, int]:
    return {field: 0 for field in _USAGE_FIELDS}


def _to_usage(acc: dict[str, int]) -> Usage:
    # Codex input_tokens INCLUDES cached input: split it into uncached + cache_read.
    return Usage(
        input_uncached=max(0, acc["input_tokens"] - acc["cached_input_tokens"]),
        cache_read=acc["cached_input_tokens"],
        cache_write=acc["cache_write_input_tokens"],
        output=acc["output_tokens"],
        reasoning=acc["reasoning_output_tokens"],
    )


def _output_bytes(output: Any) -> int:
    if isinstance(output, str):
        return len(output.encode("utf-8"))
    if isinstance(output, list):
        return sum(len(json.dumps(item, ensure_ascii=False).encode("utf-8")) for item in output)
    return 0


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None
