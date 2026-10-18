# Track C/D adversarial review

**Date:** 2026-10-18
**Scope:** working tree on top of `c1ddc42` (uncommitted): C-A `kernel/mmu/`, C-B
`sidecar/render.py` + `kernel/router/hints.py`, D-A compile manifest + `cfx replay` / `cfx seal`,
D-B Merkle inclusion proofs, and their docs/tests.
**Method:** code read, independent re-implementation of the Merkle tree in a scratch script,
real CLI runs against a temp store, a forced concurrent-write repro for the manifest, and the
full gate suite. No commits; no edits to README / CHANGELOG / IMPLEMENTATION-STATUS.

## Verdict

| Area | Verdict | Summary |
| :--- | :--- | :--- |
| C-A `kernel/mmu/` (EPOCH_FOLD + COW_FORK) | **PASS WITH NOTES** | Fold determinism, k-gate, tail passthrough, block validity and private-helper reuse verified. Fixed one policy bug: fold promoted every fact to `routable` instead of preserving the source lifecycle. Tier 2 sharing is a defensible, now honestly documented extension; the missing child-RBAC Tier 1 filter is documented as a known gap. |
| C-B cache alignment + hints | **PASS** | Tier 0 first, instruction second; tests assert the byte claims. The demo-fixture hash `1494390f54cf69eb3944a9dd84b330c39b8657019ca05e2f58a8a21e44c62734` was reproduced exactly. `shared_prefix_hash == Tier0Cartridge.kv_prefix_hash` for a real cartridge; per-agent `prefix_hash` diverges while the shared hash does not. |
| D-A manifest + replay/seal | **PASS WITH CAVEATS** | Real CLI flow works end to end; both drift modes fail cleanly; new blocks do not affect replay; seal/verify round-trips and re-seal is a no-op. Fixed an S2 race where a concurrent `POST /fabric/blocks` could enter a manifest for a packet that was compiled *before* that block existed (made replay falsely fail). Open caveats: repeated `packet_id`s are not separately receipted, missing stores are auto-created, corrupt rows are not caught, seal output is not written atomically. |
| D-B Merkle proofs | **PASS (after fixes)** | Independent brute-force tree confirms `merkle_root`/`merkle_proof` agreement for sizes 1..30; all leaves verify, paths match, flips/wrong leaves/forged internal-node leaves are rejected. The odd-node rule is **duplicate the last node**, not "carry up unchanged" (code and the pre-existing `test_merkle_root_rules` were right; the SEAL/epoch docstrings were wrong). Three crash-on-malformed-input bugs in the verifier were fixed. |
| Gates | **PASS** | ruff check, ruff format --check, mypy (fresh cache and `--no-incremental`), 226 tests green. |

No S1 bug remains. Three S1-class verifier crashes (malformed/untrusted receipt input) were
fixed; two S2 correctness/policy bugs were fixed; five documentation falsehoods were corrected;
ten items remain open (two S2 design caveats + eight S3 operational/API caveats), all documented
below.

## Findings

