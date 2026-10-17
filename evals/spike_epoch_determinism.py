"""Cross-process determinism check for sealed epochs.

Spawns two independent Python subprocesses that each create the same epoch
(fixed key seed, fixed inputs, fixed ``created_at``) and compares the canonical
bytes they emit. If they differ, the seal is not replayable.

Stand-in for two machines: valid only for the same Python + library versions
(pydantic and cryptography must match); cross-machine equality is otherwise not
guaranteed (docs/engineering/SEAL.md).

Run: uv run python -m evals.spike_epoch_determinism
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime

from protocol.canonical import canonicalize
from protocol.epoch import create_epoch, epoch_content_hash
from protocol.signing import Signer

EMIT_FLAG = "--emit"


def emit() -> None:
    epoch = create_epoch(
        tenant_id="t1",
        watermark_event_seq=1,
        packet_hashes=("sha256:aaa", "sha256:bbb", "sha256:ccc"),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        signer=Signer.from_seed_hex("00" * 32),
    )
    payload = epoch.model_dump(mode="json")
    sys.stdout.write(canonicalize(payload).decode("utf-8"))


def run_child() -> bytes:
    result = subprocess.run(
        [sys.executable, "-m", "evals.spike_epoch_determinism", EMIT_FLAG],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr.decode("utf-8", errors="replace"))
        raise SystemExit(1)
    return result.stdout


def main() -> int:
    first, second = run_child(), run_child()
    identical = first == second
    epoch = create_epoch(
        tenant_id="t1",
        watermark_event_seq=1,
        packet_hashes=("sha256:aaa", "sha256:bbb", "sha256:ccc"),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        signer=Signer.from_seed_hex("00" * 32),
    )
    print("spike_epoch_determinism: PASS" if identical else "FAIL")
    print(f"  canonical bytes : {len(first)}")
    print(f"  content hash    : {epoch_content_hash(epoch)}")
    print("  note: two processes on ONE machine, same Python + library versions;")
    print("        cross-machine equality further requires an identical stack (SEAL.md)")
    return 0 if identical else 1


if __name__ == "__main__":
    if EMIT_FLAG in sys.argv:
        emit()
    else:
        raise SystemExit(main())
