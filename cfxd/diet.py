"""``cfxd diet``: which starting-prefix items a session never used, and their cost.

Reads only attachment *names* and sizes, and tool-call names, from local Claude
Code transcripts. The default report prints real skill/plugin/connector/file names
because it is for the user's own screen; ``--share`` swaps every name for a stable
short hash label so a tester can paste the result.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cfxd.audit import (
    _SINCE_DELTAS,
    _SINCE_LABELS,
    _discover,
    _iter_objects,
)
from cfxd.daemon import percentile

# Item kinds that can be disabled (the only ones in the "never used" report).
_DISABLE_KINDS = ("skill", "connector")

_LABEL_PREFIX = {
    "skill": "skill",
    "plugin": "plugin",
    "connector": "connector",
    "memory": "file",
    "hook": "hook",
    "subagent": "subagent",
}


def _norm(name: str) -> str:
    """Lowercase, alphanumeric-only key for joining server names across formats."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _label(name: str, kind: str) -> str:
    return f"{_LABEL_PREFIX[kind]}-{hashlib.sha1(name.encode('utf-8')).hexdigest()[:4]}"


def _plugin(name: str) -> str:
    """Plugin part of a ``plugin:skill`` name, or the name itself when unprefixed."""
    return name.split(":", 1)[0] if ":" in name else name


def _med(values: list[int]) -> float:
    return percentile([float(v) for v in values], 50)


def _skill_entry_bytes(content: str, names: Any) -> dict[str, int]:
    """Split a skill listing into ``{name: bytes}`` by bullet structure only.

    The ``names`` field lists skills in the same order as the bullets; a bullet
    starts a new entry and any non-bullet line is a continuation of the previous
    entry. Falls back to parsing the bullet's leading ``name:`` when ``names`` is
    missing.
    """
    lines = content.splitlines()
    name_list = [n for n in names if isinstance(n, str)] if isinstance(names, list) else []
    out: dict[str, int] = {}
    ni = 0
    cur: str | None = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(("- ", "* ")):
            if ni < len(name_list):
                cur = name_list[ni]
                ni += 1
            else:
                match = re.match(r"^[-*]\s+(.*?):\s", stripped)
                cur = match.group(1) if match else None
            if cur is not None and cur not in out:
                out[cur] = 0
        if cur is not None:
            out[cur] += len(line.encode("utf-8"))
    return out


def _add_connector_tool(connectors: dict[str, dict[str, Any]], tool_name: str) -> None:
    server = tool_name[len("mcp__") :].split("__", 1)[0]
    slot = connectors.setdefault(
        _norm(server),
        {"name": server, "tools": 0, "tool_names_bytes": 0, "instructions_bytes": 0},
    )
    slot["name"] = server
    slot["tools"] += 1
    slot["tool_names_bytes"] += len(tool_name.encode("utf-8"))


def _add_connector_instr(connectors: dict[str, dict[str, Any]], server: str, nbytes: int) -> None:
    slot = connectors.setdefault(
        _norm(server),
        {"name": server, "tools": 0, "tool_names_bytes": 0, "instructions_bytes": 0},
    )
    slot["instructions_bytes"] += nbytes


def _add_hook(att: dict[str, Any], hooks: dict[str, int]) -> None:
    name = att.get("hookName")
    if not isinstance(name, str) or not name:
        name = str(att.get("type") or "hook")
    content = att.get("content")
    if isinstance(content, str):
        nbytes = len(content.encode("utf-8"))
    elif isinstance(content, list):
        nbytes = sum(len(x.encode("utf-8")) for x in content if isinstance(x, str))
    else:
        nbytes = 0
    if nbytes:
        hooks[name] = hooks.get(name, 0) + nbytes


