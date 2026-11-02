"""Stop hook: one plain, user-visible nudge when a session is long and expensive.

Experimental and on by default as a measurement study (``cfxd set coach off`` leaves it): the
saving is not yet proven. Nothing is sent to the model; only a ``systemMessage`` the user sees.
The transcript is read only at its tail, and receipts hold numbers and ids only.

Under ``coach_control`` (on by default, D-1) half of installs get a withheld control:
the rule is computed silently. The arm is a hash of the install, never of the session
or its cost, so expensive sessions cannot drift into treatment; it is logged once per
install per daemon run. Every nudge also leaves ``coach_outcome``: the arm (``warned`` =
shown, ``control`` = would-have-shown), what the user did within ``ACTION_TURNS`` turns
(restart, end, dismiss, none) and the cache-read cost after the decision point. All of
it is counts and dollars, never text, paths or session ids.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from cfxd.config import read_config
from cfxd.fsutil import ensure_dir, open_log
from cfxd.handlers import add_observer, register

COACH_LOG = "coach.jsonl"
TAIL_BYTES = 64 * 1024
CONTEXT_THRESHOLD = 150_000
TURN_THRESHOLD = 30
# The frozen tester setting (docs/plans/2026-11-02-coach-threshold-decision.md).
COACH_FIRE_TURN = 15
COACH_FIRE_COST_USD = 0.50
COOLDOWN_TURNS = 10
GROWTH_TOKENS = 100_000
# Turns to watch after a nudge, and how far context must fall to count as a reset.
ACTION_TURNS = 5
RESTART_DROP = 0.8
_SYNTHETIC = "<synthetic>"

_lock = threading.Lock()
_receipt_lock = threading.Lock()
# Per-session turn counter, last nudge and the open post-nudge observation, in daemon
# memory. 0 means "never".
_STATE: dict[str, dict[str, Any]] = {}
# Install ids whose assignment line this daemon run already wrote. The same install
# always gets the same arm, so a restart at most repeats that identical line.
_ASSIGNED: set[str] = set()
_INSTALL: dict[str, str] = {}
_PRICE_FAMILIES: dict[str, Any] | None = None


def install(state_dir: Path) -> None:
    """Register the ``Stop`` handler and the session-start observer."""
    register("Stop", lambda payload: handle_stop(payload, state_dir))
    add_observer("SessionStart", lambda payload: _handle_session_start(payload, state_dir))


def handle_stop(payload: dict[str, Any], state_dir: Path) -> dict[str, Any]:
    try:
        if not isinstance(payload, dict):
            return {}
        if os.environ.get("CFX_COACH") == "0":
            return {}
        config = read_config(state_dir)
        if not config.get("coach", False):
            # Turning the coach off after a nudge is the one observable dismissal.
            _flush_pending(state_dir, "dismiss")
            return {}
        control = config.get("coach_control", False)
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
        install_id = _install_id(state_dir) if control else None
        with _lock:
            session = _STATE.setdefault(
                session_id,
                {
                    "turns": 0,
                    "cost_so_far": 0.0,
                    "priced": False,
                    "last_context": 0,
                    "last_turn": 0,
                    "pending": None,
                },
            )
            session["turns"] += 1
            turns = session["turns"]
            dollars = _turn_cost(model, context)
            if dollars is not None:
                session["cost_so_far"] = session.get("cost_so_far", 0.0) + dollars
                session["priced"] = True
            settled = _settle(session, turns, context, model)
            last_context = session["last_context"]
            last_turn = session["last_turn"]
            if session.get("priced"):
                # The frozen cost rule, the same sum the sweep models.
                fires = turns >= COACH_FIRE_TURN and session["cost_so_far"] >= COACH_FIRE_COST_USD
            else:
                # No price for this session: the old token+turn rule, unchanged.
                fires = context >= CONTEXT_THRESHOLD and turns >= TURN_THRESHOLD
            if not fires:
                outcome = "below_threshold"
            elif last_turn and (
                turns - last_turn < COOLDOWN_TURNS or context - last_context < GROWTH_TOKENS
            ):
                outcome = "suppressed_recent"
            else:
                outcome = "shown"
                session["last_context"] = context
                session["last_turn"] = turns
            arm: str | None = None
            assignment: str | None = None
            if control and install_id is not None and outcome == "shown":
                arm = _arm(install_id)
                if install_id not in _ASSIGNED:
                    _ASSIGNED.add(install_id)
                    assignment = arm
                if arm == "control":
                    outcome = "withheld"
                session["pending"] = {
                    "arm": arm,
                    "turn": turns,
                    "context": context,
                    "cost_after": 0.0,
                }
        if assignment is not None:
            try:
                _assignment_receipt(state_dir, assignment)
            except OSError:
                pass
        if settled is not None:
            pending, action, after = settled
            try:
                _outcome_receipt(state_dir, pending, action, after)
            except OSError:
                pass
        if arm is not None:
            try:
                _trigger_receipt(state_dir, arm, turns, context)
            except OSError:
                pass
        try:
            # Under coach_control the receipts stay counts-only, so no session id.
            _receipt(state_dir, None if control else session_id, context, turns, model, outcome)
        except OSError:
            pass
        if outcome == "shown":
            return {"systemMessage": nudge_text(context, model)}
        return {}
    except Exception:
        # Fail open: a hook must never break the session.
        return {}


def _handle_session_start(payload: dict[str, Any], state_dir: Path) -> None:
    """A new session means any other session with an open nudge stopped producing turns.

    A session still running in parallel looks the same from here; counts only, so a
    wrongly labelled ``end`` costs one observation, never a user-facing claim.
    """
    started = payload.get("session_id") if isinstance(payload, dict) else None
    _flush_pending(state_dir, "end", except_session=started if isinstance(started, str) else None)


def _settle(
    session: dict[str, Any], turns: int, context: int, model: str | None
) -> tuple[dict[str, Any], str, int] | None:
    """Advance the open observation by one turn; return it with its action when decided."""
    pending: dict[str, Any] | None = session["pending"]
    if pending is None:
        return None
    dollars = _turn_cost(model, context)
    if dollars is not None:
        pending["cost_after"] += dollars
    after = turns - pending["turn"]
    if context < pending["context"] * RESTART_DROP:
        # A context reset: /clear, a compaction or a new session segment.
        action = "restart"
    elif after >= ACTION_TURNS:
        action = "none"
    else:
        return None
    session["pending"] = None
    return pending, action, after


def _flush_pending(state_dir: Path, action: str, except_session: str | None = None) -> None:
    """Close every open observation, except one session's, with ``action``."""
    with _lock:
        closed: list[tuple[dict[str, Any], int]] = []
        for session_id, session in _STATE.items():
            pending = session["pending"]
            if pending is None or session_id == except_session:
                continue
            closed.append((pending, session["turns"] - pending["turn"]))
            session["pending"] = None
    for pending, after in closed:
        try:
            _outcome_receipt(state_dir, pending, action, after)
        except OSError:
            pass


