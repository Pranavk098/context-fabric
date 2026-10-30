"""Count transcript usage once per assistant message.

Claude Code writes one JSONL line per content block and repeats ``message.usage`` on
every line of the same ``message.id``. Summing per line over-counts; the last line of
a message carries the final usage, so that is the one counted, once per id.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


class MessageUsage:
    """Pre-scan of a transcript: last usage and tool_use count per ``message.id``."""

    def __init__(self, objs: Iterable[dict[str, Any]]) -> None:
        self._last: dict[str, dict[str, Any]] = {}
        self._remaining: dict[str, int] = {}
        self._uses: dict[str, int] = {}
        for obj in objs:
            if obj.get("type") != "assistant":
                continue
            msg = obj.get("message")
            if not isinstance(msg, dict):
                continue
            mid = msg.get("id")
            if not isinstance(mid, str) or not mid:
                continue
            content = msg.get("content")
            if isinstance(content, list):
                self._uses[mid] = self._uses.get(mid, 0) + sum(
                    1 for b in content if isinstance(b, dict) and b.get("type") == "tool_use"
                )
            usage = msg.get("usage")
            if isinstance(usage, dict):
                self._last[mid] = usage
                self._remaining[mid] = self._remaining.get(mid, 0) + 1

    def final_usage(self, msg: dict[str, Any], usage: dict[str, Any]) -> dict[str, Any]:
        """The message's final usage (this line's own usage when it has no id)."""
        mid = msg.get("id")
        return self._last.get(mid, usage) if isinstance(mid, str) else usage

    def count_here(self, msg: dict[str, Any]) -> bool:
        """True exactly once per message id: on its last usage-bearing line.

        Call once per assistant line that carries usage, in file order.
        """
        mid = msg.get("id")
        if not isinstance(mid, str) or mid not in self._remaining:
            return True
        self._remaining[mid] -= 1
        return self._remaining[mid] == 0

    def tool_uses(self, msg: dict[str, Any]) -> int | None:
        """Total tool_use blocks across all lines of this message, if it has an id."""
        mid = msg.get("id")
        return self._uses.get(mid) if isinstance(mid, str) else None
