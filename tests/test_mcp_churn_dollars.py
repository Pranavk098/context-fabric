"""Golden test for the E-2 MCP churn report on a fixed synthetic transcript.

Hand-computed on sonnet-5-5 list prices ($0.10/Mtok cache read, $2.50/Mtok 5m cache
write). Each attachment carries one 39,998-character field, so ``_attachment_bytes``
serialises a 40,000-byte string and the token estimate is 40,000 / 4 = 10,000.

Three ``deferred_tools_delta`` lines: the first is the expected announcement, the other
two are churn. So 1 expected + 2 churn attachments = 20,000 churn tokens.
Lower bound (all reads):  20,000 x 0.10 / 1e6 = $0.0020.
Upper bound (all writes): 20,000 x 2.50 / 1e6 = $0.0500.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import mcp_churn_dollars as m  # noqa: E402

_MODEL = "claude-sonnet-5-5"
_TEXT = "x" * 39_998
_STATUS = {"input_tokens": 10, "output_tokens": 1}


def _attachment(kind: str = "deferred_tools_delta") -> dict[str, Any]:
    return {"attachment": {"type": kind, "text": _TEXT}}


def _turn(text: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "entrypoint": "sdk-cli",
        "message": {
            "id": "m1",
            "model": _MODEL,
            "usage": _STATUS,
            "content": [{"type": "text", "text": text}],
        },
    }


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _build(root: Path) -> None:
    _write(
        root / "proj" / "sess.jsonl",
        [_turn("SECRET prompt text"), _attachment(), _attachment(), _attachment()],
    )


def _prices() -> dict[str, Any]:
    return m._load_prices(m._DEFAULT_PRICES)


def test_golden_one_expected_two_churn(tmp_path: Path) -> None:
    _build(tmp_path)
    data = m.collect(tmp_path, _prices())

    (row,) = data["rows"]
    assert row["deferred"] == 3
    assert row["instructions"] == 0
    assert row["firsts"] == 1
    assert row["repeats"] == 2
    assert row["tokens"] == pytest.approx(20_000)
    assert row["family"] == "sonnet-5-5"
    assert row["headless"] is True
    assert row["flagged_old"] is True
    assert row["flagged_new"] is True
    assert row["write_usd"] == pytest.approx(0.05)
    assert row["read_usd"] == pytest.approx(0.002)

    s = m.summarise(data["rows"])
    assert s["attachments"] == 3
    assert s["firsts"] == 1
    assert s["repeats"] == 2
    assert s["tokens"] == pytest.approx(20_000)
    assert s["flagged_old"] == s["flagged_new"] == 1
    assert s["any_attachment_rule"] == 1
    assert s["more_than_one_total_rule"] == 1
    assert s["repeats_one_total"] == 2
    assert s["priced"] == 1 and s["unpriced"] == 0
    assert s["write_usd"] == pytest.approx(0.05)
    assert s["read_usd"] == pytest.approx(0.002)
    assert s["headless"] == 1 and s["interactive"] == 0


def test_report_is_aggregates_only(tmp_path: Path) -> None:
    _build(tmp_path)
    text = m.render(m.collect(tmp_path, _prices()))
    assert "Attachment lines: 3" in text
    assert "2 are repeats" in text
    assert "$0.0020" in text and "$0.0500" in text
    assert "Headless (`-p` / SDK, `entrypoint: sdk-cli`): 1 of 1" in text
    assert "SECRET" not in text
    assert "sess.jsonl" not in text
    assert "proj" not in text


def test_first_announcement_is_excluded_per_kind(tmp_path: Path) -> None:
    rows = [
        _attachment("deferred_tools_delta"),
        _attachment("deferred_tools_delta"),
        _attachment("mcp_instructions_delta"),
        _turn("x"),
    ]
    _write(tmp_path / "p" / "s.jsonl", rows)
    (row,) = m.collect(tmp_path, _prices())["rows"]
    assert row["deferred"] == 2 and row["instructions"] == 1
    assert row["firsts"] == 2
    assert row["repeats"] == 1
    assert row["tokens"] == pytest.approx(10_000)
    assert row["write_usd"] == pytest.approx(0.025)
