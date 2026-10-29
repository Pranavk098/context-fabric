"""cfxd idle tests: resume receipts, the optional notice, stats and week lines."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cfxd.config import set_flag
from cfxd.handlers import clear, dispatch
from cfxd.idle import RESUME_LOG, handle_session_start, install
from cfxd.stats import collect_stats, format_human
from cfxd.week import format_week, week

_MODEL = "claude-sonnet-4-5-20250929"


@pytest.fixture(autouse=True)
def _clean() -> Any:
    clear()
    yield
    clear()


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"source": "resume", "session_id": "s1"}
    payload.update(overrides)
    return payload


def _entries(state_dir: Path) -> list[dict[str, Any]]:
    path = state_dir / RESUME_LOG
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_cold_receipt_exact_fields(tmp_path: Path) -> None:
    response = handle_session_start(
        _payload(
            context_tokens=182_000,
            prompt_cache_likely_expired=True,
            estimated_cache_write_usd=0.381,
            model=_MODEL,
        ),
        tmp_path,
    )

    assert response == {}
    (entry,) = _entries(tmp_path)
    assert set(entry) == {
        "ts",
        "session_id",
        "event",
        "context_tokens",
        "est_write_usd",
        "model",
    }
    assert entry["session_id"] == "s1"
    assert entry["event"] == "resume_cold"
    assert entry["context_tokens"] == 182_000
    assert entry["est_write_usd"] == 0.38
    assert entry["model"] == _MODEL


def test_warm_resume_receipt_exact(tmp_path: Path) -> None:
    response = handle_session_start(
        _payload(
            context_tokens=182_000,
            prompt_cache_likely_expired=False,
            estimated_cache_write_usd=0.38,
            model=_MODEL,
        ),
        tmp_path,
    )

    assert response == {}
    (entry,) = _entries(tmp_path)
    assert set(entry) == {"ts", "session_id", "event", "outcome"}
    assert entry["event"] == "resume"
    assert entry["outcome"] == "warm"


def test_missing_fields_record_no_cache_info(tmp_path: Path) -> None:
    response = handle_session_start(_payload(), tmp_path)

    assert response == {}
    (entry,) = _entries(tmp_path)
    assert set(entry) == {"ts", "session_id", "event", "outcome"}
    assert entry["event"] == "resume"
    assert entry["outcome"] == "no_cache_info"


def test_wrong_types_never_crash_and_record_no_cache_info(tmp_path: Path) -> None:
    response = handle_session_start(
        _payload(
            context_tokens="182000",
            prompt_cache_likely_expired="true",
            estimated_cache_write_usd="0.38",
            model=42,
        ),
        tmp_path,
    )

    assert response == {}
    (entry,) = _entries(tmp_path)
    assert entry["event"] == "resume"
    assert entry["outcome"] == "no_cache_info"


def test_bool_is_not_an_int(tmp_path: Path) -> None:
    handle_session_start(
        _payload(
            context_tokens=True,
            prompt_cache_likely_expired=True,
            estimated_cache_write_usd=0.38,
        ),
        tmp_path,
    )

    (entry,) = _entries(tmp_path)
    assert entry["event"] == "resume"


def test_non_resume_sources_are_ignored(tmp_path: Path) -> None:
    for source in ("startup", "compact", "clear"):
        assert handle_session_start(_payload(source=source, context_tokens=182_000), tmp_path) == {}
    assert _entries(tmp_path) == []


def test_threshold_edges(tmp_path: Path) -> None:
    set_flag(tmp_path, "idle_notice", True)
    cases = (
        (99_999, 0.50, False),
        (100_000, 0.50, True),
        (200_000, 0.24, False),
        (200_000, 0.25, True),
    )
    for tokens, dollars, shown in cases:
        response = handle_session_start(
            _payload(
                context_tokens=tokens,
                prompt_cache_likely_expired=True,
                estimated_cache_write_usd=dollars,
            ),
            tmp_path,
        )
        assert ("systemMessage" in response) is shown, (tokens, dollars)


def test_notice_off_by_default_and_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cold = _payload(
        context_tokens=182_000,
        prompt_cache_likely_expired=True,
        estimated_cache_write_usd=0.38,
    )
    assert handle_session_start(cold, tmp_path) == {}

    set_flag(tmp_path, "idle_notice", True)
    assert "systemMessage" in handle_session_start(cold, tmp_path)

    monkeypatch.setenv("CFX_IDLE_NOTICE", "0")
    assert handle_session_start(cold, tmp_path) == {}


def test_notice_message_exact(tmp_path: Path) -> None:
    set_flag(tmp_path, "idle_notice", True)
    response = handle_session_start(
        _payload(
            context_tokens=182_000,
            prompt_cache_likely_expired=True,
            estimated_cache_write_usd=0.38,
            model=_MODEL,
        ),
        tmp_path,
    )

    assert response == {
        "systemMessage": (
            "Context Fabric: resuming after the cache expired; re-reading ~182K tokens "
            "costs about $0.38 (API-equivalent). If you do not need the old history, "
            "/clear starts cheap: the checkpoint cannot be saved after the fact, so "
            "describe the goal in one line."
        )
    }


def test_receipts_canary_never_leaks(tmp_path: Path) -> None:
    canary = "CANARYPROMPT-idle1234"
    set_flag(tmp_path, "idle_notice", True)
    response = handle_session_start(
        _payload(
            context_tokens=182_000,
            prompt_cache_likely_expired=True,
            estimated_cache_write_usd=0.38,
            model=_MODEL,
            transcript_path=f"C:\\u\\{canary}\\t.jsonl",
            prompt=canary,
            cwd=canary,
        ),
        tmp_path,
    )

    assert canary not in json.dumps(response)
    assert canary not in (tmp_path / RESUME_LOG).read_text(encoding="utf-8")


def test_install_registers_after_other_session_start_handlers(tmp_path: Path) -> None:
    from cfxd.handlers import register

    register("SessionStart", lambda payload: {})
    install(tmp_path)
    response = dispatch("SessionStart", _payload(context_tokens=182_000))

    assert response == {}
    assert _entries(tmp_path)[0]["event"] == "resume"


def test_stats_cold_resume_lines(tmp_path: Path) -> None:
    state_dir = tmp_path / "cfx"
    state_dir.mkdir()
    now = datetime.now(UTC).isoformat()
    entries = [
        {
            "ts": now,
            "session_id": "s1",
            "event": "resume_cold",
            "context_tokens": 182_000,
            "est_write_usd": 0.38,
        },
        {
            "ts": now,
            "session_id": "s1",
            "event": "resume_cold",
            "context_tokens": 200_000,
            "est_write_usd": 0.25,
        },
        {"ts": now, "session_id": "s2", "event": "resume", "outcome": "no_cache_info"},
    ]
    (state_dir / RESUME_LOG).write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
    )

    stats = collect_stats(state_dir)

    assert stats["cold_resumes"] == {"count": 2, "usd": 0.63, "receipts": 3}
    text = format_human(stats)
    assert "Cold resumes: 2   about $0.63 API-equivalent in cache writes" in text
    assert format_human(collect_stats(tmp_path / "empty")) == "No folds yet."


def _write_transcript(root: Path) -> None:
    line = {
        "type": "assistant",
        "message": {
            "model": _MODEL,
            "content": [],
            "usage": {"input_tokens": 1000, "output_tokens": 100},
        },
    }
    path = root / "proj" / "s1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")


def test_week_cold_resume_line(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _write_transcript(root)
    state_dir = tmp_path / "cfx"
    state_dir.mkdir()
    now = datetime.now(UTC).isoformat()
    (state_dir / RESUME_LOG).write_text(
        json.dumps(
            {
                "ts": now,
                "session_id": "s1",
                "event": "resume_cold",
                "context_tokens": 182_000,
                "est_write_usd": 0.38,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    text = format_week(week(root, 7, state_dir=state_dir))

    assert "Cold resumes (from receipts): 1, about $0.38" in text
    cold_line = [line for line in text.splitlines() if "Cold resumes" in line][0]
    assert "n/a" not in cold_line


def test_week_cold_resume_line_na_without_receipts(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _write_transcript(root)

    text = format_week(week(root, 7, state_dir=tmp_path / "cfx"))

    assert "Cold resumes (from receipts): n/a (no resume receipts)" in text


def test_week_cold_resume_line_na_without_state_dir(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _write_transcript(root)

    text = format_week(week(root, 7))

    assert "Cold resumes (from receipts): n/a (no state directory)" in text
