"""On/off switch for brief and carry, read from ``<state_dir>/config.json``.

Both default on so the alpha produces data. The daemon is shared across Claude
Code sessions, so a file is the only way one session can reach the daemon; it is
re-read whenever its mtime changes. A missing or malformed file falls back to
the defaults.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from cfxd.fsutil import ensure_dir

CONFIG_FILE = "config.json"
# fold_read is off: in the 2026-10-24 exploratory A/B, folding Read outputs made the model re-read
# the saved file in full and roughly doubled tokens on read-everything tasks.
_DEFAULTS: dict[str, bool] = {"brief": True, "carry": True, "fold_read": False}
_CACHE: dict[Path, tuple[int, dict[str, bool]]] = {}


def read_config(state_dir: Path) -> dict[str, bool]:
    """Return the boolean flags (brief, carry, fold_read); missing or bad file -> defaults."""
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
    """Persist one flag, preserving the other."""
    config = read_config(state_dir)
    config[key] = value
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
        config[key] = value if isinstance(value, bool) else default
    return config
