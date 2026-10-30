"""Gate 1: how much sub-agent (child) cost is re-discovery of what the parent already knew.

Free, offline, read-only over Claude Code transcripts (``<root>/*/<session>.jsonl`` with
children under ``<session>/subagents/agent-*.jsonl``). Reports aggregates only: counts,
shares, medians, quartiles. No path, prompt, command or transcript text is printed or stored.

Method (per child, joined parent ``Agent`` tool_use -> ``toolUseResult.agentId`` -> child file):

* Cost: ``cfxd.routing`` list prices on usage counted once per ``message.id``
  (``cfxd.usage_dedupe``). Child message ids that also occur in the parent transcript (a fork
  replaying history) are excluded so replayed context is not charged to the child.
* Parent knowledge at spawn: files the parent Read/Edited/Wrote on lines before the spawn
  line (normalised paths); Bash commands it ran before the spawn (whitespace-normalised).
* Redundant calls: child ``Read``/``Grep`` of a path the parent knew (re-read), or a child
  ``Bash`` command identical to one the parent ran (re-run).
* Attribution of cost to redundant calls (the ambiguity is real, so two numbers):
  - lower: for each turn that issued redundant calls, the turn's cost times
    (redundant calls / tool calls in that turn). Only the turn that asked, nothing carried.
  - upper: the full cost of every turn that issued a redundant call, plus the result tokens
    (estimated as result chars / 4) priced as one cache write and then one cache read on
    every later turn of the child. It still ignores that some of those results might have
    been needed anyway, so it is a ceiling, not an estimate.
* Exploration: cost of the turns before the first Edit/Write/MultiEdit/NotebookEdit turn
  (the whole child when it never edits).
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from cfxd.brief_receipts import (  # noqa: E402
    _OVERFLOW_RE,
    _agent_id_from_text,
    _blocks,
    _iter_jsonl,
)
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

_AGENT_TOOLS = frozenset({"Agent", "Task"})
_KNOW_TOOLS = frozenset({"Read", "NotebookRead", "Edit", "Write", "MultiEdit", "NotebookEdit"})
_EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
_MTOK = 1_000_000.0
_WS = re.compile(r"\s+")
_GITBASH = re.compile(r"^/([a-zA-Z])/")


def norm_path(p: str) -> str:
    s = p.replace("\\", "/").strip()
    s = _GITBASH.sub(lambda m: m.group(1) + ":/", s)
    return s.rstrip("/").lower()


def norm_cmd(c: str) -> str:
    return _WS.sub(" ", c).strip()


def _call_path(name: str, inp: Any) -> str | None:
    if not isinstance(inp, dict):
        return None
    if name == "Grep":
        p = inp.get("path")
    else:
        p = inp.get("file_path") or inp.get("notebook_path")
    return norm_path(p) if isinstance(p, str) and p else None


def _result_chars(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(c.get("text", "")) for c in content if isinstance(c, dict))
    return 0


# ------------------------------------------------------------------ parent


def scan_parent(path: Path) -> dict[str, Any]:
    """Per-line knowledge timeline, Agent calls, agent-id links and message ids."""
    known: list[tuple[int, str]] = []
    cmds: list[tuple[int, str]] = []
    calls: dict[str, dict[str, Any]] = {}
    links: dict[str, tuple[str | None, str | None]] = {}  # tool_use_id -> (agentId, status)
    msg_ids: set[str] = set()
    for idx, obj in enumerate(_iter_jsonl(path)):
        if obj.get("isSidechain"):
            continue
        msg = obj.get("message")
        if obj.get("type") == "assistant" and isinstance(msg, dict):
            mid = msg.get("id")
            if isinstance(mid, str):
                msg_ids.add(mid)
        tur = obj.get("toolUseResult")
        for b in _blocks(obj):
            if b.get("type") == "tool_use":
                name = b.get("name")
                inp = b.get("input")
                if name in _KNOW_TOOLS:
                    p = _call_path(name, inp)
                    if p:
                        known.append((idx, p))
                elif (
                    name == "Bash" and isinstance(inp, dict) and isinstance(inp.get("command"), str)
                ):
                    cmds.append((idx, norm_cmd(inp["command"])))
                elif name in _AGENT_TOOLS and isinstance(b.get("id"), str):
                    calls[b["id"]] = {
                        "idx": idx,
                        "bg_input": bool(isinstance(inp, dict) and inp.get("run_in_background")),
                    }
            elif b.get("type") == "tool_result" and isinstance(b.get("tool_use_id"), str):
                aid = tur.get("agentId") if isinstance(tur, dict) else None
                if not isinstance(aid, str):
                    aid = _agent_id_from_text(b.get("content"))
                status = tur.get("status") if isinstance(tur, dict) else None
                links[b["tool_use_id"]] = (aid, status if isinstance(status, str) else None)
    return {"known": known, "cmds": cmds, "calls": calls, "links": links, "msg_ids": msg_ids}


# ------------------------------------------------------------------ child


def scan_child(path: Path, parent_msg_ids: set[str], prices: dict[str, Any]) -> dict[str, Any]:
    objs = list(_iter_jsonl(path))
    dedupe = MessageUsage(objs)
    turns: dict[str, dict[str, Any]] = {}
    order: list[dict[str, Any]] = []
    result_chars: dict[str, int] = {}
    compactions = overflows = 0
    model: str | None = None
    replayed: set[str] = set()
    for n, o in enumerate(objs):
        if o.get("isCompactSummary") or o.get("subtype") == "compact_boundary":
            compactions += 1
        if o.get("type") == "user":
            for b in _blocks(o):
                if b.get("type") == "tool_result" and isinstance(b.get("tool_use_id"), str):
                    result_chars[b["tool_use_id"]] = _result_chars(b.get("content"))
        msg = o.get("message")
        if o.get("type") != "assistant" or not isinstance(msg, dict):
            continue
        if msg.get("stop_reason") == "model_context_window_exceeded" or (
            o.get("isApiErrorMessage") and _OVERFLOW_RE.search(json.dumps(msg)[:2000])
        ):
            overflows += 1
        mid = msg.get("id")
        if isinstance(mid, str) and mid in parent_msg_ids:
            replayed.add(mid)
            continue
        key = mid if isinstance(mid, str) and mid else f"line{n}"
        t = turns.get(key)
        if t is None:
            t = turns[key] = {"calls": [], "usage": None, "model": None}
            order.append(t)
        for b in _blocks(o):
            if b.get("type") == "tool_use":
                t["calls"].append((b.get("id"), b.get("name"), b.get("input")))
        m = msg.get("model")
        if isinstance(m, str) and m and m != "<synthetic>":
            t["model"] = m
            model = model or m
        usage = msg.get("usage")
        if isinstance(usage, dict) and dedupe.count_here(msg):
            t["usage"] = dedupe.final_usage(msg, usage)
    fams = prices["families"]
    for t in order:
        t["cost"] = t["carry_price"] = t["tok"] = None
        if t["usage"] is None:
            continue
        tok = _tokens(t["usage"])
        t["tok"] = sum(tok.values())
        fam = _match_family(_normalize(t["model"] or model or ""), fams)
        if fam is not None:
            prompt = _prompt(tok)
            t["cost"] = _turn_dollars(fams[fam], tok, prompt)
            t["carry_price"] = _price_for(fams[fam], prompt)
    return {
        "turns": order,
        "result_chars": result_chars,
        "compactions": compactions,
        "overflows": overflows,
        "model": model,
        "replayed": len(replayed),
    }


def _family_label(model: str | None) -> str:
    if not model:
        return "unknown"
    n = _normalize(model)
    for fam in ("haiku", "sonnet", "opus", "fable"):
        if n.startswith(fam):
            return fam
    return "other"


def analyse_child(ch: dict[str, Any], parent: dict[str, Any], spawn_idx: int) -> dict[str, Any]:
    known = {p for i, p in parent["known"] if i < spawn_idx}
    pcmds = {c for i, c in parent["cmds"] if i < spawn_idx}
    turns = ch["turns"]
    priced = all(t["cost"] is not None for t in turns if t["usage"] is not None)
    cost = sum(t["cost"] or 0.0 for t in turns) if priced else None
    n_calls = n_reread = n_rerun = 0
    reread_files: set[str] = set()
    child_files: set[str] = set()
    lower = upper = 0.0
    first_edit: int | None = None
    explore_files: set[str] = set()
    for ti, t in enumerate(turns):
        red = 0
        carry = 0.0
        for cid, name, inp in t["calls"]:
            n_calls += 1
            p = _call_path(name, inp) if name in ("Read", "NotebookRead", "Grep") else None
            if name in _EDIT_TOOLS and first_edit is None:
                first_edit = ti
            if p:
                child_files.add(p)
                if first_edit is None:
                    explore_files.add(p)
            hit = False
            if p and p in known:
                n_reread += 1
                reread_files.add(p)
                hit = True
            elif (
                name == "Bash"
                and isinstance(inp, dict)
                and isinstance(inp.get("command"), str)
                and norm_cmd(inp["command"]) in pcmds
            ):
                n_rerun += 1
                hit = True
            if hit:
                red += 1
                price = t["carry_price"]
                if price is not None:
                    est = ch["result_chars"].get(cid, 0) / 4.0
                    later = len(turns) - 1 - ti
                    carry += (
                        est
                        * (float(price["cache_write_5m"]) + float(price["cache_read"]) * later)
                        / _MTOK
                    )
        if red and t["cost"] is not None:
            lower += t["cost"] * red / max(1, len(t["calls"]))
            upper += t["cost"] + carry
    explore_end = first_edit if first_edit is not None else len(turns)
    explore_cost = sum(t["cost"] or 0.0 for t in turns[:explore_end]) if cost is not None else None
    return {
        "model": ch["model"],
        "family": _family_label(ch["model"]),
        "cost": cost,
        "tokens": sum(t["tok"] or 0 for t in turns),
        "turns": sum(1 for t in turns if t["usage"] is not None),
        "tool_calls": n_calls,
        "rereads": n_reread,
        "reread_files": len(reread_files),
        "child_files": len(child_files),
        "reruns": n_rerun,
        "lower": lower if cost is not None else None,
        "upper": min(upper, cost) if cost is not None else None,
        "compactions": ch["compactions"],
        "overflows": ch["overflows"],
        "replayed": ch["replayed"],
        "has_edit": first_edit is not None,
        "explore_cost": explore_cost,
        "explore_files": len(explore_files),
        "explore_files_known": len(explore_files & known),
        "parent_known": len(known),
    }


# ------------------------------------------------------------------ collection


def collect(root: Path, prices: dict[str, Any]) -> dict[str, Any]:
    stats: Counter[str] = Counter()
    sessions: dict[str, dict[str, Any]] = {}
    children: list[dict[str, Any]] = []
    for pf in sorted(root.glob("*/*.jsonl")):
        sid = pf.stem
        sub = pf.with_suffix("") / "subagents"
        if not sub.is_dir():
            continue
        if sid in sessions:
            stats["duplicate_session_copies_skipped"] += 1
            continue
        parent = scan_parent(pf)
        files = {f.stem[len("agent-") :]: f for f in sub.glob("agent-*.jsonl")}
        first_spawn = min((c["idx"] for c in parent["calls"].values()), default=None)
        sessions[sid] = {
            "read_before_first": len(
                {p for i, p in parent["known"] if first_spawn is not None and i < first_spawn}
            )
        }
        linked: set[str] = set()
        for tid, call in parent["calls"].items():
            stats["agent_calls"] += 1
            aid, status = parent["links"].get(tid, (None, None))
            if aid is None:
                stats["unjoinable_no_agentid_in_result"] += 1
                continue
            if aid in linked:
                continue
            linked.add(aid)
            f = files.get(aid)
            if f is None:
                stats["unjoinable_no_child_transcript"] += 1
                continue
            ch = scan_child(f, parent["msg_ids"], prices)
            if not ch["turns"]:
                stats["unjoinable_empty_child"] += 1
                continue
            rec = analyse_child(ch, parent, call["idx"])
            rec["background"] = bool(call["bg_input"] or status == "async_launched")
            rec["session"] = sid
            children.append(rec)
        stats["child_files_not_referenced_by_parent"] += len(set(files) - linked)
    return {"stats": stats, "sessions": sessions, "children": children}


# ------------------------------------------------------------------ aggregation


def _q(vals: list[float]) -> tuple[float, float, float] | None:
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0], vals[0], vals[0]
    q = statistics.quantiles(vals, n=4, method="inclusive")
    return q[0], statistics.median(vals), q[2]


def _share(num: float, den: float) -> float | None:
    return num / den if den > 0 else None


def _shares(ks: list[dict[str, Any]], key: str) -> list[float]:
    out = [_share(k[key], k["cost"]) for k in ks]
    return [v for v in out if v is not None]


def summarise(kids: list[dict[str, Any]]) -> dict[str, Any]:
    p = [k for k in kids if k["cost"] is not None]
    cost = sum(k["cost"] for k in p)
    calls = sum(k["tool_calls"] for k in kids)
    ex_edit = [k for k in p if k["has_edit"]]
    return {
        "n": len(kids),
        "n_priced": len(p),
        "cost_total": cost,
        "tokens_total": sum(k["tokens"] for k in kids),
        "redundant_call_share": _share(sum(k["rereads"] + k["reruns"] for k in kids), calls),
        "reread_call_share": _share(sum(k["rereads"] for k in kids), calls),
        "rerun_call_share": _share(sum(k["reruns"] for k in kids), calls),
        "lower_share": _share(sum(k["lower"] for k in p), cost),
        "upper_share": _share(sum(k["upper"] for k in p), cost),
        "lower_child_q": _q(_shares(p, "lower")),
        "upper_child_q": _q(_shares(p, "upper")),
        "explore_share": _share(sum(k["explore_cost"] for k in p), cost),
        "explore_share_edit": _share(
            sum(k["explore_cost"] for k in ex_edit), sum(k["cost"] for k in ex_edit)
        ),
        "explore_child_q": _q(_shares(p, "explore_cost")),
        "n_edit": sum(1 for k in kids if k["has_edit"]),
        "explore_files": sum(k["explore_files"] for k in kids),
        "explore_files_known": sum(k["explore_files_known"] for k in kids),
        "n_parent_nothing": sum(1 for k in kids if k["parent_known"] == 0),
        "n_no_overlap": sum(1 for k in kids if k["reread_files"] == 0),
        "n_no_file_reads": sum(1 for k in kids if k["child_files"] == 0),
        "n_any_reread": sum(1 for k in kids if k["rereads"] > 0),
        "n_any_rerun": sum(1 for k in kids if k["reruns"] > 0),
        "compactions": sum(k["compactions"] for k in kids),
        "overflows": sum(k["overflows"] for k in kids),
        "n_compacted": sum(1 for k in kids if k["compactions"] or k["overflows"]),
        "n_background": sum(1 for k in kids if k["background"]),
        "n_replayed": sum(1 for k in kids if k["replayed"]),
    }


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{100 * v:.1f}%"


def _usd(v: float | None) -> str:
    return "n/a" if v is None else f"${v:,.2f}"


def _qs(q: tuple[float, float, float] | None, f: Any) -> str:
    return "n/a" if q is None else f"{f(q[0])} / {f(q[1])} / {f(q[2])}"


def render(data: dict[str, Any]) -> str:
    kids = data["children"]
    st = data["stats"]
    ses = data["sessions"]
    used = {k["session"] for k in kids}
    reads0 = [float(ses[s]["read_before_first"]) for s in used]
    a = summarise(kids)
    out: list[str] = []
    w = out.append
    w("# Gate 1: sub-agent re-discovery over real transcripts")
    w("")
    w("Generated by `scripts/gate1_rediscovery.py`. Aggregates only; no paths, prompts, commands")
    w("or transcript text. Dollars are API list-price equivalents, not a bill.")
    w("")
    w("## Sample")
    w("")
    w(f"- Parent sessions with sub-agent transcripts: {len(ses)}; with a joined child: {len(used)}")
    w(f"- Agent calls seen: {st['agent_calls']}; joined children analysed: {len(kids)}")
    w(
        f"- Unjoinable: {st['unjoinable_no_agentid_in_result']} with no agentId in the tool "
        f"result (error, interrupted or no result), {st['unjoinable_no_child_transcript']} whose "
        f"child transcript is missing, {st['unjoinable_empty_child']} empty"
    )
    w(
        f"- Child transcripts no parent call points at (excluded): "
        f"{st['child_files_not_referenced_by_parent']}"
    )
    w(f"- Duplicate copies of a session id skipped: {st['duplicate_session_copies_skipped']}")
    w(f"- Children with an unpriced model (excluded from dollars): {a['n'] - a['n_priced']}")
    w(f"- Children replaying parent history (those messages excluded from cost): {a['n_replayed']}")
    w(
        "- Files the parent read/edited before its first spawn, per session "
        f"(p25 / median / p75): {_qs(_q(reads0), lambda v: f'{v:.0f}')}; sessions with zero: "
        f"{sum(1 for x in reads0 if x == 0)}, with 3 or more: {sum(1 for x in reads0 if x >= 3)}"
    )
    w("")
    w("## Method")
    w("")
    w("- Join: parent `Agent` tool_use, then `toolUseResult.agentId`, then the child transcript.")
    w("- Cost: repo list prices on usage counted once per `message.id` (`cfxd/usage_dedupe.py`).")
    w("- Parent knowledge at spawn: paths Read/Edited/Written, Bash commands run, pre-spawn.")
    w(
        "- Redundant call: child Read/Grep on a path the parent knew, or a child Bash command "
        "identical (whitespace-normalised) to one the parent ran."
    )
    w(
        "- Lower estimate: cost of turns that issued redundant calls, pro-rated by the redundant "
        "share of the turn's tool calls. Upper estimate: full cost of those turns plus their "
        "results (chars/4 tokens) carried as one cache write and a cache read on every later "
        "child turn, capped at child cost."
    )
    w("- Exploration: cost of turns before the first Edit/Write (all turns if it never edits).")
    w("")
    w("## Headline (all children)")
    w("")
    w(
        f"- Total child cost {_usd(a['cost_total'])} over {a['n_priced']} priced children, "
        f"{a['tokens_total']:,} tokens"
    )
    w(
        f"- **Re-discovery share of child cost: {_pct(a['lower_share'])} (lower) to "
        f"{_pct(a['upper_share'])} (upper)**, pooled by dollars"
    )
    w(
        f"- Per child (p25 / median / p75): lower {_qs(a['lower_child_q'], _pct)}; "
        f"upper {_qs(a['upper_child_q'], _pct)}"
    )
    w(
        f"- Parent had read/edited nothing before the spawn: {a['n_parent_nothing']} of {a['n']} "
        f"children ({_pct(_share(a['n_parent_nothing'], a['n']))}). No overlap between child "
        f"reads and parent knowledge (nothing re-read): {a['n_no_overlap']} "
        f"({_pct(_share(a['n_no_overlap'], a['n']))}). Children that read no file at all: "
        f"{a['n_no_file_reads']}"
    )
    w(
        f"- Redundant tool calls: {_pct(a['redundant_call_share'])} of child tool calls "
        f"(re-reads {_pct(a['reread_call_share'])}, re-runs {_pct(a['rerun_call_share'])}); "
        f"children with any re-read: {a['n_any_reread']}, any re-run: {a['n_any_rerun']}"
    )
    w(
        f"- Exploration (before first Edit/Write) share of cost: "
        f"{_pct(a['explore_share'])} for all "
        f"children, {_pct(a['explore_share_edit'])} among the {a['n_edit']} that edit; per child "
        f"p25 / median / p75 {_qs(a['explore_child_q'], _pct)}. Of {a['explore_files']} distinct "
        f"files read in that phase, the parent had already read/edited {a['explore_files_known']}"
    )
    w(
        f"- Compactions {a['compactions']}, overflows {a['overflows']} "
        f"(children affected: {a['n_compacted']}); background/async: "
        f"{a['n_background']} of {a['n']}"
    )
    w("")

    def table(title: str, groups: list[tuple[str, list[dict[str, Any]]]]) -> None:
        w(f"## {title}")
        w("")
        w(
            "| group | children | cost | lower | upper | redundant calls | explore share "
            "| parent knew nothing | no overlap | background |"
        )
        w("|---|---|---|---|---|---|---|---|---|---|")
        for name, ks in groups:
            if not ks:
                continue
            s = summarise(ks)
            w(
                f"| {name} | {s['n']} | {_usd(s['cost_total'])} | {_pct(s['lower_share'])} | "
                f"{_pct(s['upper_share'])} | {_pct(s['redundant_call_share'])} | "
                f"{_pct(s['explore_share'])} | {s['n_parent_nothing']} | {s['n_no_overlap']} | "
                f"{s['n_background']} |"
            )
        w("")

    fams = sorted({k["family"] for k in kids})
    table("By child model family", [(f, [k for k in kids if k["family"] == f]) for f in fams])
    table(
        "By parent knowledge at spawn",
        [
            ("parent knew 3 or more files", [k for k in kids if k["parent_known"] >= 3]),
            ("parent knew 1-2 files", [k for k in kids if 1 <= k["parent_known"] < 3]),
            ("parent knew 0 files", [k for k in kids if k["parent_known"] == 0]),
        ],
    )
    table(
        "By launch mode",
        [
            ("background / async", [k for k in kids if k["background"]]),
            ("foreground", [k for k in kids if not k["background"]]),
        ],
    )
    w("## Caveats and bias")
    w("")
    w(
        "- One person's sessions on one machine. Many delegations are research or exploration "
        "tasks where the parent has read nothing, so little can be re-read by construction."
    )
    w(
        "- Parent knowledge is only what its own Read/Edit/Write/Bash calls show. Context from "
        "the user's prompt, compacted history or MCP tools is invisible, so re-discovery is "
        "undercounted."
    )
    w(
        "- Path match is exact after slash/case normalisation. A file under another spelling, and "
        "Grep over a directory, are not matched. Bash re-runs need an exact command match."
    )
    w(
        "- Attribution is the hard part. The lower figure ignores that a redundant result stays in "
        "context; the upper figure charges the whole turn and its carried results and estimates "
        "result tokens from characters. Treat the truth as between them."
    )
    w("- A re-read is not always waste: a child may need the exact bytes in context to edit them.")
    w("- Prices are list prices at the repo's `as_of` date; the cache TTL per turn is not known.")
    w("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Gate 1 sub-agent re-discovery report")
    ap.add_argument("--root", type=Path, default=Path.home() / ".claude" / "projects")
    ap.add_argument("--out", type=Path, default=_REPO / "docs" / "gate1-rediscovery.md")
    args = ap.parse_args(argv)
    prices = _load_prices(_DEFAULT_PRICES)
    text = render(collect(args.root, prices))
    sys.stdout.write(text + "\n")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
