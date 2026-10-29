"""SessionStart(resume) receipts: how often a resume starts cold and what it costs.

Claude Code offers a resume's ``context_tokens``, ``prompt_cache_likely_expired``
and ``estimated_cache_write_usd`` when it has them. We write one content-free
line to ``resume.jsonl`` (counts, sizes and ids only) and, when ``idle_notice``
is on, return one user-visible line. Experimental: whether the notice changes
behaviour is unproven.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cfxd.config import read_config
from cfxd.fsutil import ensure_dir, open_log
from cfxd.handlers import register_after

RESUME_LOG = "resume.jsonl"
MIN_TOKENS = 100_000
MIN_USD = 0.25
NOTICE = (
    "Context Fabric: resuming after the cache expired; re-reading ~{tokens}K tokens "
    "costs about ${usd:.2f} (API-equivalent). If you do not need the old history, "
    "/clear starts cheap: the checkpoint cannot be saved after the fact, so describe "
    "the goal in one line."
)

_receipt_lock = threading.Lock()


def install(state_dir: Path) -> None:
    """Register the resume receipt after carry and checkpoint, which skip a resume."""
    register_after("SessionStart", lambda payload: handle_session_start(payload, state_dir))


def handle_session_start(payload: dict[str, Any], state_dir: Path) -> dict[str, Any]:
    """Record one resume receipt and maybe one notice. Never raises."""
    try:
        return _handle_session_start(payload, state_dir)
    except Exception:
        return {}


def _handle_session_start(payload: dict[str, Any], state_dir: Path) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("source") != "resume":
        return {}
    session_id = payload.get("session_id")
    session_id = session_id if isinstance(session_id, str) else None
    tokens = _as_int(payload.get("context_tokens"))
    dollars = _as_dollars(payload.get("estimated_cache_write_usd"))
    expired = payload.get("prompt_cache_likely_expired")
    if expired is False:
        _receipt(state_dir, session_id, "resume", "warm", None, None, None)
        return {}
    if expired is not True or tokens is None or dollars is None:
        _receipt(state_dir, session_id, "resume", "no_cache_info", None, None, None)
        return {}
    cents = round(dollars, 2)
    model = payload.get("model")
    model = model if isinstance(model, str) and model else None
    _receipt(state_dir, session_id, "resume_cold", None, tokens, cents, model)
    if not _notice_on(state_dir) or tokens < MIN_TOKENS or cents < MIN_USD:
        return {}
    return {"systemMessage": NOTICE.format(tokens=f"{tokens / 1000:,.0f}", usd=cents)}


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _as_dollars(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return float(value)


def _notice_on(state_dir: Path) -> bool:
    """Env override wins so a test or a user can silence the line without config."""
    if os.environ.get("CFX_IDLE_NOTICE") == "0":
        return False
    return read_config(state_dir).get("idle_notice", False)


def _receipt(
    state_dir: Path,
    session_id: str | None,
    event: str,
    outcome: str | None,
    tokens: int | None,
    dollars: float | None,
    model: str | None,
) -> None:
    try:
        entry: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "session_id": session_id,
            "event": event,
        }
        if outcome is not None:
            entry["outcome"] = outcome
        if tokens is not None:
            entry["context_tokens"] = tokens
        if dollars is not None:
            entry["est_write_usd"] = dollars
        if model is not None:
            entry["model"] = model
        ensure_dir(state_dir)
        with _receipt_lock:
            with open_log(state_dir / RESUME_LOG) as fh:
                fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass
