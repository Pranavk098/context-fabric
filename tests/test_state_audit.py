"""Golden test for E-7 state audit counts on a fixed synthetic state dir.

Clean transcript: four documented log files with five lines total and one documented
text store (``obs/``) plus a carry snapshot and an unrelated file.

Hand count for the clean dir: 4 JSONL files / 5 lines / 0 malformed / 0 unknown
fields / 0 content fields / 0 source-like values / 0 flagged. Census: fold_blob 1
file 5 bytes ("hello"), carry_snapshot 1 file 2 bytes ("{}"), other 1 file 5 bytes
("abcde").

Dirty transcript adds three bad lines to ``events.jsonl``: two with undocumented
content fields (``tool_output``, ``text``) whose values look like tool output/source,
and one non-JSON line. Hand count: 4 lines, 1 malformed, 2 unknown-field lines,
2 content-field lines, 2 source-like lines, 3 flagged lines.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import state_audit as s  # noqa: E402


def _write_jsonl(path: Path, rows: list[Any]) -> None:
    path.write_text(
        "\n".join(json.dumps(row) if not isinstance(row, str) else row for row in rows) + "\n",
        encoding="utf-8",
    )


def _clean(tmp_path: Path) -> None:
    _write_jsonl(
        tmp_path / "events.jsonl",
        [
            {
                "ts": "2026-10-31T10:00:00+00:00",
                "event": "PreToolUse",
                "tool_name": "Read",
                "session_id": "s1",
                "latency_ms": 1.5,
                "outcome": "handled",
                "bytes_in": 10,
                "bytes_out": 2,
            },
            {
                "ts": "2026-10-31T10:00:01+00:00",
                "event": "PostToolUse",
                "tool_name": "Bash",
                "session_id": None,
                "latency_ms": 2.5,
                "outcome": "handled",
                "bytes_in": 11,
                "bytes_out": 3,
            },
        ],
    )
    _write_jsonl(
        tmp_path / "folds.jsonl",
        [
            {
                "ts": "2026-10-31T10:00:02+00:00",
                "session_id": "s1",
                "tool_name": "Bash",
                "field": "output",
                "kind": "log",
                "bytes_in": 100,
                "bytes_out": 20,
                "obs_id": "abc123",
            }
        ],
    )
    _write_jsonl(
        tmp_path / "briefs.jsonl",
        [
            {
                "ts": "2026-10-31T10:00:03+00:00",
                "session_id": "s1",
                "decision": "rendered",
                "prompt_bytes": 10,
                "packet_bytes": 20,
                "counts": {"files": 2, "excerpts": 0, "folds": 0, "commands": 1},
                "packet_sha16": "0123456789abcdef",
                "items": [{"p": "0123456789abcdef", "r": ["recent_edit"], "x": [], "d": False}],
                "intent": {"user": 0, "assistant": 0, "delegation": 1, "deduped": 0},
            }
        ],
    )
    _write_jsonl(
        tmp_path / "carry.jsonl",
        [
            {
                "ts": "2026-10-31T10:00:04+00:00",
                "session_id": "s1",
                "event": "PreCompact",
                "decision": "saved",
                "reason": None,
                "bytes": 12,
                "counts": {"files": 1},
                "sha16": "0123456789abcdef",
            }
        ],
    )
    (tmp_path / "obs").mkdir()
    (tmp_path / "obs" / "a.txt").write_bytes(b"hello")
    (tmp_path / "carry").mkdir()
    (tmp_path / "carry" / "snap.json").write_bytes(b"{}")
    (tmp_path / "token").write_bytes(b"abcde")


def test_golden_clean_state_counts(tmp_path: Path) -> None:
    _clean(tmp_path)
    report = s.audit_jsonl(tmp_path)
    totals = report["totals"]

    assert totals == {
        "files": 4,
        "lines": 5,
        "malformed": 0,
        "lines_with_unknown_fields": 0,
        "lines_with_content_fields": 0,
        "lines_with_source_like_values": 0,
        "flagged_lines": 0,
    }
    assert report["by_role"]["events"]["files"] == 1
    assert report["by_role"]["events"]["lines"] == 2
    assert report["by_role"]["briefs"]["lines"] == 1

    assert s.audit_files(tmp_path) == {
        "carry_snapshot": {"files": 1, "bytes": 2},
        "fold_blob": {"files": 1, "bytes": 5},
        "other": {"files": 1, "bytes": 5},
    }


def test_golden_content_lines_are_counted_not_printed(tmp_path: Path) -> None:
    _clean(tmp_path)
    _write_jsonl(
        tmp_path / "events.jsonl",
        [
            {"ts": "2026-10-31T10:00:00+00:00", "event": "PreToolUse", "latency_ms": 1.5},
            {
                "ts": "2026-10-31T10:00:01+00:00",
                "event": "PostToolUse",
                "tool_output": "$ ls\nfoo.py",
            },
            {"ts": "2026-10-31T10:00:02+00:00", "event": "Read", "text": "def f():\n    return 1"},
            "not json",
        ],
    )
    report = s.audit_jsonl(tmp_path)
    totals = report["totals"]

    assert totals["lines"] == 7
    assert totals["malformed"] == 1
    assert totals["lines_with_unknown_fields"] == 2
    assert totals["lines_with_content_fields"] == 2
    assert totals["lines_with_source_like_values"] == 2
    assert totals["flagged_lines"] == 3

    text = s.render(
        {
            "jsonl": report,
            "files": s.audit_files(tmp_path),
            "acl": {"status": "UNKNOWN", "reason": "not-windows", "entries": []},
        }
    )
    assert "Flagged lines: 3" in text
    assert "foo.py" not in text and "return 1" not in text
    assert str(tmp_path) not in text


def _proc(stdout: str, returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


def _runner(stdout: str, returncode: int = 0) -> Any:
    def run(*args: Any, **kwargs: Any) -> SimpleNamespace:
        return _proc(stdout, returncode)

    return run


def test_acl_clean_is_pass_and_hides_names(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    output = (
        f"{root} NT AUTHORITY\\SYSTEM:(OI)(CI)(F)\n"
        "   DESKTOP-HOST\\owner:(OI)(CI)(F)\n"
        "   BUILTIN\\Administrators:(OI)(CI)(F)\n"
        "\n"
        "Successfully processed 1 files; Failed processing 0 files\n"
    )
    acl = s.audit_acl(root, platform_name="nt", runner=_runner(output), user="owner")

    assert acl["status"] == "PASS"
    assert acl["reason"] is None
    assert [entry["type"] for entry in acl["entries"]] == [
        "administrators",
        "owner",
        "system",
    ]
    assert "DESKTOP-HOST" not in json.dumps(acl)
    assert "owner:" not in json.dumps(acl)


def test_acl_broad_group_access_is_fail(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    output = (
        f"{root} BUILTIN\\Users:(OI)(CI)(RX)\n"
        "   NT AUTHORITY\\Authenticated Users:(OI)(CI)(R)\n"
        "   DESKTOP-HOST\\owner:(OI)(CI)(F)\n"
        "   Everyone:(OI)(CI)(N)\n"
    )
    acl = s.audit_acl(root, platform_name="nt", runner=_runner(output), user="owner")

    assert acl["status"] == "FAIL"
    assert acl["reason"] == "broad-group-access"
    types = [entry["type"] for entry in acl["entries"]]
    assert types == ["authenticated_users", "everyone", "owner", "users"]


def test_acl_unknown_cases(tmp_path: Path) -> None:
    assert s.audit_acl(tmp_path, platform_name="posix")["reason"] == "not-windows"
    assert (
        s.audit_acl(tmp_path, platform_name="nt", runner=_runner("", returncode=1))["reason"]
        == "icacls-failed"
    )
    assert (
        s.audit_acl(tmp_path, platform_name="nt", runner=_runner("nothing here"))["reason"]
        == "no-ace-parsed"
    )

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise OSError("no icacls")

    assert (
        s.audit_acl(tmp_path, platform_name="nt", runner=broken)["reason"] == "icacls-unavailable"
    )
