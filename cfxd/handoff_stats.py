"""``cfxd handoff-stats``: Gate 1 re-discovery and the Brief budget sweep, on local logs.

Re-implements the logic of ``scripts/gate1_rediscovery.py`` and
``scripts/brief_replay_score.py`` inside the shipped package (scripts are not shipped), so a
tester can run it against their own ``~/.claude/projects``:

* join: parent ``Agent`` tool_use -> ``toolUseResult.agentId`` -> ``<session>/subagents/
  agent-<agentId>.jsonl``;
* parent knowledge at spawn: distinct files the parent Read/Edited/Wrote on lines before the
  spawn;
* re-discovery: child ``Read``/``NotebookRead``/``Grep`` calls on a path the parent already
  knew, as a share of the child's tool-read calls;
* cost: repo list prices on usage counted once per ``message.id`` (``cfxd.usage_dedupe``),
  with Gate 1's lower/upper attribution, as a share of TOTAL session cost (parent +
  children);
* packet sweep: the shipped Brief v1 selection re-rendered at 2/6/12/20 KB for children whose
  parent knew 3 or more files (E-3's coverage and bytes-weighted precision).

Privacy: the output is counts, percentages and model families only. No path, prompt, command,
session id or agent id is printed or stored, and it ends with the safe-to-paste line.
Dollars are API list-price equivalents, not a bill.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cfxd.bound import test_key_any
from cfxd.brief_receipts import _iter_jsonl
from cfxd.brief_render import HEADERS, render
from cfxd.brief_select import BUDGET_BYTES, resolve_target, select
from cfxd.brief_types import Intent
from cfxd.routing import (
    _DEFAULT_PRICES,
    _load_prices,
    _norm_cmd,
    _norm_path,
    _normalize,
    _scan_child_parent,
    _scan_child_turns,
    _scan_turns,
    _tool_path,
    _turn_dollars,
)
from cfxd.session_state import (
    MAX_COMMANDS,
    MAX_FILES,
    MAX_TODOS,
    FileFact,
    SessionFacts,
    TestFact,
    redact_command,
)

_READ_TOOLS = frozenset({"Read", "NotebookRead", "Grep"})
_FILE_TOOLS = frozenset({"Read", "Edit", "Write", "MultiEdit", "NotebookEdit"})
_AGENT_TOOLS = frozenset({"Agent", "Task"})
_FAMILIES = ("haiku", "sonnet", "opus", "other")
_SWEEP_BUDGETS = (2_000, 6_000, 12_000, 20_000)
_MTOK = 1_000_000.0
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_EMPTY_INTENT = Intent(goal=None, constraints=(), decisions=(), tried=(), deduped=0)
_FILES_HEADER = HEADERS["files"]
_SECTION_HEADERS = frozenset(HEADERS.values())
_EMPTY = "No sub-agent transcripts found; nothing to report."
_SAFE = "safe to paste: counts and percentages only"


def _share(num: float, den: float) -> float | None:
    return num / den if den > 0 else None


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


def _family_label(model: str | None) -> str:
    """Model id -> haiku / sonnet / opus / other (fable and unknown count as other)."""
    name = _normalize(model or "")
    for family in ("haiku", "sonnet", "opus"):
        if name.startswith(family):
            return family
    return "other"


# ------------------------------------------------------------------ facts at spawn


def _facts_before(objs: list[dict[str, Any]], spawn_idx: int, session_id: str) -> SessionFacts:
    """The parent's facts on the lines before the spawn (same shapes as ``checkpoint``)."""
    files: dict[str, FileFact] = {}
    commands: list[str] = []
    todos: tuple[tuple[str, str], ...] = ()
    test: TestFact | None = None
    test_id: str | None = None
    for idx, obj in enumerate(objs[:spawn_idx]):
        msg = obj.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        for block in blocks:
            kind = block.get("type")
            if kind == "tool_use":
                name = block.get("name")
                tool_input = block.get("input")
                if not isinstance(name, str) or not isinstance(tool_input, dict):
                    continue
                bid = block.get("id")
                if name in _FILE_TOOLS:
                    key = "notebook_path" if name == "NotebookEdit" else "file_path"
                    file_path = tool_input.get(key)
                    if not isinstance(file_path, str) or not file_path.strip():
                        continue
                    norm = _norm_path(file_path)
                    existing = files.pop(norm, None)
                    files[norm] = FileFact(
                        path=file_path,
                        lines=existing.lines if existing is not None else None,
                        edited=(existing.edited if existing is not None else False)
                        or name != "Read",
                        last_seen=float(idx),
                    )
                    while len(files) > MAX_FILES:
                        files.pop(next(iter(files)))
                elif name == "Bash":
                    command = tool_input.get("command")
                    if not isinstance(command, str) or not command.strip():
                        continue
                    redacted = redact_command(command)
                    if redacted in commands:
                        commands.remove(redacted)
                    commands.append(redacted)
                    del commands[:-MAX_COMMANDS]
                    test_key = test_key_any(command)
                    if test_key is not None:
                        test = TestFact(key=test_key, ok=None, at=float(idx))
                        test_id = bid if isinstance(bid, str) else None
                elif name == "TodoWrite":
                    raw = tool_input.get("todos")
                    if isinstance(raw, list):
                        todos = tuple(
                            (item["status"], item["content"])
                            for item in raw
                            if isinstance(item, dict)
                            and isinstance(item.get("status"), str)
                            and isinstance(item.get("content"), str)
                        )[:MAX_TODOS]
            elif kind == "tool_result":
                tid = block.get("tool_use_id")
                if tid is not None and tid == test_id and test is not None:
                    error = block.get("is_error") is True
                    test = TestFact(key=test.key, ok=not error, at=test.at)
    return SessionFacts(
        session_id=session_id,
        files=tuple(files.values()),
        folds=(),
        commands=tuple(commands),
        todos=todos,
        test=test,
    )