def _scan(path: Path) -> dict[str, Any]:
    """Collect prefix items and whole-session usage from one parent transcript."""
    skills: dict[str, int] = {}
    connectors: dict[str, dict[str, Any]] = {}
    memory: dict[str, int] = {}
    hooks: dict[str, int] = {}
    used_skills: set[str] = set()
    used_servers: set[str] = set()
    subagent_types: set[str] = set()
    turns = 0
    listing_seen = False
    assistant_seen = 0

    for obj in _iter_objects(path):
        obj_type = obj.get("type")
        is_assistant = obj_type == "assistant"
        prefix = (assistant_seen < 3) if is_assistant else (assistant_seen <= 3)

        if obj_type == "attachment":
            att = obj.get("attachment")
            if isinstance(att, dict) and prefix:
                at = att.get("type")
                if at == "skill_listing":
                    listing_seen = True
                    content = att.get("content")
                    if isinstance(content, str) and content:
                        for name, nbytes in _skill_entry_bytes(content, att.get("names")).items():
                            skills[name] = skills.get(name, 0) + nbytes
                    else:
                        for name in att.get("names") or []:
                            if isinstance(name, str):
                                skills.setdefault(name, 0)
                elif at == "deferred_tools_delta":
                    # The initial delta's addedNames is the deferred/connector tool listing.
                    # shortcut: deferred_tools_record.entries (full definitions) are ignored;
                    # add them if a transcript has no delta but a non-empty record.
                    listing_seen = True
                    for name in att.get("addedNames") or []:
                        if isinstance(name, str) and name.startswith("mcp__"):
                            _add_connector_tool(connectors, name)
                elif at == "mcp_instructions_delta":
                    listing_seen = True
                    names = att.get("addedNames")
                    blocks = att.get("addedBlocks")
                    if isinstance(names, list) and isinstance(blocks, list):
                        for name, block in zip(names, blocks, strict=False):
                            if isinstance(name, str) and isinstance(block, str):
                                _add_connector_instr(connectors, name, len(block.encode("utf-8")))
                elif at == "instructions":
                    for f in att.get("files") or []:
                        if isinstance(f, dict):
                            fp = f.get("path")
                            text = f.get("content")
                            if isinstance(fp, str) and isinstance(text, str):
                                memory[fp] = memory.get(fp, 0) + len(text.encode("utf-8"))
                elif at == "nested_memory":
                    fp = att.get("path") or att.get("displayPath")
                    inner = att.get("content")
                    text = inner.get("content") if isinstance(inner, dict) else None
                    if isinstance(fp, str) and isinstance(text, str):
                        memory[fp] = memory.get(fp, 0) + len(text.encode("utf-8"))
                elif isinstance(at, str) and at.startswith("hook_"):
                    _add_hook(att, hooks)
            continue

        if obj_type not in ("assistant", "user"):
            continue
        msg = obj.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []

        if is_assistant:
            if isinstance(msg, dict) and isinstance(msg.get("usage"), dict):
                turns += 1
            assistant_seen += 1

        for block in blocks:
            if block.get("type") != "tool_use":
                continue
            name = block.get("name")
            inp = block.get("input")
            if not isinstance(name, str) or not isinstance(inp, dict):
                continue
            if name == "Skill":
                skill = inp.get("skill")
                if isinstance(skill, str):
                    used_skills.add(skill)
            elif name.startswith("mcp__"):
                used_servers.add(_norm(name[len("mcp__") :].split("__", 1)[0]))
            elif name == "Agent":
                sub = inp.get("subagent_type")
                if isinstance(sub, str):
                    subagent_types.add(sub)

    return {
        "skills": skills,
        "connectors": connectors,
        "memory": memory,
        "hooks": hooks,
        "used_skills": used_skills,
        "used_servers": used_servers,
        "subagent_types": subagent_types,
        "turns": turns,
        "listing_seen": listing_seen,
    }


def _skill_item(name: str, sizes: list[int], used: bool) -> dict[str, Any]:
    return {
        "kind": "skill",
        "name": name,
        "group": _plugin(name),
        "sessions": len(sizes),
        "median_bytes": round(_med(sizes), 1),
        "used": used,
    }


def _connector_item(
    name: str,
    sizes: list[int],
    tools: list[int],
    tool_names_bytes: list[int],
    instructions_bytes: list[int],
    used: bool,
) -> dict[str, Any]:
    return {
        "kind": "connector",
        "name": name,
        "group": name,
        "sessions": len(sizes),
        "median_bytes": round(_med(sizes), 1),
        "used": used,
        "tools": round(_med(tools)),
        "tool_names_bytes": round(_med(tool_names_bytes), 1),
        "instructions_bytes": round(_med(instructions_bytes), 1),
    }


def _file_item(kind: str, name: str, sizes: list[int]) -> dict[str, Any]:
    return {
        "kind": kind,
        "name": name,
        "group": name,
        "sessions": len(sizes),
        "median_bytes": round(_med(sizes), 1),
    }


def _group_plugins(items: list[dict[str, Any]], used: bool, total: float) -> list[dict[str, Any]]:
    by_plugin: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if item["kind"] == "skill" and item["used"] is used:
            by_plugin.setdefault(item["group"], []).append(item)
    groups = []
    for plugin, members in by_plugin.items():
        median_bytes = sum(m["median_bytes"] for m in members)
        groups.append(
            {
                "plugin": plugin,
                "count": len(members),
                "skills": sorted(m["name"] for m in members),
                "median_bytes": round(median_bytes, 1),
                "per_turn_tokens": round(median_bytes / 4.0, 1),
                "share": round(median_bytes / total, 4) if total else 0.0,
            }
        )
    groups.sort(key=lambda g: (-g["median_bytes"], g["plugin"]))
    return groups


