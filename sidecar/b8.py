"""B8 money-safety and window-cap primitives for the sidecar and the live runner.

Two independent, opt-in mechanisms live here so the sidecar stays byte-identical when
neither is enabled:

* **Model allow-list + daily token ledger (money safety).** Only the OpenAI complimentary
  mini-tier models are allowed; the sidecar refuses any other model name before spending.
  A persistent JSONL ledger (one file per UTC day) sums ``prompt + completion (+ reasoning)``
  tokens from every API response; before each request the sidecar refuses (HTTP 400
  ``daily_cap_reached``, "resume tomorrow") when today's total plus the request's maximum
  possible tokens would exceed ``CF_B8_DAILY_CAP``. Upstream is https://api.openai.com; the
  sidecar is the single choke point every agent request passes through.
* **Hard window cap (test mechanism).** ``--window-cap N`` returns an OpenAI-style HTTP 400
  ``context_length_exceeded`` when the rendered input tokens plus ``max_completion_tokens``
  exceed ``N``, so a small-window sub-agent overflows exactly like the real API would.

No clock-derived variance matters here: the ledger key is the UTC day (from the parameter
or the wall clock), and the window cap is a pure function of the request.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kernel.cartridge.serializer import count_tokens

# The OpenAI complimentary mini-tier pool (2.5M tokens/day on usage tiers 1-2). Nothing
# else may be routed through the sidecar for B8, so a typo'd model can never bill a
# flagship rate.
ALLOWED_MODELS = ("gpt-5.4-mini", "gpt-4.1-mini", "gpt-4o-mini")

DEFAULT_DAILY_CAP = 2_200_000
DEFAULT_LEDGER_DIR = "evals/reports/b8_ledger"

WINDOW_CAP_ENV = "CF_B8_WINDOW_CAP"
LEDGER_DIR_ENV = "CF_B8_LEDGER_DIR"
DAILY_CAP_ENV = "CF_B8_DAILY_CAP"

# Per-message role/content/name/tool_call framing overhead. The real o200k_base chat
# template costs ~7-8 tokens per message (the old 4-token constant undercounted the
# recorded prompts by ~3.5 tokens/message — audit F8), so 8 plus the serialized ``tools``
# schema makes the window cap an upper bound rather than an undercount.
_FRAME_TOKENS = 8


def utc_day(now: datetime | None = None) -> str:
    """The ledger's UTC day key (``YYYY-MM-DD``)."""
    return (now or datetime.now(tz=UTC)).date().isoformat()


def max_completion_tokens(payload: dict[str, Any]) -> int:
    """The request's output reserve: ``max_completion_tokens`` else ``max_tokens``."""
    value = payload.get("max_completion_tokens")
    if value is None:
        value = payload.get("max_tokens")
    if isinstance(value, int) and value >= 0:
        return value
    return 0


def estimate_messages_tokens(
    messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]] | None = None
) -> int:
    """Token estimate for an OpenAI-style message list (o200k_base plus framing).

    Counts string/list ``content``, any function-call ``tool_calls`` (mini-swe-agent routes
    bash commands as tool calls), the serialized ``tools`` schema, and per-message chat
    framing — so the window cap is an upper bound on the real prompt, not an undercount
    (audit F8).
    """

    def text(message: dict[str, Any]) -> str:
        parts: list[str] = []
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if isinstance(function, dict):
                parts.append(function.get("name", ""))
                parts.append(function.get("arguments", ""))
        return " ".join(parts)

    total = sum(count_tokens(text(m)) + _FRAME_TOKENS for m in messages)
    if tools:
        total += count_tokens(json.dumps(tools))
    return total


def is_allowed_model(model: str) -> bool:
    """True when ``model`` names an allow-listed mini-tier model (``openai/`` prefix ok)."""
    return model.rsplit("/", 1)[-1] in ALLOWED_MODELS


