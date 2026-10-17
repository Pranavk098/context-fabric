# Context Fabric (CPP v1.0)

A framework-agnostic **semantic MMU / context compiler** for AI agents. It compiles shared
project truth into bounded, prefix-cache-aligned, cryptographically auditable packets so
agents wake up with instant comprehension at a fraction of the token, latency, and VRAM cost.

**Thesis:** treat an agent's context window as a tiny L1 register and build the memory
management unit around it — shared Tier 0 cartridge + Tier 1 ledger, Copy-on-Write sub-agent
forking, first-class negative knowledge, and bounded append-only epochs.

## Status

Stage B (falsification) is **complete**. The load-bearing bet is validated on real hardware
(vLLM + an 8 GB RTX 5070). Headline, reproducible results:

| Result | Number |
| :--- | :--- |
| Cross-agent cost vs best-case naive broadcast | **~2×–9×** |
| Role-divergent children (naive can't share) | **9.5×** |
| Throughput at 16 concurrent agents | **~8.6×** (44.8 vs 5.2 req/s) |
| KV-cache footprint at 16 concurrent | **20× lower** |
| Compression on real code (`httpx`) | **~17×** |
| Cold-boot (hologram vs raw) | **10× fewer tokens, 2× accuracy** |
| Quality parity | **100% at ~5.6× compression** |

Honest caveats: intra-agent is a *tie* (prefix caching already helps); there is a
small-context crossover (fixed by an adaptive cartridge); model-tier arbitrage is
undemonstrated. Full audit: `docs/engineering/AUDIT.md`.

## Quickstart

```bash
uv sync --extra dev
uv run python -m evals.spike_deterministic   # zero-GPU deterministic-core check
uv run pytest -q
```

KV-cache / concurrency experiments need an OpenAI-compatible endpoint —
see `docs/engineering/RUNTIME.md`.

## Repository layout

```
protocol/   CPP v1.0 immutable schemas + RFC 8785 canonicalization (deterministic core)
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
