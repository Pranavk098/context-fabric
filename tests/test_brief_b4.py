"""WP B4: relative paths in the text block, goal/constraint dedup, byte accounting."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cfxd import brief_render
from cfxd.brief_intent import extract_intent
from cfxd.brief_render import render
from cfxd.brief_select import resolve_target, select
from cfxd.session_state import FileFact, SessionFacts

NOW = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)
PROMPT = "Implement the retry limit in src/sync/fetch_client.py and backoff_policy.py"
USER_TEXT = (
    "Add a retry limit to the fetch client in the sync module. "
    "Name the new constant MAX_FETCH_RETRIES. "
    "Never change the public signature of fetch_all."
)
CONSTRAINTS = (
    "Name the new constant MAX_FETCH_RETRIES.",
    "Never change the public signature of fetch_all.",
)


def _record(kind: str, content: object) -> str:
    return json.dumps({"type": kind, "message": {"role": kind, "content": content}})


def _fixture(tmp_path: Path) -> tuple[Path, str, SessionFacts]:
    root = tmp_path / "work" / "client-repo"
    (root / "src" / "sync").mkdir(parents=True)
    facts: list[FileFact] = []
    for stamp, (rel, count) in enumerate(
        (("src/sync/fetch_client.py", 120), ("src/sync/backoff_policy.py", 90))
    ):
        path = root / rel
        path.write_text(
            "".join(
                f"    value_{i} = compute_step({i}) + offset_{i}  # step {i}\n"
                for i in range(1, count + 1)
            ),
            encoding="utf-8",
        )
        sha = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        facts.append(
            FileFact(str(path), count, False, float(stamp + 1), ((1, count, sha, "read"),))
        )
    transcript = root / "t.jsonl"
    transcript.write_text(
        _record("user", USER_TEXT)
        + "\n"
        + _record(
            "assistant",
            [
                {
                    "type": "text",
                    "text": "We'll go with exponential backoff instead of a fixed delay.",
                }
            ],
        )
        + "\n",
        encoding="utf-8",
    )
    return root, str(transcript), SessionFacts("s", tuple(facts), (), (), ())


def _excerpt_bytes(text: str) -> int:
    return sum(len(x.encode()) + 1 for x in text.split("\n") if re.match(r"\s*\d+\| ", x))


def _render(root: Path, transcript: str, facts: SessionFacts):  # type: ignore[no-untyped-def]
    sel = select(facts, PROMPT, resolve_target({"model": "haiku"}))
    intent = extract_intent(transcript, PROMPT, facts)
    return intent, render(sel, intent, cwd=str(root), session_id="s", now=NOW)


def test_haiku_brief_relative_paths_single_constraints_more_excerpt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, transcript, facts = _fixture(tmp_path)
    intent, new = _render(root, transcript, facts)
    text = new.text

    # No absolute cwd prefix anywhere in the text block; paths are relative, forward slashes.
    assert root.as_posix() not in text and str(root) not in text
    assert "- src/sync/fetch_client.py | 120 lines | read" in text
    assert "=== src/sync/fetch_client.py lines 1-" in text

    # Each constraint sentence appears exactly once (not repeated in the goal line).
    for sentence in CONSTRAINTS:
        assert text.count(sentence) == 1
    assert intent.goal is not None and "Name the new constant" not in intent.goal.text
    assert intent.deduped == 2

    assert len(text.encode()) <= 6_000
    assert dict(new.section_bytes)["excerpts"] > 0

    # Same inputs through the previous behaviour (absolute paths, goal not deduplicated).
    monkeypatch.setattr(brief_render, "_rel_path_text", lambda p, _cwd: brief_render._path_text(p))
    _old_intent, old = _render(root, transcript, facts)
    assert root.as_posix() in old.text
    assert _excerpt_bytes(new.text) > _excerpt_bytes(old.text)
    assert len(new.text.encode()) <= len(old.text.encode())


def test_file_outside_cwd_keeps_absolute_path_and_packet_keeps_absolute(tmp_path: Path) -> None:
    root, transcript, facts = _fixture(tmp_path)
    outside = tmp_path / "elsewhere.py"
    outside.write_text("x = 1\n", encoding="utf-8")
    more = SessionFacts("s", (*facts.files, FileFact(str(outside), 1, False, 9.0)), (), (), ())
    _intent, out = _render(root, transcript, more)
    assert f"- {outside.as_posix()} | 1 lines | read" in out.text
    # Packet v0 stays absolute (relative would also validate; kept unchanged on purpose).
    assert all(Path(f["path"]).is_absolute() for f in out.packet["files"])


def test_goal_dedup_is_deterministic_and_keeps_unrelated_goal_text(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        _record(
            "user", "Refactor the cache layer so lookups are faster. Never touch cache_keys.py."
        )
        + "\n",
        encoding="utf-8",
    )
    first = extract_intent(str(transcript), "do it", None)
    second = extract_intent(str(transcript), "do it", None)
    assert first == second
    assert (
        first.goal is not None
        and first.goal.text == "Refactor the cache layer so lookups are faster."
    )
    assert [c.text for c in first.constraints] == ["Never touch cache_keys.py."]
    assert first.deduped == 1
