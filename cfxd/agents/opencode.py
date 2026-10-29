"""OpenCode reader: stream a read-only SQLite transcript database into Sessions.

Counts and sizes only, same privacy rule as the Codex reader: a tool name is kept
only as a plain identifier, and a tool result is reduced to its UTF-8 byte length
with SQLite ``json_extract``/``octet_length`` — message text, prompts, paths,
commands and tool output never leave the database. The connection is opened
``mode=ro`` and never writes or checkpoints, so a live WAL database is safe.
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cfxd.agents.model import Session, ToolResult, Turn, Usage, sanitize_tool_name

_DB_NAME = "opencode.db"
_BATCH = 1000


class OpenCodeDBError(Exception):
    """An OpenCode database that cannot be read (missing, locked, unsupported)."""


def _resolve_db_path(root: Path) -> Path:
    """``--root`` may point at the database file or the directory that holds it."""
    return root / _DB_NAME if root.is_dir() else root


def _connect(db: Path) -> sqlite3.Connection:
    if not db.exists():
        raise OpenCodeDBError(f"opencode database not found: {db}")
    # as_uri() percent-encodes '?', '#' and '%', so they cannot be read as URI syntax.
    uri = Path(db).resolve().as_uri() + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=2)
        con.execute("PRAGMA busy_timeout = 2000")
        return con
    except sqlite3.OperationalError as exc:
        raise OpenCodeDBError(f"cannot open opencode database {db}: {exc}") from exc


def _table_names(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _count(con: sqlite3.Connection, table: str) -> int:
    try:
        row = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
        return int(row[0]) if row is not None else 0
    except sqlite3.OperationalError:
        return 0


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in con.execute(f'PRAGMA table_info("{table}")')}
    except sqlite3.OperationalError:
        return set()


def _detect_family(con: sqlite3.Connection) -> str | None:
    """Return the table family to read: 'v1' (session/message/part), 'v2', or None."""
    names = _table_names(con)
    if {"session", "message", "part"} <= names:
        if _count(con, "session") or _count(con, "message") or _count(con, "part"):
            return "v1"
    if {"session_v2", "session_message"} <= names:
        if _count(con, "session_v2") or _count(con, "session_message"):
            return "v2"
    return None


def _to_ms(cutoff: datetime) -> int:
    return int(cutoff.timestamp() * 1000)


def _iso(ms: Any) -> str | None:
    if not isinstance(ms, int):
        return None
    try:
        return datetime.fromtimestamp(ms / 1000.0, UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return str(ms)


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def read_sessions(root: Path, cutoff: datetime | None = None) -> Iterator[Session]:
    """Yield one Session per OpenCode session in the window (counts/sizes only)."""
    db = _resolve_db_path(root)
    con = _connect(db)
    try:
        family = _detect_family(con)
        if family is None:
            print(f"opencode: no readable sessions in {db}", file=sys.stderr)
            return
        if family == "v2":
            raise OpenCodeDBError(
                "opencode database uses the session_v2/session_message schema, "
                "which is not supported yet"
            )
        print("opencode: reading session/message/part schema", file=sys.stderr)
        yield from _read_v1(con, cutoff)
    finally:
        con.close()


def _read_v1(con: sqlite3.Connection, cutoff: datetime | None) -> Iterator[Session]:
    cols = _columns(con, "session")
    if "time_updated" in cols:
        ts_col = "time_updated"
    elif "time_created" in cols:
        ts_col = "time_created"
    else:
        ts_col = None
    if "parent_id" in cols:
        sql = 'SELECT id, parent_id FROM "session"'
    else:
        sql = 'SELECT id, NULL FROM "session"'
    params: list[Any] = []
    if cutoff is not None and ts_col is not None:
        sql += f" WHERE {ts_col} >= ?"
        params.append(_to_ms(cutoff))
    sql += " ORDER BY id"

    for sid, parent_id in con.execute(sql, params).fetchall():
        yield Session(
            agent="opencode",
            session_id=sid,
            is_subagent=parent_id is not None,
            turns=_read_turns(con, sid),
            tool_results=_read_tools(con, sid),
            compactions=_read_compactions(con, sid),
        )


def _read_turns(con: sqlite3.Connection, session_id: str) -> list[Turn]:
    ts_expr = "time_created" if "time_created" in _columns(con, "message") else "NULL"
    sql = (
        "SELECT "
        "COALESCE(json_extract(data, '$.tokens.input'), 0), "
        "COALESCE(json_extract(data, '$.tokens.cache.read'), 0), "
        "COALESCE(json_extract(data, '$.tokens.cache.write'), 0), "
        "COALESCE(json_extract(data, '$.tokens.output'), 0), "
        "COALESCE(json_extract(data, '$.tokens.reasoning'), 0), "
        f"{ts_expr} "
        'FROM "message" '
        "WHERE session_id = ? AND json_extract(data, '$.role') = 'assistant' "
        "AND json_type(data, '$.tokens') = 'object'"
    )
    turns: list[Turn] = []
    cur = con.execute(sql, (session_id,))
    while True:
        rows = cur.fetchmany(_BATCH)
        if not rows:
            break
        for inp, cr, cw, out, reasoning, ts in rows:
            turns.append(
                Turn(
                    _iso(ts),
                    Usage(
                        input_uncached=_to_int(inp),
                        cache_read=_to_int(cr),
                        cache_write=_to_int(cw),
                        output=_to_int(out),
                        reasoning=_to_int(reasoning),
                    ),
                )
            )
    return turns


def _read_tools(con: sqlite3.Connection, session_id: str) -> list[ToolResult]:
    sql = (
        "SELECT json_extract(data, '$.tool'), "
        "COALESCE(octet_length(json_extract(data, '$.state.output')), 0) "
        'FROM "part" '
        "WHERE session_id = ? AND json_extract(data, '$.type') = 'tool'"
    )
    results: list[ToolResult] = []
    cur = con.execute(sql, (session_id,))
    while True:
        rows = cur.fetchmany(_BATCH)
        if not rows:
            break
        for name, nbytes in rows:
            if isinstance(name, str):
                results.append(ToolResult(sanitize_tool_name(name), _to_int(nbytes)))
    return results


def _read_compactions(con: sqlite3.Connection, session_id: str) -> int:
    row = con.execute(
        'SELECT COUNT(*) FROM "part" WHERE session_id = ? '
        "AND json_extract(data, '$.type') = 'compaction'",
        (session_id,),
    ).fetchone()
    return int(row[0]) if row is not None else 0
