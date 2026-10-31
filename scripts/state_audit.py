"""E-7: check that cfxd's JSONL logs store no excerpt or tool text, and that the
state directory's Windows ACL does not expose it to other users.

The daemon and its handlers document every log line as content-free: counts, ids,
sizes and outcomes only. This script reads every ``*.jsonl`` file under the state
directory and flags a line when it has (a) a top-level field outside that file's
documented schema, (b) a content-like field with a non-empty value, or (c) a string
value that looks like source code or tool output. It prints counts only — never a
path, username, field value or session id.

Fold output blobs (``obs/``) and carry/checkpoint snapshots are documented stores of
text, not logs, so they are counted (files and bytes) but not content-scanned.

On Windows the same report includes an ``icacls`` check of the state directory. Only
principal *types* (owner, system, administrators, users, everyone,
authenticated_users, other) and their rights masks are shown; names never are.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cfxd.hookclient import resolve_state_dir

# Documented top-level schema per log file (cfxd/*.py receipt writers).
_ALLOWED: dict[str, frozenset[str]] = {
    "events": frozenset(
        {
            "ts",
            "event",
            "tool_name",
            "session_id",
            "latency_ms",
            "outcome",
            "reason",
            "bytes_in",
            "bytes_out",
        }
    ),
    "folds": frozenset(
        {"ts", "session_id", "tool_name", "field", "kind", "bytes_in", "bytes_out", "obs_id"}
    ),
    "briefs": frozenset(
        {
            "ts",
            "session_id",
            "tool_use_id",
            "subagent_type",
            "decision",
            "skip_reason",
            "prompt_bytes",
            "packet_bytes",
            "counts",
            "packet_sha16",
            "event",
            "brief_v",
            "target_model",
            "model_raw",
            "model_source",
            "budget_bytes",
            "section_bytes",
            "items",
            "intent",
            "fallback",
        }
    ),
    "bound": frozenset({"ts", "event", "key", "command_len", "tail_lines", "session_id"}),
    "carry": frozenset(
        {"ts", "session_id", "event", "decision", "reason", "bytes", "counts", "sha16"}
    ),
    "checkpoint": frozenset(
        {
            "ts",
            "session_id",
            "event",
            "outcome",
            "bytes",
            "age_s",
            "counts",
            "note_chars",
            "dropped",
            "daemon_files",
            "transcript_files",
            "merged_files",
            "source",
        }
    ),
    "coach": frozenset(
        {"ts", "event", "session_id", "context_tokens", "turns", "model", "outcome"}
    ),
    "resume": frozenset(
        {"ts", "session_id", "event", "outcome", "context_tokens", "est_write_usd", "model"}
    ),
}
_ROLES = {f"{name}.jsonl": name for name in _ALLOWED}
_CONTENT_KEYS = frozenset(
    {
        "content",
        "text",
        "excerpt",
        "excerpts",
        "output",
        "tool_output",
        "tool_result",
        "source",
        "code",
        "prompt",
        "command",
        "stdout",
        "stderr",
        "body",
        "path",
        "cwd",
        "file_path",
        "transcript",
        "diff",
        "patch",
        "message",
    }
)
_CODE = re.compile(
    r"(?m)(?:^|\n)\s*(?:def |class |import |from \S+ import |package |public |private "
    r"|func |fn |#include |diff --git |\+\+\+ |--- |Traceback \(most recent call last\)|\$ )"
    r"|\b(?:function|const|let|var)\s+[A-Za-z_$]|\bif\s*\([^)]*\)\s*\{"
)
_ACE = re.compile(r"^\s*(.+?):((?:\([^)]*\))+)\s*$")
_GRANTING = frozenset({"F", "M", "RX", "R", "W", "X"})
_BROAD = frozenset({"everyone", "users", "authenticated_users"})


def _base_name(path: Path) -> str:
    """``events.jsonl.1`` -> ``events.jsonl``."""
    return re.sub(r"\.\d+$", "", path.name)


def _role(path: Path) -> str:
    return _ROLES.get(_base_name(path), "unrecognized")


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _strings(item)]
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    return []


def _has_text(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (dict, list)):
        return bool(value)
    return False


def _content_field(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in _CONTENT_KEYS and _has_text(item):
                return True
            if _content_field(item):
                return True
    elif isinstance(value, list):
        return any(_content_field(item) for item in value)
    return False


def _source_like(text: str) -> bool:
    # Short strings are ids and decisions; require 80+ chars for a single-line marker.
    return "\n" in text or (len(text) >= 80 and _CODE.search(text) is not None)


def _empty_role() -> dict[str, int]:
    return {
        "files": 0,
        "lines": 0,
        "malformed": 0,
        "lines_with_unknown_fields": 0,
        "lines_with_content_fields": 0,
        "lines_with_source_like_values": 0,
        "flagged_lines": 0,
    }


def audit_jsonl(state_dir: Path) -> dict[str, Any]:
    """Scan every JSONL log under ``state_dir``; return per-role and total counts."""
    by_role: dict[str, dict[str, int]] = {}
    for path in sorted(state_dir.rglob("*.jsonl*")):
        if not path.is_file():
            continue
        role = _role(path)
        row = by_role.setdefault(role, _empty_role())
        row["files"] += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            row["malformed"] += 1
            continue
        allowed = _ALLOWED.get(role, frozenset())
        for line in text.splitlines():
            if not line.strip():
                continue
            row["lines"] += 1
            try:
                obj = json.loads(line)
            except ValueError:
                row["malformed"] += 1
                row["flagged_lines"] += 1
                continue
            if not isinstance(obj, dict):
                row["malformed"] += 1
                row["flagged_lines"] += 1
                continue
            unknown = any(key not in allowed for key in obj)
            content = _content_field(obj)
            source = any(_source_like(value) for value in _strings(obj))
            if unknown:
                row["lines_with_unknown_fields"] += 1
            if content:
                row["lines_with_content_fields"] += 1
            if source:
                row["lines_with_source_like_values"] += 1
            if unknown or content or source:
                row["flagged_lines"] += 1

    totals = _empty_role()
    for row in by_role.values():
        for key in totals:
            totals[key] += row[key]
    return {"by_role": dict(sorted(by_role.items())), "totals": totals}


def audit_files(state_dir: Path) -> dict[str, dict[str, int]]:
    """Count the non-JSONL files by kind (documented text stores vs other)."""
    out: dict[str, dict[str, int]] = {}
    for path in sorted(state_dir.rglob("*")):
        if not path.is_file() or _base_name(path).endswith(".jsonl"):
            continue
        parts = path.relative_to(state_dir).parts
        if parts[0] == "obs":
            kind = "fold_blob"
        elif parts[0] == "carry":
            kind = "carry_snapshot"
        elif parts[0] == "checkpoint":
            kind = "checkpoint_snapshot"
        else:
            kind = "other"
        row = out.setdefault(kind, {"files": 0, "bytes": 0})
        row["files"] += 1
        row["bytes"] += path.stat().st_size
    return dict(sorted(out.items()))


def _principal_type(name: str, user: str) -> str:
    low = name.strip().lower()
    short = low.rsplit("\\", 1)[-1]
    if user and short == user.lower():
        return "owner"
    if short == "everyone":
        return "everyone"
    if short == "users":
        return "users"
    if short == "authenticated users":
        return "authenticated_users"
    if short == "system":
        return "system"
    if short == "administrators":
        return "administrators"
    return "other"


def _parse_icacls(text: str, state_dir: Path, user: str) -> list[dict[str, Any]]:
    """ACE rows as ``{type, mask, granting}``; the principal name is discarded."""
    prefix = str(state_dir.resolve())
    entries: list[dict[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith(prefix.lower()):
            stripped = stripped[len(prefix) :].strip()
        match = _ACE.match(stripped)
        if match is None:
            continue
        mask = match.group(2)
        tokens = re.findall(r"\(([^)]*)\)", mask)
        entries.append(
            {
                "type": _principal_type(match.group(1), user),
                "mask": mask,
                "granting": any(token in _GRANTING for token in tokens),
            }
        )
    return sorted(entries, key=lambda entry: (str(entry["type"]), str(entry["mask"])))


def audit_acl(
    state_dir: Path,
    platform_name: str | None = None,
    runner: Callable[..., Any] | None = None,
    user: str | None = None,
) -> dict[str, Any]:
    """PASS/FAIL/UNKNOWN from ``icacls``; only principal types and masks are returned."""
    if (platform_name or os.name) != "nt":
        return {"status": "UNKNOWN", "reason": "not-windows", "entries": []}
    run = runner or subprocess.run
    try:
        proc = run(["icacls", str(state_dir)], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return {"status": "UNKNOWN", "reason": "icacls-unavailable", "entries": []}
    if proc.returncode != 0:
        return {"status": "UNKNOWN", "reason": "icacls-failed", "entries": []}
    who = user if user is not None else (os.environ.get("USERNAME") or getpass.getuser())
    entries = _parse_icacls(proc.stdout, state_dir, who)
    if not entries:
        return {"status": "UNKNOWN", "reason": "no-ace-parsed", "entries": []}
    exposed = [entry for entry in entries if entry["type"] in _BROAD and bool(entry["granting"])]
    return {
        "status": "FAIL" if exposed else "PASS",
        "reason": "broad-group-access" if exposed else None,
        "entries": entries,
    }


def audit(state_dir: Path) -> dict[str, Any]:
    """JSONL content scan, non-JSONL file census and the ACL check."""
    return {
        "jsonl": audit_jsonl(state_dir),
        "files": audit_files(state_dir),
        "acl": audit_acl(state_dir),
    }


def render(report: dict[str, Any]) -> str:
    """Plain-text aggregates: counts only, then the ACL verdict with types only."""
    totals = report["jsonl"]["totals"]
    lines = [
        "cfxd state audit (JSONL logs, aggregates only)",
        f"Files: {totals['files']}   Lines: {totals['lines']:,}   "
        f"Malformed: {totals['malformed']:,}",
        f"Lines with unknown fields: {totals['lines_with_unknown_fields']:,}",
        f"Lines with content-like fields: {totals['lines_with_content_fields']:,}",
        f"Lines with source-like values: {totals['lines_with_source_like_values']:,}",
        f"Flagged lines: {totals['flagged_lines']:,}",
    ]
    for role, row in report["jsonl"]["by_role"].items():
        lines.append(f"  {role:<14}{row['lines']:>7,} lines, {row['flagged_lines']:>5,} flagged")
    for kind, row in report["files"].items():
        lines.append(f"  {kind:<18}{row['files']:>5} files, {row['bytes']:>10,} bytes")
    acl = report["acl"]
    lines.append(f"ACL: {acl['status']}" + (f" ({acl['reason']})" if acl["reason"] else ""))
    for entry in acl["entries"]:
        lines.append(f"  {entry['type']:<22}{entry['mask']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="cfxd state content and ACL audit (E-7).")
    parser.add_argument("--state-dir", help="cfxd state dir (default: CFX_HOME or ~/.cfx/daemon)")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = parser.parse_args(argv)
    report = audit(resolve_state_dir(args.state_dir))
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
