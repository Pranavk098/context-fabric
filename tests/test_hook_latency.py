"""Golden test for E-6 hook latency on a fixed synthetic receipt log.

Hand-computed with ``cfxd.daemon.percentile`` (linear interpolation, index =
(n-1)*p/100), with the daemon's timeouts: 150 ms ordinary, 1300 ms when the tool is
``Agent``.

PreToolUse latencies [10, 20, 200]: p50 = 20, p95 = 20*0.1 + 200*0.9 = 182,
p99 = 20*0.02 + 200*0.98 = 196.4; 200 > 150, so 1 of 3 over timeout.

SubagentStart latencies [1500, 1000] -> sorted [1000, 1500]: p50 = 1250,
p95 = 1000 + 0.95*500 = 1475, p99 = 1000 + 0.99*500 = 1495; 1500 > 1300, so 1 of 2.

PostToolUse [5] (from the rotated log): p50 = p95 = p99 = 5, none over.

All six latencies sorted [5, 10, 20, 200, 1000, 1500]: p50 index 2.5 -> (20+200)/2 =
110; p95 index 4.75 -> 1000 + 0.75*500 = 1375; p99 index 4.95 -> 1475. Two of six
(33.33%) are over their per-call deadline. One non-JSON line and one valid receipt
with no ``latency_ms`` are excluded.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import hook_latency as h  # noqa: E402


def _write(path: Path, rows: list[Any]) -> None:
    path.write_text(
        "\n".join(json.dumps(row) if not isinstance(row, str) else row for row in rows) + "\n",
        encoding="utf-8",
    )


def _build(tmp_path: Path) -> None:
    _write(
        tmp_path / "events.jsonl",
        [
            {"event": "PreToolUse", "tool_name": "Bash", "latency_ms": 10, "outcome": "handled"},
            {"event": "PreToolUse", "tool_name": "Read", "latency_ms": 20.0, "outcome": "handled"},
            {"event": "PreToolUse", "tool_name": "Bash", "latency_ms": 200, "outcome": "error"},
            {
                "event": "SubagentStart",
                "tool_name": "Agent",
                "latency_ms": 1500,
                "outcome": "error",
            },
            {
                "event": "SubagentStart",
                "tool_name": "Agent",
                "latency_ms": 1000,
                "outcome": "handled",
            },
            "not json",
            {"event": "PostToolUse", "tool_name": None},
        ],
    )
    _write(
        tmp_path / "events.jsonl.1",
        [
            {
                "event": "PostToolUse",
                "tool_name": "Bash",
                "latency_ms": 5,
                "outcome": "handled",
                "session_id": "s1",
            }
        ],
    )


def test_golden_per_event_percentiles_and_timeout_share(tmp_path: Path) -> None:
    _build(tmp_path)
    entries, malformed = h._read_entries(tmp_path)
    report = h.summarise(entries, malformed)

    assert report["receipts"] == 6
    assert report["malformed"] == 1
    assert report["skipped"] == 1

    events = {row["event"]: row for row in report["events"]}
    assert [row["event"] for row in report["events"]] == [
        "PreToolUse",
        "SubagentStart",
        "PostToolUse",
    ]
    assert events["PreToolUse"]["count"] == 3
    assert events["PreToolUse"]["p50_ms"] == 20
    assert events["PreToolUse"]["p95_ms"] == 182
    assert events["PreToolUse"]["p99_ms"] == 196.4
    assert events["PreToolUse"]["over_timeout"] == 1
    assert events["PreToolUse"]["over_timeout_share"] == 0.3333
    assert events["PreToolUse"]["errors"] == 1

    assert events["SubagentStart"]["p50_ms"] == 1250
    assert events["SubagentStart"]["p95_ms"] == 1475
    assert events["SubagentStart"]["p99_ms"] == 1495
    assert events["SubagentStart"]["over_timeout"] == 1
    assert events["SubagentStart"]["over_timeout_share"] == 0.5

    assert events["PostToolUse"]["p50_ms"] == 5
    assert events["PostToolUse"]["over_timeout"] == 0

    total = report["total"]
    assert total["count"] == 6
    assert total["p50_ms"] == 110
    assert total["p95_ms"] == 1375
    assert total["p99_ms"] == 1475
    assert total["over_timeout"] == 2
    assert total["over_timeout_share"] == 0.3333
    assert total["errors"] == 2


def test_render_is_aggregates_only(tmp_path: Path) -> None:
    _build(tmp_path)
    entries, malformed = h._read_entries(tmp_path)
    text = h.render(h.summarise(entries, malformed))
    assert "Over handler timeout: 2 of 6 (33.3%)" in text
    assert "PreToolUse" in text
    assert "Bash" not in text and "s1" not in text
    assert str(tmp_path) not in text


def test_empty_log_has_no_division_by_zero(tmp_path: Path) -> None:
    report = h.summarise([], 0)
    assert report["total"]["over_timeout_share"] == 0.0
    assert h.render(report) == "No hook receipts."
