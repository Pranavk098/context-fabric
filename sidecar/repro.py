"""``cfx repro`` — recompute the offline-verifiable claims in the numbers registry.

Two tiers, kept separate on purpose:

* **offline** — every ``strict`` entry in ``docs/engineering/NUMBERS.md`` whose value is
  recomputed from a committed artifact. This is the same check CI runs; no GPU, no network.
* **live-engine** — claims that need a running model server (pilot, fleet). They are *not*
  run here; the exact commands are printed so a reader reproduces them on their own hardware.
  A model claim is never silently treated as verified.

Exit 0 when every offline check passes, 1 on any failure, 2 when run outside a checkout.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent
_CHECKER = ROOT / "scripts" / "check_docs_numbers.py"

_LIVE_TIER = (
    "live-engine tier (NOT run by `cfx repro`; needs a GPU/engine; expected numbers in",
    "docs/engineering/REPRODUCE.md):",
    "  uv run cfx demo --scenario fleet",
    "  uv run python -m evals.pilot --fixture <clone-of-encode/httpx@b5addb64>",
)


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_docs_numbers", _CHECKER)
    if spec is None or spec.loader is None:  # pragma: no cover - environment failure
        raise RuntimeError(f"cannot load {_CHECKER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _safe(text: str) -> str:
    """ASCII-escape messages so any Windows console encoding (e.g. cp1252) can print them."""
    return text.encode("ascii", "backslashreplace").decode("ascii")


def run_repro(*, verbose: bool = False, out: Callable[[str], None] = print) -> int:
    if not _CHECKER.is_file():
        out("cfx repro: run this from a Context Fabric checkout")
        out("(scripts/check_docs_numbers.py is missing)")
        return 2
    report = _load_checker().run()
    for ok, message in report:
        if ok and not verbose:
            continue
        out(_safe(f"{'ok  ' if ok else 'FAIL'} {message}"))
    out("")
    for line in _LIVE_TIER:
        out(line)
    failures = [message for ok, message in report if not ok]
    if failures:
        out(f"cfx repro: FAIL - {len(failures)} of {len(report)} offline checks failed")
        return 1
    out(f"cfx repro: OK - {len(report)} offline checks passed")
    return 0
