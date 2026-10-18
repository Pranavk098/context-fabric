# Contributing to Context Fabric

## Development environment

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev          # create the environment
uv run python -m evals.spike_deterministic   # zero-GPU deterministic-core check
uv run pytest -q             # tests
```

For the Stage-B / audit experiments (KV-cache, concurrency/VRAM) you need an
OpenAI-compatible endpoint with prefix caching — see `docs/engineering/RUNTIME.md`.

## Quality gates (must pass before merge)

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy protocol evals kernel sidecar
uv run pytest -q
```

CI runs the same gates on every push (`.github/workflows/ci.yml`).

## Conventions

- **Deterministic core:** no learned components; no network in `protocol/`.
- **Frozen models:** `pydantic` v2, `extra="forbid"`; schema changes require an ADR.
- **Every non-trivial change ships one runnable check** (a test or a `__main__` self-check).
- **ADRs:** architectural decisions go in `docs/decisions/` as `ADR-NNNN-title.md`.
- **Findings:** experiment results go in `docs/engineering/ENGINEERING-LOG.md`;
  gaps/classification in `docs/engineering/AUDIT.md`.

## Commit style

Concise, imperative mood, scoped (e.g. `protocol: reject unknown packet fields`).
