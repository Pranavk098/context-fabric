"""``python -m sidecar`` — thin shim over the ``cfx`` CLI (``sidecar.cli``)."""

from __future__ import annotations

from sidecar.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
