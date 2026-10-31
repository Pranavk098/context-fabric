"""On/off switches for brief, brief_policy, carry, brief_v1, carry_back, fold, fold_read,
bound, coach and idle_notice, read from ``<state_dir>/config.json``.

Defaults: brief, brief_policy and carry on; brief_v1, carry_back, fold, fold_read, bound,
coach and idle_notice off. Brief and carry are on so the alpha produces data; the rest are
experimental. Fold, fold_read and bound are retired (ADR-0013): they still have defaults so
old config files load, but a stored true is read back as false. The daemon is shared across
Claude Code sessions, so a file is the only way one session can reach the daemon; it is
re-read whenever its mtime changes. A missing or malformed file falls back to the defaults.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from cfxd.fsutil import ensure_dir

CONFIG_FILE = "config.json"
# fold and fold_read are off (2026-10-24 exploratory A/Bs): folding Read outputs made the model
# re-read the saved file and roughly doubled tokens on read-everything tasks, and folding Bash/Grep
# output showed no measurable saving because Claude Code already stubs very large outputs itself.
_DEFAULTS: dict[str, bool] = {
    "brief": True,
    # R-2: stay silent where a brief cannot pay for itself (Haiku children, parents that
    # know fewer than three files).
    "brief_policy": True,
    "carry": True,
    "fold": False,
    "fold_read": False,
    "bound": False,
    "coach": False,
    # A/B half of the nudge sessions withheld, both arms receipted (F-4).
    "coach_control": False,
    "idle_notice": False,
    # Brief v1 (ADR-0011) and the sub-agent carry-back are opt-in until the v4 pilot.
    "brief_v1": False,
    "carry_back": False,
    # Carry v2 (edited-first block with ranges and constraints) is opt-in.
    "carry_v2": False,
    # Brief v1 excerpts are opt-in: pointers only until a user asks for file content (M-4).
    "brief_excerpts": False,
}
_CACHE: dict[Path, tuple[int, dict[str, bool]]] = {}
# ADR-0013: retired features (fold, fold_read, bound) read back as off even when an old
# config file still says true, so a tester build never runs them.
_RETIRED = ("fold", "fold_read", "bound")


def read_config(state_dir: Path) -> dict[str, bool]:
    """Return every boolean flag; missing or bad file -> defaults."""
    path = state_dir / CONFIG_FILE
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return dict(_DEFAULTS)
    cached = _CACHE.get(path)
    if cached is not None and cached[0] == mtime:
        return dict(cached[1])
    config = _load(path)
    _CACHE[path] = (mtime, config)
    return dict(config)


def set_flag(state_dir: Path, key: str, value: bool) -> None:
    """Persist one flag, preserving the other (retired keys always store false)."""
    config = read_config(state_dir)
    config[key] = False if key in _RETIRED else value
    path = state_dir / CONFIG_FILE
    ensure_dir(path.parent)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(config, fh)
            fh.flush()
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    _CACHE[path] = (path.stat().st_mtime_ns, config)


def _load(path: Path) -> dict[str, bool]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(_DEFAULTS)
    if not isinstance(obj, dict):
        return dict(_DEFAULTS)
    config: dict[str, bool] = {}
    for key, default in _DEFAULTS.items():
        value = obj.get(key)
        config[key] = False if key in _RETIRED else (value if isinstance(value, bool) else default)
    return config
