"""Stop hook: one plain, user-visible nudge when a session is long and expensive.

Experimental and off by default (``cfxd set coach on``): the saving is not yet
proven. Nothing is sent to the model; only a ``systemMessage`` the user sees.
The transcript is read only at its tail, and receipts hold numbers and ids only.
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
from cfxd.handlers import register

COACH_LOG = "coach.jsonl"
TAIL_BYTES = 64 * 1024
CONTEXT_THRESHOLD = 150_000
TURN_THRESHOLD = 30
COOLDOWN_TURNS = 10
GROWTH_TOKENS = 100_000
_SYNTHETIC = "<synthetic>"

_lock = threading.Lock()
_receipt_lock = threading.Lock()
# Per-session turn counter and last nudge, in daemon memory. 0 means "never".
_STATE: dict[str, dict[str, int]] = {}


def install(state_dir: Path) -> None:
    """Register the ``Stop`` handler."""
    register("Stop", lambda payload: handle_stop(payload, state_dir))


def handle_stop(payload: dict[str, Any], state_dir: Path) -> dict[str, Any]:
    try:
        if not isinstance(payload, dict):
            return {}
        if os.environ.get("CFX_COACH") == "0":
            return {}
        if not read_config(state_dir).get("coach", False):
            return {}
        session_id = payload.get("session_id")
        transcript = payload.get("transcript_path")
        if not isinstance(session_id, str) or not session_id:
            return {}
        if not isinstance(transcript, str) or not transcript:
            return {}
        usage = _tail_usage(Path(transcript))
        if usage is None:
            return {}
        context, model = usage
        with _lock:
            session = _STATE.setdefault(session_id, {"turns": 0, "last_context": 0, "last_turn": 0})
            session["turns"] += 1
            turns = session["turns"]
            last_context = session["last_context"]
            last_turn = session["last_turn"]
            if context < CONTEXT_THRESHOLD or turns < TURN_THRESHOLD:
                outcome = "below_threshold"
            elif last_turn and (
                turns - last_turn < COOLDOWN_TURNS or context - last_context < GROWTH_TOKENS
            ):
                outcome = "suppressed_recent"
            else:
                outcome = "shown"
                session["last_context"] = context
                session["last_turn"] = turns
        try:
            _receipt(state_dir, session_id, context, turns, model, outcome)
        except OSError:
            pass
        if outcome == "shown":
            return {"systemMessage": nudge_text(context, model)}
        return {}
    except Exception:
        # Fail open: a hook must never break the session.
        return {}


def nudge_text(context: int, model: str | None) -> str:
    cost = _cost_phrase(model, context)
    priced = "" if cost is None else f" ({cost})"
    return (
        f"Context Fabric: this session is at ~{round(context / 1000)}K tokens; "
        f"each turn now re-reads it{priced}. If this is a natural break: "
        "/context-fabric:checkpoint, then /clear. Turn this off: cfxd set coach off"
    )


def _cost_phrase(model: str | None, context: int) -> str | None:
    if model is None:
        return None
    try:
        # Imported here: routing imports daemon, which imports this module at startup.
        from cfxd.routing import (
            _DEFAULT_PRICES,
            _FAMILIES_KEY,
            _load_prices,
            _match_family,
            _normalize,
            _price_for,
        )

        families = _load_prices(_DEFAULT_PRICES)[_FAMILIES_KEY]
        family = _match_family(_normalize(model), families)
        if family is None:
            return None
        price = _price_for(families[family], context)
        dollars = context * float(price["cache_read"]) / 1_000_000.0
        return f"about ${dollars:.2f} at {_pretty(family)} cache-read prices, API-equivalent"
    except Exception:
        return None


def _pretty(family: str) -> str:
    name, _, version = family.partition("-")
    return f"{name.capitalize()} {version.replace('-', '.')}" if version else name.capitalize()


def _read_tail(path: Path) -> bytes:
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        end = fh.tell()
        fh.seek(max(0, end - TAIL_BYTES))
        return fh.read(TAIL_BYTES)


def _tail_usage(path: Path) -> tuple[int, str | None] | None:
    """Context tokens and model from the last usage line within the file's tail."""
    data = _read_tail(path)
    lines = data.splitlines()
    if len(data) == TAIL_BYTES and lines:
        lines = lines[1:]  # the first line may have been cut mid-way
    for raw in reversed(lines):
        try:
            obj = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        message = obj.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
        if not isinstance(usage, dict):
            continue
        model = message.get("model") if isinstance(message, dict) else None
        if model == _SYNTHETIC:
            continue
        context = (
            (_as_int(usage.get("input_tokens")) or 0)
            + (_as_int(usage.get("cache_creation_input_tokens")) or 0)
            + (_as_int(usage.get("cache_read_input_tokens")) or 0)
        )
        return context, model if isinstance(model, str) and model else None
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _receipt(
    state_dir: Path,
    session_id: str,
    context: int,
    turns: int,
    model: str | None,
    outcome: str,
) -> None:
    entry = {
        "ts": datetime.now(UTC).isoformat(),
        "event": "coach_nudge",
        "session_id": session_id,
        "context_tokens": context,
        "turns": turns,
        "model": model,
        "outcome": outcome,
    }
    ensure_dir(state_dir)
    with _receipt_lock:
        with open_log(state_dir / COACH_LOG) as fh:
            fh.write(json.dumps(entry) + "\n")
