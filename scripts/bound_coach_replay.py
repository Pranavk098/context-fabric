"""Offline replay of bound and coach over real Claude Code transcripts.

Read-only over ``<root>/*/<session>.jsonl``. Reports aggregates only: counts, shares,
medians. No path, prompt, command or transcript text is printed or stored.

Bound: a Bash result whose command the PreToolUse rule would rewrite
(``cfxd.bound._command_key``) is replayed by keeping the last ``TAIL_LINES`` lines of
the text the model actually saw. Tokens are estimated as characters / 4. The risk
metric is the share of dropped heads that contain ``error|Error|FAILED|Traceback``.

Coach: the Stop hook is replayed per session. A turn ends on an assistant message
whose ``stop_reason`` is not ``tool_use``; its context is input + cache creation +
cache read, and its dollars use the repo list prices. The nudge rule is imported from
``cfxd.coach`` so the replay cannot drift from the shipped thresholds. Precision and
recall are measured against the top 10% of sessions by cost.
"""

from __future__ import annotations

import argparse
import math
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from cfxd.bound import TAIL_LINES, _command_key, test_key_any  # noqa: E402
from cfxd.brief_receipts import _blocks, _iter_jsonl  # noqa: E402
from cfxd.coach import (  # noqa: E402
    CONTEXT_THRESHOLD,
    COOLDOWN_TURNS,
    GROWTH_TOKENS,
    TURN_THRESHOLD,
)
from cfxd.routing import (  # noqa: E402
    _DEFAULT_PRICES,
    _load_prices,
    _match_family,
    _normalize,
    _prompt,
    _tokens,
    _turn_dollars,
)
from cfxd.usage_dedupe import MessageUsage  # noqa: E402

_ERROR_RE = re.compile(r"error|Error|FAILED|Traceback")
_SYNTHETIC = "<synthetic>"
_CHARS_PER_TOKEN = 4.0
_TOP_SHARE = 0.10


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


# ------------------------------------------------------------------ bound


def scan_bound(objs: list[dict[str, Any]]) -> dict[str, Any]:
    """Replay each linked Bash result the shipped rule rewrites, plus a wider variant.

    ``rule`` is ``shipped`` for a command the PreToolUse handler would rewrite today,
    ``recognised`` for a test runner only found inside a compound command (the
    ``test_key_any`` variant; hypothetical, not shipped). Nothing else is rewritable.
    """
    uses: dict[str, tuple[str, bool]] = {}
    records: list[dict[str, Any]] = []
    stats: Counter[str] = Counter()
    for obj in objs:
        if obj.get("isSidechain"):
            continue
        for block in _blocks(obj):
            if block.get("type") == "tool_use":
                inp = block.get("input")
                if (
                    block.get("name") == "Bash"
                    and isinstance(block.get("id"), str)
                    and isinstance(inp, dict)
                    and isinstance(inp.get("command"), str)
                ):
                    uses[block["id"]] = (
                        inp["command"],
                        inp.get("run_in_background") is True,
                    )
            elif block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str):
                use = uses.get(block["tool_use_id"])
                if use is None:
                    stats["result_unlinked"] += 1
                    continue
                if use[1]:
                    stats["background"] += 1
                    continue
                stats["bash_results"] += 1
                text = _content_text(block.get("content"))
                if not text:
                    stats["empty_text"] += 1
                    continue
                key = _command_key(use[0])
                rule = "shipped"
                if key is None:
                    key = test_key_any(use[0])
                    rule = "recognised"
                if key is None:
                    stats["not_recognised"] += 1
                    continue
                lines = text.splitlines(keepends=True)
                head = "".join(lines[:-TAIL_LINES])
                tail = "".join(lines[-TAIL_LINES:])
                full_error = _ERROR_RE.search(text) is not None
                records.append(
                    {
                        "rule": rule,
                        "key": key,
                        "full_tokens": len(text) / _CHARS_PER_TOKEN,
                        "kept_tokens": len(tail) / _CHARS_PER_TOKEN,
                        "dropped": bool(head),
                        "dropped_error": bool(head) and _ERROR_RE.search(head) is not None,
                        "full_error": full_error,
                        "error_lost": full_error and _ERROR_RE.search(tail) is None,
                    }
                )
    return {"records": records, "stats": stats}