def context_window_error(input_tokens: int, max_tokens: int, window_cap: int) -> dict[str, Any]:
    """OpenAI-style ``context_length_exceeded`` payload (HTTP 400).

    ``code == "context_length_exceeded"`` is what ``litellm`` maps to
    ``ContextWindowExceededError`` (raised by mini-swe-agent's ``LitellmModel`` and
    propagated out of ``DefaultAgent.run`` as a real, un-gamed overflow).
    """
    total = input_tokens + max_tokens
    return {
        "error": {
            "message": (
                f"This model's maximum context length is {window_cap} tokens. However, "
                f"your messages resulted in {total} tokens ({input_tokens} in the messages, "
                f"{max_tokens} in the completion)."
            ),
            "type": "invalid_request_error",
            "param": "messages",
            "code": "context_length_exceeded",
        }
    }


def daily_cap_error(used: int, max_possible: int, cap: int, day: str) -> dict[str, Any]:
    """HTTP 400 ``daily_cap_reached`` payload: a clean, non-retryable "resume tomorrow".

    ``code == "daily_cap_reached"`` with HTTP 400 maps to ``litellm.BadRequestError``
    (never the retried 429/5xx path); the runner treats it as ``cap_stopped``.
    """
    return {
        "error": {
            "message": (
                f"Daily token cap reached: {used} tokens used today ({day}); this request "
                f"needs up to {max_possible} more, exceeding CF_B8_DAILY_CAP={cap}. "
                "Resume tomorrow."
            ),
            "type": "invalid_request_error",
            "param": None,
            "code": "daily_cap_reached",
        }
    }


class Ledger:
    """Persistent daily token ledger: one JSONL file per UTC day.

    Thread-safe: the sidecar serves one handler thread per connection, so appends and
    reads must not interleave on the same file.
    """

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def day_path(self, day: str) -> Path:
        return self.directory / f"tokens-{day}.jsonl"

    def day_total(self, day: str) -> int:
        """Sum of prompt + completion (+ reasoning) tokens recorded for ``day``."""
        return self.summary(day)["total_tokens"]

    def summary(self, day: str) -> dict[str, int]:
        """Sum every token field recorded for ``day`` (0s when the day has no ledger file)."""
        with self._lock:
            return self._summary_locked(day)

    def _summary_locked(self, day: str) -> dict[str, int]:
        path = self.day_path(day)
        keys = (
            "prompt_tokens",
            "completion_tokens",
            "reasoning_tokens",
            "cached_tokens",
            "uncached_tokens",
            "total_tokens",
        )
        totals = {key: 0 for key in keys}
        if not path.is_file():
            return totals
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            for key in keys:
                totals[key] += int(row.get(key) or 0)
        return totals

    def append(
        self,
        day: str,
        *,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        reasoning_tokens: int = 0,
        cached_tokens: int = 0,
    ) -> int:
        """Record one API response's usage; returns the line's ``total_tokens``."""
        total = prompt_tokens + completion_tokens + reasoning_tokens
        record = {
            "day": day,
            "ts": datetime.now(tz=UTC).isoformat(),
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "reasoning_tokens": reasoning_tokens,
            "cached_tokens": cached_tokens,
            "uncached_tokens": max(prompt_tokens - cached_tokens, 0),
            "total_tokens": total,
        }
        with self._lock:
            with self.day_path(day).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
        return total


def parse_usage(payload: dict[str, Any]) -> dict[str, int]:
    """Extract ``prompt/completion/reasoning/cached`` from an OpenAI ``usage`` block."""
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return {}
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    details = usage.get("prompt_tokens_details")
    completion_details = usage.get("completion_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    reasoning = (
        completion_details.get("reasoning_tokens") if isinstance(completion_details, dict) else None
    )
    result: dict[str, int] = {}
    if isinstance(prompt, int):
        result["prompt_tokens"] = prompt
    if isinstance(completion, int):
        result["completion_tokens"] = completion
    if isinstance(cached, int):
        result["cached_tokens"] = cached
    if isinstance(reasoning, int):
        result["reasoning_tokens"] = reasoning
    return result
