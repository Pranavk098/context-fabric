"""Offline trajectory replay lab — replay recorded mini-swe-agent trajectories through
observation-folding policies and measure context shape. No model, no GPU, no API.

**EXPLORATORY. Not a result of record.** Replay shows *context shape* (tokens, overflow,
prefix stability), not task success — a folded trajectory that fits the window may still
solve nothing, and one that overflows may still solve the task. Numbers here are
diagnostics, not accuracy claims. Receipts land under ``evals/reports/replay_lab_*.json``.

For every recorded trajectory (a mini-swe-agent ``*.traj.json`` message list), the lab:

* splits the message list into per-step prefixes (the exact input to each API call);
* replays each prefix through three policies — ``none`` (verbatim), ``obsfold``
  (:mod:`sidecar.obsfold`, imported and called as-is) and ``obsfold_v2``
  (:mod:`sidecar.obsfold_v2`) — with the default ``n=3``/``k=3`` mask and no window guard;
* reports, per step and policy: input tokens (tiktoken ``o200k_base``), what was folded,
  would-overflow at the 24,576 and 16,384 window caps, and the byte-identical leading
  prefix length vs the previous step (a cache-hit proxy);
* reports the share of tokens that are tool outputs in the raw recording, cross-checked
  against the recorded ``prompt_tokens``/``cached_tokens`` when the trajectory records them.

Trajectory files are read-only inputs; run with ``python -m evals.replay_lab --traj PATH ...``
(or ``--traj-dir DIR`` to scan for ``*.traj.json``).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from protocol.canonical import canonicalize
from sidecar import obsfold, obsfold_v2

REPORTS = Path(__file__).parent / "reports"

POLICIES = ("none", "obsfold", "obsfold_v2")
WINDOW_CAPS = (24576, 16384)
NOTE = (
    "exploratory context-shape diagnostic; replay shows token/overflow/prefix shape, "
    "not task success"
)


def load_trajectory(path: Path) -> tuple[str, list[dict[str, Any]]]:
    """Load a ``*.traj.json``; return ``(name, messages)`` (the OpenAI-style message list)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    messages = data.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError(f"{path}: no messages list")
    return path.stem, messages


def _action_indices(messages: list[dict[str, Any]]) -> list[int]:
    """Indices of assistant messages that issued tool calls (one per API call / step)."""
    return [
        index
        for index, message in enumerate(messages)
        if message.get("role") == "assistant"
        and isinstance(message.get("tool_calls"), list)
        and message["tool_calls"]
    ]


