# Epochs + Durable Store — Adversarial Review

**Status:** Complete. Reviewed 2026-10-17 against the working tree containing `kernel/epochs.py`,
`kernel/store/`, `sidecar/server.py`'s `load_fabric` dispatch, and their tests/docs (all
untracked; no commit made).
**Reviewer:** auditor agent. **Method:** full code read of every module and doc listed below, plus
live adversarial smokes (temp-dir scripts, corrupt databases, forged/duplicate states, seq-0 and
huge-count stores, uppercase dispatch, Windows file locks) and the complete CI gate set before and
after fixes.
**Scope:** `kernel/epochs.py`, `kernel/store/sqlite_store.py`, `kernel/store/__main__.py`,
`kernel/store/__init__.py`, `sidecar/server.py` (`load_fabric` path), `sidecar/__main__.py`,
`protocol/canonical.py`, `protocol/models.py`, `tests/test_epochs.py`, `tests/test_store.py`,
`tests/test_sidecar.py`, `docs/engineering/SHARED-COPY.md`, `docs/engineering/STORE.md`,
`docs/design/Module-Implementation.md` §D (spec divergence).

## Verdict

No S1 (crash/corruption/security) defects found. Four S2 defects were found; all four fixed in
place with regression tests (three code, one CI-gate/doc-format). Eleven S3 notes are recorded
below, none block shipping. The copy-on-write contract in `SHARED-COPY.md` matches the code
bullet-for-bullet except one false line in the usage example (ES-05); `STORE.md` matches the
schema and semantics. The Block-Plan `/kernel/mmu/epoch_folder.py` packet-level algorithm is **not**
what shipped — `kernel/epochs.py` is a block-level v0 — and neither engineering doc overclaims it.
All gates pass on the reviewed tree (55 tests green).

## Findings

