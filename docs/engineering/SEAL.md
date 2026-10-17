# The Seal — signed, chained context receipts (v0)

## What this is

Context Fabric compiles the exact bytes an agent receives into a **context packet** (CPP v1.0):
tiered memory, negative knowledge, the watermark it was compiled from. The seal wraps those
packets in receipts. Every packet is RFC 8785-canonical-hashed and Ed25519-signed — that was
already true. The seal adds an **append-only epoch chain**: batches of packet hashes are
committed under a deterministic Merkle root, each epoch links to the previous one
(`prev_epoch_hash`) and is itself signed (`protocol/epoch.py`). One command,
`python -m protocol.epoch verify`, recomputes every hash and checks every link.

## Why it matters

If an agent gets a wrong answer, the first question is "what was it actually told, and was it
changed?". The seal answers exactly that, with cryptography instead of trust: which notes were
handed over, that they were never altered, and that a recompile from the same watermark
reproduces them. It is the evidence layer for the debugging-first wedge (vLLM operators
chasing nondeterministic runs) and, later, for AI Act / ISO 42001 evidence pipelines.

## The claims we may make

Use this wording verbatim:

> Context Fabric seals the context: every hand-off packet is canonically hashed, Ed25519-signed,
> and chained, so you can prove exactly which notes an agent received, that they were never
> altered, and that a recompile from the same watermark reproduces them byte-for-byte. It does
> not claim the model's answer is reproducible — model outputs depend on hardware, batching, and
> backend versions, as the 2025-2026 determinism literature shows.

Shorter form: it **proves which memory bytes were handed to the agent and that they are
unmodified**. Never say it "proves why the model answered". As with C2PA credentials, a
receipt makes no statement about the truth or reliability of the content itself. Every receipt
carries `meta.note = "output_reproduction_out_of_scope"` so the machine-readable form says the
same thing.

## Usage

```python
from datetime import UTC, datetime
from protocol.epoch import ReceiptMeta, create_epoch, epoch_content_hash, verify_chain
from protocol.signing import Signer

signer = Signer.from_seed_hex("...")  # 32-byte seed
e1 = create_epoch(
    tenant_id="t1",
    watermark_event_seq=17,
    packet_hashes=tuple(sorted(p.hashes.content_hash for p in packets)),
    created_at=datetime(2026, 10, 17, tzinfo=UTC),  # explicit, never wall-clock
    signer=signer,
    meta=ReceiptMeta(model_id="Qwen2.5-1.5B", tokenizer_id="o200k_base", engine="vLLM 0.9"),
)
e2 = create_epoch(..., prev_epoch_hash=epoch_content_hash(e1))  # next epoch links back
assert verify_chain([e1, e2], bytes.fromhex(signer.public_hex))
```

```powershell
uv run python -m protocol.epoch selftest                          # create -> verify -> tamper detected
uv run python -m protocol.epoch verify epochs.json --pubkey <hex> # OK/FAIL + exit code
uv run python -m evals.spike_epoch_determinism                    # cross-process byte equality
```

## Mechanics (for reviewers)

- **Epoch hash:** canonical (RFC 8785) form of the epoch with `signature`, `key_id`, and
  `epoch_id` excluded — the first two for signature-independence (ADR-0003), `epoch_id` because
  it is derived from this hash. `epoch_id = "epc_" + sha256(content_hash)[:16]`.
- **Signing payload:** the UTF-8 bytes of the content-hash string, same convention as packet
  signing (`kernel/router/compiler.py`).
- **Merkle rule:** leaf digest = `sha256(utf8(content-hash string))`; parent =
  `sha256(left_digest + right_digest)` over raw 32-byte digests; an odd node is carried up
  unchanged; a single leaf's root is its digest. Leaves are sorted, so the root is
  order-independent and deterministic.
- **Chaining:** epoch *N*'s `prev_epoch_hash` equals epoch *N-1*'s content hash; genesis has
  `prev_epoch_hash = None`. `verify_chain` also enforces consistent tenant, non-decreasing
  `created_at` and watermark, and single-key signatures.
- **Packet membership:** `verify_packet_in_epoch` recomputes the packet's content hash and
  checks it is a leaf under the epoch root. That is a membership check, not yet a compact
  inclusion proof (the next step: emit sibling hashes along the Merkle path).

## Honest limits (v0)

- **Output reproduction is out of scope.** The seal says which context bytes were handed over;
  it says nothing about the model's answer. Temperature 0 is not determinism, and batch
  invariance does not survive hardware/version changes.
- **Cross-machine guarantees require an identical stack.** Byte-identical recompiles hold only
  with the deterministic core, pinned tokenizer, no embeddings (or pinned revision), and the
  same watermark — *and the same Python + pydantic + cryptography versions*. The
  `spike_epoch_determinism` check stands in for two machines; it is valid only under that
  same-stack condition.
- **No transparency log, on-chain anchoring, or TEE in v0** — deferred per
  `docs/research/PROVENANCE-PRIOR-ART.md` (operational burden, rare need for no-mutual-trust
  anchoring, immature zkML). RFC 3161 timestamps are the cheap first upgrade if third-party
  time is needed.
- **Single key, out-of-band trust.** Verification needs the signer's public key delivered
  out-of-band; v0 has no key management and no keyless/OIDC signing.

## How to verify on two machines

1. On machine A: pin the stack (`uv sync`, record `python --version`, `uv pip freeze`), compile
   the packets, create epochs with an explicit `created_at`, and export them
   (`[e.model_dump(mode="json") for e in epochs]`).
2. On machine B: check out the same commit and run the same pinned `uv sync`.
3. Run `uv run python -m evals.spike_epoch_determinism` on both machines; both must print PASS.
4. Recompile the packets from the same watermark on machine B and compare each packet's
   `hashes.content_hash` with A's epoch leaves — they must match exactly.
5. Run `uv run python -m protocol.epoch verify epochs.json --pubkey <hex>` on machine B with
   A's public key (shared out-of-band). Exit code 0 = the chain and every packet are intact.
