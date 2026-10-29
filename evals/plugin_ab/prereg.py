"""Pre-registration hash: ``sha256(PROTOCOL.md + tasks.json + prices.json)``.

The runner refuses to start a non-pilot run unless the committed
``prereg.sha256`` matches this digest, so the protocol cannot change after the
founder commits the hash. ``python -m evals.plugin_ab.prereg --write``
recomputes and writes the hash file.

``--v2`` does the same for the v2 protocol
(``PROTOCOL_V2.md + tasks_v2.json + prices.json`` -> ``prereg_v2.sha256``);
the v1 files and gate are unchanged.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
FILES = ("PROTOCOL.md", "tasks.json", "prices.json")
HASH_FILE = "prereg.sha256"
FILES_V2 = ("PROTOCOL_V2.md", "tasks_v2.json", "prices.json")
HASH_FILE_V2 = "prereg_v2.sha256"


def _compute(directory: Path, files: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for name in files:
        # read_text() normalizes CRLF/CR to LF, so the hash is the same regardless
        # of the checkout's line endings (the repo pins LF via .gitattributes).
        text = (directory / name).read_text(encoding="utf-8")
        digest.update(text.encode("utf-8"))
    return digest.hexdigest()


def _stored(directory: Path, name: str) -> str | None:
    path = directory / name
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def compute_hash(directory: Path = _DIR) -> str:
    return _compute(directory, FILES)


def stored_hash(directory: Path = _DIR) -> str | None:
    return _stored(directory, HASH_FILE)


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


def compute_hash_v2(directory: Path = _DIR) -> str:
    return _compute(directory, FILES_V2)


def stored_hash_v2(directory: Path = _DIR) -> str | None:
    return _stored(directory, HASH_FILE_V2)


def write_hash_v2(directory: Path = _DIR) -> str:
    digest = compute_hash_v2(directory)
    (directory / HASH_FILE_V2).write_text(digest + "\n", encoding="utf-8")
    return digest


def gate_ok_v2(directory: Path, pilot: bool) -> tuple[bool, str]:
    """Return ``(ok, message)`` for the v2 prereg gate (pilot bypasses it)."""
    if pilot:
        return True, "pilot bypasses the prereg gate"
    stored = stored_hash_v2(directory)
    if stored is None:
        return False, "missing prereg_v2.sha256"
    if stored != compute_hash_v2(directory):
        return (
            False,
            "prereg_v2.sha256 does not match PROTOCOL_V2.md + tasks_v2.json + prices.json",
        )
    return True, "prereg v2 hash matches"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    v2 = "--v2" in args
    compute, write = (compute_hash_v2, write_hash_v2) if v2 else (compute_hash, write_hash)
    if "--write" in args:
        print(write())
        return 0
    print(compute())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
