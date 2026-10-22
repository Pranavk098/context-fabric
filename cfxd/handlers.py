"""Hook handler registry.

Sprint 1 ships no real handlers, only a test-only echo. Later tasks (fold,
brief, carry) call :func:`register` to plug in logic.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

Handler = Callable[[dict[str, Any]], dict[str, Any] | None]

_HANDLERS: dict[str, Handler] = {}


def register(event: str, fn: Handler) -> None:
    """Register ``fn`` as the handler for hook ``event`` (e.g. ``PreToolUse``)."""
    _HANDLERS[event] = fn


def dispatch(event: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Run the handler for ``event``; no handler or a ``None`` result yields ``{}``."""
    fn = _HANDLERS.get(event)
    if fn is None:
        return {}
    result = fn(payload)
    return {} if result is None else result


def echo(payload: dict[str, Any]) -> dict[str, Any]:
    """Test-only handler: return the payload unchanged to prove dispatch works."""
    return dict(payload)


def clear() -> None:
    """Test helper: drop every registered handler."""
    _HANDLERS.clear()