def summarise_bound(records: list[dict[str, Any]], rule: str = "shipped") -> dict[str, Any]:
    records = [r for r in records if r["rule"] == rule]
    full = sum(r["full_tokens"] for r in records)
    kept = sum(r["kept_tokens"] for r in records)
    dropped = [r for r in records if r["dropped"]]
    by_key: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"n": 0, "dropped": 0, "full": 0.0, "kept": 0.0, "dropped_error": 0}
    )
    for r in records:
        k = by_key[r["key"]]
        k["n"] += 1
        k["dropped"] += int(r["dropped"])
        k["full"] += r["full_tokens"]
        k["kept"] += r["kept_tokens"]
        k["dropped_error"] += int(r["dropped_error"])
    return {
        "n": len(records),
        "n_dropped": len(dropped),
        "full_tokens": full,
        "kept_tokens": kept,
        "saved_tokens": full - kept,
        "saved_share": (full - kept) / full if full else None,
        "n_dropped_error": sum(1 for r in dropped if r["dropped_error"]),
        "dropped_error_share": (
            sum(1 for r in dropped if r["dropped_error"]) / len(dropped) if dropped else None
        ),
        "n_full_error": sum(1 for r in records if r["full_error"]),
        "n_error_lost": sum(1 for r in records if r["error_lost"]),
        "by_key": dict(sorted(by_key.items())),
    }


# ------------------------------------------------------------------ coach


def coach_nudges(turns: list[tuple[int, str | None]]) -> list[int]:
    """Turn numbers at which the shipped Stop rule would have shown a nudge."""
    shown: list[int] = []
    last_context = 0
    last_turn = 0
    for number, (context, _model) in enumerate(turns, 1):
        if context < CONTEXT_THRESHOLD or number < TURN_THRESHOLD:
            continue
        if last_turn and (
            number - last_turn < COOLDOWN_TURNS or context - last_context < GROWTH_TOKENS
        ):
            continue
        shown.append(number)
        last_context = context
        last_turn = number
    return shown


def scan_coach(objs: list[dict[str, Any]], prices: dict[str, Any]) -> dict[str, Any]:
    """Session cost and one (context, model) pair per assistant turn end."""
    dedupe = MessageUsage(objs)
    families = prices["families"]
    cost = 0.0
    unpriced = 0
    turns: list[tuple[int, str | None]] = []
    for obj in objs:
        if obj.get("isSidechain") or obj.get("type") != "assistant":
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        usage = msg.get("usage")
        if not isinstance(usage, dict) or not dedupe.count_here(msg):
            continue
        tokens = _tokens(dedupe.final_usage(msg, usage))
        prompt = _prompt(tokens)
        model = msg.get("model")
        model = model if isinstance(model, str) and model and model != _SYNTHETIC else None
        family = _match_family(_normalize(model), families) if model else None
        if family is None:
            unpriced += 1
        else:
            cost += _turn_dollars(families[family], tokens, prompt)
        stop = msg.get("stop_reason")
        if isinstance(stop, str) and stop != "tool_use":
            turns.append((prompt, model))
    return {"cost": cost, "unpriced": unpriced, "turns": turns}


