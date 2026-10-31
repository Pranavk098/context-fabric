"""Golden tests for the carry scorer's equal-budget (``--cap``) comparison.

Both synthetic transcripts are fixed and every expected number below was computed by
hand from the renderer's exact output, not by calling the scorer to generate it:

* ``_small`` has three edited files and no other sections. A rendered line is
  ``- /p/a.py | edited`` (18 bytes); the block is header 46 + prologue 125 + files
  header 36 + 3*18 + close 23 + 6 newlines = 290 bytes for both renderers.
* ``_overflow`` has 19 edited files whose paths are exactly 160 bytes, so each
  rendered line is 171 bytes. With ``header 46 + prologue 125 + files header 36 +
  close 23`` and ``n + 3`` newlines the block is ``233 + 172*n`` bytes. At a
  3,000-byte cap v1 drops its whole file section (196 bytes, 0 restored) and v2 keeps
  16 lines (233 + 172*16 = 2,985 bytes); at 4,000 both fit all 19 (3,501 bytes).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import carry_fidelity_score as score  # noqa: E402

_USAGE = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100}


def _use(uid: str, name: str, inp: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "id": f"m-{uid}",
            "model": "claude-sonnet-5-5",
            "usage": _USAGE,
            "content": [{"type": "tool_use", "id": uid, "name": name, "input": inp}],
        },
    }


def _result(uid: str) -> dict[str, Any]:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": uid, "content": "ok"}],
        },
    }


def _write_transcript(root: Path, paths: list[str]) -> None:
    rows: list[dict[str, Any]] = []
    for i, path in enumerate(paths):
        rows.append(
            _use(f"e{i}", "Edit", {"file_path": path, "old_string": "a", "new_string": "b"})
        )
        rows.append(_result(f"e{i}"))
    rows.append(
        {
            "type": "user",
            "isCompactSummary": True,
            "message": {"role": "user", "content": "No file was named in this summary."},
        }
    )
    proj = root / "proj"
    proj.mkdir(parents=True)
    (proj / "sess.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )


def _long_path(i: int) -> str:
    return "/p/" + "a" * 152 + f"{i:02d}" + ".py"


def test_small_golden_same_bytes_at_both_caps(tmp_path: Path) -> None:
    _write_transcript(tmp_path, ["/p/a.py", "/p/b.py", "/p/c.py"])
    data = score.collect(tmp_path, "all", caps=(3_000, 4_000))

    assert data["events"] == 1
    assert data["dropped"] == 3
    assert data["v1_dropped"] == 3 and data["v2_dropped"] == 3
    for cap in ("3000", "4000"):
        row = data["equal"][cap]
        assert row["v1_dropped"] == 3
        assert row["v2_dropped"] == 3
        assert row["bytes_v1"] == 290
        assert row["bytes_v2"] == 290
        assert row["v1_rate"] == 1.0
        assert row["v2_rate"] == 1.0


def test_overflow_golden_cap_changes_restores(tmp_path: Path) -> None:
    _write_transcript(tmp_path, [_long_path(i) for i in range(19)])
    data = score.collect(tmp_path, "all", caps=(3_000, 4_000))

    assert data["events"] == 1
    assert data["dropped"] == 19
    assert data["v1_dropped"] == 0 and data["v2_dropped"] == 19

    at_3k = data["equal"]["3000"]
    assert at_3k["v1_dropped"] == 0
    assert at_3k["v2_dropped"] == 16
    assert at_3k["bytes_v1"] == 196
    assert at_3k["bytes_v2"] == 2_985
    assert at_3k["v1_per_kb"] == 0.0
    assert round(at_3k["v2_per_kb"], 2) == round(16 * 1024 / 2985, 2)

    at_4k = data["equal"]["4000"]
    assert at_4k["v1_dropped"] == 19
    assert at_4k["v2_dropped"] == 19
    assert at_4k["bytes_v1"] == 3_501
    assert at_4k["bytes_v2"] == 3_501


def test_single_cap_reports_only_that_cap(tmp_path: Path) -> None:
    _write_transcript(tmp_path, ["/p/a.py", "/p/b.py", "/p/c.py"])
    data = score.collect(tmp_path, "all", caps=(3_000,))

    assert data["caps"] == [3_000]
    assert list(data["equal"]) == ["3000"]
    text = score.render(data)
    assert "| 3,000 | carry v1 |" in text
    assert "| 4,000 |" not in text
