"""R-1: replay Brief v1 on real spawns and score the packet it would have sent.

Free, offline, read-only over Claude Code transcripts (same layout and join as Gate 1:
parent ``Agent`` tool_use -> ``toolUseResult.agentId`` -> child transcript). For every
joined child, rebuild the parent's ``SessionFacts`` from the lines before the spawn, call
``cfxd.brief_select.select`` with the child's own prompt and model, and measure the packet:

* **coverage**: share of the child's redundant reads (a path the parent already knew)
  whose path is in the packet.
* **precision**: share of the packet's files the child actually read.
* **packet bytes**: UTF-8 bytes of the rendered Brief v1 block for that selection. Intent
  sections need the parent's message text and are left out, so this is a floor.
* **avoidable cost/tokens**: Gate 1's lower and upper attribution restricted to the
  redundant reads the packet covers.

Reports aggregates only: no paths, prompts, commands or transcript text is printed or
written. Dollars are API list-price equivalents, not a bill.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
for _p in (str(_REPO), str(_REPO / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import gate1_rediscovery as gate1  # noqa: E402

from cfxd.bound import test_key_any  # noqa: E402
from cfxd.brief_intent import delegation_items  # noqa: E402
from cfxd.brief_receipts import _iter_jsonl  # noqa: E402
from cfxd.brief_render import HEADERS, render  # noqa: E402
from cfxd.brief_select import BUDGET_BYTES, resolve_target, select  # noqa: E402
from cfxd.brief_types import Intent  # noqa: E402
from cfxd.routing import _DEFAULT_PRICES, _load_prices  # noqa: E402
from cfxd.session_state import (  # noqa: E402
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
_MTOK = 1_000_000.0
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_EMPTY_INTENT = Intent(goal=None, constraints=(), decisions=(), tried=(), deduped=0)
SWEEP_BUDGETS = (2_000, 6_000, 12_000, 20_000)
_FILES_HEADER = HEADERS["files"]
_SECTION_HEADERS = frozenset(HEADERS.values())


# ------------------------------------------------------------------ facts at spawn


def facts_before(objs: list[dict[str, Any]], spawn_idx: int, session_id: str) -> SessionFacts:
    """``SessionFacts`` as the daemon would hold them just before the spawn line.

    Mirrors ``checkpoint.facts_from_transcript`` but restricted to the lines before the
    ``Agent`` call. ``last_seen`` is the line index (monotonic, no clock needed); ranges
    are not rebuilt because the daemon does not record them for brief v1 either.
    """
    files: dict[str, FileFact] = {}
    commands: deque[str] = deque()
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
                    norm = gate1.norm_path(file_path)
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
                    try:
                        commands.remove(redacted)
                    except ValueError:
                        pass
                    commands.append(redacted)
                    while len(commands) > MAX_COMMANDS:
                        commands.popleft()
                    test_key = test_key_any(command)
                    if test_key is not None:
                        test = TestFact(key=test_key, ok=None, at=float(idx))
                        test_id = bid if isinstance(bid, str) else None
                elif name == "TodoWrite":
                    raw = tool_input.get("todos")
                    if isinstance(raw, list):
                        parsed = [
                            (item["status"], item["content"])
                            for item in raw
                            if isinstance(item, dict)
                            and isinstance(item.get("status"), str)
                            and isinstance(item.get("content"), str)
                        ]
                        todos = tuple(parsed[:MAX_TODOS])
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


def agent_inputs(objs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """``Agent`` tool_use id -> its input, for prompt and requested model."""
    out: dict[str, dict[str, Any]] = {}
    for obj in objs:
        for block in gate1._blocks(obj):
            if (
                block.get("type") == "tool_use"
                and block.get("name") in gate1._AGENT_TOOLS
                and isinstance(block.get("id"), str)
                and isinstance(block.get("input"), dict)
            ):
                out[block["id"]] = block["input"]
    return out


# ------------------------------------------------------------------ per child


def _child_model(ch: dict[str, Any], prompt_input: dict[str, Any]) -> Any:
    return ch["model"] or prompt_input.get("model")


def file_bytes(text: str) -> dict[str, int]:
    """Rendered bytes attributable to each packet file: its pointer line.

    Excerpt bytes are not attributed: the replay's facts carry no line ranges, so
    ``render`` never emits an excerpt here. shortcut: an excerpt would be counted as
    irrelevant, upgrade when the replay records ranges.
    """
    out: dict[str, int] = {}
    in_files = False
    for line in text.split("\n"):
        if line in _SECTION_HEADERS:
            in_files = line == _FILES_HEADER
            continue
        if in_files and line.startswith("- "):
            path = line[2:].split(" | ", 1)[0]
            key = gate1.norm_path(path)
            out[key] = out.get(key, 0) + len(line.encode("utf-8")) + 1
    return out


def score_child(
    ch: dict[str, Any],
    parent: dict[str, Any],
    spawn_idx: int,
    rendered_text: str,
    packet_paths: list[str],
) -> dict[str, Any]:
    """Coverage, precision and the covered part of Gate 1's lower/upper attribution."""
    known = {p for i, p in parent["known"] if i < spawn_idx}
    packet = {gate1.norm_path(p) for p in packet_paths}
    turns = ch["turns"]
    cost = (
        sum(t["cost"] or 0.0 for t in turns)
        if all(t["cost"] is not None for t in turns if t["usage"] is not None)
        else None
    )
    redundant = covered = 0
    child_files: set[str] = set()
    packet_hit: set[str] = set()
    lower = upper = result_tokens = 0.0
    for ti, t in enumerate(turns):
        red = 0
        carry = 0.0
        for cid, name, inp in t["calls"]:
            path = gate1._call_path(name, inp) if name in _READ_TOOLS else None
            if not path:
                continue
            child_files.add(path)
            if path not in known:
                continue
            redundant += 1
            red += 1
            if path not in packet:
                continue
            covered += 1
            packet_hit.add(path)
            price = t["carry_price"]
            if price is not None:
                est = ch["result_chars"].get(cid, 0) / 4.0
                result_tokens += est
                later = len(turns) - 1 - ti
                carry += (
                    est
                    * (float(price["cache_write_5m"]) + float(price["cache_read"]) * later)
                    / _MTOK
                )
        if red and t["cost"] is not None:
            lower += t["cost"] * red / max(1, len(t["calls"]))
            upper += t["cost"] + carry
    return {
        "model": ch["model"],
        "family": gate1._family_label(ch["model"]),
        "parent_known": len(known),
        "cost": cost,
        "tokens": sum(t["tok"] or 0 for t in turns),
        "redundant": redundant,
        "covered": covered,
        "packet_files": len(packet),
        "packet_hit": len(packet & child_files),
        "packet_bytes": len(rendered_text.encode("utf-8")),
        "relevant_bytes": sum(
            n for path, n in file_bytes(rendered_text).items() if path in child_files
        ),
        "lower": lower if cost is not None else None,
        "upper": min(upper, cost) if cost is not None else None,
        "result_tokens": result_tokens,
        "has_read": bool(child_files),
    }