def _agent_inputs(objs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """``Agent`` tool_use id -> its input (prompt and requested model)."""
    out: dict[str, dict[str, Any]] = {}
    for obj in objs:
        msg = obj.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") in _AGENT_TOOLS
                and isinstance(block.get("id"), str)
                and isinstance(block.get("input"), dict)
            ):
                out[block["id"]] = block["input"]
    return out


# ------------------------------------------------------------------ per child


def _analyse_child(child: dict[str, Any], parent: dict[str, Any], spawn_idx: int) -> dict[str, Any]:
    """Reads and Gate 1's lower/upper re-discovery attribution for one joined child."""
    known = {p for i, p in parent["known"] if i < spawn_idx}
    pcmds = {c for i, c in parent["cmds"] if i < spawn_idx}
    turns = child["turns"]
    priced = all(t["cost"] is not None for t in turns if t["usage"] is not None)
    cost = sum(t["cost"] or 0.0 for t in turns) if priced else None
    reads: list[tuple[str, bool]] = []  # (normalised path, was the parent already there?)
    lower = upper = 0.0
    for ti, turn in enumerate(turns):
        red = 0
        carry = 0.0
        for cid, name, inp in turn["calls"]:
            path = _tool_path(name, inp) if name in _READ_TOOLS else None
            redundant = False
            if path is not None:
                redundant = path in known
                reads.append((path, redundant))
            elif name == "Bash" and isinstance(inp, dict) and isinstance(inp.get("command"), str):
                redundant = _norm_cmd(inp["command"]) in pcmds
            if not redundant:
                continue
            red += 1
            price = turn["carry_price"]
            if price is not None:
                est = child["result_chars"].get(cid, 0) / 4.0
                later = len(turns) - 1 - ti
                carry += (
                    est
                    * (float(price["cache_write_5m"]) + float(price["cache_read"]) * later)
                    / _MTOK
                )
        if red and turn["cost"] is not None:
            lower += turn["cost"] * red / max(1, len(turn["calls"]))
            upper += turn["cost"] + carry
    return {
        "family": _family_label(child["model"]),
        "parent_known": len(known),
        "reads": reads,
        "read_calls": len(reads),
        "rereads": sum(1 for _path, redundant in reads if redundant),
        "cost": cost,
        "lower": lower if cost is not None else None,
        "upper": min(upper, cost) if cost is not None else None,
    }


def _file_bytes(text: str) -> dict[str, int]:
    """Rendered bytes per packet file: its pointer line (excerpts are never rendered here)."""
    out: dict[str, int] = {}
    in_files = False
    for line in text.split("\n"):
        if line in _SECTION_HEADERS:
            in_files = line == _FILES_HEADER
            continue
        if in_files and line.startswith("- "):
            key = _norm_path(line[2:].split(" | ", 1)[0])
            out[key] = out.get(key, 0) + len(line.encode("utf-8")) + 1
    return out


def _sweep(
    record: dict[str, Any],
    facts: SessionFacts,
    prompt: str,
    target: Any,
    budgets: tuple[int, ...],
    session_id: str,
) -> dict[int, dict[str, Any]]:
    """Re-render the Brief packet at each budget; coverage and bytes-weighted precision."""
    family = target.family if target.family in BUDGET_BYTES else "unknown"
    out: dict[int, dict[str, Any]] = {}
    for budget in budgets:
        default = BUDGET_BYTES[family]
        BUDGET_BYTES[family] = budget
        try:
            rendered = render(
                select(facts, prompt, target),
                _EMPTY_INTENT,
                cwd="",
                session_id=session_id,
                now=_EPOCH,
            )
        except (ValueError, OSError):
            continue
        finally:
            BUDGET_BYTES[family] = default
        packet = {_norm_path(item.path) for item in rendered.items}
        reads = record["reads"]
        read_paths = {path for path, _redundant in reads}
        out[budget] = {
            "redundant": record["rereads"],
            "covered": sum(1 for path, redundant in reads if redundant and path in packet),
            "packet_bytes": len(rendered.text.encode("utf-8")),
            "relevant_bytes": sum(
                n for path, n in _file_bytes(rendered.text).items() if path in read_paths
            ),
        }
    return out