def nudge_text(context: int, model: str | None) -> str:
    cost = _cost_phrase(model, context)
    priced = "" if cost is None else f" ({cost})"
    return (
        f"Context Fabric: this session is at ~{round(context / 1000)}K tokens; "
        f"each turn now re-reads it{priced}. If this is a natural break: "
        "/clear starts a fresh context. Turn this off: cfxd set coach off"
    )


def _cost_phrase(model: str | None, context: int) -> str | None:
    priced = _priced(model, context)
    if priced is None:
        return None
    family, dollars = priced
    return f"about ${dollars:.2f} at {_pretty(family)} cache-read prices, API-equivalent"


def _turn_cost(model: str | None, context: int) -> float | None:
    """Cache-read dollars one turn re-reads at this context, the figure the nudge quotes."""
    priced = _priced(model, context)
    return None if priced is None else priced[1]


def _priced(model: str | None, context: int) -> tuple[str, float] | None:
    if model is None:
        return None
    try:
        # Imported here: routing imports daemon, which imports this module at startup.
        from cfxd.routing import _match_family, _normalize, _price_for

        families = _families()
        family = _match_family(_normalize(model), families)
        if family is None:
            return None
        price = _price_for(families[family], context)
        return family, context * float(price["cache_read"]) / 1_000_000.0
    except Exception:
        return None


def _families() -> dict[str, Any]:
    """The price table, read once: a Stop must not re-read it for every observation."""
    global _PRICE_FAMILIES
    if _PRICE_FAMILIES is None:
        from cfxd.routing import _DEFAULT_PRICES, _FAMILIES_KEY, _load_prices

        _PRICE_FAMILIES = _load_prices(_DEFAULT_PRICES)[_FAMILIES_KEY]
    return _PRICE_FAMILIES


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


def _install_id(state_dir: Path) -> str:
    """Stable per install: the resolved state dir, hashed. Cached, so a Stop does no I/O."""
    key = str(state_dir)
    known = _INSTALL.get(key)
    if known is None:
        known = sha256(str(state_dir.resolve()).encode("utf-8")).hexdigest()[:16]
        _INSTALL[key] = known
    return known


def _arm(install_id: str) -> str:
    """Deterministic per install: an even first hash byte warns, an odd one withholds."""
    return "warned" if sha256(install_id.encode("utf-8")).digest()[0] % 2 == 0 else "control"


def _receipt(
    state_dir: Path,
    session_id: str | None,
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
    _append(state_dir, entry)


def _trigger_receipt(state_dir: Path, arm: str, turns: int, context: int) -> None:
    """Counts-only line for the A/B arms: no session id, path or text."""
    entry = {
        "ts": datetime.now(UTC).isoformat(),
        "event": "coach_trigger",
        "arm": arm,
        "turn": turns,
        "context_tokens": context,
    }
    _append(state_dir, entry)


def _assignment_receipt(state_dir: Path, arm: str) -> None:
    """One line per install: which half this install is in, so the split is auditable."""
    entry = {"ts": datetime.now(UTC).isoformat(), "event": "coach_assignment", "arm": arm}
    _append(state_dir, entry)


def _outcome_receipt(state_dir: Path, pending: dict[str, Any], action: str, after: int) -> None:
    """What the user did after a nudge, and the cache-read dollars since the decision."""
    entry = {
        "ts": datetime.now(UTC).isoformat(),
        "event": "coach_outcome",
        "arm": pending["arm"],
        "action": action,
        "turns_after": after,
        "cost_after_usd": round(float(pending["cost_after"]), 4),
    }
    _append(state_dir, entry)


def _append(state_dir: Path, entry: dict[str, Any]) -> None:
    ensure_dir(state_dir)
    with _receipt_lock:
        with open_log(state_dir / COACH_LOG) as fh:
            fh.write(json.dumps(entry) + "\n")