| ID | Sev | What | Evidence | Fixed |
| :--- | :--- | :--- | :--- | :--- |
| **ES-01** | S2 | `MemoryState` accepted **duplicate `block_id`s**, including the same id with different `content_hash`. `new_state([b1, b1'])` produced a state whose block set is ambiguous and whose `state_id` hash is computed over a repeated hash; `cow_fork` propagated it, and only a later `epoch_fold` happened to catch the conflict. | Smoke 1 §2: `new_state([b1, b1'])` accepted, n=2. Code: `_canonical` checked ordering + `state_id` only (`kernel/epochs.py:54-60`). | **Yes** — `_canonical` now rejects duplicate ids (`kernel/epochs.py:58-60`); regression `test_new_state_rejects_duplicate_block_ids` (identical and conflicting duplicates). |
| **ES-02** | S2 | `SqliteStore.load_blocks()` defaulted to `since_seq=0` with strict `>` semantics, so a stored block with `source_event_seq=0` (a value `ContextBlock` permits and the compiler itself uses for synthetic instruction blocks) was **silently invisible** to default loads and to any `max_source_event_seq()`-driven catch-up loop (max is also 0). The sidecar's `load_fabric` used that default. | Smoke 1 §10: append seq-0 block → `count=1`, `load_blocks()` → `[]`, `max=0`. | **Yes** — default is now `-1` (all blocks) while explicit `since_seq` keeps the documented strict `>` watermark semantics (`kernel/store/sqlite_store.py:89-95`); `STORE.md` updated; regression `test_load_blocks_default_includes_seq_zero`. |
| **ES-03** | S2 | `load_fabric` on a **missing `.db`/`.sqlite` path silently created an empty SQLite database** and started the sidecar with zero blocks — amnesia-by-typo, the exact failure mode `STORE.md` exists to prevent. `SIDECAR-REVIEW.md` SR-14 assumed missing fabrics "fail as raw tracebacks"; that was only true for JSONL. | Smoke 1 §5: `load_fabric(tmp/"nope.db")` → `exists_after=True`, 0 blocks, no error. | **Yes** — `load_fabric` raises `FileNotFoundError` and creates nothing (`sidecar/server.py:78-84`); regression `test_load_fabric_missing_file_raises_and_does_not_create` (`.db` and `.jsonl`). |
| **ES-04** | S2 (gate) | The new `SHARED-COPY.md` failed `ruff format --check .` (ruff 0.17.1 formats Python blocks inside Markdown): CI would be red for the feature that just landed. | Baseline gate output: `unformatted: File would be reformatted --> docs\engineering\SHARED-COPY.md:66:64`. | **Yes** — `uv run ruff format docs/engineering/SHARED-COPY.md`; full-tree check now `82 files already formatted`. |
| **ES-05** | S3 (doc) | `SHARED-COPY.md:71` usage example asserts `shared.state_id == folded.state.state_id` ("add-only, no surprises"). **False**: the folded state contains `block_c`, so its id differs by construction. What is true is that `shared` (the parent) is unchanged. | Smoke 1 §1: counts `(1, 2)` match, equality `False`. Code: `epoch_fold` rebuilds from `parent.parent_state_id` + merged blocks (`kernel/epochs.py:139-154`). | No — doc example fix is a spec-owner call (suggest asserting `shared.blocks`/`shared.state_id` unchanged instead). |
| **ES-06** | S3 | `state_id` is **block_id-blind** by design (formula covers sorted `content_hash`s + fork point), so two states with different `block_id`s but equal content hashes share a `state_id`; `epoch_fold`'s "immediate descendant" check is therefore content equivalence, not lineage identity. | Smoke 1 §3: `new_state([mk("id1",…,"SAME")]).state_id == new_state([mk("id2",…,"SAME")]).state_id` → `True`. Formula is exactly as documented (`SHARED-COPY.md:39-41`). | No — documented formula; including `block_id`s is a spec change (would also change ids shipped in any future signed manifest). |
| **ES-07** | S3 | `watermark_event_seq` is **not covered by `state_id`**, and pydantic `model_copy(update=…)` bypasses all validators: a state can carry a forged watermark under a valid id, and fold output (max watermark) changes accordingly. Intentional per module docstring; `test_watermark_propagates_to_fold_result` itself relies on it. | Smoke 1 §4: `model_copy(update={"watermark_event_seq": 10**9})` accepted, `state_id` unchanged; §12: forged `state_id` accepted by `child_add` (in-process only). | No — documented; signed state manifests (SHARED-COPY.md §v0 limits) are the planned mitigation. |
| **ES-08** | S3 | STORE.md's "Roundtrip is lossless (full model equality)" is **not true for non-JSON-native values inside `counterfactual_meta`** (`dict[str, Any]`): a tuple becomes a list, a datetime becomes a string, so `load_blocks()[0] != original`. JSON-native meta, typed tuples, microsecond/tz datetimes, and `1/3` floats are lossless. | Smoke 1 §8 `back != blk`, `meta["t"]` is `list`, `meta["when"]` is `str`; smoke 2 §5 isolates: JSON-native `True`, Any-tuple `False`. | No code fix possible (wire format is JSON); `STORE.md` claim qualified with "for JSON-native payloads". |
| **ES-09** | S3 | `python -m kernel.store info <missing.db>` **creates** an empty database as a side effect of a read-only command. | Smoke 2 §9: rc=0, prints `blocks=0`, file created. | No — same class as ES-03 but harmless; fix by opening read-only if `info` grows. |
| **ES-10** | S3 | Corrupt db, directory-as-db, and missing JSONL surface as raw tracebacks (`sqlite3.DatabaseError: file is not a database`, `sqlite3.OperationalError`, `FileNotFoundError`) at CLI/sidecar startup. Fail-loud, not silent; matches pre-existing SR-14. | Smoke 1 §6-7; smoke 2 §4 and §9. | No — operator error path with self-explanatory messages. |
| **ES-11** | S3 | `SqliteStore` never exposes `close()`/context-manager; on Windows an open store blocks file deletion and leaves `-wal`/`-shm` files until the connection is garbage-collected (CPython refcounting releases them promptly; verified clean after GC). | Smoke 1 §14: `unlink` → `PermissionError`, 3 `lock.db*` files while alive; smoke 2 §8: after `del`+`gc.collect()` only `walx.db` remains. | No — v0 single-process, temporary store in `load_fabric` is released immediately; add `close()` when stores become long-lived. |
| **ES-12** | S3 | `append` commits per row; 2,000 appends took ≈5.1 s on Windows. `ingest_jsonl`/`append_many` inherit it. Fine at v0 scale (dozens of blocks), painful at 10⁵. | Smoke 2 §7. | No — batch the commit in `append_many` if ingest volume grows. |
| **ES-13** | S3 | Both store and epochs **trust caller-supplied `content_hash`** — it is never recomputed from `content`. Same `block_id` + same claimed hash + different actual content dedupes silently; the conflict check cannot see it. Consistent across the two modules and mirrored in the docs' `(block_id, content_hash)` identity. | Code reading (`sqlite_store.py:60-77`, `epochs.py:85-96,129-149`); protocol-wide design. | No — verifying `content_hash == sha256(content)` is a protocol-level decision (upgrade path: validate on append). |
| **ES-14** | S3 | `ingest_jsonl` is **non-atomic**: rows before a conflicting/duplicate row are committed; the exception aborts the rest. Re-running after fixing the file is idempotent (dedup), so no corruption — partial progress only. | Code: `ingest_jsonl` → `append_many` → per-row `append` with commit. | No — append-only + idempotent rerun covers it; wrap in one transaction if atomic ingest is wanted. |
| **ES-15** | S3 (spec) | Spec divergence confirmed: `Build-Plan-Validation-Gate.md:44`, `Repo-Tech-Stack.md:45-46`, and `Module-Implementation.md` §D describe a **packet/Tier-level** `kernel/mmu/epoch_folder.py` (k-step tool-loop fold minting Tier 1 facts + Tier 2 `ctx://` handles) and `cow_forker.py`; what shipped is the block-level `kernel/epochs.py` `MemoryState` contract. `SHARED-COPY.md`/`STORE.md` do **not** overclaim (SHARED-COPY explicitly scopes itself to "the contract, not a new KV engine" and names next steps). | Doc-vs-doc read. Also `IMPLEMENTATION-STATUS.md:23,31` still marks `EPOCH_FOLD`/`COW_FORK` ⬜ pending. | No — engineering docs are honest; update `IMPLEMENTATION-STATUS.md` to "🟡 block-level v0 at `kernel/epochs.py`" to keep the map current. |

