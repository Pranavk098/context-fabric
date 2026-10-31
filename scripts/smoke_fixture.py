"""Build the small synthetic transcript tree the install-smoke workflow runs the tester commands on.

Usage: ``uv run --with pytest python scripts/smoke_fixture.py OUT_DIR``. It reuses the
``cfxd checkup`` test seed (a parent with reads, an Agent spawn, a child and a compaction), so
the smoke run exercises real code paths on data that holds no personal text.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: smoke_fixture.py OUT_DIR", file=sys.stderr)
        return 2
    sys.path.insert(0, str(_REPO))
    from tests.test_checkup import _seed  # noqa: PLC0415

    out = Path(argv[1])
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as state:
        _seed(out, Path(state))
    print(f"fixture ready: {sum(1 for _ in out.rglob('*.jsonl'))} transcript files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