def summarise_coach(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    priced = [s for s in sessions if s["cost"] > 0]
    ranked = sorted(priced, key=lambda s: s["cost"], reverse=True)
    k = max(1, math.ceil(_TOP_SHARE * len(ranked))) if ranked else 0
    top = {s["session"] for s in ranked[:k]}
    fired: list[tuple[dict[str, Any], list[int]]] = []
    for s in ranked:
        nudges = coach_nudges(s["turns"])
        if nudges:
            fired.append((s, nudges))
    hit = sum(1 for s, _ in fired if s["session"] in top)
    eligible_top = sum(
        1
        for s in ranked[:k]
        if any(
            context >= CONTEXT_THRESHOLD and i >= TURN_THRESHOLD
            for i, (context, _m) in enumerate(s["turns"], 1)
        )
    )
    firsts = [nudges[0] for _s, nudges in fired]
    return {
        "n_sessions": len(sessions),
        "n_priced": len(priced),
        "n_zero": len(sessions) - len(priced),
        "top_k": k,
        "cutoff_usd": ranked[k - 1]["cost"] if k else None,
        "n_fired": len(fired),
        "n_fired_top": hit,
        "precision": hit / len(fired) if fired else None,
        "recall": hit / k if k else None,
        "eligible_top": eligible_top,
        "recall_ceiling": eligible_top / k if k else None,
        "median_first_nudge": statistics.median(firsts) if firsts else None,
        "n_first_nudge_q": _quartiles([float(x) for x in firsts]),
        "n_multi_nudge": sum(1 for _s, nudges in fired if len(nudges) > 1),
        "n_unpriced_turns": sum(s["unpriced"] for s in sessions),
    }


def _quartiles(values: list[float]) -> tuple[float, float, float] | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0], values[0], values[0]
    q = statistics.quantiles(values, n=4, method="inclusive")
    return q[0], statistics.median(values), q[2]


# ------------------------------------------------------------------ collection


def collect(root: Path, prices: dict[str, Any]) -> dict[str, Any]:
    stats: Counter[str] = Counter()
    seen: set[str] = set()
    bound: list[dict[str, Any]] = []
    sessions: list[dict[str, Any]] = []
    for path in sorted(root.glob("*/*.jsonl")):
        session = path.stem
        if session in seen:
            stats["duplicate_session_copies_skipped"] += 1
            continue
        seen.add(session)
        objs = list(_iter_jsonl(path))
        if not objs:
            stats["empty_transcripts_skipped"] += 1
            continue
        bounded = scan_bound(objs)
        bound.extend(bounded["records"])
        stats.update(bounded["stats"])
        coach = scan_coach(objs, prices)
        coach["session"] = session
        sessions.append(coach)
    return {"stats": stats, "bound": bound, "sessions": sessions}


# ------------------------------------------------------------------ render


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


def _num(value: float | None) -> str:
    return "n/a" if value is None else f"{value:,.0f}"


def _q(value: tuple[float, float, float] | None) -> str:
    return "n/a" if value is None else f"{value[0]:.0f} / {value[1]:.0f} / {value[2]:.0f}"


