"""cfxd trend tests: hand-made snapshots, percent changes and the feature markers."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from cfxd import __version__
from cfxd.trend import format_trend, trend

ROOT = Path(__file__).resolve().parents[1]


def _features(**overrides: object) -> dict:
    base = {
        "bound": False,
        "coach": False,
        "idle_notice": False,
        "diet_applied": 0,
        "checkpoint_saves": 0,
    }
    base.update(overrides)
    return base


def _snapshot(
    state: Path,
    date: str,
    *,
    sessions: int,
    median_dollars: float | None,
    first_turn: int | None,
    long_share: float,
    cache: float,
    features: dict | None = None,
) -> Path:
    path = state / "history" / f"week-{date}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "sessions": sessions,
        "sessions_under_2_turns": 0,
        "dollars_total": round(median_dollars or 0.0, 4),
        "median_dollars_per_session": median_dollars,
        "buckets": {
            "2-10": {"sessions": 1, "dollars": 0.0, "share": round(1 - long_share, 4)},
            "11-30": {"sessions": 0, "dollars": 0.0, "share": 0.0},
            "31+": {"sessions": sessions, "dollars": 0.0, "share": long_share},
        },
        "cache_read_share_tokens": cache,
        "first_turn_context_median": first_turn,
        "compactions": 0,
        "clear_restarts": 0,
        "hook_delivery_rate": 1.0,
        "features_on": features or _features(),
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _three(state: Path) -> None:
    _snapshot(
        state,
        "2026-01-01",
        sessions=10,
        median_dollars=1.0,
        first_turn=10000,
        long_share=0.5,
        cache=0.8,
    )
    _snapshot(
        state,
        "2026-01-08",
        sessions=15,
        median_dollars=1.5,
        first_turn=9000,
        long_share=0.4,
        cache=0.85,
        features=_features(bound=True),
    )
    _snapshot(
        state,
        "2026-01-15",
        sessions=20,
        median_dollars=2.0,
        first_turn=8000,
        long_share=0.3,
        cache=0.9,
        features=_features(bound=True, coach=True),
    )


def test_percent_change_first_to_last(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _three(state)

    report = trend(state)

    changes = {c["metric"]: c for c in report["changes"]}
    assert changes["sessions"]["pct_change"] == 100.0
    assert changes["median_dollars_per_session"]["pct_change"] == 100.0
    assert changes["first_turn_context_median"]["pct_change"] == -20.0
    assert changes["long_share"]["pct_change"] == -40.0
    assert changes["cache_read_share_tokens"]["pct_change"] == round(12.5, 1)
    text = format_trend(report)
    assert "sessions: 10 -> 20 (+100.0%)" in text
    assert "first-turn context: 10,000 -> 8,000 (-20.0%)" in text


def test_feature_marker_first_appearance(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _three(state)

    report = trend(state)

    assert report["first_on"] == {"bound": "2026-01-08", "coach": "2026-01-15"}
    assert [row["new_features"] for row in report["snapshots"]] == [[], ["bound"], ["coach"]]
    text = format_trend(report)
    assert "* bound first appears in the 2026-01-08 snapshot." in text
    assert "* coach first appears in the 2026-01-15 snapshot." in text


def test_fewer_than_two_snapshots(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _snapshot(
        state,
        "2026-01-01",
        sessions=10,
        median_dollars=1.0,
        first_turn=10000,
        long_share=0.5,
        cache=0.8,
    )

    text = format_trend(trend(state))

    assert "Not enough snapshots yet (found 1; need at least 2)." in text
    assert "cfxd week --save" in text


def test_json_shape(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _three(state)

    data = json.loads(json.dumps(trend(state)))

    assert data["version"] == __version__
    assert set(data) >= {
        "version",
        "weeks",
        "snapshots",
        "changes",
        "first_on",
        "observed",
        "safe_to_paste",
    }
    assert len(data["snapshots"]) == 3
    assert set(data["snapshots"][0]) >= {
        "date",
        "sessions",
        "median_dollars_per_session",
        "first_turn_context_median",
        "long_share",
        "cache_read_share_tokens",
        "features_on",
    }
    assert set(data["changes"][0]) == {"metric", "label", "first", "last", "pct_change"}


def test_share_cli_leaks_no_paths_or_names(tmp_path: Path) -> None:
    canary = "CANARYTRENDPROJECT-7777"
    state = tmp_path / canary / "state"
    _three(state)

    for extra in (["--share"], ["--share", "--json"]):
        result = subprocess.run(
            [sys.executable, "-m", "cfxd", "trend", "--state-dir", str(state), *extra],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        assert canary not in result.stdout
        assert canary not in result.stderr
        if "--json" not in extra:
            assert "Safe to paste: counts, sizes and model ids only." in result.stdout
            assert (
                "Other changes also move these numbers; this is not a counterfactual."
                in result.stdout
            )


def test_weeks_limits_rows(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _three(state)

    report = trend(state, weeks=2)

    assert [row["date"] for row in report["snapshots"]] == ["2026-01-08", "2026-01-15"]
