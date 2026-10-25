"""Pre-registration hash: ``sha256(PROTOCOL.md + tasks.json + prices.json)``.

The runner refuses to start a non-pilot run unless the committed
``prereg.sha256`` matches this digest, so the protocol cannot change after the
founder commits the hash. ``python -m evals.plugin_ab.prereg --write``
recomputes and writes the hash file.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
FILES = ("PROTOCOL.md", "tasks.json", "prices.json")
HASH_FILE = "prereg.sha256"


def compute_hash(directory: Path = _DIR) -> str:
    digest = hashlib.sha256()
    for name in FILES:
        # read_text() normalizes CRLF/CR to LF, so the hash is the same regardless
        # of the checkout's line endings (the repo pins LF via .gitattributes).
        text = (directory / name).read_text(encoding="utf-8")
        digest.update(text.encode("utf-8"))
    return digest.hexdigest()


def stored_hash(directory: Path = _DIR) -> str | None:
    path = directory / HASH_FILE
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def write_hash(directory: Path = _DIR) -> str:
    digest = compute_hash(directory)
    (directory / HASH_FILE).write_text(digest + "\n", encoding="utf-8")
    return digest


def gate_ok(directory: Path, pilot: bool) -> tuple[bool, str]:
    """Return ``(ok, message)`` for the prereg gate (pilot bypasses it)."""
    if pilot:
        return True, "pilot bypasses the prereg gate"
    stored = stored_hash(directory)
    if stored is None:
        return False, "missing prereg.sha256"
    if stored != compute_hash(directory):
        return False, "prereg.sha256 does not match PROTOCOL.md + tasks.json + prices.json"
    return True, "prereg hash matches"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "--write":
        print(write_hash())
        return 0
    print(compute_hash())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
