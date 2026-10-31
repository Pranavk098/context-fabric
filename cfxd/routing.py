"""``cfxd routing``: where tokens and API-equivalent dollars go, by model and role.

Reads only model ids and usage numbers from local Claude Code transcripts — never
paths, prompts, commands, tool names or message text. The report contains model
ids, roles, counts, tokens and dollars only.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cfxd.audit import (
    _EDIT_TOOLS,
    _SINCE_DELTAS,
    _SINCE_LABELS,
    _as_int,
    _discover,
    _iter_objects,
    _weighted,
)
from cfxd.brief_receipts import _agent_id_from_text, _blocks
from cfxd.daemon import percentile
from cfxd.usage_dedupe import MessageUsage

_DEFAULT_PRICES = Path(__file__).with_name("data") / "prices.json"

_ROLES = ("parent", "subagent")
_TOKEN_KINDS = ("input", "cache_creation_5m", "cache_creation_1h", "cache_read", "output")

# Claude Code writes a placeholder model id on synthetic assistant turns (titles,
# summaries). Those turns have no billable model, so they are skipped.
_SYNTHETIC_MODEL = "<synthetic>"
_HAIKU_100K = 100_000
_MTOK = 1_000_000.0

# A cold model switch must rewrite a context above this many tokens where at
# least half of it is cache creation (the new model's first cache write).
_COLD_CONTEXT = 20_000

_DISCLAIMER = (
    "Dollars are API-list-price equivalents, not a bill. Subscription users pay no "
    "per-token dollars; this is a common currency for relative weight."
)
_LIMITS = (
    "Same token counts assumed: a cheaper model may take more turns, fail more, or need re-runs.",
    "Quality is not measured here.",
    "This is an upper bound on the price effect only.",
    "Model-switch premiums are API-list-price equivalents, not a bill.",
)

_FAMILIES_KEY = "families"

# Gate 1 join: the parent's Agent calls point at child transcripts. Only ids, usage,
# tool names and result sizes are read; paths are used for matching and never printed.
_CHILD_AGENT_TOOLS = frozenset({"Agent", "Task"})
_CHILD_KNOW_TOOLS = frozenset(
    {"Read", "NotebookRead", "Edit", "Write", "MultiEdit", "NotebookEdit"}
)
_CHILD_READ_TOOLS = frozenset({"Read", "NotebookRead", "Grep"})
_WS = re.compile(r"\s+")
_GITBASH = re.compile(r"^/([a-zA-Z])/")


def _load_prices(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    families: dict[str, Any] = {}
    for entry in data.get("models", []):
        if not isinstance(entry, dict):
            continue
        fam = entry.get("family")
        if isinstance(fam, str) and fam:
            families[fam] = entry
    data[_FAMILIES_KEY] = families
    return data


def _normalize(model: str) -> str:
    """``claude-sonnet-5-5`` / ``claude-haiku-4-5-20251001`` -> ``sonnet-5-5`` / ``haiku-4-5``."""
    name = model.strip().lower()
    if name.startswith("claude-"):
        name = name[len("claude-") :]
    parts = [p for p in name.split("-") if not (p.isdigit() and len(p) >= 4)]
    return "-".join(parts)


def _match_family(norm: str, families: dict[str, Any]) -> str | None:
    """Longest-prefix family match (``sonnet-5-5`` beats ``sonnet-5``)."""
    best: str | None = None
    for fam in families:
        if norm.startswith(fam) and (best is None or len(fam) > len(best)):
            best = fam
    return best


def _tokens(usage: dict[str, Any]) -> dict[str, int]:
    cc = usage.get("cache_creation")
    cc5m = 0
    cc1h = 0
    if isinstance(cc, dict) and (
        "ephemeral_5m_input_tokens" in cc or "ephemeral_1h_input_tokens" in cc
    ):
        cc5m = _as_int(cc.get("ephemeral_5m_input_tokens")) or 0
        cc1h = _as_int(cc.get("ephemeral_1h_input_tokens")) or 0
    else:
        # No 5m/1h split in this transcript: price the whole creation at the 1h rate.
        cc1h = _as_int(usage.get("cache_creation_input_tokens")) or 0
    return {
        "input": _as_int(usage.get("input_tokens")) or 0,
        "cache_creation_5m": cc5m,
        "cache_creation_1h": cc1h,
        "cache_read": _as_int(usage.get("cache_read_input_tokens")) or 0,
        "output": _as_int(usage.get("output_tokens")) or 0,
    }


def _prompt(tokens: dict[str, int]) -> int:
    return (
        tokens["input"]
        + tokens["cache_creation_5m"]
        + tokens["cache_creation_1h"]
        + tokens["cache_read"]
    )


def _price_for(entry: dict[str, Any], prompt: int) -> dict[str, Any]:
    over = entry.get("over_100k")
    if isinstance(over, dict) and prompt > _HAIKU_100K:
        return over
    return entry


def _turn_dollars(entry: dict[str, Any], tokens: dict[str, int], prompt: int) -> float:
    price = _price_for(entry, prompt)
    total = (
        tokens["input"] * float(price["input"])
        + tokens["cache_creation_5m"] * float(price["cache_write_5m"])
        + tokens["cache_creation_1h"] * float(price["cache_write_1h"])
        + tokens["cache_read"] * float(price["cache_read"])
        + tokens["output"] * float(price["output"])
    )
    return total / _MTOK


def _turn_cache_read_dollars(entry: dict[str, Any], tokens: dict[str, int], prompt: int) -> float:
    price = _price_for(entry, prompt)
    return tokens["cache_read"] * float(price["cache_read"]) / _MTOK


def _switch_premium(entry: dict[str, Any], tokens: dict[str, int], prompt: int) -> float:
    """Cold-switch premium: cache writes at write price minus the same tokens at read price."""
    price = _price_for(entry, prompt)
    written = tokens["cache_creation_5m"] + tokens["cache_creation_1h"]
    write_cost = tokens["cache_creation_5m"] * float(price["cache_write_5m"]) + tokens[
        "cache_creation_1h"
    ] * float(price["cache_write_1h"])
    read_cost = written * float(price["cache_read"])
    return (write_cost - read_cost) / _MTOK


def _tier(fam: str) -> int:
    if fam.startswith("fable"):
        return 4
    if fam.startswith("opus"):
        return 3
    if fam.startswith("sonnet"):
        return 2
    if fam.startswith("haiku"):
        return 1
    return 0


def _latest_family(families: dict[str, Any], prefix: str) -> str | None:
    matches = [f for f in families if f.startswith(prefix)]
    return max(matches) if matches else None


def _empty_tokens() -> dict[str, int]:
    return {k: 0 for k in _TOKEN_KINDS}


def _add_tokens(dst: dict[str, int], src: dict[str, int]) -> None:
    for k in _TOKEN_KINDS:
        dst[k] += src[k]


def _scan_turns(path: Path, families: dict[str, Any]) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    dedupe = MessageUsage(_iter_objects(path))
    for obj in _iter_objects(path):
        if obj.get("type") != "assistant":
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue
        if not dedupe.count_here(msg):
            continue
        model = msg.get("model")
        if not isinstance(model, str) or not model.strip() or model == _SYNTHETIC_MODEL:
            continue
        tokens = _tokens(usage)
        turns.append(
            {
                "model": model,
                "family": _match_family(_normalize(model), families),
                "tokens": tokens,
                "weighted": _weighted(usage),
                "prompt": _prompt(tokens),
            }
        )
    return turns


def _sum_tokens(tokens: dict[str, int]) -> int:
    return sum(tokens.values())


def _model_entries(models: dict[str, dict[str, Any]], role_dollars: float) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for fam, m in models.items():
        entries.append(
            {
                "model": fam,
                "sessions": m["sessions"],
                "turns": m["turns"],
                "tokens": m["tokens"],
                "weighted_tokens": round(m["weighted"], 1),
                "dollars": round(m["dollars"], 4),
                "share_of_dollars": round(m["dollars"] / role_dollars, 4) if role_dollars else 0.0,
            }
        )
    entries.sort(key=lambda e: (-e["dollars"], e["model"]))
    return entries


# ------------------------------------------------------------------ child models
# Same join and attribution as ``scripts/gate1_rediscovery.py`` so the two reports
# agree on the same logs. Kept local to avoid importing a script at runtime.


def _norm_path(p: str) -> str:
    s = _GITBASH.sub(lambda m: m.group(1) + ":/", p.replace("\\", "/").strip())
    return s.rstrip("/").lower()


def _norm_cmd(c: str) -> str:
    return _WS.sub(" ", c).strip()


def _tool_path(name: str, inp: Any) -> str | None:
    if not isinstance(inp, dict):
        return None
    p = inp.get("path") if name == "Grep" else inp.get("file_path") or inp.get("notebook_path")
    return _norm_path(p) if isinstance(p, str) and p else None


def _result_chars(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(c.get("text", "")) for c in content if isinstance(c, dict))
    return 0


def _family_label(model: str | None) -> str:
    if not model:
        return "unknown"
    name = _normalize(model)
    for fam in ("haiku", "sonnet", "opus", "fable"):
        if name.startswith(fam):
            return fam
    return "other"


def _scan_child_parent(path: Path) -> dict[str, Any]:
    """Parent knowledge, Bash commands, Agent calls and agent-id links by line index."""
    known: list[tuple[int, str]] = []
    cmds: list[tuple[int, str]] = []
    calls: dict[str, int] = {}
    links: dict[str, str] = {}
    msg_ids: set[str] = set()
    for idx, obj in enumerate(_iter_objects(path)):
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
                if name in _CHILD_KNOW_TOOLS:
                    p = _tool_path(name, inp)
                    if p:
                        known.append((idx, p))
                elif (
                    name == "Bash" and isinstance(inp, dict) and isinstance(inp.get("command"), str)
                ):
                    cmds.append((idx, _norm_cmd(inp["command"])))
                elif name in _CHILD_AGENT_TOOLS and isinstance(b.get("id"), str):
                    calls[b["id"]] = idx
            elif b.get("type") == "tool_result" and isinstance(b.get("tool_use_id"), str):
                aid = tur.get("agentId") if isinstance(tur, dict) else None
                if not isinstance(aid, str):
                    aid = _agent_id_from_text(b.get("content"))
                if isinstance(aid, str) and aid:
                    links[b["tool_use_id"]] = aid
    return {"known": known, "cmds": cmds, "calls": calls, "links": links, "msg_ids": msg_ids}


def _scan_child_turns(
    path: Path, parent_msg_ids: set[str], families: dict[str, Any]
) -> dict[str, Any]:
    """One entry per child message id (deduped), with priced turns and result sizes."""
    objs = list(_iter_objects(path))
    dedupe = MessageUsage(objs)
    turns: list[dict[str, Any]] = []
    by_key: dict[str, dict[str, Any]] = {}
    result_chars: dict[str, int] = {}
    model: str | None = None
    for n, obj in enumerate(objs):
        if obj.get("type") == "user":
            for b in _blocks(obj):
                if b.get("type") == "tool_result" and isinstance(b.get("tool_use_id"), str):
                    result_chars[b["tool_use_id"]] = _result_chars(b.get("content"))
        if obj.get("type") != "assistant":
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        mid = msg.get("id")
        if isinstance(mid, str) and mid in parent_msg_ids:
            continue  # forked history replayed by the child: not its cost
        key = mid if isinstance(mid, str) and mid else f"line{n}"
        turn = by_key.get(key)
        if turn is None:
            turn = {"calls": [], "usage": None, "model": None, "cost": None, "carry_price": None}
            by_key[key] = turn
            turns.append(turn)
        for b in _blocks(obj):
            if b.get("type") == "tool_use":
                turn["calls"].append((b.get("id"), b.get("name"), b.get("input")))
        m = msg.get("model")
        if isinstance(m, str) and m and m != _SYNTHETIC_MODEL:
            turn["model"] = m
            model = model or m
        usage = msg.get("usage")
        if isinstance(usage, dict) and dedupe.count_here(msg):
            turn["usage"] = dedupe.final_usage(msg, usage)
    for turn in turns:
        if turn["usage"] is None:
            continue
        tokens = _tokens(turn["usage"])
        fam = _match_family(_normalize(turn["model"] or model or ""), families)
        if fam is not None:
            prompt = _prompt(tokens)
            turn["cost"] = _turn_dollars(families[fam], tokens, prompt)
            turn["carry_price"] = _price_for(families[fam], prompt)
    return {"turns": turns, "result_chars": result_chars, "model": model}


def _analyse_child(child: dict[str, Any], parent: dict[str, Any], spawn_idx: int) -> dict[str, Any]:
    """Cost and the Gate 1 lower/upper re-discovery estimates for one child."""
    known = {p for i, p in parent["known"] if i < spawn_idx}
    pcmds = {c for i, c in parent["cmds"] if i < spawn_idx}
    turns = child["turns"]
    priced = all(t["cost"] is not None for t in turns if t["usage"] is not None)
    cost = sum(t["cost"] or 0.0 for t in turns) if priced else None
    calls = 0
    redundant = 0
    lower = 0.0
    upper = 0.0
    first_edit: int | None = None
    for ti, turn in enumerate(turns):
        red = 0
        carry = 0.0
        for cid, name, inp in turn["calls"]:
            calls += 1
            p = _tool_path(name, inp) if name in _CHILD_READ_TOOLS else None
            if name in _EDIT_TOOLS and first_edit is None:
                first_edit = ti
            hit = bool(p and p in known) or (
                name == "Bash"
                and isinstance(inp, dict)
                and isinstance(inp.get("command"), str)
                and _norm_cmd(inp["command"]) in pcmds
            )
            if hit:
                red += 1
                redundant += 1
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
    explore_end = first_edit if first_edit is not None else len(turns)
    explore_cost = sum(t["cost"] or 0.0 for t in turns[:explore_end]) if cost is not None else None
    return {
        "family": _family_label(child["model"]),
        "cost": cost,
        "tool_calls": calls,
        "redundant": redundant,
        "lower": lower if cost is not None else None,
        "upper": min(upper, cost) if cost is not None else None,
        "explore_cost": explore_cost,
    }


def _child_model_report(
    parents: list[Path], subs: list[tuple[Path, str]], families: dict[str, Any]
) -> dict[str, Any]:
    """Joined children grouped by child model family, priced with Gate 1 attribution."""
    parents_by_key: dict[tuple[Path, str], Path] = {}
    for path in sorted(parents):
        parents_by_key.setdefault((path.parent, path.stem), path)
    subs_by_key: dict[tuple[Path, str], dict[str, Path]] = {}
    for path, sid in sorted(subs, key=lambda item: str(item[0])):
        if not path.stem.startswith("agent-") or len(path.parents) < 3:
            continue
        kids = subs_by_key.setdefault((path.parents[2], sid), {})
        kids.setdefault(path.stem[len("agent-") :], path)

    children: list[dict[str, Any]] = []
    seen_sids: set[str] = set()
    for (proj, sid), parent_path in parents_by_key.items():
        if sid in seen_sids:
            continue  # duplicate copies of a session id, as in Gate 1
        files = subs_by_key.get((proj, sid))
        if not files:
            continue
        seen_sids.add(sid)
        parent = _scan_child_parent(parent_path)
        linked: set[str] = set()
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
            children.append(_analyse_child(child, parent, spawn_idx))

    rows: list[dict[str, Any]] = []
    for fam in sorted({c["family"] for c in children}):
        group = [c for c in children if c["family"] == fam]
        priced = [c for c in group if c["cost"] is not None]
        cost = sum(c["cost"] for c in priced)
        calls = sum(c["tool_calls"] for c in group)
        rows.append(
            {
                "family": fam,
                "children": len(group),
                "priced_children": len(priced),
                "dollars": round(cost, 4),
                "lower_share": round(sum(c["lower"] for c in priced) / cost, 4) if cost else None,
                "upper_share": round(sum(c["upper"] for c in priced) / cost, 4) if cost else None,
                "redundant_call_share": round(sum(c["redundant"] for c in group) / calls, 4)
                if calls
                else None,
                "explore_share": round(sum(c["explore_cost"] for c in priced) / cost, 4)
                if cost
                else None,
            }
        )
    return {"note": "price only; quality unmeasured", "families": rows}


def routing(
    root: Path,
    since: str = "30d",
    prices_path: Path | None = None,
    whatif: str = "both",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate per-model, per-role tokens and API-equivalent dollars."""
    if now is None:
        now = datetime.now(UTC)
    delta = _SINCE_DELTAS.get(since)
    cutoff = now - delta if delta is not None else None
    parents, subs = _discover(root, cutoff)

    prices = _load_prices(prices_path if prices_path is not None else _DEFAULT_PRICES)
    families: dict[str, Any] = prices[_FAMILIES_KEY]
    child_models = _child_model_report(parents, subs, families)

    role_sessions = {"parent": len(parents), "subagent": len(subs)}
    role_turns: dict[str, int] = {r: 0 for r in _ROLES}
    role_tokens: dict[str, dict[str, int]] = {r: _empty_tokens() for r in _ROLES}
    role_weighted: dict[str, float] = {r: 0.0 for r in _ROLES}
    role_dollars: dict[str, float] = {r: 0.0 for r in _ROLES}
    role_cr_dollars: dict[str, float] = {r: 0.0 for r in _ROLES}
    role_models: dict[str, dict[str, dict[str, Any]]] = {r: {} for r in _ROLES}

    unpriced_tokens = _empty_tokens()
    unpriced_weighted = 0.0
    unpriced_turns = 0
    unpriced_sessions: set[str] = set()
    unpriced_models: dict[str, dict[str, Any]] = {}

    targets: dict[str, str | None] = {}
    if whatif in ("haiku", "both"):
        targets["haiku"] = _latest_family(families, "haiku")
    if whatif in ("sonnet", "both"):
        targets["sonnet"] = _latest_family(families, "sonnet")
    wa: dict[str, dict[str, Any]] = {}
    for name, fam in targets.items():
        wa[name] = {"family": fam, "actual": 0.0, "target": 0.0, "turns": 0, "sessions": set()}

    switch_cold = 0
    switch_warm = 0
    switch_unpriced = 0
    switch_sessions: set[str] = set()
    switch_ctx: list[int] = []
    switch_premiums: list[float] = []
    switch_transitions: dict[tuple[str, str], int] = {}

    for role, paths in (("parent", parents), ("subagent", [p for p, _ in subs])):
        for path in paths:
            turns = _scan_turns(path, families)
            seen_families: set[str] = set()
            seen_unpriced: set[str] = set()
            have_prev = False
            prev_fam: str | None = None
            for turn in turns:
                role_turns[role] += 1
                _add_tokens(role_tokens[role], turn["tokens"])
                role_weighted[role] += turn["weighted"]
                fam = turn["family"]
                if role == "parent" and have_prev and fam != prev_fam:
                    ctx = turn["prompt"]
                    cc = turn["tokens"]["cache_creation_5m"] + turn["tokens"]["cache_creation_1h"]
                    if fam is None or prev_fam is None:
                        switch_unpriced += 1
                    elif ctx > _COLD_CONTEXT and cc * 2 >= ctx:
                        switch_cold += 1
                        switch_sessions.add(str(path))
                        switch_ctx.append(ctx)
                        switch_premiums.append(_switch_premium(families[fam], turn["tokens"], ctx))
                    else:
                        switch_warm += 1
                    if fam is not None and prev_fam is not None:
                        key = (prev_fam, fam)
                        switch_transitions[key] = switch_transitions.get(key, 0) + 1
                if role == "parent":
                    have_prev = True
                    prev_fam = fam
                if fam is None:
                    unpriced_turns += 1
                    unpriced_weighted += turn["weighted"]
                    _add_tokens(unpriced_tokens, turn["tokens"])
                    unpriced_sessions.add(str(path))
                    slot = unpriced_models.setdefault(
                        turn["model"],
                        {
                            "turns": 0,
                            "tokens": _empty_tokens(),
                            "weighted": 0.0,
                            "sessions": 0,
                        },
                    )
                    slot["turns"] += 1
                    slot["weighted"] += turn["weighted"]
                    _add_tokens(slot["tokens"], turn["tokens"])
                    seen_unpriced.add(turn["model"])
                    continue

                entry = families[fam]
                dollars = _turn_dollars(entry, turn["tokens"], turn["prompt"])
                role_dollars[role] += dollars
                role_cr_dollars[role] += _turn_cache_read_dollars(
                    entry, turn["tokens"], turn["prompt"]
                )
                model = role_models[role].setdefault(
                    fam,
                    {
                        "sessions": 0,
                        "turns": 0,
                        "tokens": _empty_tokens(),
                        "weighted": 0.0,
                        "dollars": 0.0,
                    },
                )
                model["turns"] += 1
                model["weighted"] += turn["weighted"]
                model["dollars"] += dollars
                _add_tokens(model["tokens"], turn["tokens"])
                seen_families.add(fam)

                if role == "subagent":
                    for acc in wa.values():
                        target_fam = acc["family"]
                        if target_fam is not None and _tier(fam) > _tier(target_fam):
                            acc["actual"] += dollars
                            acc["target"] += _turn_dollars(
                                families[target_fam], turn["tokens"], turn["prompt"]
                            )
                            acc["turns"] += 1
                            acc["sessions"].add(str(path))

            for fam in seen_families:
                role_models[role][fam]["sessions"] += 1
            for m in seen_unpriced:
                unpriced_models[m]["sessions"] += 1

    total_dollars = role_dollars["parent"] + role_dollars["subagent"]
    total_weighted = role_weighted["parent"] + role_weighted["subagent"]
    total_tokens = _empty_tokens()
    _add_tokens(total_tokens, role_tokens["parent"])
    _add_tokens(total_tokens, role_tokens["subagent"])

    roles: dict[str, Any] = {}
    for role in _ROLES:
        tokens = role_tokens[role]
        token_sum = _sum_tokens(tokens)
        roles[role] = {
            "sessions": role_sessions[role],
            "turns": role_turns[role],
            "tokens": tokens,
            "weighted_tokens": round(role_weighted[role], 1),
            "dollars": round(role_dollars[role], 4),
            "share_of_dollars": round(role_dollars[role] / total_dollars, 4)
            if total_dollars
            else 0.0,
            "share_of_weighted_tokens": round(role_weighted[role] / total_weighted, 4)
            if total_weighted
            else 0.0,
            "cache_read_share_tokens": round(tokens["cache_read"] / token_sum, 4)
            if token_sum
            else 0.0,
            "cache_read_share_dollars": round(role_cr_dollars[role] / role_dollars[role], 4)
            if role_dollars[role]
            else 0.0,
            "models": _model_entries(role_models[role], role_dollars[role]),
        }

    unpriced_report: dict[str, Any] = {
        "tokens": unpriced_tokens,
        "weighted_tokens": round(unpriced_weighted, 1),
        "turns": unpriced_turns,
        "sessions": len(unpriced_sessions),
        "models": sorted(
            (
                {
                    "model": m,
                    "turns": slot["turns"],
                    "sessions": slot["sessions"],
                    "tokens": slot["tokens"],
                    "weighted_tokens": round(slot["weighted"], 1),
                }
                for m, slot in unpriced_models.items()
            ),
            key=lambda e: (-e["weighted_tokens"], e["model"]),
        ),
    }

    whatif_report: dict[str, Any] = {}
    for name in ("haiku", "sonnet"):
        scenario = wa.get(name)
        if scenario is None or scenario["family"] is None:
            whatif_report[name] = None
            continue
        change = scenario["target"] - scenario["actual"]
        whatif_report[name] = {
            "target": scenario["family"],
            "dollars_before": round(scenario["actual"], 4),
            "dollars_after": round(scenario["target"], 4),
            "change": round(change, 4),
            "percent_of_total": round(change / total_dollars * 100.0, 2) if total_dollars else 0.0,
            "turns": scenario["turns"],
            "sessions": len(scenario["sessions"]),
        }

    premium_total = sum(switch_premiums)
    model_switches: dict[str, Any] = {
        "cold": switch_cold,
        "warm": switch_warm,
        "sessions": len(switch_sessions),
        "unpriced": switch_unpriced,
        "context_tokens": {
            "median": round(percentile([float(v) for v in switch_ctx], 50), 1),
            "p90": round(percentile([float(v) for v in switch_ctx], 90), 1),
            "max": max(switch_ctx) if switch_ctx else 0,
        },
        "premium_total": round(premium_total, 4),
        "premium_median": round(percentile(switch_premiums, 50), 4),
        "premium_share_of_parent_dollars": round(premium_total / role_dollars["parent"], 4)
        if role_dollars["parent"]
        else 0.0,
        "transitions": [
            {"from": frm, "to": to, "count": count}
            for (frm, to), count in sorted(
                switch_transitions.items(),
                key=lambda kv: (-kv[1], kv[0][0], kv[0][1]),
            )[:5]
        ],
    }

    return {
        "since": since,
        "as_of": prices.get("as_of"),
        "source": prices.get("source"),
        "note": prices.get("note"),
        "disclaimer": _DISCLAIMER,
        "totals": {
            "sessions": role_sessions["parent"] + role_sessions["subagent"],
            "turns": role_turns["parent"] + role_turns["subagent"],
            "tokens": total_tokens,
            "weighted_tokens": round(total_weighted, 1),
            "dollars": round(total_dollars, 4),
        },
        "roles": roles,
        "unpriced": unpriced_report,
        "child_models": child_models,
        "whatif": whatif_report,
        "model_switches": model_switches,
        "limits": list(_LIMITS),
    }