| ID | Sev | Area | Finding and evidence | Fixed |
| :--- | :--- | :--- | :--- | :--- |
| CD-01 | **S2** | D-A sidecar | **Manifest/fabric race.** `_compile` passed `list(fabric)` to the compiler but `_log_compile` later read the live `fabric` list; a `POST /fabric/blocks` arriving between the two made the manifest name a block the packet never contained. Forced repro (`race_repro.py`): manifest `('blk_f7e4…','d1')`, `cfx replay` exit 1 `REPLAY MISMATCH`. | **Yes** — snapshot taken under `write_lock` (`sidecar/server.py:373-375`), same tuple passed to compiler and logger; regression test `test_manifest_matches_compile_snapshot_under_concurrent_write` (post-fix: manifest `('d1',)`, replay exit 0). |
| CD-02 | **S2** | C-A fold | **Fold promoted non-routable events.** `_fold_fact` omitted `lifecycle_status`, so it defaulted to `routable` (`protocol/models.py:92`) while the policy gate denies anything else (`policy_engine.py:36`). A `draft`/`quarantined` raw observation folded into a fact would bypass lifecycle policy. | **Yes** — `lifecycle_status=event.lifecycle_status` (`kernel/mmu/epoch_folder.py:86`); draft-preservation assertion added to `tests/test_mmu.py`, doc updated. |
| CD-03 | **S1** | D-B verifier | **`verify_merkle_proof` crashed on non-`str` elements.** Scratch check with `123`, `None`, `bytes`, tuple elements raised `AttributeError`/`TypeError` instead of failing closed (untrusted JSON can contain these). | **Yes** — `isinstance(element, str)` guard (`protocol/epoch.py:129`); regression test `test_malformed_proof_elements_fail_closed`; independent script now reports "no crash, all rejected". |
| CD-04 | **S1** | D-B verifier | **`verify_epoch` crashed on an empty packet set.** A pydantic-valid epoch with `packet_hashes=()`, `packet_count=0` and a non-null signature reached `merkle_root(())` and raised `ValueError`; `cfx verify` printed a traceback instead of FAIL. | **Yes** — `if not epoch.packet_hashes: return False` (`protocol/epoch.py:204`); `test_verify_epoch_rejects_empty_packet_set_without_crashing`. |
| CD-05 | **S1** | D-B verifier | **`verify_chain` crashed on mixed naive/aware `created_at`.** Two individually valid signed epochs (one naive, one aware) made `ep.created_at < prev.created_at` raise `TypeError`; reachable via `cfx verify`/library. | **Yes** — mixed-awareness guard returns False (`protocol/epoch.py:230-231`); `test_verify_chain_rejects_mixed_naive_and_aware_timestamps`. |
| CD-06 | **S2** | D-A receipts | **Repeated `packet_id`s are silently un-receipted.** `packet_id` excludes `as_of` and the block set (`compiler.py:155-162`), so a second compile of the same objective/agent/watermark reuses the id and `INSERT OR IGNORE` keeps the first manifest (`sqlite_store.py:311-333`). CLI run: 3 compiles → 2 manifests. The asked edge is safe: the first manifest is consistent with what the **first** compile sent (post-CD-01). But the second hand-off has no receipt under any id. | **No (documented)** — `SEAL.md` now says the first manifest wins and repeats are proxied but not separately receipted. Upgrade path: include `as_of`/input hash in `packet_id`, or append a per-compile audit row keyed by `(packet_id, content_hash)`. |
| CD-07 | **S2** | C-A fork | **Child Tier 1 is shared unfiltered.** `Module-Implementation.md:90` requires filtering the parent ledger when the child has stricter `denied_roles` (e.g. auditor); `fork_packet` reuses `parent.tiers.tier1` (`cow_forker.py:106`) with no `apply_policy` pass, so a role-denied parent block reaches the child. | **No (documented)** — new divergence bullet in `MMU-FOLD-FORK.md` states the gap and the upgrade (policy pass + `total_tokens` recompute). Fork is not wired into the sidecar yet, so no runtime exposure today. |
| CD-08 | **S3** | Docs D-B | **Wrong odd-node rule in text.** `SEAL.md` and the `protocol/epoch.py` module docstring said an odd node is "carried up unchanged"; `merkle_root` duplicates the last node (`level = level + [level[-1]]`, `protocol/epoch.py:81-83`), and the pre-existing `test_merkle_root_rules` already asserted duplication. Independent brute-force confirmed duplication; carried-up produces a different root for non-power-of-two sizes. | **Yes** — both texts corrected; proof docstrings already said "duplicates". |
| CD-09 | **S3** | Docs D-B | **Stale "no compact proof yet" claim.** `SEAL.md` said membership is "not yet a compact inclusion proof (next step …)", false after D-B. | **Yes** — SEAL.md now documents `merkle_proof`/`verify_merkle_proof`, the L/R encoding, O(log n), and malformed-input fail-closed behavior. |
| CD-10 | **S3** | Docs C-A | **Overclaims in `MMU-FOLD-FORK.md`.** (a) "adds no token growth" is false when the child instruction exceeds the parent's Tier 3 — measured parent `total_tokens=15`, child `23` (still under the 4000 budget). (b) "identical Tier 0+1+2 prefix" KV reuse is false under the C-B render order: Tier 0, then the per-agent instruction, then Tier 1/2 (`sidecar/render.py:73-80`), so only Tier 0 is a shared rendered prefix. | **Yes** — intro, "Constant window size", and Tier-2 divergence bullets reworded; `cow_forker.py` module docstring corrected. Tier-2 sharing itself is defensible (keeps `ctx://` handles resolvable) and is now documented as *not* extending the cacheable prefix. |
| CD-11 | **S3** | Docs D-A | **Two-machine recipe omitted `as_of`.** `SEAL.md` step 4 said recompile "from the same watermark"; `as_of` feeds the instruction block's `observed_at`/`valid_from` (`compiler.py:57-74`) and therefore the content hash. | **Yes** — step 4 now requires the same explicit `as_of` and explains why. |
| CD-12 | **S3** | Docs D-A | **"Every successful compile appends a manifest" contradicted first-write-wins** (same doc, later paragraph). | **Yes** — wording now says every compile *records* a manifest, first per `packet_id` wins, repeats are not separately receipted (see CD-06). |
| CD-13 | S3 | D-A CLI | `cfx replay`/`cfx seal` on a mistyped `--store` path **create a new SQLite file** (`SqliteStore.__init__` connects/creates) and report "no manifest"/"empty log" (exit 1) instead of "cannot open store" (exit 2); leaves a stray empty DB. | No — operational caveat. Upgrade: require `Path.exists()` for replay/seal (not `add`). |
| CD-14 | S3 | D-A CLI | Corrupt block payload or compile-log row raises an uncaught pydantic `ValidationError`/`sqlite3.Error` from `get_compile`/`load_blocks`; exit code is still 1 but the user sees a traceback, not a clear drift message (`sidecar/replay.py:72,76`; `seal` similarly). | No — documented. Upgrade: catch `(ValueError, sqlite3.Error)` around manifest/block loads and print `cfx replay: cannot read store: …`. |
| CD-15 | S3 | D-A seal | `seal` writes the epochs file with `Path.write_text` (truncate then write, `sidecar/replay.py:169`), so a crash mid-write corrupts the whole receipt file. Recoverable: the epoch is deterministic from the store + key + metadata, so re-sealing rebuilds it. | No — documented. Upgrade: write a temp file in the same directory then `os.replace`. |
| CD-16 | S3 | D-A seal | `--private-key <hex>` exposes the signing seed in shell history and process listings (`sidecar/cli.py:103`). | No — operational caveat; SEAL.md already says key management is out of scope for v0. Upgrade: env/stdin/file descriptor input. |
| CD-17 | S3 | D-A replay | Manifest stores **sorted** `block_ids` (`sqlite_store.py:326`) but replay rebuilds in **store order**. The compiler is not fully input-order-insensitive: `RecencyAnchor.prohibitions` iterates input order (`packer.py:221-225`), which is part of the packet hash. The sidecar is the only producer and compiles in store order, so replay is exact today; another producer logging a differently ordered compile would produce a false mismatch. | No — documented. Upgrade: record input order, or make the prohibition list order-independent. |
| CD-18 | S3 | C-A fold | `fold_raw_events` is policy-agnostic: if handed pre-policy raw events, the fact now preserves a non-routable lifecycle (CD-02 fix), but the returned `SwapHandle` summary still carries the raw content and has no policy fields. Intended use is post-policy Tier 3 (as in the MMU doc example). | No — API caveat documented here; the lifecycle fix closes the direct promotion path. |
| CD-19 | S3 | C-A fork | `fork_packet` takes both `recipient` and `stage` (`cow_forker.py:52-61`); callers can pass a recipient whose `stage` differs, producing a packet whose instruction stage and recipient stage disagree. | No — API footgun; validate equality or derive `stage` from `recipient`. |
| CD-20 | S3 | D-A receipts | The manifest is logged **before** the upstream request is proxied (`server.py:385`, proxy at `server.py:291`), so an upstream failure/abort still leaves a receipt. The manifest is a compile receipt; SEAL.md's "handed to the agent" wording is slightly stronger than what is proven. | No — wording caveat; compile-time logging is intentional (streaming makes delivery confirmation expensive). |