def render(data: dict[str, Any]) -> str:
    bound = summarise_bound(data["bound"])
    relaxed = summarise_bound(data["bound"], "recognised")
    coach = summarise_coach(data["sessions"])
    stats = data["stats"]
    out: list[str] = []
    w = out.append
    w("# Bound and coach replay over real transcripts")
    w("")
    w("Generated by `scripts/bound_coach_replay.py`. Aggregates only: no paths, prompts,")
    w("commands or transcript text. Tokens are characters / 4; dollars are API list-price")
    w("equivalents, not a bill.")
    w("")
    w("## Sample")
    w("")
    w(
        f"- Sessions scanned: {coach['n_sessions']}; duplicate copies skipped: "
        f"{stats['duplicate_session_copies_skipped']}; empty transcripts skipped: "
        f"{stats['empty_transcripts_skipped']}"
    )
    w(
        f"- Sessions with a priced turn: {coach['n_priced']} (zero cost: "
        f"{coach['n_zero']}); assistant turns on an unpriced model: "
        f"{coach['n_unpriced_turns']}"
    )
    w(f"- Linked Bash results: {stats['bash_results']} (background skipped: {stats['background']})")
    w(
        f"- Rewritable under the shipped rule: {bound['n']}; only recognised inside a "
        f"compound command: {relaxed['n']}; not recognised at all: {stats['not_recognised']}"
    )
    w("")
    w("## Bound")
    w("")
    w("Method: each Bash tool result whose command the PreToolUse rule would rewrite is")
    w(f"split into its last {TAIL_LINES} lines (kept) and the earlier head (dropped). The")
    w("result text is what the model actually saw, so a result already cut by the harness")
    w("is measured as shown. Token counts are characters / 4.")
    w("")

    def bound_block(title: str, s: dict[str, Any]) -> None:
        w(f"### {title}")
        w("")
        w(
            f"- Full output {_num(s['full_tokens'])} tokens; kept "
            f"{_num(s['kept_tokens'])}; **saved {_num(s['saved_tokens'])} "
            f"({_pct(s['saved_share'])})**"
        )
        w(
            f"- Results with a dropped head: {s['n_dropped']} of {s['n']}; of those, "
            f"**{s['n_dropped_error']} ({_pct(s['dropped_error_share'])}) dropped an "
            f"`error|Error|FAILED|Traceback` line**"
        )
        w(
            f"- Results whose full output matched the error pattern: {s['n_full_error']}; "
            f"after the rewrite the kept tail no longer matched: {s['n_error_lost']}"
        )
        w("")
        w("| command key | results | with a drop | saved tokens | dropped an error line |")
        w("|---|---|---|---|---|")
        for key, k in s["by_key"].items():
            w(
                f"| {key} | {k['n']} | {k['dropped']} | "
                f"{_num(k['full'] - k['kept'])} | {k['dropped_error']} |"
            )
        w("")

    bound_block("Shipped rule (a bare allowlisted command)", bound)
    bound_block("Wider variant (test runner inside a compound command; not shipped)", relaxed)
    w("## Coach")
    w("")
    w("Method: one Stop per assistant message whose `stop_reason` is not `tool_use`.")
    w(
        f"Context is input + cache creation + cache read. The shipped rule (context >= "
        f"{CONTEXT_THRESHOLD:,}, turn >= {TURN_THRESHOLD}, cooldown {COOLDOWN_TURNS} turns, "
        f"growth >= {GROWTH_TOKENS:,} tokens) is replayed unchanged."
    )
    w("")
    w(
        f"- Top 10% of priced sessions: {coach['top_k']} of {coach['n_priced']} "
        f"(cost at the cutoff {_usd(coach['cutoff_usd'])})"
    )
    w(f"- Sessions where the nudge would have fired: {coach['n_fired']}")
    w(
        f"- **Precision {_pct(coach['precision'])}, recall {_pct(coach['recall'])}** "
        f"against the top 10% (hits: {coach['n_fired_top']})"
    )
    w(
        f"- Top sessions that reach the nudge's own thresholds (recall ceiling): "
        f"{coach['eligible_top']} of {coach['top_k']} ({_pct(coach['recall_ceiling'])})"
    )
    w(
        f"- First nudge turn among fired sessions: median "
        f"{_num(coach['median_first_nudge'])}, p25 / median / p75 "
        f"{_q(coach['n_first_nudge_q'])}; sessions with more than one nudge: "
        f"{coach['n_multi_nudge']}"
    )
    w("")
    w("## Caveats")
    w("")
    w(
        "- One person's sessions on one machine. Transcripts are not the hook payload: a "
        "Stop is inferred from `stop_reason`, and an interrupted or resumed session can "
        "end a turn without one."
    )
    w(
        "- Bound text is the tool_result content, so harness truncation has already "
        "happened; the true saving on an untruncated stream is larger. The replay also "
        "charges no cache-read saving on later turns, which makes it a lower bound."
    )
    w(
        "- The shipped bound rule rewrites only a bare allowlisted command. The wider "
        "variant additionally recognises a test runner inside a compound command; it is "
        "a hypothetical extension, not shipped behaviour."
    )
    w(
        "- The error pattern is the WP's literal regex. A head line that matches is not "
        "proof the model needed it, and a lost line is not proof the task failed."
    )
    w(
        "- Coach cost includes every priced turn in the session. The top 10% is a rank "
        "cut, so ties at the cutoff are not split."
    )
    w("")
    return "\n".join(out)


def _usd(value: float | None) -> str:
    return "n/a" if value is None else f"${value:,.2f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bound and coach replay report")
    parser.add_argument("--root", type=Path, default=Path.home() / ".claude" / "projects")
    parser.add_argument("--out", type=Path, default=_REPO / "docs" / "bound-coach-replay.md")
    args = parser.parse_args(argv)
    prices = _load_prices(_DEFAULT_PRICES)
    text = render(collect(args.root, prices))
    sys.stdout.write(text + "\n")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