# ------------------------------------------------------------------ collection


def _parent_cost(path: Path, families: dict[str, Any]) -> tuple[float, bool]:
    """Parent dollars and whether any turn had an unpriced model."""
    total = 0.0
    unpriced = False
    for turn in _scan_turns(path, families):
        family = turn["family"]
        if family is None:
            unpriced = True
            continue
        total += _turn_dollars(families[family], turn["tokens"], turn["prompt"])
    return total, unpriced


def collect(
    root: Path, prices: dict[str, Any], budgets: tuple[int, ...] = _SWEEP_BUDGETS
) -> dict[str, Any]:
    """Walk transcripts, join children, and price each session and its redundant reads."""
    families: dict[str, Any] = prices["families"]
    children: list[dict[str, Any]] = []
    sessions: set[str] = set()
    costed: list[dict[str, Any]] = []
    excluded = 0
    seen: set[str] = set()
    for path in sorted(root.glob("*/*.jsonl")) if root.is_dir() else []:
        sid = path.stem
        sub = path.with_suffix("") / "subagents"
        if sid in seen or not sub.is_dir():
            continue
        seen.add(sid)
        parent = _scan_child_parent(path)
        if not parent["calls"]:
            continue
        files = {f.stem[len("agent-") :]: f for f in sub.glob("agent-*.jsonl")}
        objs = list(_iter_jsonl(path))
        inputs = _agent_inputs(objs)
        linked: set[str] = set()
        joined: list[dict[str, Any]] = []
        for tid, spawn_idx in parent["calls"].items():
            aid = parent["links"].get(tid)
            if aid is None or aid in linked:
                continue
            linked.add(aid)
            child_path = files.get(aid)
            if child_path is None:
                continue
            child = _scan_child_turns(child_path, parent["msg_ids"], families)
            if not child["turns"]:
                continue
            record = _analyse_child(child, parent, spawn_idx)
            if record["parent_known"] >= 3:
                spawn_input = inputs.get(tid, {})
                prompt = spawn_input.get("prompt")
                target = resolve_target(
                    {
                        "model": child["model"] or spawn_input.get("model"),
                        "subagent_type": spawn_input.get("subagent_type"),
                    }
                )
                record["sweep"] = _sweep(
                    record,
                    _facts_before(objs, spawn_idx, sid),
                    prompt if isinstance(prompt, str) else "",
                    target,
                    budgets,
                    sid,
                )
            joined.append(record)
        if not joined:
            continue
        sessions.add(sid)
        children.extend(joined)
        parent_cost, unpriced = _parent_cost(path, families)
        priced = [r for r in joined if r["cost"] is not None]
        # Same rule as scripts/denominator.py: an unpriced child drops that child, not the
        # whole session; only a session with no priced child (or an unpriced parent) is out.
        if unpriced or not priced:
            excluded += 1
            continue
        total = parent_cost + sum(r["cost"] or 0.0 for r in priced)
        if total <= 0:
            excluded += 1
            continue
        costed.append(
            {
                "total": total,
                "lower": sum(r["lower"] or 0.0 for r in priced),
                "upper": sum(r["upper"] or 0.0 for r in priced),
            }
        )
    return {
        "sessions": len(sessions),
        "children": children,
        "costed": costed,
        "excluded": excluded,
        "budgets": list(budgets),
    }


# ------------------------------------------------------------------ aggregation


