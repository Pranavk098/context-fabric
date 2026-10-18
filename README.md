# Context Fabric (CPP v1.0)

A framework-agnostic **semantic MMU / context compiler** for AI agents. It compiles shared
project truth into bounded, prefix-cache-aligned, cryptographically auditable packets so
agents wake up with instant comprehension at a fraction of the token, latency, and VRAM cost.

**Thesis:** treat an agent's context window as a tiny L1 register and build the memory
management unit around it — shared Tier 0 cartridge + Tier 1 ledger, Copy-on-Write sub-agent
forking, first-class negative knowledge, and bounded append-only epochs.

## Status

Stage B (falsification) is **complete** and the Stage C kernel (deterministic `.cfx`
cartridge + 9-stage router/compiler) is in place. The v0 product shell is also in place:
the OpenAI-compatible **sidecar** (plug), signed **epoch receipts** (seal), memory-state
**CoW/fold** (shared copy), and a **SQLite store** (filing cabinet). Headline results:

| Result | Number |
| :--- | :--- |
| Cross-agent cost | **~2× vs best-case naive, ~9× vs realistic naive** |
| Role-divergent children (naive can't share) | **9.5×** |
| Throughput at 16 concurrent agents | **~8.6×** vs unshared `B0_divergent` (44.8 vs 5.2 req/s) |
| KV-cache footprint at 16 concurrent | **~9×–20× lower** vs *unshared* raw broadcast (~2× vs a shared raw prefix) |
| Compression on real code (`httpx`) | **~17×** |
| Cold-boot (hologram vs raw, same info) | **~9× fewer tokens, 100% vs 25% accuracy** (n=4×3) |
| Quality parity | **100% at ~5.6× compression** |

### Caveats (see `docs/engineering/VERIFICATION-REPORT.md`)

- The **cold-boot** comparison was re-run information-equal (2026-10-17): the raw arm now
  carries the same symbol→module mapping. Hologram 348 tok/**100%** vs raw 3,146 tok/**25%** —
  a narrow probe (4 questions × 3 reps, 1.5B model), not a general retrieval claim.
- The **20× KV** figure reproduces vs *unshared* raw broadcast (27.95% vs 1.38% peak at k=16)
  but is only **~2×** vs a *shared* raw prefix; a finer-sampled run gives ~9–12×. Baseline- and
  sampling-dependent — the old metric-name error read 0, not 20×.
- The **15× tax** is an arithmetic identity (15 agents × per-hop), not an independent result.
- Intra-agent is a **tie**; there is a small-context crossover (mitigated by an adaptive
  cartridge). Model-tier arbitrage is a **weak signal** (one task, 0.5B vs 1.5B).
- Spec features still **pending**: Postgres store (SQLite v0 exists), framework adapters,
  Shadow Cortex probes, full U-curve scheduling (tail recap shipped; the position probe was a
  null); embeddings recall is optional (`[embeddings]` extra). The kernel realizes the
  deterministic core: cartridge, policy, BM25/hybrid recall, λ-MMR packing, closure, hashing,
  signing.

### The plug demo (`cfx demo`, 2026-10-17)

One command runs two arms against your local model — **broadcast** (every agent receives the
entire raw project state; the strongest naive baseline) vs **compile** (each agent receives a
packet from the shared fabric). Real GPU run (Qwen2.5-1.5B, temp 0, 6 agents, 26-block /
~6.7k-token synthetic fixture):

| Arm | Prompt tokens | Per request | Accuracy | Wall | KV peak |
| :--- | ---: | ---: | ---: | ---: | ---: |
| broadcast | 44,854 | 7,476 | 6/6 | 2.80 s | 0.061 |
| compile | 14,170 | 2,362 | 6/6 | 2.08 s | 0.020 |

**3.17× fewer prompt tokens at equal accuracy** (warm cache both arms; regenerated post-audit;
receipts and method: `docs/engineering/DEMO.md`). Negative-knowledge traps: 1.5B shows no
effect; 3B is directional only — the queue trap complies on manual read but scoring is
instrument-limited, and the metrics trap went 4/4 → 0/4 bad under both scorers in the
regenerated set (n tiny; prompt perturbation confounds greedy outputs). Independent audit:
`docs/engineering/DEMO-REVIEW.md`.

## Quickstart

```bash
uv sync --extra dev
uv run python -m evals.spike_deterministic   # zero-GPU deterministic-core check
uv run pytest -q
```

**Use it as a local plug** — point any OpenAI-compatible tool at the sidecar:

```bash
uv run cfx serve --upstream http://127.0.0.1:8000 --fabric fabric.db  # in front of your vLLM
uv run cfx add --store fabric.db --file block.json                    # quarantined (draft)
uv run cfx add --store fabric.db --file block.json --trusted          # routable
uv run cfx verify epochs.json --pubkey <hex>                          # verify signed receipts
uv run cfx mcp --store fabric.db                                      # MCP tools (search/verify/add)
uv run cfx replay --store fabric.db --packet-id <cpk_...>             # prove a past hand-off
uv run cfx seal --store fabric.db --private-key <hex> --out epochs.json  # seal real traffic
```

See `docs/engineering/SIDECAR.md` for the 10-minute setup and the A/B recipe. KV-cache /
concurrency experiments need an OpenAI-compatible endpoint — see `docs/engineering/RUNTIME.md`.

## Repository layout

```
protocol/   CPP v1.0 immutable schemas + RFC 8785 canonicalization + Ed25519 signing
kernel/     deterministic cartridge compiler (.cfx) + 9-stage router/compiler + CoW epochs + store
sidecar/    OpenAI-compatible context-compiling proxy (the "plug")
evals/      telemetry harness + Stage-B / audit experiments
tests/      unit tests for the deterministic core
scripts/    WSL/GPU bring-up + runners
docs/       documentation (see docs/README.md)
```

## Documentation

Start at **[`docs/README.md`](docs/README.md)**. Key docs:

- `docs/product/Prod-Intro.md` — product & positioning
- `docs/design/Arch-Spec.md` — normative architecture (invariants, tiers, router)
- `docs/design/` — storage, adapters, modules, tech stack, build plan
- `docs/research/` — research foundation + references ledger
- `docs/engineering/` — change log, engineering log, audit, runtime
- `docs/decisions/` — Architecture Decision Records

## License

MIT — see `LICENSE`.
