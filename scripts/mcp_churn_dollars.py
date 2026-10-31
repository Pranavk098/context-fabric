"""E-2: MCP churn in dollars, after excluding the expected first announcement.

The R-7 rule (``cfxd/waste.py``) counts ``deferred_tools_delta`` and
``mcp_instructions_delta`` attachment lines. The first announcement of each kind in a
session is needed context; every later one is a re-sent attachment. This script uses the
same discovery, transcript reader and byte counter, recounts the repeats, prices them
under two cache bounds (all cache reads vs all cache writes) at the repo's list prices,
and emits a 20-row anonymous sample plus the headless/interactive split.

Aggregates only: counts, tokens, dollars and an ordinal. No path, prompt, command or
transcript text is printed or written.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
for _p in (_REPO, _REPO / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from cfxd.audit import _SINCE_DELTAS, _attachment_bytes, _discover, _iter_objects  # noqa: E402
from cfxd.routing import (  # noqa: E402
    _DEFAULT_PRICES,
    _load_prices,
    _scan_turns,
)
from cfxd.waste import _CHURN_MAX  # noqa: E402

_MTOK = 1_000_000.0
_ALLOWED = frozenset({"deferred_tools_delta", "mcp_instructions_delta"})
_SAMPLE = 20
_HEADLESS = {"sdk-cli": True, "cli": False, "claude-desktop": False}


def _entrypoint(path: Path) -> str:
    for obj in _iter_objects(path):
        value = obj.get("entrypoint")
        if isinstance(value, str) and value:
            return value
    return "unknown"


def _family(path: Path, families: dict[str, Any]) -> str | None:
    """Dominant priced model family among the session's assistant turns."""
    counts = Counter(t["family"] for t in _scan_turns(path, families) if t["family"])
    return counts.most_common(1)[0][0] if counts else None


def scan_session(path: Path, families: dict[str, Any]) -> dict[str, Any]:
    """One transcript: delta counts, repeat bytes, model family and entrypoint."""
    deferred = instructions = 0
    repeat_bytes = 0
    for obj in _iter_objects(path):
        att = obj.get("attachment")
        kind = att.get("type") if isinstance(att, dict) else None
        if kind not in _ALLOWED:
            continue
        if kind == "deferred_tools_delta":
            deferred += 1
            seen = deferred
        else:
            instructions += 1
            seen = instructions
        if seen > _CHURN_MAX:
            repeat_bytes += _attachment_bytes(obj)
    family = _family(path, families)
    entry = _entrypoint(path)
    return {
        "deferred": deferred,
        "instructions": instructions,
        "firsts": (1 if deferred else 0) + (1 if instructions else 0),
        "repeats": max(0, deferred - _CHURN_MAX) + max(0, instructions - _CHURN_MAX),
        "repeat_bytes": repeat_bytes,
        "family": family,
        "entrypoint": entry,
        "headless": _HEADLESS.get(entry),
    }


def collect(
    root: Path, prices: dict[str, Any], since: str = "30d", now: datetime | None = None
) -> dict[str, Any]:
    """Scan every transcript in the window; attach dollars per session model."""
    if now is None:
        now = datetime.now(UTC)
    delta = _SINCE_DELTAS.get(since)
    cutoff = now - delta if delta is not None else None
    parents, subs = _discover(root, cutoff)
    paths = sorted(parents + [p for p, _ in subs])
    families: dict[str, Any] = prices["families"]

    rows: list[dict[str, Any]] = []
    for ordinal, path in enumerate(paths, start=1):
        s = scan_session(path, families)
        if not s["deferred"] and not s["instructions"]:
            continue
        s["ordinal"] = ordinal
        s["flagged_old"] = s["deferred"] > _CHURN_MAX or s["instructions"] > _CHURN_MAX
        s["flagged_new"] = s["repeats"] > 0
        s["tokens"] = s["repeat_bytes"] / 4.0
        family = s["family"]
        if family is None or not s["tokens"]:
            s["write_usd"] = s["read_usd"] = None
        else:
            entry = families[family]
            s["write_usd"] = s["tokens"] * float(entry["cache_write_5m"]) / _MTOK
            s["read_usd"] = s["tokens"] * float(entry["cache_read"]) / _MTOK
        rows.append(s)

    return {"rows": rows, "sessions": len(paths)}


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    attachments = sum(r["deferred"] + r["instructions"] for r in rows)
    firsts = sum(r["firsts"] for r in rows)
    repeats = sum(r["repeats"] for r in rows)
    tokens = sum(r["tokens"] for r in rows)
    priced = [r for r in rows if r["repeats"] and r["write_usd"] is not None]
    unpriced = [r for r in rows if r["repeats"] and r["write_usd"] is None]
    flagged = [r for r in rows if r["flagged_new"]]
    headless = sum(1 for r in flagged if r["headless"] is True)
    interactive = sum(1 for r in flagged if r["entrypoint"] == "cli")
    return {
        "with_any": len(rows),
        "attachments": attachments,
        "firsts": firsts,
        "repeats": repeats,
        "tokens": tokens,
        "flagged_old": sum(1 for r in rows if r["flagged_old"]),
        "flagged_new": len(flagged),
        "any_attachment_rule": sum(1 for r in rows if r["deferred"] + r["instructions"] > 0),
        "more_than_one_total_rule": sum(
            1 for r in rows if r["deferred"] + r["instructions"] > _CHURN_MAX
        ),
        "repeats_one_total": sum(max(0, r["deferred"] + r["instructions"] - 1) for r in rows),
        "priced": len(priced),
        "unpriced": len(unpriced),
        "write_usd": sum(r["write_usd"] or 0.0 for r in priced),
        "read_usd": sum(r["read_usd"] or 0.0 for r in priced),
        "headless": headless,
        "interactive": interactive,
        "desktop": sum(1 for r in flagged if r["entrypoint"] == "claude-desktop"),
        "unknown_type": sum(1 for r in flagged if r["headless"] is None),
    }