## Checklist verification (claims vs code)

| Claim / checklist item | Result |
| :--- | :--- |
| `state_id` formula matches docs | **Verified** — `sha256_hex(canonicalize(sorted(content_hashes) + [parent_state_id or ""]))` (`epochs.py:39-41`) is exactly `SHARED-COPY.md:39-41`. A directly-constructed state with a wrong id is rejected by recomputation; only pydantic's `model_copy` escape hatch (ES-07) bypasses it. |
| Watermark excluded from `state_id` is intentional and documented | **Verified** — module docstring ("sha256 over the sorted block content hashes plus the fork point"); `test_watermark_propagates_to_fold_result` uses `model_copy` to change it; fold takes `max` (`epochs.py:151`). Residual trust issue noted (ES-07). |
| Duplicate blocks / ordering for equal `(source_event_seq, block_id)` | **Was broken, fixed** — duplicate ids (equal or conflicting content) are now rejected; ordering comparison relies on stable sort, and with unique ids the `(seq, id)` key is total. |
| `cow_fork` object-sharing claim | **Verified** — `_order` reuses the same element objects; `test_fork_shares_block_objects_without_copying` asserts identity (`c is p`). |
| `child_remove` of missing id | **Verified** — returns the same object (identity), covers `test_child_remove_is_private_and_missing_is_noop`. |
| Remove-then-re-add same id, different content → FoldConflict reachable | **Verified** — `test_fold_rejects_same_id_different_content` passes; the fold's `seen_hash` walk (`epochs.py:129-137`) catches parent/child divergence. |
| Fold idempotence / determinism / empty fold | **Verified** — repeated folds give identical ids and `(added, deduped)`; empty fold returns a model equal to the parent (same `state_id`, same parent chain, same watermark). |
| Grandchild and sibling folds rejected | **Verified** — grandchild: `parent_state_id` is the child's id, ≠ grandparent id → `FoldConflict("not-a-descendant")`; sibling-into-sibling likewise. Child-into-common-parent is allowed for each child, as designed. |
| `FoldResult` counts vs `SHARED-COPY.md` example `(1, 2)` | **Verified** — counts match; the adjacent state-id assertion in that example is the one false line (ES-05). |
| Watermark propagation | **Verified** — inherited verbatim at fork, `max()` at fold, parent untouched. |
| SQL-injection surface | **Verified clean** — every value is bound (`?`) in `append`, `load_blocks`; table/index names static; hostile-looking ids (`'; DROP TABLE blocks;--`) store and load intact (smoke 1 §9). |
| Canonical-JSON roundtrip | **Verified for JSON-native payloads** — microseconds + non-UTC tz offsets, `1/3` float, typed tuples, optionals all equal after roundtrip (test + smoke 1 §8, smoke 2 §5); only `Any`-typed non-JSON-native values lose type (ES-08). |
| WAL + AUTOINCREMENT behavior | **Verified** — WAL journal requested and active; `-wal`/`-shm` created, removed when the connection is GC'd; AUTOINCREMENT prevents rowid reuse (no delete API exists anyway). |
| `INSERT OR IGNORE` + hash-compare | **Verified for the documented single writer** — ignored insert → `rowcount 0` → re-read compares hashes → `StoreConflict`. The check/insert pair is racy only under concurrent writers, which the module and `STORE.md` explicitly do not support. |
| `since_seq` strict `>` vs `STORE.md` | **Matched**; default changed from 0 to -1 so seq-0 blocks are not invisible (ES-02), explicit `since_seq` semantics unchanged; doc updated. |
| `ingest_jsonl` blank lines + idempotence | **Verified** — whitespace-only lines skipped; rerun dedupes; malformed lines reported as `path:lineno` (`ValidationError` covers invalid JSON too). Non-atomicity noted (ES-14). |
| CLI argument validation | **Verified** — required subcommand via argparse; `info`/`ingest` paths unvalidated by design (tracebacks, ES-10/ES-09). |
| Windows file-lock/resource behavior | **Verified** — no leaked connections after GC; WAL side files released; open-store deletion blocked until release (ES-11). |
| `.db`/`.sqlite` suffix detection case-insensitive; corrupt db at startup | **Verified** — `path.name.lower().endswith`; uppercase `fabric.DB` dispatches to SQLite (smoke 2 §4); corrupt db raises `DatabaseError` (fail-loud, ES-10). Missing db now a clean `FileNotFoundError`, not silent creation (ES-03). |
| JSONL path unchanged | **Verified** — all 10 pre-existing sidecar tests pass unchanged; `load_blocks` untouched. |
| SHARED-COPY consistency bullets vs code | **Verified bullet-for-bullet** (immutable blocks, reference share, private edits, explicit descendant-only fold, conflict rejection with `.detail`, add-only v0, deterministic lineage, watermark max) except the usage-example line ES-05. |
| STORE.md schema/limits vs code | **Verified** — columns/index/types match (doc omits `IF NOT EXISTS`, harmless); single-writer, no relations, no typed columns, decode-on-read limits all match. Roundtrip claim qualified per ES-08. |
| Do the docs overclaim the Build-Plan `kernel/mmu` packet semantics? | **No** — SHARED-COPY.md scopes itself to the block-level contract with explicit v0 limits; STORE.md says nothing about `kernel/mmu`. The divergence is location/granularity plus a stale `IMPLEMENTATION-STATUS.md` row (ES-15). |

