"""P4 M1 + M2: repeat-share and Read-stub counts over Claude Code transcripts.

M1 counting definition (must not drift). repeat-share is the bytes of tool-result
content that landed in a transcript and duplicates earlier landed content, over
new (non-cached) tokens. Three splits are reported separately and are never added
up: by tool (Read / Bash / Grep / other), exact vs near-duplicate, and within a
compaction segment only. A repeat whose original was compacted away is not
elidable, so it is excluded from the numerator and kept in the denominator.

M2 counts, for every Read result, a full response vs a FILE_UNCHANGED-style stub,
with the bytes each represents.

Aggregates only (R6): counts, bytes and token estimates. No path, prompt, command
or tool output is printed, and nothing is written to disk.
"""

from __future__ import annotations

import argparse
import sys
import zlib
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
for _p in (_REPO, _REPO / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from cfxd.audit import (  # noqa: E402
    _UNCHANGED_PHRASES,
    DEFAULT_ROOT,
    _as_int,
    _discover,
    _iter_objects,
    _texts,
)
from cfxd.usage_dedupe import MessageUsage  # noqa: E402

# Split 1 tools, in report order. Everything else (incl. MCP tools) lands in "other".
_TOOL_SPLIT = ("Read", "Bash", "Grep", "other")

# A line-set overlap at or above this counts as a near-duplicate. Test reruns with a
# few changed lines land here; two unrelated outputs do not. Definitional choice: a
# changed threshold is a new dated decision, never an edit (P4 M1).
_NEAR_JACCARD = 0.8

# FILE_UNCHANGED-style stub markers, matched case-insensitively (M2).
_STUB_PHRASES = _UNCHANGED_PHRASES + ("file_unchanged",)

# Frozen bars from docs/plans/2026-11-01-wave-decisions.md, printed for the reader.
_BARS = ((15.0, "lead"), (5.0, "quiet, off by default, no tester claim"))


def _blank() -> dict[str, Any]:
    return {
        "sessions": 0,
        "messages": 0,
        "new_tokens": 0,
        "results": 0,
        "landed": 0,
        "exact": {"events": 0, "bytes": 0},
        "near": {"events": 0, "bytes": 0},
        "cross": {"events": 0, "bytes": 0},
        "by_tool": {t: {"results": 0, "landed": 0, "exact": 0, "near": 0} for t in _TOOL_SPLIT},
        "read": {
            "reread_full": {"results": 0, "bytes": 0},
            "stub": {"results": 0, "bytes": 0},
            "first": {"results": 0, "bytes": 0},
        },
    }


def _merge(dst: dict[str, Any], src: dict[str, Any]) -> None:
    for key, value in src.items():
        if isinstance(value, dict):
            _merge(dst.setdefault(key, {}), value)
        else:
            dst[key] = dst.get(key, 0) + value


def _line_digest(line: str) -> int:
    return zlib.crc32(line.encode("utf-8"))


def _is_stub(texts: list[str]) -> bool:
    return any(p in text.lower() for text in texts for p in _STUB_PHRASES)


def _classify(
    digest: int,
    lines: frozenset[int],
    seg: int,
    seg_digests: set[int],
    old_digests: set[int],
    line_sets: list[tuple[int, frozenset[int]]],
) -> str:
    """Classify one landed result: exact, near, cross (not elidable) or new."""
    if digest in seg_digests:
        return "exact"
    if digest in old_digests:
        return "cross"
    if len(lines) < 2:
        return "new"
    same = across = 0.0
    # shortcut: every earlier result in the session is a candidate, O(n^2) per session;
    # upgrade to an inverted index if one session ever holds ~10k tool results.
    for s, fs in line_sets:
        if not 0.5 <= len(fs) / len(lines) <= 2.0:
            continue
        jaccard = len(lines & fs) / len(lines | fs)
        if s == seg:
            same = max(same, jaccard)
        else:
            across = max(across, jaccard)
    if same >= _NEAR_JACCARD:
        return "near"
    return "cross" if across >= _NEAR_JACCARD else "new"


def scan_session(path: Path) -> dict[str, Any]:
    """One transcript, streamed: repeat bytes, split three ways, plus Read stubs."""
    out = _blank()
    dedupe = MessageUsage(_iter_objects(path))
    names: dict[str, str] = {}
    read_paths: set[int] = set()
    rereads: set[str] = set()
    seg = 0
    seg_digests: set[int] = set()
    old_digests: set[int] = set()
    line_sets: list[tuple[int, frozenset[int]]] = []

    for obj in _iter_objects(path):
        if "isCompactSummary" in obj or obj.get("subtype") == "compact_boundary":
            seg += 1
            old_digests |= seg_digests
            seg_digests = set()
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        if obj.get("type") == "assistant":
            for block in blocks:
                if block.get("type") == "tool_use":
                    bid, name = block.get("id"), block.get("name")
                    if isinstance(bid, str) and isinstance(name, str):
                        names[bid] = name
                    inp = block.get("input")
                    fp = inp.get("file_path") if isinstance(inp, dict) else None
                    if not isinstance(bid, str) or name != "Read" or not isinstance(fp, str):
                        continue
                    if isinstance(inp, dict) and ("offset" in inp or "limit" in inp):
                        continue  # a partial read is not a full re-read
                    if hash(fp) in read_paths:
                        rereads.add(bid)
                    read_paths.add(hash(fp))
            usage = msg.get("usage")
            # One usage record per message.id: Claude Code repeats it on every line.
            if isinstance(usage, dict) and dedupe.count_here(msg):
                out["messages"] += 1
                out["new_tokens"] += (_as_int(usage.get("input_tokens")) or 0) + (
                    _as_int(usage.get("cache_creation_input_tokens")) or 0
                )
            continue
        if obj.get("type") != "user":
            continue
        for block in blocks:
            if block.get("type") != "tool_result":
                continue
            tid = block.get("tool_use_id")
            name = names.get(tid) if isinstance(tid, str) else None
            texts = _texts(block.get("content"))
            text = "\x00".join(texts)
            nbytes = len(text.encode("utf-8"))
            bucket = name if name in _TOOL_SPLIT else "other"
            out["results"] += 1
            out["landed"] += nbytes
            out["by_tool"][bucket]["results"] += 1
            out["by_tool"][bucket]["landed"] += nbytes
            if name == "Read":
                if _is_stub(texts):
                    kind = "stub"
                elif isinstance(tid, str) and tid in rereads:
                    kind = "reread_full"
                else:
                    kind = "first"
                out["read"][kind]["results"] += 1
                out["read"][kind]["bytes"] += nbytes

            digest = zlib.crc32(text.encode("utf-8"))
            lines = frozenset(_line_digest(line) for line in text.splitlines() if line.strip())
            kind = _classify(digest, lines, seg, seg_digests, old_digests, line_sets)
            if kind in ("exact", "near", "cross"):
                out[kind]["events"] += 1
                out[kind]["bytes"] += nbytes
            if kind in ("exact", "near"):
                out["by_tool"][bucket][kind] += nbytes
            seg_digests.add(digest)
            if len(lines) >= 2:
                line_sets.append((seg, lines))
    return out


def collect(root: Path) -> dict[str, Any]:
    """Scan every transcript under ``root``; aggregate totals only."""
    parents, subs = _discover(root, None)
    total = _blank()
    for path in sorted(parents + [p for p, _ in subs]):
        _merge(total, scan_session(path))
        total["sessions"] += 1
    return total


def _pct(part: float, total: float) -> str:
    return f"{100.0 * part / total:.1f}%" if total else "n/a"


def _band(share: str) -> str:
    if not share.endswith("%"):
        return "no denominator"
    value = float(share.rstrip("%"))
    for floor, label in _BARS:
        if value >= floor:
            return label
    return "demote"


def _row(label: str, cells: list[str]) -> str:
    return "| " + " | ".join([label, *cells]) + " |"


def render(data: dict[str, Any], bars_commit: str) -> str:
    new_tokens = float(data["new_tokens"])
    landed = float(data["landed"])
    exact, near, cross = data["exact"], data["near"], data["cross"]
    exact_share = _pct(exact["bytes"] / 4.0, new_tokens)
    near_share = _pct(near["bytes"] / 4.0, new_tokens)
    out: list[str] = []
    out.append("# P4 M1 + M2: repeat-share and Read-stub counts")
    out.append("")
    out.append(f"Decisions commit the bars were frozen in: `{bars_commit}`")
    out.append(
        "Frozen bar on total elidable repeat-share: >= 15% lead; 5-15% quiet "
        "(off by default, no tester claim); < 5% demote. Never edit a bar after the run."
    )
    out.append(
        "Evidence class: *measured* (read from this run's transcripts); token estimates "
        "are bytes / 4. These are shares of observed repeat content, not an observed reduction."
    )
    out.append("Exact and near-duplicate shares are reported separately and are never summed.")
    out.append("")
    out.append("## Denominator: new, non-cached content")
    out.append("")
    out.append("| quantity | value |")
    out.append("|---|---|")
    out.append(f"| transcripts scanned | {data['sessions']:,} |")
    out.append(f"| assistant usage records counted (one per `message.id`) | {data['messages']:,} |")
    out.append(f"| new (non-cached) tokens | {data['new_tokens']:,} |")
    out.append(f"| tool results landed | {data['results']:,} |")
    out.append(
        f"| landed tool-result bytes (token estimate) | {data['landed']:,} "
        f"({data['landed'] / 4:,.0f}) |"
    )
    out.append("")
    out.append(
        "New tokens are `input_tokens + cache_creation_input_tokens`, summed once per "
        "`message.id`; `cache_read_input_tokens` are the re-sent prefix and are not new. "
        "The primary share is repeat tokens over new tokens (the bar's denominator); the "
        "tool-result-byte share is printed beside it because the plan also words the "
        "denominator as tool-result content."
    )
    out.append("")
    out.append("## Split 1: exact vs near-duplicate (never summed)")
    out.append("")
    out.append(
        "| class | events | repeat bytes | repeat tokens (bytes / 4) "
        "| share of new tokens | share of landed tool-result bytes |"
    )
    out.append("|---|---|---|---|---|---|")
    out.append(
        _row(
            "exact",
            [
                f"{exact['events']:,}",
                f"{exact['bytes']:,}",
                f"{exact['bytes'] / 4:,.0f}",
                exact_share,
                _pct(float(exact["bytes"]), landed),
            ],
        )
    )
    out.append(
        _row(
            "near-duplicate",
            [
                f"{near['events']:,}",
                f"{near['bytes']:,}",
                f"{near['bytes'] / 4:,.0f}",
                near_share,
                _pct(float(near["bytes"]), landed),
            ],
        )
    )
    out.append("")
    out.append(
        f"Bar reading, each class on its own: exact {exact_share} -> {_band(exact_share)}; "
        f"near-duplicate {near_share} -> {_band(near_share)}. No combined figure exists."
    )
    out.append("")
    out.append("## Split 2: by tool")
    out.append("")
    out.append(
        "| tool | landed results | landed bytes | exact repeat bytes "
        "| exact share of new tokens | near repeat bytes | near share of new tokens |"
    )
    out.append("|---|---|---|---|---|---|---|")
    for tool in _TOOL_SPLIT:
        row = data["by_tool"][tool]
        out.append(
            _row(
                tool,
                [
                    f"{row['results']:,}",
                    f"{row['landed']:,}",
                    f"{row['exact']:,}",
                    _pct(row["exact"] / 4.0, new_tokens),
                    f"{row['near']:,}",
                    _pct(row["near"] / 4.0, new_tokens),
                ],
            )
        )
    out.append("")
    out.append("## Split 3: within a compaction segment")
    out.append("")
    out.append("| excluded repeats | events | bytes |")
    out.append("|---|---|---|")
    out.append(
        _row(
            "original was compacted away (not elidable)",
            [f"{cross['events']:,}", f"{cross['bytes']:,}"],
        )
    )
    out.append("")
    out.append(
        "Matching resets at every compaction boundary (`isCompactSummary` / "
        "`compact_boundary`). An elision stub pointing across that boundary points at "
        "content the model may no longer hold, so these repeats stay in the denominator "
        "and never enter the numerator."
    )
    out.append("")
    out.append("## M2: Read — full re-reads vs FILE_UNCHANGED-style stubs")
    out.append("")
    out.append("| Read response | results | bytes | share of Read bytes |")
    out.append("|---|---|---|---|")
    read = data["read"]
    read_bytes = sum(read[k]["bytes"] for k in ("reread_full", "stub", "first"))
    for kind, label in (
        ("reread_full", "full re-read (content returned)"),
        ("stub", "stub (FILE_UNCHANGED)"),
        ("first", "first or partial read (reference row)"),
    ):
        out.append(
            _row(
                label,
                [
                    f"{read[kind]['results']:,}",
                    f"{read[kind]['bytes']:,}",
                    _pct(float(read[kind]["bytes"]), float(read_bytes)),
                ],
            )
        )
    out.append("")
    out.append(
        "A re-read is a Read of a file path read earlier in the same transcript with no "
        "`offset`/`limit`. Stub responses are landed content and stay in the Split 1 "
        "denominator; their bytes are small. If stubs already capture most Read repeats, "
        "the Read row of Split 2 collapses, which is the vendor-solved case M2 exists to "
        "expose."
    )
    out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P4 M1 + M2: repeat-share and Read-stub counts")
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    ap.add_argument(
        "--bars-commit",
        default="",
        help="decisions-file commit the bars were frozen in (printed in the header; required)",
    )
    args = ap.parse_args(argv)
    if not args.bars_commit.strip():
        print(
            "repeat_share: --bars-commit is required: the frozen P4 bars must be cited "
            "before any number is read",
            file=sys.stderr,
        )
        return 2
    sys.stdout.write(render(collect(args.root), args.bars_commit.strip()) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