## What was independently verified

### D-B Merkle (`merkle_audit.py`, independent tree)

- `merkle_root` matches a duplicate-last tree and **differs** from a carried-up tree on odd
  levels (checked sizes 1..30; non-power-of-two).
- Every leaf proves for sizes 1..30; each emitted path equals an independently built
  `(side, sibling)` path; every proof verifies.
- L/R flips, digest flips, wrong leaves, and an internal-node digest claimed as a leaf are all
  rejected.
- Malformed elements (missing prefix, empty, bad hex, wrong length, trailing junk, non-`str`)
  all return `False` with no exception (after CD-03).
- Empty proof is accepted only when the root equals the leaf digest (single leaf).

### D-A replay/seal, as a user (`cfx` subprocesses against a temp store + fake upstream)

Built the deterministic 26-block `sidecar.demo` fixture; started `cfx serve --fabric
fabric.db`; POSTed a chat completion; inspected `compile_log` with `sqlite3`:

- Manifest `as_of` fell inside the request's before/after window; `CF_MODEL`/`CF_ENGINE` were
  recorded; block list was the 26 fixture ids.
- `cfx replay` → `REPLAY OK`.
- Tamper **content only** (payload content changed, payload/column `content_hash` untouched):
  no drift, recompile differs → `REPLAY MISMATCH <logged> != <replayed>`, exit 1.
