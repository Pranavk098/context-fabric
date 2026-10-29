"""``cfxd trend``: week-over-week snapshots, always labelled OBSERVED.

Reads the numbers-only files written by ``cfxd week --save`` and shows whether a
metric moved between the first and the last snapshot. A before/after in a
tester's own data is an observation, not a counterfactual: other changes move the
same numbers. Experimental; nothing is proven.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cfxd import __version__

_SAFE = "Safe to paste: counts, sizes and model ids only."
_NOT_COUNTERFACTUAL = "Other changes also move these numbers; this is not a counterfactual."
_NEED_TWO = (
    "Not enough snapshots yet (found {found}; need at least 2).\n"
    "Run `cfxd week --save` once a week; each run adds one snapshot to your cfxd state directory."
)
_METRICS = (
    ("sessions", "sessions"),
    ("median_dollars_per_session", "median $/session"),
    ("first_turn_context_median", "first-turn context"),
    ("long_share", "31+ $ share"),
    ("cache_read_share_tokens", "cache-read share"),
    ("compactions", "compactions"),
    ("clear_restarts", "/clear restarts"),
    ("hook_delivery_rate", "hook delivery rate"),
)


def _read(state_dir: Path, weeks: int) -> list[dict[str, Any]]:
    """Snapshots sorted by filename date, oldest first; unreadable files are skipped."""
    try:
        paths = sorted((state_dir / "history").glob("week-*.json"))
    except OSError:
        return []
    snapshots: list[dict[str, Any]] = []
    for path in paths[-weeks:]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            data["date"] = path.stem.removeprefix("week-")
            snapshots.append(data)
    return snapshots


def _share(snapshot: dict[str, Any], label: str) -> float | None:
    bucket = (snapshot.get("buckets") or {}).get(label)
    if isinstance(bucket, dict) and isinstance(bucket.get("share"), (int, float)):
        return float(bucket["share"])
    return None


def _feature_names(features: dict[str, Any]) -> list[str]:
    names = [key for key in ("bound", "coach", "idle_notice") if features.get(key) is True]
    if features.get("diet_applied"):
        names.append(f"diet({features['diet_applied']})")
    if features.get("checkpoint_saves"):
        names.append(f"ckpt({features['checkpoint_saves']})")
    return names


def _metric(snapshot: dict[str, Any], key: str) -> float | None:
    value = _share(snapshot, "31+") if key == "long_share" else snapshot.get(key)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _pct(first: float | None, last: float | None) -> float | None:
    """Percent change from the first to the last snapshot; 0 or a missing value is n/a."""
    if first is None or last is None or first == 0:
        return None
    return round((last - first) / first * 100.0, 1)


def trend(state_dir: Path, weeks: int = 8, share: bool = False) -> dict[str, Any]:
    """Read the last ``weeks`` snapshots and compare the first with the last."""
    snapshots = _read(state_dir, weeks)[-weeks:]
    rows: list[dict[str, Any]] = []
    first_on: dict[str, str] = {}
    seen: set[str] = set()
    for snapshot in snapshots:
        date = str(snapshot["date"])
        features = snapshot.get("features_on") or {}
        names = _feature_names(features)
        new = [name for name in names if name not in seen]
        for name in new:
            first_on[name] = date
        seen.update(names)
        rows.append(
            {
                "date": date,
                "sessions": snapshot.get("sessions"),
                "median_dollars_per_session": snapshot.get("median_dollars_per_session"),
                "first_turn_context_median": snapshot.get("first_turn_context_median"),
                "long_share": _share(snapshot, "31+"),
                "cache_read_share_tokens": snapshot.get("cache_read_share_tokens"),
                "compactions": snapshot.get("compactions"),
                "clear_restarts": snapshot.get("clear_restarts"),
                "hook_delivery_rate": snapshot.get("hook_delivery_rate"),
                "features_on": features,
                "new_features": new,
            }
        )
    changes: list[dict[str, Any]] = []
    if len(rows) >= 2:
        for key, label in _METRICS:
            first = _metric(snapshots[0], key)
            last = _metric(snapshots[-1], key)
            changes.append(
                {
                    "metric": key,
                    "label": label,
                    "first": first,
                    "last": last,
                    "pct_change": _pct(first, last),
                }
            )
    return {
        "version": __version__,
        "weeks": weeks,
        "snapshots": rows,
        "changes": changes,
        "first_on": first_on,
        "observed": _NOT_COUNTERFACTUAL,
        "safe_to_paste": _SAFE,
        "share": share,
    }


def _number(value: Any, spec: str) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "n/a"
    return format(value, spec)


def _cell(key: str, value: Any) -> str:
    if key == "median_dollars_per_session":
        return _number(value, ",.4f")
    if key == "first_turn_context_median":
        return _number(value, ",.0f")
    if key in ("long_share", "cache_read_share_tokens", "hook_delivery_rate"):
        return "n/a" if value is None else f"{float(value) * 100:.1f}%"
    return _number(value, ",.0f")


def format_trend(report: dict[str, Any], share: bool = False) -> str:
    """A compact table plus the first-to-last change; no paths, names or transcript text."""
    rows = report["snapshots"]
    if len(rows) < 2:
        return _NEED_TWO.format(found=len(rows))
    lines = [
        f"cfxd trend {report['version']}, last {len(rows)} snapshots (experimental)",
        "date        sessions  median $/session  first-turn ctx  31+ $ share  features on",
    ]
    for row in rows:
        features = ",".join(_feature_names(row["features_on"])) or "-"
        marker = "*" if row["new_features"] else ""
        lines.append(
            f"{row['date']}  {_number(row['sessions'], ',.0f'):>8}  "
            f"{_cell('median_dollars_per_session', row['median_dollars_per_session']):>16}  "
            f"{_cell('first_turn_context_median', row['first_turn_context_median']):>14}  "
            f"{_cell('long_share', row['long_share']):>11}  {features}{marker}"
        )
    lines.append("Change from first to last snapshot (OBSERVED):")
    for change in report["changes"]:
        key = change["metric"]
        first = _cell(key, change["first"])
        last = _cell(key, change["last"])
        pct = change["pct_change"]
        pct_text = "n/a" if pct is None else f"{pct:+.1f}%"
        lines.append(f"  {change['label']}: {first} -> {last} ({pct_text}) (OBSERVED)")
    lines.append(_NOT_COUNTERFACTUAL)
    for name, date in report["first_on"].items():
        lines.append(f"* {name} first appears in the {date} snapshot.")
    if share:
        lines.append(_SAFE)
    return "\n".join(lines)
