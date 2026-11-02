"""P4 M1 + M2 on synthetic transcripts with hand-computed answers.

Golden fixture ``tests/fixtures/repeat/golden_session.jsonl`` (all sizes exact):

* Read ``/fixture/a.txt``: 400 bytes, landed once, re-read exactly once (400 B).
* Bash output: 10 lines of 20 bytes = 209 B; the rerun changes one line, so the
  line-set Jaccard is 9 / 11 = 0.818 -> one near-duplicate of 209 B.
* Grep output: 120 bytes, landed twice -> one exact repeat of 120 B.
* Compaction boundary between the first Read and its third landing: that third
  landing duplicates compacted-away content, so it is excluded (400 B, not elidable).
* One Read result carries the ``FILE_UNCHANGED`` stub (46 B).
* Usage: ``message.id`` m3 writes two lines (text block + tool_use block) with the
  same usage record; it must be counted once. New tokens = 150 + 10 + 20 + 20 + 30
  + 5 + 7 + 7 = 249.

So exact = 400 + 120 = 520 B over 249 new tokens (130 / 249 = 52.2%), near = 209 B
(52.25 / 249 = 21.0%), and the two are reported apart, never summed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import repeat_share as r  # noqa: E402

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "repeat"
_GOLDEN = _FIXTURES / "golden_session.jsonl"
_HALF_CHANGED = _FIXTURES / "half_changed_rerun.jsonl"
_FILE = "a" * 400
_BASH = "\n".join(f"{i:02d}" + "z" * 18 for i in range(10))
_BASH_MOD_LINES = [f"{i:02d}" + "z" * 18 for i in range(10)]
_BASH_MOD_LINES[5] = "99" + "q" * 18
_BASH_MOD = "\n".join(_BASH_MOD_LINES)


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _turn(mid: str, n_in: int, block: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "id": mid,
            "usage": {"input_tokens": n_in, "cache_creation_input_tokens": 0},
            "content": [block],
        },
    }


def _use(tid: str, name: str, inp: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"type": "tool_use", "id": tid, "name": name, "input": inp or {}}


def _result(tid: str, text: str) -> dict[str, Any]:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tid, "content": [{"text": text}]}],
        },
    }


def _compact() -> dict[str, Any]:
    return {"type": "system", "subtype": "compact_boundary", "isCompactSummary": True}


def test_golden_session_answer() -> None:
    d = r.scan_session(_GOLDEN)
    assert d["messages"] == 8
    assert d["new_tokens"] == 249
    assert d["results"] == 8
    assert d["landed"] == 1904
    assert d["exact"] == {"events": 2, "bytes": 520}
    assert d["near"] == {"events": 1, "bytes": 209}
    assert d["cross"] == {"events": 1, "bytes": 400}
    assert d["by_tool"]["Read"] == {"results": 4, "landed": 1246, "exact": 400, "near": 0}
    assert d["by_tool"]["Bash"] == {"results": 2, "landed": 418, "exact": 0, "near": 209}
    assert d["by_tool"]["Grep"] == {"results": 2, "landed": 240, "exact": 120, "near": 0}
    assert d["by_tool"]["other"] == {"results": 0, "landed": 0, "exact": 0, "near": 0}


def test_usage_record_repeated_per_message_id_counts_once() -> None:
    d = r.scan_session(_GOLDEN)
    # message.id m3 appears on two lines with the same usage record. Counting lines
    # instead of ids would give 269 new tokens (m3 charged twice).
    assert d["messages"] == 8
    assert d["new_tokens"] == 249
    assert d["new_tokens"] != 269


def test_repeat_across_compaction_boundary_is_excluded() -> None:
    d = r.scan_session(_GOLDEN)
    assert d["cross"] == {"events": 1, "bytes": 400}
    # The Read row keeps only the intra-segment repeat (t2), not t5 after the boundary.
    assert d["by_tool"]["Read"]["exact"] == 400
    # The excluded repeat is still landed content, so it stays in the denominator.
    assert d["landed"] == 1904


def test_near_duplicate_across_a_boundary_is_excluded_too(tmp_path: Path) -> None:
    _write(
        tmp_path / "p" / "s.jsonl",
        [
            _turn("a", 10, _use("u1", "Bash")),
            _result("u1", _BASH),
            _compact(),
            _turn("b", 10, _use("u2", "Bash")),
            _result("u2", _BASH_MOD),
        ],
    )
    d = r.scan_session(tmp_path / "p" / "s.jsonl")
    assert d["near"] == {"events": 0, "bytes": 0}
    assert d["cross"] == {"events": 1, "bytes": 209}


def test_rerun_with_half_the_lines_changed_is_not_near() -> None:
    d = r.scan_session(_HALF_CHANGED)
    assert d["exact"] == {"events": 0, "bytes": 0}
    assert d["near"] == {"events": 0, "bytes": 0}
    assert d["landed"] == 418


def test_read_full_rereads_vs_stubs() -> None:
    d = r.scan_session(_GOLDEN)
    # t1 is the first read; t2 and t5 are full re-reads of the same path; t6 is a stub.
    assert d["read"]["first"] == {"results": 1, "bytes": 400}
    assert d["read"]["reread_full"] == {"results": 2, "bytes": 800}
    assert d["read"]["stub"] == {"results": 1, "bytes": 46}


def test_partial_read_of_a_known_path_is_not_a_full_reread(tmp_path: Path) -> None:
    _write(
        tmp_path / "p" / "s.jsonl",
        [
            _turn("a", 10, _use("u1", "Read", {"file_path": "/f/a.txt"})),
            _result("u1", _FILE),
            _turn("b", 10, _use("u2", "Read", {"file_path": "/f/a.txt", "offset": 100})),
            _result("u2", "b" * 50),
            _turn("c", 10, _use("u3", "Read", {"file_path": "/f/b.txt"})),
            _result("u3", "c" * 30),
        ],
    )
    d = r.scan_session(tmp_path / "p" / "s.jsonl")
    assert d["read"]["first"] == {"results": 3, "bytes": 480}
    assert d["read"]["reread_full"] == {"results": 0, "bytes": 0}


def test_exact_and_near_shares_are_never_summed() -> None:
    text = r.render(r.scan_session(_GOLDEN), "b6f1c26")
    assert "52.2%" in text  # exact: 520 B -> 130 tokens over 249
    assert "21.0%" in text  # near: 209 B -> 52.25 tokens over 249
    assert "729" not in text  # 520 + 209 must not appear as one figure
    assert "73.2%" not in text  # nor as one share of new tokens
    assert "exact 52.2% -> lead" in text
    assert "near-duplicate 21.0% -> lead" in text


def test_report_is_aggregates_only() -> None:
    text = r.render(r.scan_session(_GOLDEN), "b6f1c26")
    assert "/fixture/a.txt" not in text
    assert "golden_session" not in text
    assert "fixtures" not in text
    assert "a" * 50 not in text
    assert "run-step" not in text
    assert "File unchanged since last read" not in text
    assert "| transcripts scanned | 0 |" in text


def test_main_refuses_without_bars_commit(capsys: Any) -> None:
    assert r.main(["--root", str(_FIXTURES)]) == 2
    assert "--bars-commit is required" in capsys.readouterr().err


def test_main_prints_the_bars_commit(tmp_path: Path, capsys: Any) -> None:
    assert r.main(["--root", str(tmp_path), "--bars-commit", "b6f1c26"]) == 0
    out = capsys.readouterr().out
    assert "Decisions commit the bars were frozen in: `b6f1c26`" in out
    assert "| transcripts scanned | 0 |" in out
    assert "n/a" in out  # no denominator, no invented number
