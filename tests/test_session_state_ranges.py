"""Range recording on FileFact (ADR-0011 decision 3): hashed from disk, parent events only."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from cfxd import session_state as ss


@pytest.fixture(autouse=True)
def _ranges_on() -> Iterator[None]:
    ss.reset()
    ss.set_range_recording(True)
    yield
    ss.set_range_recording(False)
    ss.reset()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _event(sid: str, tool: str, path: Path, tool_input: dict[str, Any] | None = None, **extra: Any):
    payload: dict[str, Any] = {
        "session_id": sid,
        "tool_name": tool,
        "tool_input": {"file_path": str(path), **(tool_input or {})},
        **extra,
    }
    ss.observe_post_tool_use(payload)


def _read(sid: str, path: Path, offset: int | None, num: int, **extra: Any) -> None:
    tool_input = {} if offset is None else {"offset": offset}
    _event(
        sid,
        "Read",
        path,
        tool_input,
        tool_response={"file": {"numLines": num, "totalLines": 99}},
        **extra,
    )


def _facts(sid: str, path: Path) -> ss.FileFact:
    snap = ss.snapshot(sid)
    assert snap is not None
    (fact,) = [f for f in snap.files if f.path == os.path.abspath(path)]
    return fact


def _lines(n: int) -> str:
    return "".join(f"line {i}\n" for i in range(1, n + 1))


def test_read_range_uses_offset_and_num_lines_and_disk_hash(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text(_lines(50), encoding="utf-8")
    _read("s", f, 10, 5)
    assert _facts("s", f).ranges == ((10, 14, _sha(f), "read"),)


def test_read_defaults_to_line_one_and_clamps_to_file(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text(_lines(3), encoding="utf-8")
    _read("s", f, None, 100)
    assert _facts("s", f).ranges == ((1, 3, _sha(f), "read"),)


def test_hash_comes_from_disk_not_tool_response(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text(_lines(5), encoding="utf-8")
    _event(
        "s",
        "Read",
        f,
        tool_response={"file": {"numLines": 5, "content": "FAKE", "sha": "0" * 16}},
    )
    assert _facts("s", f).ranges[0][2] == _sha(f)


def test_write_records_whole_file_as_edit(tmp_path: Path) -> None:
    f = tmp_path / "w.py"
    f.write_text(_lines(7), encoding="utf-8")
    _event("s", "Write", f)
    assert _facts("s", f).ranges == ((1, 7, _sha(f), "edit"),)


def test_edit_range_is_span_of_new_string(tmp_path: Path) -> None:
    f = tmp_path / "e.py"
    f.write_text("a\nb\nNEW1\nNEW2\nc\n", encoding="utf-8")
    _event("s", "Edit", f, {"new_string": "NEW1\nNEW2\n", "old_string": "x"})
    assert _facts("s", f).ranges == ((3, 4, _sha(f), "edit"),)


def test_edit_span_works_with_crlf_files(tmp_path: Path) -> None:
    f = tmp_path / "crlf.py"
    f.write_bytes(b"a\r\nb\r\nNEW1\r\nNEW2\r\nc\r\n")
    _event("s", "Edit", f, {"new_string": "NEW1\nNEW2"})
    assert _facts("s", f).ranges == ((3, 4, _sha(f), "edit"),)


def test_multiedit_records_each_new_string(tmp_path: Path) -> None:
    f = tmp_path / "m.py"
    f.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    _event("s", "MultiEdit", f, {"edits": [{"new_string": "two"}, {"new_string": "four"}, "junk"]})
    sha = _sha(f)
    assert _facts("s", f).ranges == ((2, 2, sha, "edit"), (4, 4, sha, "edit"))


def test_edit_with_missing_new_string_records_nothing(tmp_path: Path) -> None:
    f = tmp_path / "e.py"
    f.write_text("a\nb\n", encoding="utf-8")
    _event("s", "Edit", f, {"new_string": "not there"})
    _event("s", "Edit", f, {"new_string": ""})
    assert _facts("s", f).ranges == ()


def test_notebook_edit_records_no_ranges(tmp_path: Path) -> None:
    f = tmp_path / "n.ipynb"
    f.write_text("{}\n", encoding="utf-8")
    ss.observe_post_tool_use(
        {"session_id": "s", "tool_name": "NotebookEdit", "tool_input": {"notebook_path": str(f)}}
    )
    assert _facts("s", f).ranges == ()


def test_off_by_default_after_toggle(tmp_path: Path) -> None:
    ss.set_range_recording(False)
    f = tmp_path / "a.py"
    f.write_text(_lines(5), encoding="utf-8")
    _read("s", f, None, 5)
    assert _facts("s", f).ranges == ()


def test_changed_file_drops_ranges_hashed_against_older_content(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text(_lines(10), encoding="utf-8")
    _read("s", f, 1, 3)
    f.write_text(_lines(10) + "extra\n", encoding="utf-8")
    _read("s", f, 5, 2)
    assert _facts("s", f).ranges == ((5, 6, _sha(f), "read"),)


def test_same_range_is_moved_to_newest_and_not_duplicated(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text(_lines(10), encoding="utf-8")
    _read("s", f, 1, 3)
    _read("s", f, 5, 2)
    _read("s", f, 1, 3)
    sha = _sha(f)
    assert _facts("s", f).ranges == ((5, 6, sha, "read"), (1, 3, sha, "read"))


def test_at_most_eight_ranges_newest_kept(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text(_lines(40), encoding="utf-8")
    for start in range(1, 13):
        _read("s", f, start, 1)
    ranges = _facts("s", f).ranges
    assert len(ranges) == ss.RANGES_PER_FILE
    assert [r[0] for r in ranges] == list(range(5, 13))


def test_ranges_survive_events_that_record_none(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text(_lines(10), encoding="utf-8")
    _read("s", f, 1, 3)
    _event("s", "Read", f, tool_response={"file": {}})  # no numLines: no new range
    assert _facts("s", f).ranges == ((1, 3, _sha(f), "read"),)


def test_oversized_and_missing_files_record_nothing(tmp_path: Path) -> None:
    big = tmp_path / "big.txt"
    big.write_bytes(b"x\n" * 600_000)
    _read("s", big, None, 10)
    gone = tmp_path / "gone.py"
    _read("s", gone, None, 10)
    assert _facts("s", big).ranges == ()
    assert _facts("s", gone).ranges == ()


def test_sub_agent_events_are_not_recorded(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text(_lines(10), encoding="utf-8")
    _read("s", f, 1, 3, agent_id="agent-123")
    _read("s", f, 1, 3, isSidechain=True)
    fact = _facts("s", f)
    assert fact.ranges == ()  # the file fact itself is still recorded as before
    _read("s", f, 2, 2)
    assert _facts("s", f).ranges == ((2, 3, _sha(f), "read"),)


def test_sessions_do_not_share_ranges(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text(_lines(10), encoding="utf-8")
    _read("s1", f, 1, 3)
    _read("s2", f, 4, 2)
    assert _facts("s1", f).ranges == ((1, 3, _sha(f), "read"),)
    assert _facts("s2", f).ranges == ((4, 5, _sha(f), "read"),)


def test_recording_never_raises_on_hostile_input(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text(_lines(3), encoding="utf-8")
    _event("s", "Read", f, {"offset": "x"}, tool_response={"file": {"numLines": True}})
    _event("s", "MultiEdit", f, {"edits": 7})
    _event("s", "Edit", f, {"new_string": 5})
    _event("s", "Read", f, {"offset": -5}, tool_response={"file": {"numLines": 2}})
    assert _facts("s", f).ranges == ((1, 2, _sha(f), "read"),)