def _step_prefixes(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Per-step inputs: everything before each assistant tool call."""
    return [messages[:index] for index in _action_indices(messages)]


def _recorded_usage(messages: list[dict[str, Any]]) -> list[dict[str, int | None]] | None:
    """Recorded ``prompt_tokens``/``cached_tokens`` per step, if the trajectory has them."""
    rows: list[dict[str, int | None]] = []
    for index in _action_indices(messages):
        usage = messages[index].get("extra", {}).get("response", {}).get("usage")
        if not isinstance(usage, dict):
            return None
        prompt = usage.get("prompt_tokens")
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        rows.append(
            {
                "prompt_tokens": prompt if isinstance(prompt, int) else None,
                "cached_tokens": cached if isinstance(cached, int) else None,
            }
        )
    return rows or None


def render(messages: list[dict[str, Any]]) -> bytes:
    """Newline-joined canonical bytes — the prompt-shape proxy for prefix stability."""
    return b"\n".join(canonicalize(message) for message in messages)


def _apply_none(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return [dict(message) for message in messages], []


def _apply_obsfold(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    result = obsfold.fold_messages(messages)
    return result.messages, [
        {"tool": event.tool_name, "bytes": event.content_bytes} for event in result.events
    ]


def _apply_obsfold_v2(
    messages: list[dict[str, Any]], task_identifiers: tuple[str, ...]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    result = obsfold_v2.fold_messages(messages, task_identifiers=task_identifiers)
    return result.messages, [
        {"tool": event.tool_name, "bytes": event.content_bytes} for event in result.events
    ]


def apply_policy(
    name: str, messages: list[dict[str, Any]], *, task_identifiers: tuple[str, ...] = ()
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return ``(folded_messages, folded_info)`` for one policy."""
    if name == "none":
        return _apply_none(messages)
    if name == "obsfold":
        return _apply_obsfold(messages)
    if name == "obsfold_v2":
        return _apply_obsfold_v2(messages, task_identifiers)
    raise ValueError(f"unknown policy {name!r}")


def _common_prefix(a: bytes, b: bytes) -> int:
    length = min(len(a), len(b))
    low, high = 0, length
    while low < high:
        mid = (low + high + 1) // 2
        if a[:mid] == b[:mid]:
            low = mid
        else:
            high = mid - 1
    return low


def tool_output_share(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Share of tokens that are tool observations (the 67-84% external claim).

    ``share`` is tool / all messages; ``share_of_turns`` is tool / (assistant + tool),
    i.e. the growing multi-turn portion excluding the fixed system/user handoff — the
    closest match to how the external studies measure "tool outputs as % of agent tokens".
    """
    tool_messages = [message for message in messages if message.get("role") == "tool"]
    turn_messages = [
        message for message in messages if message.get("role") in {"assistant", "tool"}
    ]
    tool_tokens = obsfold.estimate_tokens(tool_messages)
    total_tokens = obsfold.estimate_tokens(messages)
    turn_tokens = obsfold.estimate_tokens(turn_messages)
    return {
        "tool_tokens": tool_tokens,
        "total_tokens": total_tokens,
        "share": round(tool_tokens / total_tokens, 4) if total_tokens else 0.0,
        "turn_tokens": turn_tokens,
        "share_of_turns": round(tool_tokens / turn_tokens, 4) if turn_tokens else 0.0,
    }


def replay_messages(
    messages: list[dict[str, Any]], *, task_identifiers: tuple[str, ...] = (), name: str = ""
) -> dict[str, Any]:
    """Replay one message list through every policy; return the per-policy report."""
    prefixes = _step_prefixes(messages)
    recorded = _recorded_usage(messages)
    policies: dict[str, Any] = {}
    for policy in POLICIES:
        steps: list[dict[str, Any]] = []
        previous: bytes | None = None
        peak = 0
        total = 0
        overflow_24576: int | None = None
        overflow_16384: int | None = None
        prefix_lengths: list[int] = []
        for step, prefix in enumerate(prefixes):
            folded, folded_info = apply_policy(policy, prefix, task_identifiers=task_identifiers)
            tokens = obsfold.estimate_tokens(folded)
            rendered = render(folded)
            if previous is not None:
                shared = _common_prefix(previous, rendered)
                prefix_lengths.append(shared)
                prefix_bytes = shared
            else:
                prefix_bytes = len(rendered)
            previous = rendered
            total += tokens
            peak = max(peak, tokens)
            if overflow_24576 is None and tokens > 24576:
                overflow_24576 = step
            if overflow_16384 is None and tokens > 16384:
                overflow_16384 = step
            steps.append(
                {
                    "step": step,
                    "input_tokens": tokens,
                    "folded": len(folded_info),
                    "folded_info": folded_info,
                    "overflow_24576": tokens > 24576,
                    "overflow_16384": tokens > 16384,
                    "prefix_bytes": prefix_bytes,
                }
            )
        policies[policy] = {
            "steps": steps,
            "total_tokens": total,
            "peak_tokens": peak,
            "overflow_step_24576": overflow_24576,
            "overflow_step_16384": overflow_16384,
            "prefix_stability": {
                "steps": len(prefix_lengths),
                "min_common_prefix": min(prefix_lengths) if prefix_lengths else None,
                "avg_common_prefix": round(sum(prefix_lengths) / len(prefix_lengths), 1)
                if prefix_lengths
                else None,
            },
        }
    report: dict[str, Any] = {
        "scenario": "replay_lab",
        "kind": "dry_run",
        "note": NOTE,
        "trajectory": name,
        "n_steps": len(prefixes),
        "recorded_usage": recorded,
        "tool_output_share": tool_output_share(messages),
        "policies": policies,
    }
    return report


def _print_summary(report: dict[str, Any]) -> None:
    print(
        f"replay_lab: trajectory={report['trajectory']} steps={report['n_steps']} "
        f"tool_output_share={report['tool_output_share']['share']:.1%} "
        f"(of turns: {report['tool_output_share']['share_of_turns']:.1%})"
    )
    print(
        f"{'policy':<12}{'total_tokens':>14}{'peak_tokens':>13}{'overflow@24k':>14}{'overflow@16k':>14}"
    )
    for policy in POLICIES:
        body = report["policies"][policy]
        print(
            f"{policy:<12}{body['total_tokens']:>14}{body['peak_tokens']:>13}"
            f"{str(body['overflow_step_24576']):>14}{str(body['overflow_step_16384']):>14}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.replay_lab",
        description="Offline trajectory replay lab (context-shape diagnostic, no model).",
    )
    parser.add_argument("--traj", action="append", default=[], help="*.traj.json path (repeatable)")
    parser.add_argument("--traj-dir", default="", help="scan a directory for *.traj.json")
    parser.add_argument("--out", default="", help="receipt path override")
    args = parser.parse_args(argv)

    paths: list[Path] = [Path(path) for path in args.traj]
    if args.traj_dir:
        paths.extend(sorted(Path(args.traj_dir).rglob("*.traj.json")))
    if not paths:
        print("replay_lab: --traj or --traj-dir is required", file=sys.stderr)
        return 2

    receipts: list[dict[str, Any]] = []
    for path in paths:
        try:
            name, messages = load_trajectory(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"replay_lab: cannot load {path}: {exc}", file=sys.stderr)
            return 2
        task = next(
            (
                str(message.get("content", ""))
                for message in messages
                if message.get("role") == "user"
            ),
            "",
        )
        identifiers = obsfold_v2.extract_identifiers(task)
        report = replay_messages(messages, task_identifiers=identifiers, name=name)
        receipts.append(report)
        _print_summary(report)

    ts = time.strftime("%Y%m%d_%H%M%S")
    REPORTS.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if args.out else REPORTS / f"replay_lab_{ts}.json"
    out.write_text(json.dumps(receipts, indent=2), encoding="utf-8")
    print(f"receipt: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
