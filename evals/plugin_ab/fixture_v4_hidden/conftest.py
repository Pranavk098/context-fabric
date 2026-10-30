"""Put the v4 fixture's ``src`` directory on ``sys.path`` for hidden tests.

``CLICK_FIXTURE_ROOT`` lets the verify script point the same tests at a temporary copy
with a reference or variant patch applied. It is unset for a normal run, which uses the
vendored fixture next to this directory.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_default = Path(__file__).resolve().parents[1] / "fixture_v4"
_root = Path(os.environ.get("CLICK_FIXTURE_ROOT", str(_default))).resolve()
_src = _root / "src"

if not _src.is_dir():
    raise RuntimeError(f"CLICK_FIXTURE_ROOT does not contain src/: {_root}")

sys.path.insert(0, str(_src))