- Tamper **content + both hashes**: `store drift: block arch changed/missing`, exit 1.
- Add a block via `cfx add`: replay still OK (new blocks filtered out).
- Recompile the same objective after the live store changed: same `packet_id`, manifest
  unchanged (first-write-wins), replay of the first receipt still OK.
- `cfx seal` → `cfx verify` exit 0; immediate re-seal → "nothing new", exit 0.
- 3 compiles produced 2 manifests (CD-06 evidence).

### D-A manifest logging

- `as_of` is the value constructed immediately before `compile_packet` and passed to both the
  compiler and the manifest (code read + DB window check).
- Logging failure path catches `Exception` around the write only; `KeyboardInterrupt`/
  `SystemExit` still propagate. The monkeypatched `RuntimeError` test confirms proxying keeps
  working (HTTP 200 + `X-Fabric-Compiled: 1` + stderr warning).
- Block writes, snapshotting, and manifest writes all take the same `write_lock`; the forced
  concurrent repro is clean after CD-01.

### C-B cache alignment / hints

- Tests assert exactly the claimed bytes: tier 0 lines first, first
  `len(tier0_markdown.encode())` bytes identical across two agents and equal to the markdown,
  no objective sentinel inside that prefix, divergence starting after it; empty-tier-0 render
  is byte-identical to the old instruction-first order.
- Claimed fixture hash reproduced: `sha256(render_packet(compile_packet(…)))` =
  `1494390f54cf69eb3944a9dd84b330c39b8657019ca05e2f58a8a21e44c62734` for the fleet objective
  `implementer` → `"Which port do Atlas services expose gRPC on? Give the port only."` with the
  sidecar's real recipient (`default-agent`/`user`/`chat`), no cartridge. (`as_of` does not
  appear in rendered text, so fixed vs wall-clock matches the same hash.)
- Real `build_cartridge` → compiled packet: `tier0.kv_prefix_hash == shared_prefix_hash`;
  two agents/objectives share `shared_prefix_hash` but differ in `prefix_hash`.

### C-A MMU

- Fold: deterministic byte equality across runs; `k=0` rejected; below-threshold,
  non-foldable, content-less, and incomplete-tail events pass through unchanged; facts
  validate and round-trip; trust domain, classification, RBAC, validity and (after CD-02)
  lifecycle are preserved.
- Fork: Tier 0/1/2 objects shared by identity; fresh Tier 3 (no probes/observations/anchor);
  parent untouched; lineage fields, budget, deterministic ids/hashes, sign-only-with-signer
  verified. `_det_id`/`_instruction` imports are both used; `_instruction` semantics match the
  compiler (stage/role/objective and `as_of` timestamps). Child total can grow relative to the
  parent when the new instruction exceeds the discarded Tier 3, but the packet validator and
  the explicit check keep it within `hard_total_tokens`.
- Divergence judgement: sharing Tier 2 is defensible (handles stay resolvable) and the MMU
  doc now states it is a divergence from §D.2 without overclaiming KV reuse. Tier 1 RBAC
  filtering remains the one substantive spec gap (CD-07).

