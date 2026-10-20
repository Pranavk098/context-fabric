# Observation Folding for Sub-Agent Windows (`ctx://obs`)

**Status:** implemented at `sidecar/obsfold.py`, wired into the sidecar proxy behind a
flag (default OFF), with a CLI `cfx expand` and an MCP `expand` tool. Tests:
`tests/test_obsfold.py`, `tests/test_mcp.py`.

## What it does

A spawned sub-agent on a small context window overflows because tool outputs pile up:
environment observations are **67-84%** of multi-turn tokens (external round C,
`docs/research/RESEARCH-ROUND-C-EXTERNAL.md` §Q1/Q4). Observation folding replaces older
tool messages with a short, *reversible* stub and leaves the most recent ones verbatim, so
the window stops growing and nothing is lost.

The policy is a single pass over one agent session's OpenAI-style message list
(`system`/`user`/`assistant`/`tool`):

* **mask** — keep the last `N` `tool` messages verbatim (default `N=3`); replace older ones
  with `[ctx://obs/<sha256-12> folded: <tool>, <bytes> bytes, <first line>]`, storing the
  full content in a SQLite store keyed by the content hash so it can be expanded later.
* **cache alignment (ReFold-style)** — the fold boundary only advances every `k=3` new
  observations, so between fold events the rendered prompt is strictly append-only: the
  leading bytes of consecutive requests are byte-identical and the prefix cache survives.
* **window guard** — given `max_context_tokens`, fold more observations (oldest first)
  until the request fits. It never touches the system prompt or the latest user message;
  in fact it only ever folds `tool` messages.
* **receipt** — every fold event is appended to the store's append-only `fold_log` table
  (`kernel/store`), together with `seen_hash` — the sha256 of the exact folded message list
  the model saw — so replay can reconstruct and verify it.

## Reversibility

`ctx://obs/<id>` stubs are content-addressed (`id` = first 12 hex of the content sha256).
The full content is stored as an `observation` block with `block_id = obs_<id>`; expanding
is a direct lookup:

```
cfx expand --store obs.db ctx://obs/<id>          # CLI
{"name": "expand", "arguments": {"uri": "ctx://obs/<id>"}}   # MCP tool
```

The MCP `expand` tool resolves against whatever store the MCP server was launched with
(`--store`/`CF_FABRIC`); point it at the obs store to expand folded observations.

## Configuration

`cfx serve --obs-fold [--obs-store obs.db] [--obs-n 3] [--obs-k 3] [--obs-max-tokens N]`
(env: `CF_OBS_FOLD`, `CF_OBS_STORE`, `CF_OBS_N`, `CF_OBS_K`, `CF_OBS_MAX_TOKENS`). The
flag is **OFF by default**; when off the proxy is byte-identical to the pre-folding
sidecar. The obs store is a dedicated SQLite file (folded content + `fold_log` receipt),
kept separate from the fabric store so stashed observations are never compiled into
packets. With the flag on but no `--obs-store`, folding still reduces tokens but stubs are
not reversible.

### Weak / short-lived agents

With the defaults (`--obs-n 3 --obs-k 3`) nothing folds until a session has **6** tool
messages: the mask keeps the last `n=3` verbatim and the fold boundary only advances every
`k=3` new observations, so the first fold fires at `n+k=6`. A weak or short-lived sub-agent
(e.g. a 1.5B model that errors or wanders off after 2–4 tool calls) rarely reaches that, so
the feature silently does nothing on exactly the traffic it targets (measured in
`docs/engineering/DOGFOOD.md` §3/§5 S6). For those agents, run
`cfx serve --obs-fold --obs-n 1 --obs-k 1 --obs-max-tokens <window>` so folding engages from
the first couple of tool messages. The defaults are unchanged (frozen for the B8 pilot).

## What it does NOT claim

* **No savings numbers.** Token, KV, and cost reductions are not measured here; the
  external literature reports 18-60% token / 30-58% KV reductions for ReFold-style
  folding, but Context Fabric has no measurement of its own yet. A separate, pre-registered
  run must produce those numbers before any headline.
* **No accuracy claim.** Folding older observations *can* lose dead-end signals the model
  would otherwise see; whether that changes task success is unmeasured. The last `N`
  observations stay verbatim to bound the risk, but parity is not asserted.
* **Only tool messages fold.** System, user, and assistant messages are never folded, so a
  budget that is exceeded by reasoning traces alone is not recoverable by this mechanism.
* **Fold events are not yet sealed into a signed epoch.** They live in the append-only
  `fold_log` receipt (hash-linked by `seen_hash`); committing them to the signed
  `protocol/epoch.py` chain is future work.
* **Stub ids are a 48-bit content prefix.** A collision would surface as a `StoreConflict`;
  the probability is negligible at sub-agent scale but not cryptographic.

## Determinism

No clock (fold timestamps are parameters), no RNG, input order is authoritative, stub ids
are content-addressed, and `seen_hash` uses RFC 8785 canonicalization. The `first line`
display hint is truncated to 80 chars for a short stub; reversibility depends only on the
sha256 id, not the hint.
