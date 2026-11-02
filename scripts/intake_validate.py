"""P10 intake validator: one tester submission in, an accepted row or a reason code out.

A payload is one JSON record, ``{"share": <cf-share/2>, "feedback": <form fields>}`` (P10:
"one cf-share/2 payload plus the feedback form"). Validation never repairs: a good record is
appended to ``accepted.jsonl`` byte for byte, a bad one is quarantined with a reason code and
kept out of the accepted log. The quarantine line holds the reason and the payload hash only —
never the payload, so a record that smuggled in free text cannot be stored.

Aggregates only: the schema below has no field for a path, prompt, command or transcript text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from cfxd.share import _VERSION, FIELDS, METRIC, SCHEMA, validate_share

# Quarantine reason codes, in reporting order.
REASONS = ("json", "shape", "schema", "version", "range", "duplicate")

# Controlled vocabularies. The issue form offers exactly these words.
OS_FAMILIES = ("macos", "linux", "windows")
TASK_TYPES = ("feature", "bugfix", "refactor", "exploration", "other")
REPO_TYPES = ("own", "employer", "open-source")
FRICTION = ("install", "comprehension", "trust", "performance")

FEEDBACK_FIELDS = (
    "developer",
    "os_family",
    "task_type",
    "repo_type",
    "brief_enabled",
    "brief_sessions",
    "carry_enabled",
    "carry_sessions",
    "friction",
    "consent_quote_free_text",
)

# Assigned index, never a name: dev-01, dev-002.
_DEVELOPER = re.compile(r"dev-[0-9]{1,3}")

# A gross plausibility bound, not a style check: children per session, days in a window.
MAX_CHILDREN_PER_SESSION = 64
MAX_PERIOD_DAYS = 366


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _canonical(record: Any) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")


def payload_digest(path: Path) -> str:
    """SHA-256 of the payload's canonical form (raw bytes when it does not parse)."""
    raw = path.read_bytes()
    try:
        return hashlib.sha256(_canonical(json.loads(raw))).hexdigest()
    except ValueError:
        return hashlib.sha256(raw).hexdigest()


def check_record(record: Any) -> str | None:
    """Return a reason code, or ``None`` when the record is valid. Never repairs.

    ``range`` covers a share payload that fails cf-share/2's own type or value checks as
    well as an intake bound; both mean "a number is not believable", not "a key is missing".
    """
    if not isinstance(record, dict) or set(record) != {"share", "feedback"}:
        return "shape"
    share, feedback = record["share"], record["feedback"]
    if not isinstance(share, dict) or set(share) != set(FIELDS):
        return "shape"
    if share["schema"] != SCHEMA:
        return "schema"
    if not isinstance(share["plugin_version"], str) or not _VERSION.fullmatch(
        share["plugin_version"]
    ):
        return "version"
    if share["metric"] != METRIC:
        return "version"
    if not isinstance(feedback, dict) or set(feedback) != set(FEEDBACK_FIELDS):
        return "shape"
    if not isinstance(feedback["developer"], str) or not _DEVELOPER.fullmatch(
        feedback["developer"]
    ):
        return "shape"
    if (
        feedback["os_family"] not in OS_FAMILIES
        or feedback["task_type"] not in TASK_TYPES
        or feedback["repo_type"] not in REPO_TYPES
    ):
        return "shape"
    for key in ("brief_enabled", "carry_enabled", "consent_quote_free_text"):
        if not isinstance(feedback[key], bool):
            return "shape"
    friction = feedback["friction"]
    if not isinstance(friction, dict) or set(friction) != set(FRICTION):
        return "shape"
    if not all(_is_count(value) for value in friction.values()):
        return "range"
    for key in ("brief_sessions", "carry_sessions"):
        if not _is_count(feedback[key]):
            return "range"
    try:
        validate_share(share)
    except ValueError:
        return "range"
    if feedback["brief_sessions"] > share["sessions"]:
        return "range"
    if feedback["carry_sessions"] > share["sessions"]:
        return "range"
    if share["children"] > share["sessions"] * MAX_CHILDREN_PER_SESSION:
        return "range"
    if share["period_days"] > MAX_PERIOD_DAYS:
        return "range"
    return None


def load_record(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Parse one payload file; returns ``(record, None)`` or ``(None, reason)``."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "json"
    reason = check_record(record)
    if reason is not None:
        return None, reason
    assert isinstance(record, dict)  # check_record accepted it
    return record, None


def _hashes(path: Path) -> set[str]:
    """Accepted payload hashes already on disk, so a re-sent payload stays a duplicate."""
    if not path.is_file():
        return set()
    hashes = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            hashes.add(hashlib.sha256(_canonical(json.loads(line))).hexdigest())
        except ValueError:
            continue
    return hashes


def _append(path: Path, lines: list[str]) -> None:
    if not lines:
        return
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate P10 intake payloads; quarantine the bad."
    )
    parser.add_argument("payloads", nargs="+", type=Path, help="one JSON file per submission")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("intake"),
        help="directory for accepted.jsonl and quarantine.jsonl (default: ./intake)",
    )
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    accepted_path = args.out / "accepted.jsonl"
    quarantine_path = args.out / "quarantine.jsonl"
    accepted_hashes = _hashes(accepted_path)
    accepted_lines: list[str] = []
    quarantine_lines: list[str] = []
    counts: Counter[str] = Counter()
    for index, path in enumerate(args.payloads, start=1):
        record, reason = load_record(path)
        if record is not None and hashlib.sha256(_canonical(record)).hexdigest() in accepted_hashes:
            record, reason = None, "duplicate"
        if record is not None:
            accepted_hashes.add(hashlib.sha256(_canonical(record)).hexdigest())
            accepted_lines.append(json.dumps(record, sort_keys=True))
            counts["accepted"] += 1
            print(f"payload {index} ({record['feedback']['developer']}): accepted")
            continue
        assert reason is not None
        counts[reason] += 1
        quarantine_lines.append(
            json.dumps({"reason": reason, "payload_sha256": payload_digest(path)}, sort_keys=True)
        )
        print(f"payload {index}: quarantined ({reason})")
    _append(accepted_path, accepted_lines)
    _append(quarantine_path, quarantine_lines)
    print(f"accepted: {counts['accepted']}; quarantined: {len(quarantine_lines)}")
    for reason in REASONS:
        if counts[reason]:
            print(f"  {reason}: {counts[reason]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