def _group_connectors(
    items: list[dict[str, Any]], used: bool, total: float
) -> list[dict[str, Any]]:
    out = []
    for item in items:
        if item["kind"] == "connector" and item["used"] is used:
            out.append(
                {
                    "name": item["name"],
                    "tools": item["tools"],
                    "tool_names_bytes": item["tool_names_bytes"],
                    "instructions_bytes": item["instructions_bytes"],
                    "sessions": item["sessions"],
                    "median_bytes": item["median_bytes"],
                    "per_turn_tokens": round(item["median_bytes"] / 4.0, 1),
                    "share": round(item["median_bytes"] / total, 4) if total else 0.0,
                }
            )
    out.sort(key=lambda c: (-c["median_bytes"], c["name"]))
    return out


def diet(root: Path, since: str = "30d", now: datetime | None = None) -> dict[str, Any]:
    """Aggregate starting-prefix dead weight across parent transcripts under ``root``."""
    if now is None:
        now = datetime.now(UTC)
    delta = _SINCE_DELTAS.get(since)
    cutoff = now - delta if delta is not None else None
    parents, _subs = _discover(root, cutoff)

    used_skills: set[str] = set()
    used_servers: set[str] = set()
    subagent_types: set[str] = set()
    total_turns = 0
    listing_not_found = 0

    skill_sizes: dict[str, list[int]] = {}
    conn_sizes: dict[str, list[int]] = {}
    conn_meta: dict[str, dict[str, Any]] = {}
    mem_sizes: dict[str, list[int]] = {}
    hook_sizes: dict[str, list[int]] = {}
    session_prefix_bytes: list[int] = []

    for path in parents:
        r = _scan(path)
        used_skills |= r["used_skills"]
        used_servers |= r["used_servers"]
        subagent_types |= r["subagent_types"]
        total_turns += r["turns"]
        if not r["listing_seen"]:
            listing_not_found += 1

        for name, nbytes in r["skills"].items():
            skill_sizes.setdefault(name, []).append(nbytes)
        for key, slot in r["connectors"].items():
            conn_sizes.setdefault(key, []).append(
                slot["tool_names_bytes"] + slot["instructions_bytes"]
            )
            meta = conn_meta.setdefault(
                key,
                {
                    "name": slot["name"],
                    "tools": [],
                    "tool_names_bytes": [],
                    "instructions_bytes": [],
                },
            )
            meta["tools"].append(slot["tools"])
            meta["tool_names_bytes"].append(slot["tool_names_bytes"])
            meta["instructions_bytes"].append(slot["instructions_bytes"])
        for name, nbytes in r["memory"].items():
            mem_sizes.setdefault(name, []).append(nbytes)
        for name, nbytes in r["hooks"].items():
            hook_sizes.setdefault(name, []).append(nbytes)

        session_prefix_bytes.append(
            sum(r["skills"].values())
            + sum(s["tool_names_bytes"] + s["instructions_bytes"] for s in r["connectors"].values())
            + sum(r["memory"].values())
            + sum(r["hooks"].values())
        )

    items: list[dict[str, Any]] = []
    for name, sizes in skill_sizes.items():
        items.append(_skill_item(name, sizes, name in used_skills))
    for key, sizes in conn_sizes.items():
        meta = conn_meta[key]
        items.append(
            _connector_item(
                meta["name"],
                sizes,
                meta["tools"],
                meta["tool_names_bytes"],
                meta["instructions_bytes"],
                key in used_servers,
            )
        )
    for name, sizes in mem_sizes.items():
        items.append(_file_item("memory", name, sizes))
    for name, sizes in hook_sizes.items():
        items.append(_file_item("hook", name, sizes))

    items.sort(key=lambda it: (-it["median_bytes"], it["name"]))

    total = sum(it["median_bytes"] for it in items)
    never_tokens = (
        sum(it["median_bytes"] for it in items if it["kind"] in _DISABLE_KINDS and not it["used"])
        / 4.0
    )

    return {
        "since": since,
        "sessions": len(parents),
        "listing_not_found": listing_not_found,
        "turns": total_turns,
        "subagent_types": sorted(subagent_types),
        "prefix_tokens_estimated_median": round(_med(session_prefix_bytes) / 4.0, 1),
        "total_median_bytes": round(total, 1),
        "items": items,
        "never_used": {
            "plugins": _group_plugins(items, False, total),
            "connectors": _group_connectors(items, False, total),
        },
        "used": {
            "plugins": _group_plugins(items, True, total),
            "connectors": _group_connectors(items, True, total),
        },
        "effect": {
            "per_turn_tokens": round(never_tokens, 1),
            "turns": total_turns,
            "total_tokens": round(never_tokens * total_turns, 1),
        },
    }


