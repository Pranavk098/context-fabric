"""Codex CLI (beta) rollout reader behind the ``cfxd.logs`` interface.

Discovery, bounded line iteration and the optional-zstd policy come from
``cfxd.agents.codex``. Per-turn usage is the sum of ``token_count`` events'
``last_token_usage``; a consecutive repeat of the same ``total_token_usage``
snapshot is skipped (the same step is logged twice around completion). Codex has
no separate cache-write tier, so that bucket is always 0. Only counts, model ids
and token integers leave the file: never a path, prompt, command or tool output.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from cfxd.agents.codex import _USAGE_FIELDS, _discover, _iter_lines, _zstd_open
from cfxd.agents.model import default_root as _agent_root
from cfxd.agents.model import since_cutoff
from cfxd.logs import BUCKETS, Session, Turn

name = "codex"

_LABELS = {"7d": "last 7 days", "30d": "last 30 days", "all": "all time"}
_SKIP_MSG = "{:,} compressed session(s) skipped: no zstd decoder installed"
_MTOK = 1_000_000.0


def default_root() -> Path:
    """``$CODEX_HOME/sessions`` (default ``~/.codex/sessions``)."""
    return _agent_root("codex")


def sessions(root: Path, cutoff: datetime | None = None) -> Iterator[Session]:
    """Yield one Session per Codex rollout file under ``root`` (counts only)."""
    zmod = _zstd_open()
    skipped = 0
    for path, is_zst in _discover(root, cutoff):
        if is_zst and zmod is None:
            skipped += 1
            continue
        yield _read(path, is_zst, zmod)
    if skipped:
        print(_SKIP_MSG.format(skipped), file=sys.stderr)


def _read(path: Path, is_zst: bool, zmod: Any) -> Session:
    turns: list[Turn] = []
    model: str | None = None
    acc: dict[str, int] | None = None
    turn_model: str | None = None
    calls = 0
    prev = tuple(0 for _ in _USAGE_FIELDS)

    def flush() -> None:
        nonlocal acc, turn_model, calls
        if acc is not None:
            turns.append(Turn(_bucket(acc), turn_model, calls))
        acc = None
        turn_model = None
        calls = 0

    for obj in _iter_lines(path, is_zst, zmod):
        kind = obj.get("type")
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            continue
        if kind == "turn_context":
            value = payload.get("model")
            if isinstance(value, str) and value.strip():
                model = value
                if acc is not None and turn_model is None:
                    # A turn that started before its context line now knows its model.
                    turn_model = model
        elif kind == "event_msg":
            etype = payload.get("type")
            if etype == "task_started":
                flush()
                acc = _zero()
                turn_model = model
            elif etype == "token_count":
                info = payload.get("info")
                if not isinstance(info, dict):
                    continue
                total = info.get("total_token_usage")
                if not isinstance(total, dict):
                    continue
                snapshot = tuple(_int(total.get(f)) for f in _USAGE_FIELDS)
                if snapshot == prev:
                    continue  # duplicate snapshot around turn completion
                last = info.get("last_token_usage")
                if isinstance(last, dict):
                    delta = [_int(last.get(f)) for f in _USAGE_FIELDS]
                else:
                    delta = [max(0, snapshot[i] - prev[i]) for i in range(len(_USAGE_FIELDS))]
                prev = snapshot
                if acc is None:
                    acc = _zero()
                    turn_model = model
                for field, value in zip(_USAGE_FIELDS, delta, strict=True):
                    acc[field] += max(0, value)
        elif kind == "response_item":
            if payload.get("type") in ("function_call", "custom_tool_call"):
                if acc is None:
                    acc = _zero()
                    turn_model = model
                calls += 1

    flush()
    return Session(turns)


def report(root: Path, since: str = "30d", now: datetime | None = None) -> dict[str, Any]:
    """Aggregate the Codex sessions under ``root`` (counts and token totals only)."""
    totals = dict.fromkeys(BUCKETS, 0)
    models: dict[str, dict[str, Any]] = {}
    sessions_n = turns_n = tool_calls = 0
    for session in sessions(root, since_cutoff(since, now)):
        sessions_n += 1
        for turn in session.turns:
            turns_n += 1
            tool_calls += turn.tool_calls
            for bucket in BUCKETS:
                totals[bucket] += turn.usage.get(bucket, 0)
            model = turn.model
            if model is not None:
                slot = models.setdefault(model, {"turns": 0, "usage": dict.fromkeys(BUCKETS, 0)})
                slot["turns"] += 1
                for bucket in BUCKETS:
                    slot["usage"][bucket] += turn.usage.get(bucket, 0)
    rows, dollars, unpriced = _price(models)
    token_sum = sum(totals.values())
    return {
        "source": "codex",
        "since": since,
        "sessions": sessions_n,
        "turns": turns_n,
        "tool_calls": tool_calls,
        "usage": totals,
        "tokens": token_sum,
        "cache_read_share": round(totals["cache_read"] / token_sum, 4) if token_sum else None,
        "models": rows,
        "dollars": dollars,
        "unpriced_models": unpriced,
    }


def format_report(report: dict[str, Any], kind: str, label: str | None = None) -> str:
    """Render the Codex (beta) report; no path, prompt, command or guess anywhere."""
    if report["sessions"] == 0:
        return "Codex (beta): no sessions found in this window (try --since all)."
    when = label or _LABELS.get(str(report["since"]), str(report["since"]))
    usage = report["usage"]
    lines = [f"Codex (beta) {kind}, {when}"]
    if kind == "week":
        lines.append("Limit-weighted new tokens: n/a (no limit weights for this model)")
        lines.append(
            "Limit share: n/a (no official 5-hour or weekly token limit for any plan tier; "
            "decision D-3)"
        )
    lines.append(
        f"Sessions: {report['sessions']:,}, turns: {report['turns']:,}, "
        f"tool calls: {report['tool_calls']:,}"
    )
    lines.append(
        f"Tokens: input {usage['input']:,} (uncached), cache read {usage['cache_read']:,}, "
        f"cache write {usage['cache_write']:,}, output {usage['output']:,} "
        f"({report['tokens']:,} total)"
    )
    share = report["cache_read_share"]
    lines.append(
        "Cache-read share of tokens: "
        + ("n/a (no tokens recorded)" if share is None else f"{share * 100:.1f}%")
    )
    if report["dollars"] is None:
        names = ", ".join(report["unpriced_models"]) or "no model recorded"
        lines.append(
            f"Dollars: no price for this model ({names}); "
            "counts and token totals only, no price is guessed."
        )
    else:
        lines.append(
            f"Dollars: ${report['dollars']:,.2f} at OpenAI list prices from "
            "cfxd/data/prices.json (not a bill)."
        )
    lines.append("Models:")
    for row in report["models"]:
        lines.append(f"  {row['model']}: {_plural(row['turns'], 'turn')}, {row['tokens']:,} tokens")
    lines.append("Cache write is always 0: Codex records no separate cache-write tier.")
    lines.append(
        "This report contains counts and token totals only; no paths, prompts or commands."
    )
    return "\n".join(lines)


def _bucket(acc: dict[str, int]) -> dict[str, int]:
    """Codex's cumulative fields as the interface's four buckets (cache write = 0)."""
    return {
        "input": max(0, acc["input_tokens"] - acc["cached_input_tokens"]),
        "cache_read": acc["cached_input_tokens"],
        "cache_write": 0,
        "output": acc["output_tokens"],
    }


def _zero() -> dict[str, int]:
    return {field: 0 for field in _USAGE_FIELDS}


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _plural(count: int, word: str) -> str:
    return f"{count:,} {word}" + ("" if count == 1 else "s")


def _price(
    models: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], float | None, list[str]]:
    """Per-model rows plus dollars, or ``None`` when any model is unpriced."""
    from cfxd.routing import _DEFAULT_PRICES, _load_prices, _match_family, _normalize

    try:
        families: dict[str, Any] | None = _load_prices(_DEFAULT_PRICES)["families"]
    except (OSError, ValueError, KeyError):
        families = None
    rows: list[dict[str, Any]] = []
    unpriced: list[str] = []
    total = 0.0
    for model, slot in models.items():
        usage = slot["usage"]
        family = _match_family(_normalize(model), families) if families else None
        if family is None or families is None:
            unpriced.append(model)
            dollars = None
        else:
            price = families[family]
            dollars = round(
                (
                    usage["input"] * float(price["input"])
                    + usage["cache_read"] * float(price["cache_read"])
                    + usage["output"] * float(price["output"])
                )
                / _MTOK,
                4,
            )
            total += dollars
        rows.append(
            {
                "model": model,
                "turns": slot["turns"],
                "tokens": sum(usage.values()),
                "dollars": dollars,
            }
        )
    rows.sort(key=lambda row: (-row["tokens"], row["model"]))
    unpriced.sort()
    return rows, (None if unpriced else round(total, 4)), unpriced