## Executed (commands and observed results)

| Command | Observed |
| :--- | :--- |
| `uv run ruff check .` | Passed, pre- and post-fix. |
| `uv run ruff format --check .` | **Pre-fix: FAILED** (`SHARED-COPY.md` would be reformatted). Post-fix: `82 files already formatted`. |
| `uv run mypy protocol evals kernel sidecar` | `Success: no issues found in 38 source files`, pre- and post-fix. |
| `uv run pytest -q` | Pre-fix **52 passed**; post-fix **55 passed** (3 new regressions). |
| `uv run pytest tests/test_epochs.py tests/test_store.py tests/test_sidecar.py -q` | `31 passed`. |
| `uv run python -m kernel.epochs` | `epochs.py self-check: OK (fork -> private edit -> isolation -> fold)`. |
| Smoke 1 (temp script): doc example, duplicate ids, id-blind collision, forged watermark, missing/corrupt/dir db, tz + tuple roundtrip, injection ids, seq-0, forged state into `child_add`, dup-parent fold, Windows unlink | Caught ES-01/02/03/05/06/07/08/10/11/13; SQL injection clean; no crashes on hostile ids. |
| Smoke 2 (post-fix): duplicate rejection, missing-db raise, seq-0 visibility, uppercase dispatch, corrupt payload row, 2000-block store timing, WAL cleanup after GC, CLI surfaces | All fixes verified; corrupt payload fails loud (`ValidationError`); 2,000 blocks: append 5.13 s, load 0.02 s; WAL side files released on GC. |

No commit or push was made. Files changed by this review: `kernel/epochs.py`,
`kernel/store/sqlite_store.py`, `sidecar/server.py`, `tests/test_epochs.py`, `tests/test_store.py`,
`tests/test_sidecar.py`, `docs/engineering/SHARED-COPY.md` (formatting only),
`docs/engineering/STORE.md` (ES-02/ES-08 doc accuracy).

## Residual risks (none block shipping)

1. **In-process trust only.** `MemoryState` lineage ids are reproducible by any caller; a state
   can be minted directly or mutated via `model_copy` (ES-06/ES-07). No persistence or signing
   of states exists yet — signed epoch manifests embedding state ids are the documented next step.
2. **`content_hash` is asserted, not verified** (ES-13). Both the store and epoch fold inherit
   this; a future append-time check `content_hash == sha256(content)` closes it.
3. **Non-JSON-native `counterfactual_meta` is lossy through any JSON store** (ES-08). Keep meta
   JSON-native, or add a typed field when it matters.
4. **CLI error paths are tracebacks** (ES-10) and `info` creates missing dbs (ES-09). Acceptable
   for a local single-operator tool; polish if the CLI becomes an operator surface.
5. **Write throughput is one commit per block** (ES-12); batch if ingest grows beyond v0 scale.
6. **Spec divergence is a location/granularity gap** (ES-15): the Build-Plan packet-level
   `kernel/mmu/epoch_folder.py` is not implemented; the shipped contract is block-level. Keep
   `IMPLEMENTATION-STATUS.md` current so the gap stays visible.
7. **Windows file handles** (ES-11): stores held open block file deletion; add
   `close()`/context-manager support before stores become long-lived or movable.
