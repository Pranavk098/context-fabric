# Contributing to Context Fabric

## Repository layout

| Path | What it is | Generation |
|---|---|---|
| `cfxd/`, `hooks/`, `.claude-plugin/` | The Claude Code plugin: daemon, launcher, handlers (brief, carry, fold), `audit`, `replay`, manifests | current |
| `tests/test_cfxd_*.py` | Tests for the plugin | current |
| `spikes/` | Throwaway experiments kept as evidence (hook capabilities) | current |
| `protocol/`, `kernel/`, `sidecar/`, `adapters/` | Context compiler, OpenAI-compatible sidecar (`cfx`), framework adapters | first generation; not used by the plugin |
| `evals/` | Experiment harnesses and the raw reports behind published numbers | both |
| `docs/` | Index in `docs/README.md`; decisions in `docs/decisions/` | both |

## Development environment

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                      # create the environment (dev tools are in the default `dev` group)
uv run python -m evals.spike_deterministic   # zero-GPU deterministic-core check
uv run pytest -q             # tests
```

For the Stage-B / audit experiments (KV-cache, concurrency/VRAM) you need an
OpenAI-compatible endpoint with prefix caching — see `docs/engineering/RUNTIME.md`.

## Quality gates (must pass before merge)

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy protocol evals kernel sidecar adapters
uv run pytest -q
```

CI runs the same gates on every push (`.github/workflows/ci.yml`).

## Keeping the README current

The README is the product's public record, so it changes in the same commit as the code:

- A new or renamed `cfxd` command goes in the README table and `docs/INSTALL-ALPHA.md` (`tests/test_readme_up_to_date.py` fails otherwise).
- A measured number goes in `docs/engineering/NUMBERS.md` with its artifact, then in the README (`scripts/check_docs_numbers.py` fails on drift).
- A mechanism's state (`working`, `experimental`, `off by default`) is updated when its evidence changes; nothing is called a saving until a paired test shows it.
- The M8ven badge line in the README is one intact line: it carries the ownership proof.

## Conventions

- **Deterministic core:** no learned components; no network in `protocol/`.
- **Frozen models:** `pydantic` v2, `extra="forbid"`; schema changes require an ADR.
- **Every non-trivial change ships one runnable check** (a test or a `__main__` self-check).
- **ADRs:** architectural decisions go in `docs/decisions/` as `ADR-NNNN-title.md`.
- **Findings:** experiment results go in `docs/engineering/ENGINEERING-LOG.md`;
  gaps/classification in `docs/engineering/AUDIT.md`.

## Commit style

Concise, imperative mood, scoped (e.g. `protocol: reject unknown packet fields`).