def _packet_of(rendered: Any) -> list[str]:
    return [item.path for item in rendered.items]


def collect(root: Path, prices: dict[str, Any], budgets: tuple[int, ...] = ()) -> dict[str, Any]:
    """Walk transcripts and score every child that can be joined and rendered.

    ``budgets`` additionally re-renders every child with the packet budget forced to each
    value (via the public ``BUDGET_BYTES`` knob) and stores the result under ``sweep``.
    """
    stats: Counter[str] = Counter()
    seen: set[str] = set()
    children: list[dict[str, Any]] = []
    for pf in sorted(root.glob("*/*.jsonl")):
        sid = pf.stem
        sub = pf.with_suffix("") / "subagents"
        if not sub.is_dir():
            continue
        if sid in seen:
            stats["duplicate_session_copies_skipped"] += 1
            continue
        seen.add(sid)
        parent = gate1.scan_parent(pf)
        objs = list(_iter_jsonl(pf))
        inputs = agent_inputs(objs)
        files = {f.stem[len("agent-") :]: f for f in sub.glob("agent-*.jsonl")}
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
            child_path = files.get(aid)
            if child_path is None:
                stats["unjoinable_no_child_transcript"] += 1
                continue
            ch = gate1.scan_child(child_path, parent["msg_ids"], prices)
            if not ch["turns"]:
                stats["unjoinable_empty_child"] += 1
                continue
            spawn_input = inputs.get(tid, {})
            prompt = spawn_input.get("prompt")
            if not isinstance(prompt, str):
                prompt = ""
            target = resolve_target(
                {
                    "model": _child_model(ch, spawn_input),
                    "subagent_type": spawn_input.get("subagent_type"),
                }
            )
            facts = facts_before(objs, call["idx"], sid)
            try:
                rendered = render(
                    select(facts, prompt, target),
                    _EMPTY_INTENT,
                    cwd="",
                    session_id=sid,
                    now=_EPOCH,
                )
            except (ValueError, OSError):
                stats["render_failed"] += 1
                continue
            rec = score_child(ch, parent, call["idx"], rendered.text, _packet_of(rendered))
            rec["background"] = bool(call["bg_input"] or status == "async_launched")
            rec["session"] = sid
            rec["prompt_constraints"] = delegation_items(prompt)
            rec["sweep"] = _sweep(ch, parent, call["idx"], facts, prompt, target, budgets, sid)
            children.append(rec)
        stats["child_files_not_referenced_by_parent"] += len(set(files) - linked)
    return {"stats": stats, "children": children, "sweep_budgets": list(budgets)}


