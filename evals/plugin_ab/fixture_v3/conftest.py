# mypy: ignore-errors

"""Make the fixture's ``textkit`` package importable when pytest is run from this dir."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
