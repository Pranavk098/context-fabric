"""Golden-transcript accounting tests (M-8).

The fixed synthetic Claude Code transcripts under ``tests/golden/`` are replayed through
the accounting code with hand-computed expected values. Every number below shows its
arithmetic, so a change in the modules under test has to prove itself against the fixture
rather than against a regenerated snapshot.

Fixture (parent + one child, both <= 40 lines):

* parent: skill/connector prefix, reads a.py and c.py, edits b.py, runs ``pytest   -q``,
  then spawns one child (``Agent`` -> ``toolUseResult.agentId`` = ``kid1``), and has two
  extra deferred-tool / instruction deltas after the spawn (the MCP churn rule).
* child: duplicate ``message.id`` usage lines, a re-read of a.py (git-bash ``/c/`` path)
  and b.py, a re-run of ``pytest  -q``, a read of a new file, then an edit.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "scripts"))

import brief_replay_score as replay  # noqa: E402
import gate1_rediscovery as gate1  # noqa: E402

from cfxd.audit import _weighted  # noqa: E402
from cfxd.diet import diet  # noqa: E402
from cfxd.routing import _DEFAULT_PRICES, _load_prices  # noqa: E402
from cfxd.usage_dedupe import MessageUsage  # noqa: E402
from cfxd.waste import waste  # noqa: E402

GOLDEN = Path(__file__).with_name("golden")
CHILD = GOLDEN / "proj" / "sess-golden" / "subagents" / "agent-kid1.jsonl"

# Prices: claude-haiku-4-5 is $1/M input, $5/M output, $1.25/M cache write, $0.10/M
# cache read, with no 100k tier.
# Child turn k1's final (last) usage line is 2000 input / 200 output.
COST_K1 = (2000 * 1.0 + 200 * 5.0) / 1_000_000  # $0.003
# Child turns k2..k5 are 1000 input / 100 output each.
COST_K = (1000 * 1.0 + 100 * 5.0) / 1_000_000  # $0.0015
CHILD_COST = COST_K1 + 4 * COST_K  # $0.009


def _rows(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _usage_lines(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if row.get("type") == "assistant"
        and isinstance(row.get("message"), dict)
        and isinstance(row["message"].get("usage"), dict)
    ]


def test_usage_dedupe_counts_each_message_id_once() -> None:
    rows = _rows(CHILD)
    # Six usage-bearing lines, five message ids: k1 is written twice (tool_use on the
    # first line, final usage on the second).
    assert len(_usage_lines(rows)) == 6
    dedupe = MessageUsage(rows)
    counts = [dedupe.count_here(row["message"]) for row in _usage_lines(rows)]
    # Only the LAST line of k1 counts; every other message has one line.
    assert counts == [False, True, True, True, True, True]

    first, last = _usage_lines(rows)[:2]
    first_msg, last_msg = first["message"], last["message"]
    # final_usage returns the last line's usage for a repeated id...
    assert dedupe.final_usage(first_msg, first_msg["usage"]) == last_msg["usage"]
    assert dedupe.tool_uses(first_msg) == 1  # the Read block lives on the first line only

    fresh = MessageUsage(rows)
    deduped = 0.0
    for row in _usage_lines(rows):
        msg = row["message"]
        if fresh.count_here(msg):
            deduped += _weighted(fresh.final_usage(msg, msg["usage"]))
    # Per-message weights (input + 5*output): k1 2000+5*200 = 3000, k2..k5 1000+500 = 1500.
    assert deduped == pytest.approx(3000 + 4 * 1500)  # 9000
    # Summing every line instead would charge k1 twice: 1500 + 3000 + 4*1500 = 10500.
    naive = sum(_weighted(row["message"]["usage"]) for row in _usage_lines(rows))
    assert naive == pytest.approx(10500)


def test_gate1_join_and_rediscovery_counts() -> None:
    data = gate1.collect(GOLDEN, _load_prices(_DEFAULT_PRICES))
    stats = data["stats"]
    # One Agent call, joined through toolUseResult.agentId to agent-kid1.jsonl.
    assert stats["agent_calls"] == 1
    assert stats["unjoinable_no_agentid_in_result"] == 0
    assert stats["unjoinable_no_child_transcript"] == 0
    assert stats["unjoinable_empty_child"] == 0
    assert stats["child_files_not_referenced_by_parent"] == 0
    # Parent read a.py and c.py and edited b.py before its first spawn.
    assert data["sessions"]["sess-golden"]["read_before_first"] == 3

    (kid,) = data["children"]
    assert kid["session"] == "sess-golden"
    assert kid["background"] is False
    # 5 distinct message ids; the duplicate k1 line must not create a 6th turn.
    assert kid["turns"] == 5
    assert kid["tool_calls"] == 5
    # Re-reads: a.py and b.py (parent knew both). Re-run: "pytest  -q" == "pytest   -q".
    assert kid["rereads"] == 2
    assert kid["reruns"] == 1
    assert kid["reread_files"] == 2
    assert kid["child_files"] == 3  # a.py, new.py, b.py
    assert kid["parent_known"] == 3
    assert kid["has_edit"] is True
    # Exploration ends at the first edit (k5): a.py, new.py, b.py, two of them known.
    assert kid["explore_files"] == 3
    assert kid["explore_files_known"] == 2
    assert kid["replayed"] == 0
    assert kid["cost"] == pytest.approx(CHILD_COST)  # 0.003 + 4*0.0015 = 0.009
    # k1 final 2000+200 = 2200 tokens; k2..k5 (1000+100) each -> 4400; total 6600.
    assert kid["tokens"] == 6600

    # Lower: each redundant call is the only call in its turn, so add those turns in full:
    # k1 (re-read) + k2 (re-run) + k4 (re-read) = 0.003 + 0.0015 + 0.0015 = 0.006.
    assert kid["lower"] == pytest.approx(COST_K1 + 2 * COST_K)
    # Upper: full cost of those turns + their 400-char result (100 tokens) carried as one
    # cache write and a cache read on every later turn of the five:
    #   k1, 4 later: 0.003   + 100*(1.25 + 0.10*4)/1e6 = 0.003165
    #   k2, 3 later: 0.0015  + 100*(1.25 + 0.10*3)/1e6 = 0.001655
    #   k4, 1 later: 0.0015  + 100*(1.25 + 0.10*1)/1e6 = 0.001635
    #   total = 0.006455, below the 0.009 child cap.
    upper = (
        COST_K1
        + 100 * (1.25 + 0.10 * 4) / 1_000_000
        + COST_K
        + 100 * (1.25 + 0.10 * 3) / 1_000_000
        + COST_K
        + 100 * (1.25 + 0.10 * 1) / 1_000_000
    )
    assert kid["upper"] == pytest.approx(upper)
    # Exploration = k1..k4 = 0.003 + 3*0.0015 = 0.0075.
    assert kid["explore_cost"] == pytest.approx(COST_K1 + 3 * COST_K)

    summary = gate1.summarise(data["children"])
    assert summary["redundant_call_share"] == pytest.approx(3 / 5)  # 3 redundant of 5 calls
    assert summary["explore_share"] == pytest.approx(0.0075 / 0.009)


def test_brief_replay_coverage_and_precision() -> None:
    data = replay.collect(GOLDEN, _load_prices(_DEFAULT_PRICES))
    (kid,) = data["children"]
    # Parent knew a.py, c.py (read) and b.py (edited) before the spawn; all three fit
    # the packet (haiku budget, max 8 files), so packet_files = 3.
    assert kid["packet_files"] == 3
    # The child reads a.py, new.py, b.py; only a.py and b.py were known -> 2 redundant,
    # and both are in the packet -> coverage 2/2.
    assert kid["redundant"] == 2
    assert kid["covered"] == 2
    # Precision counts every packet file the child touched: a.py and b.py -> 2 of 3.
    assert kid["packet_hit"] == 2
    assert kid["cost"] == pytest.approx(CHILD_COST)
    # Only covered reads contribute: k1 and k4, 100 tokens each -> 200.
    assert kid["result_tokens"] == pytest.approx(200)
    # Lower: covered turns in full: k1 0.003 + k4 0.0015 = 0.0045.
    assert kid["lower"] == pytest.approx(COST_K1 + COST_K)
    # Upper: k1 (0.003165) + k4 (0.001635) = 0.0048.
    assert kid["upper"] == pytest.approx(
        COST_K1 + 100 * (1.25 + 0.10 * 4) / 1_000_000 + COST_K + 100 * (1.25 + 0.10 * 1) / 1_000_000
    )
    assert kid["packet_bytes"] > 0  # exact bytes are rendering-defined, not arithmetic

    summary = replay.summarise(data["children"])
    assert summary["coverage"] == pytest.approx(2 / 2)
    assert summary["precision"] == pytest.approx(2 / 3)
    assert summary["lower_share"] == pytest.approx(0.0045 / 0.009)
    assert summary["upper_share"] == pytest.approx(0.0048 / 0.009)


def test_mcp_churn_rule_counts_only_repeats() -> None:
    report = waste(GOLDEN, "all")
    churn = report["mcp_churn"]
    # Two deferred-tool deltas and two instruction deltas per transcript; the first of
    # each kind is needed context, so only the second of each is churn.
    assert churn["deferred_attachments"] == 2
    assert churn["instruction_attachments"] == 2
    assert churn["attachments"] == 4
    assert churn["sessions_with_churn"] == 1
    # Bytes: json(["mcp__srv__tool_c"]) = 20, and the larger instruction field is
    # json(["bye!!"]) = 9 (json(["srv"]) is 7). 20 + 9 = 29 bytes -> 29/4 = 7.25 tokens,
    # rounded to one decimal = 7.2.
    assert churn["est_tokens"] == pytest.approx(7.2)


def test_diet_delta_arithmetic() -> None:
    report = diet(GOLDEN, "all")
    assert report["sessions"] == 1
    assert report["listing_not_found"] == 0
    # p1 is written twice; the duplicate usage line must not add a turn.
    assert report["turns"] == 5

    # Prefix bytes: skill bullets "- alpha:one: A" = 14 B and "- beta:two: B" = 13 B;
    # connector srv lists two tool names (2 * 16 = 32 B) plus "hello" (5 B) = 37 B.
    assert report["prefix_tokens_estimated_median"] == pytest.approx((14 + 13 + 37) / 4)  # 16.0
    assert report["first_turn_tokens_median"] == 1000  # p1's input_tokens
    assert report["total_median_bytes"] == pytest.approx(14 + 13 + 37)  # 64.0

    # Never used: skill beta:two (13 B) and connector srv (37 B) = 50 B per turn.
    # 50/4 = 12.5 tokens per turn * 5 turns = 62.5 tokens less re-read.
    assert report["effect"]["per_turn_tokens"] == pytest.approx(12.5)
    assert report["effect"]["turns"] == 5
    assert report["effect"]["total_tokens"] == pytest.approx(62.5)

    (plugin,) = report["never_used"]["plugins"]
    assert plugin["plugin"] == "beta"
    assert plugin["skills"] == ["beta:two"]
    assert plugin["median_bytes"] == 13.0
    assert plugin["per_turn_tokens"] == pytest.approx(3.2)  # 13/4 = 3.25, rounded to 1 dp
    (connector,) = report["never_used"]["connectors"]
    assert connector["name"] == "srv"
    assert connector["tools"] == 2
    assert connector["tool_names_bytes"] == 32.0
    assert connector["instructions_bytes"] == 5.0
    assert connector["median_bytes"] == 37.0
    assert connector["per_turn_tokens"] == pytest.approx(9.2)  # 37/4 = 9.25, rounded to 1 dp
    (used,) = report["used"]["plugins"]
    assert used["plugin"] == "alpha"
    assert used["skills"] == ["alpha:one"]
    assert used["median_bytes"] == 14.0