def summarise(data: dict[str, Any]) -> dict[str, Any]:
    """Aggregates only: per-family counts and shares, pooled cost shares, packet sweep."""
    kids: list[dict[str, Any]] = data["children"]
    families: list[dict[str, Any]] = []
    for family in _FAMILIES:
        group = [k for k in kids if k["family"] == family]
        read_calls = sum(k["read_calls"] for k in group)
        rereads = sum(k["rereads"] for k in group)
        families.append(
            {
                "family": family,
                "children": len(group),
                "parent_knew_3": sum(1 for k in group if k["parent_known"] >= 3),
                "read_calls": read_calls,
                "rereads": rereads,
                "rediscovery_share": _share(rereads, read_calls),
            }
        )
    rows: list[dict[str, Any]] = data["costed"]
    total = sum(r["total"] for r in rows)
    sweep: list[dict[str, Any]] = []
    groups: tuple[tuple[str, Callable[[dict[str, Any]], bool]], ...] = (
        ("all", lambda k: k["parent_known"] >= 3),
        ("opus/sonnet", lambda k: k["parent_known"] >= 3 and k["family"] in ("opus", "sonnet")),
    )
    for budget in data["budgets"]:
        for name, keep in groups:
            picked = [k["sweep"][budget] for k in kids if keep(k) and budget in k.get("sweep", {})]
            if not picked:
                continue
            sweep.append(
                {
                    "budget": budget,
                    "group": name,
                    "children": len(picked),
                    "coverage": _share(
                        sum(r["covered"] for r in picked), sum(r["redundant"] for r in picked)
                    ),
                    "bytes_precision": _share(
                        sum(r["relevant_bytes"] for r in picked),
                        sum(r["packet_bytes"] for r in picked),
                    ),
                }
            )
    return {
        "sessions": data["sessions"],
        "children": len(kids),
        "priced_children": sum(1 for k in kids if k["cost"] is not None),
        "families": families,
        "cost_share": {
            "sessions": len(rows),
            "excluded": data["excluded"],
            "lower": _share(sum(r["lower"] for r in rows), total),
            "upper": _share(sum(r["upper"] for r in rows), total),
        },
        "packet_sweep": sweep,
        "safe_to_paste": "counts and percentages only",
    }


def _row(values: tuple[str, ...], widths: tuple[int, ...]) -> str:
    return "  " + " ".join(v.rjust(w) for v, w in zip(values, widths, strict=True))


def format_report(report: dict[str, Any]) -> str:
    """The numbers-only block, ending with the safe-to-paste line."""
    lines = ["cfxd handoff-stats (aggregates only, one machine)"]
    lines.append(
        f"Sessions with sub-agent transcripts: {report['sessions']}; "
        f"joined children: {report['children']}; priced children: {report['priced_children']}"
    )
    lines.append("By child model family:")
    family_widths = (8, 10, 16, 12, 10, 19)
    header = (
        "family",
        "children",
        "parent knew 3+",
        "read calls",
        "re-reads",
        "re-discovery share",
    )
    lines.append(_row(header, family_widths))
    for row in report["families"]:
        lines.append(
            _row(
                (
                    str(row["family"]),
                    str(row["children"]),
                    str(row["parent_knew_3"]),
                    str(row["read_calls"]),
                    str(row["rereads"]),
                    _pct(row["rediscovery_share"]),
                ),
                family_widths,
            )
        )
    cost = report["cost_share"]
    if cost["lower"] is None:
        lines.append("Re-discovery cost share of total session cost: n/a (no priced session)")
    else:
        lines.append(
            "Re-discovery cost as a share of total session cost (parent + children, "
            f"{cost['sessions']} sessions): {_pct(cost['lower'])} (lower) to "
            f"{_pct(cost['upper'])} (upper)"
        )
    if cost["excluded"]:
        lines.append(
            f"Sessions excluded from the cost share (no priced child or unpriced parent): "
            f"{cost['excluded']}"
        )
    sweep = report["packet_sweep"]
    if not sweep:
        lines.append("Packet sweep: n/a (no child whose parent knew 3 or more files)")
    else:
        lines.append("Packet sweep for children whose parent knew 3 or more files:")
        sweep_widths = (8, 13, 10, 10, 25)
        lines.append(
            _row(
                ("budget", "group", "children", "coverage", "bytes-weighted precision"),
                sweep_widths,
            )
        )
        for row in sweep:
            lines.append(
                _row(
                    (
                        f"{row['budget'] // 1000} KB",
                        str(row["group"]),
                        str(row["children"]),
                        _pct(row["coverage"]),
                        _pct(row["bytes_precision"]),
                    ),
                    sweep_widths,
                )
            )
    lines.append(_SAFE)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cfxd handoff-stats",
        description="sub-agent re-discovery and Brief packet coverage (numbers only)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.home() / ".claude" / "projects",
        help="transcripts root (default: ~/.claude/projects)",
    )
    parser.add_argument(
        "--share",
        action="store_true",
        help="numeric aggregates only (the default; kept for command-line symmetry)",
    )
    parser.add_argument("--json", action="store_true", help="emit a single JSON object")
    args = parser.parse_args(argv)
    prices = _load_prices(_DEFAULT_PRICES)
    data = collect(args.root, prices)
    if not data["children"]:
        if args.json:
            print(json.dumps(summarise(data)))
        else:
            print(_EMPTY)
        return 0
    report = summarise(data)
    print(json.dumps(report) if args.json else format_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
