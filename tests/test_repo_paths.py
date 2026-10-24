"""Windows refuses paths over 260 chars, and the plugin is cloned under ~/.claude/plugins/....

Keep every tracked path short so a friend with a long home folder can still install it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_TRACKED_PATH = 140


def test_tracked_paths_stay_short() -> None:
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    too_long = [p for p in out.splitlines() if len(p) > MAX_TRACKED_PATH]
    assert not too_long, f"paths over {MAX_TRACKED_PATH} chars: {too_long[:5]}"
