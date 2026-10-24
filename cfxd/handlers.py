"""Hook handler registry.

Sprint 1 ships no real handlers, only a test-only echo. Later tasks (fold,
brief, carry) call :func:`register` to plug in logic.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

Handler = Callable[[dict[str, Any]], dict[str, Any] | None]
Observer = Callable[[dict[str, Any]], None]

_HANDLERS: dict[str, Handler] = {}
_OBSERVERS: dict[str, list[Observer]] = {}


def register(event: str, fn: Handler) -> None:
    """Register ``fn`` as the handler for hook ``event`` (e.g. ``PreToolUse``)."""
    _HANDLERS[event] = fn


def add_observer(event: str, fn: Observer) -> None:
    """Register ``fn`` as an observer for ``event``.

    Observers run before the handler; their result is ignored and exceptions
    are swallowed, so they can never change or break the hook response.
    """
    _OBSERVERS.setdefault(event, []).append(fn)


def dispatch(event: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Run observers then the handler for ``event``; no handler or ``None`` -> ``{}``."""
    for observer in _OBSERVERS.get(event, ()):
        try:
            observer(payload)
        except Exception:
            pass
    fn = _HANDLERS.get(event)
    if fn is None:
        return {}
    result = fn(payload)
    return {} if result is None else result


def echo(payload: dict[str, Any]) -> dict[str, Any]:
    """Test-only handler: return the payload unchanged to prove dispatch works."""
    return dict(payload)


def clear() -> None:
    """Test helper: drop every registered handler and observer."""
    _HANDLERS.clear()
    _OBSERVERS.clear()
