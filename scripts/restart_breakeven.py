"""Restart break-even simulation over real Claude Code transcripts (P4 M3, D-2).

Read-only over ``<root>/*/<session>.jsonl``. Aggregates only: counts, shares, medians,
quartiles. No path, prompt, command or transcript text is printed or stored.

Model. A boundary restart re-pays that session's *measured* starting prefix (its first
turn's prompt: input + cache creation + cache read) as a cache write, plus a carry
packet (``--packet-kb``, 4 bytes/token). Continuing pays the growing context on every
remaining turn, mostly at cache-read prices. For each boundary candidate turn ``t`` the
script compares cost-to-finish if continuing with cost-to-finish if restarted at ``t``:
the restarted session holds prefix + packet and re-grows by the same per-turn increments
the log recorded. Headroom is continuing minus restarted, so positive headroom means
the restart is strictly cheaper at that boundary.

Boundary candidates are deterministic only: a test-runner result that passed after an
earlier test-runner result in the same session failed, a ``git commit``, or an explicit
user task switch (a fixed phrase list). Nothing is inferred from model output.

The bar is D-2 (``docs/plans/2026-11-01-wave-decisions.md``): at least 25% of 40+ turn
sessions have a break-even turn *with positive headroom*. A bare crossing (headroom >=
0, which includes a session that only ties at its last turn) is printed as context and
is never the bar figure. Dollars are API list-price equivalents from
``cfxd/data/prices.json``, not a bill; ``--bars-commit`` is required and echoed in the
header so a table never travels without the bars it was run under.
"""

from __future__ import annotations

import argparse
import re
import statistics
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from cfxd.bound import test_key_any  # noqa: E402
from cfxd.brief_receipts import _blocks, _iter_jsonl  # noqa: E402
from cfxd.routing import (  # noqa: E402
    _DEFAULT_PRICES,
    _load_prices,
    _match_family,
    _normalize,
    _price_for,
    _prompt,
    _tokens,
    _turn_dollars,
)
from cfxd.usage_dedupe import MessageUsage  # noqa: E402

LONG_SESSION_TURNS = 40
D2_BAR_SHARE = 0.25
PACKET_KB_DEFAULT = 3.0
BYTES_PER_TOKEN = 4
_MTOK = 1_000_000.0
_SYNTHETIC = "<synthetic>"

# A test run failed if its output carries an error marker; the bound replay's literal
# pattern plus the lowercase forms pytest and jest print.
_FAIL = re.compile(r"error|Error|FAILED|failed|FAILURES?|Traceback")
_PASS = re.compile(r"passed|PASSED|\bok\b|All tests")
_COMMIT = re.compile(r"\bgit\s+commit\b")
# shortcut: a fixed phrase list is a proxy for an explicit task switch, not a semantic
# read of the prompt; upgrade when transcripts carry a first-class switch signal.
_TASK_SWITCH = re.compile(
    r"\b(?:next task|new task|different task|switch(?:ing)? to|move on to|unrelated)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Turn:
    """One assistant message's final usage."""

    tokens: dict[str, int]
    prompt: int
    family: str | None


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b["text"]
            for b in content
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        )
    return ""


def scan_turns(
    objs: list[dict[str, Any]], families: dict[str, Any]
) -> tuple[list[Turn], dict[str, int]]:
    """One turn per main-chain assistant message id, plus its id -> turn index."""
    dedupe = MessageUsage(objs)
    turns: list[Turn] = []
    index: dict[str, int] = {}
    for obj in objs:
        if obj.get("isSidechain") or obj.get("type") != "assistant":
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        mid = msg.get("id")
        if not isinstance(mid, str) or not mid or mid in index:
            continue
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue
        tokens = _tokens(dedupe.final_usage(msg, usage))
        model = msg.get("model")
        family = None
        if isinstance(model, str) and model and model != _SYNTHETIC:
            family = _match_family(_normalize(model), families)
        index[mid] = len(turns)
        turns.append(Turn(tokens, _prompt(tokens), family))
    return turns, index


def boundary_candidates(
    objs: list[dict[str, Any]], index: dict[str, int]
) -> tuple[list[int], Counter[str]]:
    """Turn indices a restart could land on, and how many of each deterministic kind.

    ``t`` is the number of turns already spent, so a restart at ``t`` replays turns
    ``t..`` in a fresh session.
    """
    seen: set[int] = set()
    kinds: Counter[str] = Counter()
    cmds: dict[str, str] = {}
    failed_earlier = False
    spent = 0
    for obj in objs:
        if obj.get("isSidechain"):
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        mid = msg.get("id")
        if isinstance(mid, str) and mid in index:
            spent = index[mid] + 1
        if obj.get("type") == "user" and _TASK_SWITCH.search(_text(msg.get("content"))):
            seen.add(spent)
            kinds["task_switch"] += 1
        for block in _blocks(obj):
            kind = block.get("type")
            if kind == "tool_use":
                if block.get("name") != "Bash":
                    continue
                inp = block.get("input")
                command = inp.get("command") if isinstance(inp, dict) else None
                if not isinstance(command, str):
                    continue
                uid = block.get("id")
                if isinstance(uid, str):
                    cmds[uid] = command
                if _COMMIT.search(command):
                    seen.add(spent)
                    kinds["commit"] += 1
            elif kind == "tool_result":
                command = cmds.get(block.get("tool_use_id"))
                if command is None or test_key_any(command) is None:
                    continue
                text = _text(block.get("content"))
                if _FAIL.search(text):
                    failed_earlier = True
                elif _PASS.search(text) and failed_earlier:
                    seen.add(spent)
                    kinds["test_pass"] += 1
                    failed_earlier = False
    return sorted(seen), kinds


