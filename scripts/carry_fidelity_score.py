"""R-4: would carry v1 or v2 have restored what the summary dropped?

Offline, read-only over Claude Code transcripts. For every compaction boundary with a
summary and at least one held file, rebuild the session facts the daemon would have
snapshotted (files read/edited, last test, open todos, user constraints), render the
carry block with the v1 and the v2 renderer, and count how many of the frozen fidelity
sets each block restores:

* **dropped edited files**: files edited before the boundary that the summary does not
  name (``docs/FIDELITY-METRICS.md``, M2). Naming uses M2's path matching.
* **held files**: distinct files touched before the boundary (fidelity's held set).
* **bytes**: UTF-8 size of the rendered block.

Output is aggregates only: counts, rates and byte sizes. No paths, prompts, commands or
transcript text are printed or written. The frozen metric definitions are imported from
``cfxd.fidelity``, not restated.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from cfxd import carry  # noqa: E402
from cfxd.audit import _SINCE_LABELS, _discover, _iter_objects, _texts  # noqa: E402
from cfxd.bound import test_key_any  # noqa: E402
from cfxd.brief_intent import _make_item, _record_text, _section_of, _sentences  # noqa: E402
from cfxd.checkpoint import _object_ts  # noqa: E402
from cfxd.daemon import percentile  # noqa: E402
from cfxd.fidelity import (  # noqa: E402
    _base,
    _hash,
    _Held,
    _matches,
    _norm,
    _path_tokens,
    _summary_text,
    _test_failed,
)
from cfxd.refetch import _ctx, _trigger  # noqa: E402
from cfxd.session_state import (  # noqa: E402
    MAX_COMMANDS,
    MAX_TODOS,
    FileFact,
    SessionFacts,
    TestFact,
    redact_command,
)

_FILE_TOOLS = frozenset({"Read", "Edit", "Write", "MultiEdit", "NotebookEdit"})
_EDIT_TOOLS = _FILE_TOOLS - {"Read"}
_TRIGGERS = ("manual", "auto", "unknown")
_SKIP_REASONS = ("no_summary_text", "no_held_files")
# Folds live in a daemon state dir, which a transcript-only scorer does not have.
_STATE = Path(__file__).resolve().parent / "_no_carry_state_"


def _named(block: str) -> tuple[set[str], set[str]]:
    """Fidelity's full-path and basename hashes for a rendered block."""
    mentions = set(_path_tokens(block))
    return {_hash(m) for m in mentions}, {_hash(_base(m)) for m in mentions}


def _constraints(prose: list[str]) -> tuple[str, ...]:
    """Constraint sentences from allowed user/assistant prose, in transcript order."""
    out: list[str] = []
    for text in prose:
        for sentence in _sentences(text):
            if _section_of(sentence) != "constraints":
                continue
            item = _make_item(sentence, "user", 0)
            if item is not None and item.text not in out:
                out.append(item.text)
    return tuple(out)