def _fmt_tokens(tokens: dict[str, int]) -> str:
    return (
        f"input {tokens['input']:,}, cache write 5m {tokens['cache_creation_5m']:,}, "
        f"cache write 1h {tokens['cache_creation_1h']:,}, cache read {tokens['cache_read']:,}, "
        f"output {tokens['output']:,}"
    )


def format_human(report: dict[str, Any], dir_name: str = ".") -> str:
    """Render the routing report as plain text (model ids, counts, tokens, dollars)."""
    if report["totals"]["sessions"] == 0:
        return f"No transcripts found under {dir_name}."
    label = _SINCE_LABELS.get(str(report["since"]), str(report["since"]))
    t = report["totals"]
    meta = []
    if report.get("as_of"):
        meta.append(f"as of {report['as_of']}")
    if report.get("source"):
        meta.append(f"source {report['source']}")
    if report.get("note"):
        meta.append(str(report["note"]))
    lines = [f"Claude Code routing, {label}", report["disclaimer"]]
    if meta:
        lines.append("; ".join(meta))
    lines.append(
        f"Totals: {t['sessions']:,} sessions, {t['turns']:,} turns, "
        f"{t['weighted_tokens']:,.1f} weighted tokens, ${t['dollars']:,.2f}"
    )
    lines.append("  " + _fmt_tokens(t["tokens"]))
    for role in _ROLES:
        r = report["roles"][role]
        lines.append("")
        lines.append(
            f"{role.capitalize()} ({r['sessions']:,} sessions, {r['turns']:,} turns, "
            f"${r['dollars']:,.2f}, {r['share_of_dollars'] * 100:.1f}% of dollars):"
        )
        lines.append(
            f"  cache read: {r['cache_read_share_tokens'] * 100:.1f}% of tokens, "
            f"{r['cache_read_share_dollars'] * 100:.1f}% of dollars"
        )
        for m in r["models"]:
            lines.append(
                f"  {m['model']}: {m['turns']:,} turns in {m['sessions']:,} sessions, "
                f"{m['weighted_tokens']:,.1f} weighted tokens, ${m['dollars']:,.2f} "
                f"({m['share_of_dollars'] * 100:.1f}%)"
            )
    cm = report["child_models"]
    lines.append("")
    lines.append("Sub-agent cost by child model family (price only; quality unmeasured):")
    if not cm["families"]:
        lines.append("  no joined child transcripts in this window")
    for f in cm["families"]:
        lower = "n/a" if f["lower_share"] is None else f"{f['lower_share'] * 100:.1f}%"
        upper = "n/a" if f["upper_share"] is None else f"{f['upper_share'] * 100:.1f}%"
        explore = "n/a" if f["explore_share"] is None else f"{f['explore_share'] * 100:.1f}%"
        priced = ""
        if f["priced_children"] != f["children"]:
            priced = f", {f['priced_children']:,} priced"
        lines.append(
            f"  {f['family']}: {f['children']:,} children{priced}, ${f['dollars']:,.2f}, "
            f"re-discovery {lower} (lower) to {upper} (upper) of cost, explore {explore}"
        )
    u = report["unpriced"]
    lines.append("")
    lines.append(
        f"Unpriced models ({u['turns']:,} turns in {u['sessions']:,} sessions, "
        f"{u['weighted_tokens']:,.1f} weighted tokens, no dollars):"
    )
    if u["models"]:
        for m in u["models"]:
            lines.append(
                f"  {m['model']}: {m['turns']:,} turns, {m['weighted_tokens']:,.1f} weighted tokens"
            )
    else:
        lines.append("  (none)")
    ms = report["model_switches"]
    lines.append("")
    lines.append(
        f"Model switches: {ms['cold']:,} cold, {ms['warm']:,} warm, "
        f"{ms['unpriced']:,} involving an unpriced model"
    )
    if ms["cold"] == 0:
        lines.append("  no cold model switches in this window")
    else:
        ctx = ms["context_tokens"]
        lines.append(f"  cold switches in {ms['sessions']:,} parent sessions")
        lines.append(
            f"  context tokens per cold switch: median {ctx['median']:,.0f}, "
            f"p90 {ctx['p90']:,.0f}, max {ctx['max']:,}"
        )
        lines.append(
            f"  premium: total ${ms['premium_total']:,.4f}, "
            f"median ${ms['premium_median']:,.4f}, "
            f"{ms['premium_share_of_parent_dollars'] * 100:.2f}% of parent dollars"
        )
        if ms["transitions"]:
            transitions = ", ".join(
                f"{t['from']} -> {t['to']} ({t['count']})" for t in ms["transitions"]
            )
            lines.append(f"  top transitions: {transitions}")
    lines.append("")
    lines.append("What-if (sub-agent turns re-priced on a cheaper tier, same tokens):")
    for name in ("haiku", "sonnet"):
        w = report["whatif"].get(name)
        if w is None:
            continue
        lines.append(
            f"  {name}: ${w['dollars_before']:,.2f} -> ${w['dollars_after']:,.2f} "
            f"(change {w['change']:+,.2f}, {w['percent_of_total']:+.2f}% of total cost), "
            f"touches {w['turns']:,} turns in {w['sessions']:,} sessions"
        )
    lines.append("Limits:")
    for lim in report["limits"]:
        lines.append(f"  - {lim}")
    lines.append(
        "This report contains model ids, counts, tokens and dollars only; "
        "no paths, commands or text."
    )
    return "\n".join(lines)