def simulate(
    turns: list[Turn],
    cands: list[int],
    prefix: int,
    packet: int,
    entry: dict[str, Any],
) -> dict[str, Any]:
    """Cost-to-finish continuing vs restarted at each candidate; headroom = continue - restart."""
    n = len(turns)
    opening = (
        (prefix + packet) * float(_price_for(entry, prefix + packet)["cache_write_5m"]) / _MTOK
    )
    cont = [0.0] * (n + 1)
    for i in range(n - 1, -1, -1):
        cont[i] = cont[i + 1] + _turn_dollars(entry, turns[i].tokens, turns[i].prompt)
    rows: list[tuple[int, float, float]] = []
    for t in cands:
        if not 1 <= t <= n - 1:
            continue
        restart = opening
        for i in range(t, n):
            ctx = prefix + packet + max(0, turns[i].prompt - turns[t - 1].prompt)
            price = _price_for(entry, ctx)
            # Turn t writes prefix + packet (already charged) and reads only the new growth.
            read = max(0, ctx - (prefix + packet)) if i == t else ctx
            restart += (
                read * float(price["cache_read"])
                + turns[i].tokens["output"] * float(price["output"])
            ) / _MTOK
        rows.append((t, cont[t] - restart, cont[t]))
    crossed = next((r for r in rows if r[1] > 0), None)
    return {
        "rows": rows,
        "exists": any(h >= 0 for _t, h, _c in rows),
        "crossed": crossed,
    }


def collect(root: Path, families: dict[str, Any], packet: int) -> dict[str, Any]:
    stats: Counter[str] = Counter()
    kinds: Counter[str] = Counter()
    sessions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in sorted(root.glob("*/*.jsonl")):
        if path.stem in seen:
            stats["duplicate_copies"] += 1
            continue
        seen.add(path.stem)
        objs = list(_iter_jsonl(path))
        if not objs:
            stats["empty"] += 1
            continue
        turns, index = scan_turns(objs, families)
        if not turns:
            stats["no_turns"] += 1
            continue
        stats["sessions"] += 1
        named = [t.family for t in turns if t.family is not None]
        if len(named) != len(turns):
            stats["unpriced_sessions"] += 1
            continue
        entry = families[Counter(named).most_common(1)[0][0]]
        cands, found = boundary_candidates(objs, index)
        kinds.update(found)
        prefix = turns[0].prompt
        result = simulate(turns, cands, prefix, packet, entry)
        sessions.append(
            {
                "turns": len(turns),
                "prefix": prefix,
                "candidates": len(result["rows"]),
                "exists": result["exists"],
                "crossed": result["crossed"],
            }
        )
    return {"stats": stats, "kinds": kinds, "sessions": sessions}


def _quartiles(values: list[float]) -> tuple[float, float, float] | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0], values[0], values[0]
    q = statistics.quantiles(values, n=4, method="inclusive")
    return q[0], statistics.median(values), q[2]