def _todos(raw: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(raw, list):
        return ()
    parsed: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = item.get("content")
        status = item.get("status")
        if isinstance(text, str) and isinstance(status, str):
            parsed.append((status, redact_command(text)))
    return tuple(parsed[:MAX_TODOS])


def _score(
    summary: str,
    facts: SessionFacts,
    constraints: tuple[str, ...],
    trigger: str,
    held: list[str],
    edited: set[str],
) -> dict[str, Any]:
    mentions = set(_path_tokens(summary))
    full = {_hash(m) for m in mentions}
    base = {_hash(_base(m)) for m in mentions}
    dropped = [path for path in edited if not _matches(path, full, base)]

    v1, _ = carry.render_report(facts, _STATE, trigger, version=1)
    v2, _ = carry.render_report(facts, _STATE, trigger, constraints=constraints, version=2)
    v1_full, v1_base = _named(v1)
    v2_full, v2_base = _named(v2)

    def count(block_full: set[str], block_base: set[str], paths: list[str]) -> int:
        return sum(1 for path in paths if _matches(path, block_full, block_base))

    return {
        "trigger": trigger,
        "held": len(held),
        "edited": len(edited),
        "dropped": len(dropped),
        "v1_held": count(v1_full, v1_base, held),
        "v2_held": count(v2_full, v2_base, held),
        "v1_dropped": count(v1_full, v1_base, dropped),
        "v2_dropped": count(v2_full, v2_base, dropped),
        "bytes_v1": len(v1.encode("utf-8")),
        "bytes_v2": len(v2.encode("utf-8")),
    }


def scan(path: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Every scored compaction in one transcript, plus skip counts."""
    events: list[dict[str, Any]] = []
    skipped = {reason: 0 for reason in _SKIP_REASONS}
    held = _Held()
    files: dict[str, FileFact] = {}
    edited: set[str] = set()
    commands: list[str] = []
    todos: tuple[tuple[str, str], ...] = ()
    test: TestFact | None = None
    pending_tests: dict[str, str] = {}
    pending_files: dict[str, str] = {}
    prose: list[str] = []
    triggering = "unknown"
    sequence = 0

    try:
        fallback = path.stat().st_mtime
    except OSError:
        fallback = 0.0

    for obj in _iter_objects(path):
        is_summary = "isCompactSummary" in obj
        if is_summary or obj.get("subtype") == "compact_boundary":
            if not is_summary:
                triggering = _trigger(obj)
                skipped["no_summary_text"] += 1
                continue
            trigger = _trigger(obj)
            if trigger == "unknown":
                trigger = triggering
            triggering = "unknown"
            text = _summary_text(obj)
            if not text.strip():
                skipped["no_summary_text"] += 1
                continue
            paths = held.paths()
            if not paths:
                skipped["no_held_files"] += 1
                continue
            facts = SessionFacts(
                session_id=path.stem,
                files=tuple(files.values()),
                folds=(),
                commands=tuple(commands),
                todos=todos,
                test=test,
            )
            events.append(_score(text, facts, _constraints(prose), trigger, paths, set(edited)))
            continue

        msg = obj.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        ts = _object_ts(obj, msg, fallback + sequence * 1e-6)
        sequence += 1

        kind = obj.get("type")
        if kind in ("user", "assistant"):
            text = _record_text(obj, kind)
            if text is not None:
                prose.append(text)

        if kind == "assistant":
            usage = msg.get("usage") if isinstance(msg, dict) else None
            has_usage = _ctx(usage if isinstance(usage, dict) else None) is not None
            for block in blocks:
                if block.get("type") != "tool_use":
                    continue
                name = block.get("name")
                inp = block.get("input")
                if not isinstance(name, str) or not isinstance(inp, dict):
                    continue
                file_path = inp.get("file_path")
                if name == "NotebookEdit" and not isinstance(file_path, str):
                    file_path = inp.get("notebook_path")
                if name in _FILE_TOOLS and isinstance(file_path, str):
                    key = _norm(file_path)
                    if key:
                        previous = files.get(key)
                        files[key] = FileFact(
                            path=file_path,
                            lines=previous.lines if previous is not None else None,
                            edited=(previous.edited if previous is not None else False)
                            or name != "Read",
                            last_seen=ts,
                            ranges=(),
                        )
                        if name in _EDIT_TOOLS:
                            edited.add(key)
                        if has_usage:
                            held.add(file_path)
                        if isinstance(block.get("id"), str):
                            pending_files[block["id"]] = key
                command = inp.get("command")
                if name == "Bash" and isinstance(command, str) and command.strip():
                    redacted = redact_command(command)
                    if redacted in commands:
                        commands.remove(redacted)
                    commands.append(redacted)
                    del commands[:-MAX_COMMANDS]
                    key = test_key_any(command)
                    if key is not None:
                        test = TestFact(key=key, ok=None, at=ts)
                        if isinstance(block.get("id"), str):
                            pending_tests[block["id"]] = key
                if name == "TodoWrite":
                    todos = _todos(inp.get("todos"))

        for block in blocks:
            if block.get("type") != "tool_result":
                continue
            result_id = block.get("tool_use_id")
            if isinstance(result_id, str) and result_id in pending_tests:
                key = pending_tests.pop(result_id)
                texts = _texts(block.get("content"))
                test = TestFact(key=key, ok=not _test_failed(block, texts), at=ts)
            if isinstance(result_id, str) and result_id in pending_files:
                key = pending_files.pop(result_id)
                result = obj.get("toolUseResult")
                file = result.get("file") if isinstance(result, dict) else None
                lines = file.get("numLines") if isinstance(file, dict) else None
                if isinstance(lines, int) and not isinstance(lines, bool):
                    fact = files.get(key)
                    if fact is not None:
                        files[key] = FileFact(
                            path=fact.path,
                            lines=lines,
                            edited=fact.edited,
                            last_seen=fact.last_seen,
                            ranges=fact.ranges,
                        )
            # A tool result is never prose; _record_text already refuses it.

    return events, skipped


def _rate(num: int, den: int) -> float | None:
    return num / den if den else None


def _dist(values: list[float]) -> dict[str, float]:
    return {
        "median": round(percentile(values, 50), 1),
        "p25": round(percentile(values, 25), 1),
        "p75": round(percentile(values, 75), 1),
    }


def summarise(events: list[dict[str, Any]], skipped: dict[str, int]) -> dict[str, Any]:
    held = sum(e["held"] for e in events)
    edited = sum(e["edited"] for e in events)
    dropped = sum(e["dropped"] for e in events)
    v1_held = sum(e["v1_held"] for e in events)
    v2_held = sum(e["v2_held"] for e in events)
    v1_dropped = sum(e["v1_dropped"] for e in events)
    v2_dropped = sum(e["v2_dropped"] for e in events)
    by_trigger: dict[str, Any] = {}
    for trigger in _TRIGGERS:
        group = [e for e in events if e["trigger"] == trigger]
        if not group:
            continue
        by_trigger[trigger] = {
            "events": len(group),
            "held": sum(e["held"] for e in group),
            "dropped": sum(e["dropped"] for e in group),
            "v1_dropped": sum(e["v1_dropped"] for e in group),
            "v2_dropped": sum(e["v2_dropped"] for e in group),
        }
    return {
        "events": len(events),
        "skipped": skipped,
        "held": held,
        "edited": edited,
        "dropped": dropped,
        "v1_held": v1_held,
        "v2_held": v2_held,
        "v1_dropped": v1_dropped,
        "v2_dropped": v2_dropped,
        "v1_held_rate": _rate(v1_held, held),
        "v2_held_rate": _rate(v2_held, held),
        "v1_dropped_rate": _rate(v1_dropped, dropped),
        "v2_dropped_rate": _rate(v2_dropped, dropped),
        "bytes_v1": _dist([float(e["bytes_v1"]) for e in events]),
        "bytes_v2": _dist([float(e["bytes_v2"]) for e in events]),
        "v2_bigger": sum(1 for e in events if e["v2_dropped"] > e["v1_dropped"]),
        "v2_equal": sum(1 for e in events if e["v2_dropped"] == e["v1_dropped"]),
        "v2_smaller": sum(1 for e in events if e["v2_dropped"] < e["v1_dropped"]),
        "by_trigger": by_trigger,
    }


def collect(root: Path, since: str = "30d", now: datetime | None = None) -> dict[str, Any]:
    if now is None:
        now = datetime.now(UTC)
    from cfxd.audit import _SINCE_DELTAS

    delta = _SINCE_DELTAS.get(since)
    cutoff = now - delta if delta is not None else None
    parents, subs = _discover(root, cutoff)
    events: list[dict[str, Any]] = []
    skipped = {reason: 0 for reason in _SKIP_REASONS}
    for path in [*parents, *(p for p, _ in subs)]:
        found, file_skipped = scan(path)
        events.extend(found)
        for reason, count in file_skipped.items():
            skipped[reason] += count
    summary = summarise(events, skipped)
    summary["since"] = since
    summary["sessions"] = {"parents": len(parents), "subagents": len(subs)}
    return summary


def _pct(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate * 100:.1f}%"


def _pair(named: int, total: int, rate: float | None) -> str:
    return f"{named:,} of {total:,} ({_pct(rate)})"


def render(data: dict[str, Any]) -> str:
    label = _SINCE_LABELS.get(str(data["since"]), str(data["since"]))
    skipped = data["skipped"]
    out: list[str] = []
    w = out.append
    w("# Carry v2 vs v1: what a compaction carry block would restore")
    w("")
    w("Generated by `scripts/carry_fidelity_score.py`. Aggregates only: no paths, prompts,")
    w("commands or transcript text. Held files and dropped edited files use the frozen")
    w("definitions in `docs/FIDELITY-METRICS.md`, imported from `cfxd/fidelity.py`.")
    w("")
    w("## Sample")
    w("")
    w(f"- Window: {label}")
    w(
        f"- Sessions scanned: {data['sessions']['parents']:,} parents, "
        f"{data['sessions']['subagents']:,} sub-agent transcript files"
    )
    w(
        f"- Compaction boundaries scored: {data['events']:,}; skipped "
        f"{skipped['no_summary_text'] + skipped['no_held_files']:,} "
        f"(no summary text {skipped['no_summary_text']:,}, "
        f"no held files {skipped['no_held_files']:,})"
    )
    if data["events"] == 0:
        w("")
        w("No compaction with a summary and held files was found, so there is nothing to compare.")
        w("")
        return "\n".join(out)
    w("")
    w("## Headline (pooled over every scored compaction)")
    w("")
    w("| measure | carry v1 | carry v2 |")
    w("|---|---|---|")
    w(
        f"| dropped edited files restored | "
        f"{_pair(data['v1_dropped'], data['dropped'], data['v1_dropped_rate'])} | "
        f"{_pair(data['v2_dropped'], data['dropped'], data['v2_dropped_rate'])} |"
    )
    w(
        f"| held files restored | "
        f"{_pair(data['v1_held'], data['held'], data['v1_held_rate'])} | "
        f"{_pair(data['v2_held'], data['held'], data['v2_held_rate'])} |"
    )
    w(
        f"| block bytes (p25 / median / p75) | "
        f"{data['bytes_v1']['p25']:.0f} / {data['bytes_v1']['median']:.0f} / "
        f"{data['bytes_v1']['p75']:.0f} | "
        f"{data['bytes_v2']['p25']:.0f} / {data['bytes_v2']['median']:.0f} / "
        f"{data['bytes_v2']['p75']:.0f} |"
    )
    w("")
    w(
        f"- Frozen M2 on the same compactions: {data['dropped']:,} of "
        f"{data['edited']:,} edited files were not named by the summary "
        f"({_pct(_rate(data['dropped'], data['edited']))}). Those are the files carry can "
        "add back."
    )
    w(
        f"- Compactions where v2 restores more dropped edited files than v1: "
        f"{data['v2_bigger']:,}; equal: {data['v2_equal']:,}; fewer: {data['v2_smaller']:,}"
    )
    if data["by_trigger"]:
        w("")
        w("## By trigger")
        w("")
        w("| trigger | events | dropped edited | v1 restored | v2 restored |")
        w("|---|---|---|---|---|")
        for trigger, group in data["by_trigger"].items():
            total = group["dropped"]
            v1 = _pair(group["v1_dropped"], total, _rate(group["v1_dropped"], total))
            v2 = _pair(group["v2_dropped"], total, _rate(group["v2_dropped"], total))
            w(f"| {trigger} | {group['events']:,} | {total:,} | {v1} | {v2} |")
    w("")
    w("## Method")
    w("")
    w(
        "- Facts are rebuilt from the transcript before each boundary: file read/edit calls "
        "(last-seen order, edited flag), the last test-runner result, open todos and constraint "
        "sentences from allowed user/assistant prose (same source rules as Brief v1 intent)."
    )
    w(
        "- Both blocks are rendered by `cfxd.carry`: v1 is the shipped renderer (3,000-byte "
        "cap, last-seen order); v2 is the 4,000-byte renderer (edited files first, with line "
        "ranges, then test result, todos, constraints, folds, commands)."
    )
    w(
        "- A file counts as restored when the block names it under the frozen M2 matching "
        "rule (full path, separator-aligned suffix, or basename)."
    )
    w("")
    w("## Limitations")
    w("")
    w("- One person's sessions on one machine; few compactions move these rates a lot.")
    w(
        "- Folds and line ranges are not in transcripts: the scorer renders no fold section "
        "and no ranges, so v2's real byte cost is a little higher than shown."
    )
    w(
        "- Constraint extraction mirrors `brief_intent.extract_intent` over the records seen "
        "so far rather than reading the whole file at snapshot time; counts and bytes are "
        "close, not byte-identical."
    )
    w(
        "- Restoring a file name is not the same as restoring what mattered in it; this "
        "measures coverage of the frozen sets, not downstream quality."
    )
    w("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Carry v1 vs v2 compaction restore report")
    ap.add_argument("--root", type=Path, default=Path.home() / ".claude" / "projects")
    ap.add_argument("--since", choices=("7d", "30d", "all"), default="30d")
    ap.add_argument("--out", type=Path, default=_REPO / "docs" / "carry-fidelity-score.md")
    args = ap.parse_args(argv)
    text = render(collect(args.root, args.since))
    sys.stdout.write(text + "\n")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