def _anonymize(report: dict[str, Any]) -> dict[str, Any]:
    """Deep-copy ``report`` with every name replaced by a stable hash label."""
    r: dict[str, Any] = json.loads(json.dumps(report))
    for item in r["items"]:
        item["name"] = _label(item["name"], item["kind"])
        item["group"] = _label(item["group"], "plugin") if item["kind"] == "skill" else item["name"]
    r["subagent_types"] = [_label(s, "subagent") for s in r["subagent_types"]]
    for section in ("never_used", "used"):
        for g in r[section]["plugins"]:
            g["plugin"] = _label(g["plugin"], "plugin")
            g["skills"] = [_label(s, "skill") for s in g["skills"]]
        for c in r[section]["connectors"]:
            c["name"] = _label(c["name"], "connector")
    return r


def _item_line(item: dict[str, Any]) -> str:
    if item["kind"] == "connector":
        head = f"connector {item['name']} ({item['tools']} tools)"
    else:
        head = f"{item['kind']} {item['name']}"
    flag = "" if item["kind"] in ("memory", "hook") else ("used" if item["used"] else "unused")
    line = (
        f"  {head}  {item['median_bytes']:,.0f} bytes  "
        f"~{item['median_bytes'] / 4:,.0f} est. tokens  in {item['sessions']} sessions"
    )
    return line + f"  [{flag}]" if flag else line


def _plugin_line(group: dict[str, Any]) -> str:
    return (
        f"  skills in plugin '{group['plugin']}' ({group['count']} skills)  "
        f"{group['median_bytes']:,.0f} bytes  ~{group['per_turn_tokens']:,.0f} est. tokens/turn  "
        f"{group['share'] * 100:.1f}% of prefix"
    )


def _connector_line(conn: dict[str, Any]) -> str:
    return (
        f"  connector '{conn['name']}' ({conn['tools']} tools)  "
        f"{conn['median_bytes']:,.0f} bytes  ~{conn['per_turn_tokens']:,.0f} est. tokens/turn  "
        f"{conn['share'] * 100:.1f}% of prefix"
    )


def format_human(report: dict[str, Any], share: bool = False, dir_name: str = ".") -> str:
    """Render the diet report as plain text."""
    if report["sessions"] == 0:
        return f"No transcripts found under {dir_name}."
    label = _SINCE_LABELS.get(str(report["since"]), str(report["since"]))
    lines: list[str] = []
    if not share:
        lines.append("contains names from your setup; do not paste")
    lines.append(f"Claude Code diet, {label}")
    header = f"Sessions: {report['sessions']:,}"
    if report["listing_not_found"]:
        header += f"   listing not found in {report['listing_not_found']} sessions"
    lines.append(header)
    lines.append(
        f"Typical starting prefix: ~{report['prefix_tokens_estimated_median']:,.0f} est. tokens"
    )

    lines.append("Top items by median size:")
    for item in report["items"][:10]:
        lines.append(_item_line(item))

    lines.append("Never used in this window:")
    if not report["never_used"]["plugins"] and not report["never_used"]["connectors"]:
        lines.append("  (none)")
    for group in report["never_used"]["plugins"]:
        lines.append(_plugin_line(group))
        for skill in group["skills"]:
            lines.append(f"    - {skill}")
    for conn in report["never_used"]["connectors"]:
        lines.append(_connector_line(conn))

    lines.append("Used in this window:")
    if not report["used"]["plugins"] and not report["used"]["connectors"]:
        lines.append("  (none)")
    for group in report["used"]["plugins"]:
        lines.append(_plugin_line(group))
    for conn in report["used"]["connectors"]:
        lines.append(_connector_line(conn))

    effect = report["effect"]
    lines.append("Estimated effect of removing never-used items:")
    lines.append(
        f"  ~{effect['per_turn_tokens']:,.0f} est. tokens/turn x {effect['turns']:,} turns "
        f"= ~{effect['total_tokens']:,.0f} tokens not re-read"
    )

    lines.append("To act:")
    lines.append(
        "  disable a plugin:    claude plugin disable <plugin>@<marketplace>   (or /plugin panel)"
    )
    lines.append(
        "  disable a connector: claude mcp remove <name>                         (or /mcp panel)"
    )
    lines.append("  trim a memory file:  edit the file to drop instructions you no longer use")
    lines.append("  silence one skill:   set 'disable-model-invocation: true' in its frontmatter")
    lines.append(
        "  (removing an item removes its capability; items unused in this window may still matter)"
    )
    return "\n".join(lines)