def _pct(part: int, total: int) -> str:
    return f"{100.0 * part / total:.1f}%" if total else "0.0%"


def sample(rows: list[dict[str, Any]], n: int = _SAMPLE) -> list[dict[str, Any]]:
    """The n largest flagged sessions by repeat tokens (ordinal breaks ties)."""
    flagged = [r for r in rows if r["flagged_new"]]
    return sorted(flagged, key=lambda r: (-r["tokens"], r["ordinal"]))[:n]


def render(data: dict[str, Any]) -> str:
    rows = data["rows"]
    s = summarise(rows)
    top = sample(rows)
    out: list[str] = []
    out.append("# E-2: MCP churn in dollars, after the expected first announcement")
    out.append("")
    out.append("Generated by `scripts/mcp_churn_dollars.py`. Aggregates only: counts, tokens,")
    out.append("dollars and session ordinals; no paths, prompts, commands or transcript text.")
    out.append("Dollars are API list-price equivalents, not a bill.")
    out.append("")
    out.append("## What was counted")
    out.append("")
    out.append(
        f"- Sessions scanned in the window: {data['sessions']:,}; sessions with at least one "
        f"`deferred_tools_delta` or `mcp_instructions_delta` attachment: {s['with_any']:,}."
    )
    out.append(
        f"- Attachment lines: {s['attachments']:,}. Of these, {s['firsts']:,} are the expected "
        f"first announcement of a kind in their session; {s['repeats']:,} are repeats."
    )
    launch = (
        f"- The R-7 headline count of {s['attachments']:,} attachments therefore overstates "
        f"churn {s['attachments'] / s['repeats']:.1f}x by line count."
        if s["repeats"]
        else "- No repeat attachments were found in this window."
    )
    out.append(launch)
    out.append(
        f"- Repeat attachments carry ~{s['tokens']:,.0f} tokens (bytes / 4, the same estimate "
        f"`cfxd waste` prints)."
    )
    out.append("")
    out.append("## Sessions flagged: old vs new rule")
    out.append("")
    out.append("| rule | test per session | sessions |")
    out.append("|---|---|---|")
    out.append(
        f"| old (shipped `cfxd/waste.py`) | more than one of the same kind | {s['flagged_old']:,} |"
    )
    out.append(
        f"| new (first announcement excluded) | at least one repeat remains "
        f"| {s['flagged_new']:,} |"
    )
    out.append(
        f"| naive: any attachment | at least one attachment | {s['any_attachment_rule']:,} |"
    )
    out.append(
        f"| naive: more than one attachment in total | `deferred + instructions > 1` "
        f"| {s['more_than_one_total_rule']:,} |"
    )
    out.append("")
    out.append(
        "The shipped rule and the new rule flag the same sessions: it already requires a "
        "repeat, so excluding the first announcement does not change the session set. The "
        "inflation is in the attachment, token and dollar figures, not in which sessions "
        "are called churny. The two naive rows show the false-positive band a reader reaches "
        "if every announcement (or any two, including one of each kind) is treated as churn."
    )
    out.append("")
    out.append("## Dollars under two cache bounds")
    out.append("")
    out.append("| bound | price basis | dollars |")
    out.append("|---|---|---|")
    out.append(f"| lower | every repeat token as a cache **read** | ${s['read_usd']:,.4f} |")
    out.append(
        f"| upper | every repeat token as a cache **write** (5-minute) | ${s['write_usd']:,.4f} |"
    )
    out.append("")
    out.append(
        f"Priced sessions: {s['priced']:,}; sessions with repeats but no known model: "
        f"{s['unpriced']:,}. Each session's repeats are priced at its dominant model family "
        f"from the repo's `cfxd/data/prices.json`. The truth sits between the bounds: the "
        f"same bytes are written once and then read on later turns, and neither bound "
        f"charges the write once and the reads separately."
    )
    out.append("")
    out.append("## Who is flagged")
    out.append("")
    out.append(
        f"- Headless (`-p` / SDK, `entrypoint: sdk-cli`): {s['headless']:,} of "
        f"{s['flagged_new']:,} flagged sessions ({_pct(s['headless'], s['flagged_new'])})."
    )
    out.append(
        f"- Interactive CLI (`entrypoint: cli`): {s['interactive']:,} "
        f"({_pct(s['interactive'], s['flagged_new'])})."
    )
    out.append(
        f"- Desktop app (`entrypoint: claude-desktop`): {s['desktop']:,} "
        f"({_pct(s['desktop'], s['flagged_new'])})."
    )
    if s["unknown_type"]:
        out.append(
            f"- No entrypoint in the transcript: {s['unknown_type']:,} "
            f"({_pct(s['unknown_type'], s['flagged_new'])})."
        )
    out.append("")
    out.append(f"## Sample: {len(top)} flagged sessions, largest repeat token counts")
    out.append("")
    out.append(
        "Anonymous by construction: a session ordinal (position in the sorted transcript "
        "list), the two attachment counts, repeat count and tokens only. No id, path or text."
    )
    out.append("")
    out.append("| # | deferred | instructions | repeats | tokens | model | headless |")
    out.append("|---|---|---|---|---|---|---|")
    for r in top:
        headless = {True: "yes", False: "no"}.get(r["headless"], "?")
        out.append(
            f"| {r['ordinal']} | {r['deferred']} | {r['instructions']} | {r['repeats']} "
            f"| {r['tokens']:,.0f} | {r['family'] or 'unknown'} | {headless} |"
        )
    out.append("")
    out.append("## Method")
    out.append("")
    out.append(
        "- Discovery, transcript streaming and attachment byte counting are imported from "
        "`cfxd/audit.py`; the repeat threshold (`_CHURN_MAX`) is imported from "
        "`cfxd/waste.py`. Neither module is modified."
    )
    out.append(
        "- An attachment's size is the largest non-`type` field JSON-serialised once "
        "(`_attachment_bytes`), so the names list and the rendered text are not double "
        "counted. Tokens = bytes / 4."
    )
    out.append(
        "- The expected first announcement is excluded per kind, matching the R-7 comment "
        "and code: one `deferred_tools_delta` and one `mcp_instructions_delta` per session "
        "are context; every later line is a repeat. Counting one first per session in total "
        f"instead would leave {s['repeats_one_total']:,} repeats in this window."
    )
    out.append(
        "- Model family: the most common priced family among the session's assistant turns "
        "(`cfxd/routing.py`). Headless: the transcript's `entrypoint` field."
    )
    out.append("")
    out.append("## Caveats")
    out.append("")
    out.append(
        "- One person's sessions on one machine; a 30-day window by file mtime. Counts, not "
        "a census."
    )
    out.append(
        "- Attachment bytes measure the re-sent payload, not the marginal bill: prefix "
        "caching may turn most of a repeat into a cheap read, and the turn that receives it "
        "pays for the rest of its prompt too. The two bounds bracket the cache price only."
    )
    out.append(
        "- Tokens are estimated from characters at 4 bytes/token, the same estimate "
        "`cfxd waste` reports; no tokenizer is run."
    )
    out.append(
        "- A repeat is not always avoidable: a session that connects a new server or "
        "reloads instructions mid-run gets a legitimate second announcement. The sample "
        "exists so those can be checked by hand."
    )
    out.append(
        "- A resumed session appends to the same transcript, so several invocation starts "
        "are counted as one session and only the first announcement per kind is excluded. "
        "That can understate churn, not overstate it."
    )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="E-2 MCP churn dollars report")
    ap.add_argument("--root", type=Path, default=Path.home() / ".claude" / "projects")
    ap.add_argument("--since", default="30d")
    ap.add_argument("--out", type=Path, default=_REPO / "docs" / "mcp-churn-dollars.md")
    args = ap.parse_args(argv)
    prices = _load_prices(_DEFAULT_PRICES)
    text = render(collect(args.root, prices, args.since))
    sys.stdout.write(text + "\n")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