## Known mypy issue

`uv run mypy protocol evals kernel sidecar` **did not reproduce** the reported
`kernel/mmu/epoch_folder.py:170: Item "None" … has no attribute "endswith"` on the pinned mypy
2.4.0, including with a deleted cache and `--no-incremental`. The assertion already narrowed
under this analyzer. The line was hardened anyway (local `fact_content`) so the narrowing does
not depend on analyzer version; the full command is clean.

## Commands run

```powershell
uv run ruff check .                                  # All checks passed!
uv run ruff format --check .                         # 125 files already formatted
uv run mypy protocol evals kernel sidecar            # Success: no issues found in 51 source files
uv run mypy --no-incremental --cache-dir <fresh> protocol evals kernel sidecar  # clean
uv run python -m pytest -q                           # 226 passed
# scratch (temp, not committed):
uv run python ...\merkle_audit.py                    # sizes 1..30, odd rule, flips, parser
uv run python ...\hash_probe.py                      # fixture hash 1494… reproduced
uv run python ...\hints_check.py                     # shared/kv prefix identity
uv run python ...\race_repro.py                      # pre-fix mismatch, post-fix REPLAY OK
uv run python ...\cli_e2e.py                         # real cfx serve/replay/add/seal/verify flow
uv run python ...\mmu_check.py                       # fold bytes, lifecycle, fork sharing/budget
# CLI exercised via subprocess in cli_e2e.py:
#   uv run cfx serve --fabric <db> --upstream <fake>
#   uv run cfx replay --store <db> --packet-id <id>
#   uv run cfx add --store <db> --file <json> --trusted
#   uv run cfx seal --store <db> --private-key <hex> --out <json> --model m --engine e
#   uv run cfx verify <json> --pubkey <hex>
```

One transient failure was observed in the first full-suite run:
`tests/test_sidecar.py::test_cors_on_stdlib_error_path` raised `httpx.ReadError [WinError 10053]`.
It passed in isolation and in two subsequent full-suite runs; treated as a Windows socket/timing
flake, not a product regression (residual risk below).

## Residual risks

- **Receipt completeness:** only unique `packet_id`s are receipted (CD-06). Repeated compiles
  under the same objective/agent/watermark are proxied but have no manifest row of their own.
- **Manifest trust boundary:** replay detects drift against the store, but a writer with DB
  access can edit both the blocks and the compile log; the manifests are unsigned until `cfx
  seal`, and `seal` signs the log as-is (it does not replay-verify each row).
- **Input-order dependence:** replay relies on store order matching the original compile input
  order (CD-17). Sidecar-produced manifests satisfy this; other producers may not.
- **Policy-agnostic fold:** `fold_raw_events` does not run the policy gate; callers must fold
  already-admitted blocks (CD-18).
- **Tier-1 fork RBAC:** child agents inherit the parent ledger without a role filter (CD-07).
- **Operational:** missing-store auto-creation (CD-13), tracebacks on corrupt rows (CD-14),
  non-atomic seal write (CD-15), signing key on argv (CD-16), receipt logged before upstream
  delivery (CD-20), and the observed Windows test flake.

## Blockers

None. All required gates pass and no S1 issue remains.

## Fixed in this pass

- `sidecar/server.py`: compile snapshot under `write_lock`, passed to compiler and manifest
  (CD-01) + regression test.
- `kernel/mmu/epoch_folder.py`: preserve `lifecycle_status` (CD-02) + doc/test.
- `protocol/epoch.py`: non-`str` proof guard (CD-03), empty-epoch guard (CD-04), mixed-tz guard
  (CD-05), corrected module docstring + membership docstring; tests in
  `tests/test_merkle_proofs.py`, `tests/test_epoch.py`.
- `kernel/mmu/cow_forker.py`: corrected KV-prefix wording (CD-10).
- `docs/engineering/SEAL.md`: Merkle rule (CD-08), compact proofs (CD-09), `as_of` in the
  two-machine recipe (CD-11), first-write-wins wording (CD-12).
- `docs/engineering/MMU-FOLD-FORK.md`: lifecycle preservation, token-growth bound, Tier-2/KV
  honesty, Tier-1 RBAC gap (CD-02/CD-07/CD-10).