def summarise(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    """D-2 share over 40+ turn sessions, the bare-crossing share, and headroom quartiles."""
    long = [s for s in sessions if s["turns"] >= LONG_SESSION_TURNS]
    cleared = [s["crossed"] for s in long if s["crossed"] is not None]
    headroom = [c[1] for c in cleared]
    relative = [c[1] / c[2] for c in cleared if c[2] > 0]
    return {
        "n_sessions": len(sessions),
        "n_long": len(long),
        "n_no_candidate": sum(1 for s in long if not s["candidates"]),
        "n_cleared": len(cleared),
        "share_cleared": len(cleared) / len(long) if long else None,
        "n_exists": sum(1 for s in long if s["exists"]),
        "n_tie_only": sum(1 for s in long if s["exists"] and s["crossed"] is None),
        "share_exists": sum(1 for s in long if s["exists"]) / len(long) if long else None,
        "headroom_q": _quartiles(headroom),
        "relative_q": _quartiles(relative),
        "turn_q": _quartiles([float(c[0]) for c in cleared]),
    }


def scan_prefix(sessions: list[dict[str, Any]]) -> tuple[float, float, float] | None:
    return _quartiles([float(s["prefix"]) for s in sessions])


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


def _num(value: float | None) -> str:
    return "n/a" if value is None else f"{value:,.0f}"


def _usd(value: float | None) -> str:
    # Three decimals: a per-session headroom of a few cents must not render as $0.00.
    return "n/a" if value is None else f"${value:,.3f}"


def _q(value: tuple[float, float, float] | None, render: Any = _num) -> str:
    return "n/a" if value is None else " / ".join(render(v) for v in value)


def render(data: dict[str, Any], bars_commit: str, packet_kb: float) -> str:
    s = summarise(data["sessions"])
    stats = data["stats"]
    kinds = data["kinds"]
    packet = int(round(packet_kb * 1024 / BYTES_PER_TOKEN))
    out: list[str] = []
    w = out.append
    w("# Restart break-even over real transcripts (P4 M3)")
    w("")
    w(f"Bars commit: `{bars_commit}`; packet: {packet} tokens ({packet_kb:g} KB target)")
    w("")
    w("Generated by `scripts/restart_breakeven.py`. Aggregates only: no paths, prompts,")
    w("commands or transcript text. Prefix is *measured* from these logs; every share,")
    w("turn and dollar below is *modelled* and is an API list-price equivalent, not a bill.")
    w("")
    w("## Sample")
    w("")
    w(
        f"- Sessions scanned: {stats['sessions']}; duplicate copies skipped: "
        f"{stats['duplicate_copies']}; empty skipped: {stats['empty']}; "
        f"no assistant turn: {stats['no_turns']}; unpriced model: "
        f"{stats['unpriced_sessions']}"
    )
    w(
        f"- 40+ turn sessions (the D-2 population): {s['n_long']}; of those with no "
        f"boundary candidate: {s['n_no_candidate']}"
    )
    w(
        f"- Starting prefix, measured per session as its first turn's prompt "
        f"(p25 / median / p75): {_q(scan_prefix(data['sessions']))} tokens"
    )
    w("")
    w("## The bar (D-2)")
    w("")
    w(
        f"Bar: at least {_pct(D2_BAR_SHARE)} of 40+ turn sessions have a break-even turn "
        f"with **positive headroom** (restarted strictly cheaper, net of prefix re-pay "
        f"and packet)."
    )
    w("")
    cleared = s["share_cleared"]
    verdict = (
        "n/a" if cleared is None else ("cleared" if cleared >= D2_BAR_SHARE else "not cleared")
    )
    w(
        f"- Modelled: {s['n_cleared']} of {s['n_long']} sessions = **{_pct(cleared)}** "
        f"-> bar {verdict}"
    )
    w(
        f"- Context only, never the bar figure: break-even exists at all (headroom >= 0, "
        f"so a session that only ties at its last turn is included) in {s['n_exists']} of "
        f"{s['n_long']} = {_pct(s['share_exists'])}; tie-only sessions: {s['n_tie_only']}"
    )
    w("")
    w("## Headroom after the break-even turn")
    w("")
    w(f"- Dollars per session (p25 / median / p75): {_q(s['headroom_q'], _usd)}")
    w(
        f"- As a share of cost-to-finish continuing (p25 / median / p75): "
        f"{_q(s['relative_q'], lambda v: _pct(v))}"
    )
    w(f"- Break-even turn (p25 / median / p75): {_q(s['turn_q'])}")
    w(
        f"- Boundary candidates found: test pass after a failure {kinds['test_pass']}, "
        f"commit {kinds['commit']}, task switch {kinds['task_switch']} "
        f"(a session may have several)"
    )
    w("")
    w("## Method and caveats")
    w("")
    w(
        "- Restart at turn t pays the session's own first-turn prompt as a cache write "
        "plus the packet, then holds prefix + packet and re-grows by the recorded "
        "per-turn increments. Continuing pays the logged context on every remaining turn."
    )
    w(
        "- The packet is assumed complete. A packet that misses part of the working set "
        "raises the restarted side and lowers every share here; if break-even needs that "
        "assumption to hold, it does not count (P4 M3)."
    )
    w(
        "- A pass after a failure in the same session is the only test boundary; a run "
        "that never failed first is not one. Task switches come from a fixed phrase list, "
        "so they are a proxy, not a semantic read."
    )
    w(
        "- One person's sessions on one machine. Dollar figures use one price row per "
        "session (its most common model family); an unpriced family drops the session."
    )
    w("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restart break-even simulator (P4 M3)")
    parser.add_argument("--root", type=Path, default=Path.home() / ".claude" / "projects")
    parser.add_argument(
        "--bars-commit",
        required=True,
        metavar="HASH",
        help="decisions-file commit the frozen bars come from (refuses to run without it)",
    )
    parser.add_argument("--packet-kb", type=float, default=PACKET_KB_DEFAULT)
    args = parser.parse_args(argv)
    families = _load_prices(_DEFAULT_PRICES)["families"]
    data = collect(args.root, families, int(round(args.packet_kb * 1024 / BYTES_PER_TOKEN)))
    sys.stdout.write(render(data, args.bars_commit, args.packet_kb) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