def _sweep(
    ch: dict[str, Any],
    parent: dict[str, Any],
    spawn_idx: int,
    facts: SessionFacts,
    prompt: str,
    target: Any,
    budgets: tuple[int, ...],
    session_id: str,
) -> dict[int, dict[str, Any]]:
    """Re-render one child at each sweep budget, restoring the module knob every time."""
    out: dict[int, dict[str, Any]] = {}
    family = target.family if target.family in BUDGET_BYTES else "unknown"
    for budget in budgets:
        default = BUDGET_BYTES[family]
        BUDGET_BYTES[family] = budget
        try:
            swept = render(
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
        scored = score_child(ch, parent, spawn_idx, swept.text, _packet_of(swept))
        out[budget] = {
            key: scored[key]
            for key in (
                "redundant",
                "covered",
                "packet_files",
                "packet_hit",
                "packet_bytes",
                "relevant_bytes",
            )
        }
    return out


# ------------------------------------------------------------------ aggregation


def _share(num: float, den: float) -> float | None:
    return num / den if den > 0 else None


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{100 * v:.1f}%"


def _usd(v: float | None) -> str:
    return "n/a" if v is None else f"${v:,.2f}"


def _int(v: float) -> str:
    return f"{v:,.0f}"


def summarise(kids: list[dict[str, Any]]) -> dict[str, Any]:
    priced = [k for k in kids if k["cost"] is not None]
    cost = sum(k["cost"] for k in priced)
    packet_bytes = [k["packet_bytes"] for k in kids]
    return {
        "n": len(kids),
        "n_priced": len(priced),
        "n_with_read": sum(1 for k in kids if k["has_read"]),
        "n_redundant": sum(1 for k in kids if k["redundant"] > 0),
        "cost_total": cost,
        "redundant": sum(k["redundant"] for k in kids),
        "covered": sum(k["covered"] for k in kids),
        "coverage": _share(sum(k["covered"] for k in kids), sum(k["redundant"] for k in kids)),
        "packet_files": sum(k["packet_files"] for k in kids),
        "packet_hit": sum(k["packet_hit"] for k in kids),
        "precision": _share(
            sum(k["packet_hit"] for k in kids), sum(k["packet_files"] for k in kids)
        ),
        "packet_bytes_total": sum(packet_bytes),
        "packet_bytes_median": sorted(packet_bytes)[len(packet_bytes) // 2] if packet_bytes else 0,
        "lower": sum(k["lower"] for k in priced),
        "upper": sum(k["upper"] for k in priced),
        "lower_share": _share(sum(k["lower"] for k in priced), cost),
        "upper_share": _share(sum(k["upper"] for k in priced), cost),
        "result_tokens": sum(k["result_tokens"] for k in kids),
    }


def summarise_sweep(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Coverage, precision and bytes-weighted precision over one budget's rows."""
    packet_bytes = [r["packet_bytes"] for r in rows]
    return {
        "n": len(rows),
        "coverage": _share(sum(r["covered"] for r in rows), sum(r["redundant"] for r in rows)),
        "precision": _share(
            sum(r["packet_hit"] for r in rows), sum(r["packet_files"] for r in rows)
        ),
        "bytes_precision": _share(sum(r["relevant_bytes"] for r in rows), sum(packet_bytes)),
        "packet_bytes_median": sorted(packet_bytes)[len(packet_bytes) // 2] if packet_bytes else 0,
    }


_SWEEP_GROUPS: tuple[tuple[str, Any], ...] = (
    ("haiku", lambda k: k["family"] == "haiku"),
    ("sonnet", lambda k: k["family"] == "sonnet"),
    ("opus", lambda k: k["family"] == "opus"),
    ("parent knew 3 or more files", lambda k: k["parent_known"] >= 3),
)


def _sweep_section(kids: list[dict[str, Any]], budgets: list[int]) -> list[str]:
    out = [
        "## Budget sweep: precision-recall by packet budget",
        "",
        "The same replay, re-rendered with the packet budget forced to each value below",
        "(`cfxd.brief_select.BUDGET_BYTES`, the public per-family byte ceiling; item caps are",
        "unchanged). Coverage is recall over redundant reads, precision is packet files the",
        "child read over packet files. Bytes-weighted precision is the rendered bytes of the",
        "packet files the child read over all rendered packet bytes, so the fixed header,",
        "prologue and command lines count as irrelevant. Groups with no children are omitted.",
        "",
        "| budget | group | children | coverage | precision | bytes-weighted precision "
        "| packet bytes (median) |",
        "|---|---|---|---|---|---|---|",
    ]
    for budget in budgets:
        for name, keep in _SWEEP_GROUPS:
            rows = [k["sweep"][budget] for k in kids if keep(k) and budget in k["sweep"]]
            if not rows:
                continue
            s = summarise_sweep(rows)
            out.append(
                f"| {budget:,} B ({budget // 1000} KB) | {name} | {s['n']} | "
                f"{_pct(s['coverage'])} | {_pct(s['precision'])} | "
                f"{_pct(s['bytes_precision'])} | {_int(s['packet_bytes_median'])} |"
            )
    out.append("")
    return out


def _table(title: str, groups: list[tuple[str, list[dict[str, Any]]]]) -> list[str]:
    out = [
        f"## {title}",
        "",
        "| group | children | coverage | precision | packet bytes (median) "
        "| avoidable lower | avoidable upper | covered result tokens |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, kids in groups:
        if not kids:
            continue
        s = summarise(kids)
        out.append(
            f"| {name} | {s['n']} | {_pct(s['coverage'])} | {_pct(s['precision'])} | "
            f"{_int(s['packet_bytes_median'])} | {_usd(s['lower'])} ({_pct(s['lower_share'])}) | "
            f"{_usd(s['upper'])} ({_pct(s['upper_share'])}) | {_int(s['result_tokens'])} |"
        )
    out.append("")
    return out


def render_doc(data: dict[str, Any]) -> str:
    kids = data["children"]
    st = data["stats"]
    a = summarise(kids)
    out: list[str] = []
    w = out.append
    w("# Brief replay score: what a packet would have covered")
    w("")
    w("Generated by `scripts/brief_replay_score.py`. Aggregates only; no paths, prompts,")
    w("commands or transcript text. Dollars are API list-price equivalents, not a bill.")
    w("")
    w("## Sample")
    w("")
    w(f"- Agent calls seen: {st['agent_calls']}; joined children scored: {len(kids)}")
    w(
        f"- Unjoinable: {st['unjoinable_no_agentid_in_result']} with no agentId in the tool "
        f"result, {st['unjoinable_no_child_transcript']} with a missing child transcript, "
        f"{st['unjoinable_empty_child']} empty; {st['render_failed']} failed to render"
    )
    w(
        f"- Child transcripts no parent call points at (excluded): "
        f"{st['child_files_not_referenced_by_parent']}; duplicate session copies skipped: "
        f"{st['duplicate_session_copies_skipped']}"
    )
    w(
        f"- Children that read at least one file: {a['n_with_read']} of {a['n']}; with a "
        f"redundant read: {a['n_redundant']}; priced: {a['n_priced']}"
    )
    w("")
    w("## Method")
    w("")
    w("- Join: parent `Agent` tool_use, then `toolUseResult.agentId`, then the child transcript.")
    w(
        "- Facts at spawn: the parent's Read/Edit/Write paths, redacted Bash commands and todos "
        "from lines before the `Agent` call, rebuilt with `cfxd.session_state` shapes."
    )
    w(
        "- Packet: `cfxd.brief_select.select` on those facts with the spawn prompt and the "
        "child's real model, rendered by `cfxd.brief_render`. Intent sections are omitted "
        "(they need parent message text), so packet bytes are a floor."
    )
    w(
        "- Redundant read: a child `Read`/`NotebookRead`/`Grep` on a path the parent had "
        "already read or edited before the spawn (Gate 1's definition)."
    )
    w(
        "- Coverage: covered redundant reads / all redundant reads, pooled. Precision: packet "
        "files the child read / all packet files, pooled."
    )
    w(
        "- Avoidable lower/upper: Gate 1's attribution (turn cost pro-rated by the turn's "
        "covered-read share; upper also charges the covered results carried to later turns) "
        "restricted to covered reads. `covered result tokens` is their chars/4 estimate."
    )
    w("")
    w("## Headline")
    w("")
    w(
        f"- **Coverage {_pct(a['coverage'])} of redundant reads** ({a['covered']} of "
        f"{a['redundant']}), precision {_pct(a['precision'])} "
        f"({a['packet_hit']} of {a['packet_files']} packet files)"
    )
    w(
        f"- Packet bytes: median {_int(a['packet_bytes_median'])}, total "
        f"{_int(a['packet_bytes_total'])} over {a['n']} rendered packets"
    )
    w(
        f"- Avoidable cost {_usd(a['lower'])} lower to {_usd(a['upper'])} upper "
        f"({_pct(a['lower_share'])} to {_pct(a['upper_share'])} of "
        f"{_usd(a['cost_total'])} child cost); {_int(a['result_tokens'])} result tokens"
    )
    w("")
    fams = sorted({k["family"] for k in kids})
    out.extend(
        _table(
            "By child model family",
            [(f, [k for k in kids if k["family"] == f]) for f in fams],
        )
    )
    out.extend(
        _table(
            "By parent knowledge at spawn",
            [
                ("parent knew 3 or more files", [k for k in kids if k["parent_known"] >= 3]),
                ("parent knew 1-2 files", [k for k in kids if 1 <= k["parent_known"] < 3]),
                ("parent knew 0 files", [k for k in kids if k["parent_known"] == 0]),
            ],
        )
    )
    best = [k for k in kids if k["family"] in ("opus", "sonnet") and k["parent_known"] >= 3]
    if best:
        s = summarise(best)
        w("## Gate 2 segment (Opus/Sonnet children, parent knew 3 or more files)")
        w("")
        w(
            f"- {s['n']} children: coverage {_pct(s['coverage'])}, precision "
            f"{_pct(s['precision'])}, median packet {_int(s['packet_bytes_median'])} bytes"
        )
        w("")
    w("## Caveats and bias")
    w("")
    w(
        "- One person's sessions on one machine. Many delegations are exploration tasks where "
        "the parent has read nothing, so the packet has nothing to offer by construction."
    )
    w(
        "- Parent knowledge is only what its Read/Edit/Write/Bash calls show. Context from the "
        "user's prompt, compacted history or MCP tools is invisible."
    )
    w(
        "- Path match is exact after slash/case normalisation; a file under another spelling "
        "is not matched."
    )
    w(
        "- Packet bytes exclude intent sections and are measured on today's files: an excerpt "
        "can be truncated or degraded if the file changed or vanished, so real packets may "
        "differ slightly."
    )
    w(
        "- Coverage says a path was in the packet, not that the child would have used it, and "
        "some re-reads are needed anyway to edit exact bytes."
    )
    w("")
    budgets = data.get("sweep_budgets") or []
    if budgets:
        out.extend(_sweep_section(kids, budgets))
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Brief replay coverage/precision report")
    ap.add_argument("--root", type=Path, default=Path.home() / ".claude" / "projects")
    ap.add_argument("--out", type=Path, default=_REPO / "docs" / "brief-replay-score.md")
    ap.add_argument(
        "--sweep",
        action="store_true",
        help="append a 2/6/12/20 KB budget sweep section (default output unchanged)",
    )
    args = ap.parse_args(argv)
    prices = _load_prices(_DEFAULT_PRICES)
    budgets = SWEEP_BUDGETS if args.sweep else ()
    text = render_doc(collect(args.root, prices, budgets))
    sys.stdout.write(text + "\n")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
