# AGENTS.md

Rules every agent working in this repository follows. Source: docs/plans/2026-11-01-plugin-waves-orca.md section 6.

## Rules

**Scope**
- R1. Touch only the files in your WP. Need another file? Stop and say why in your final message.
- R2. Python 3.12, `uv`. No new dependencies without the orchestrator's written OK.
- R3. No model or API calls in any code path, test or script. No network in tests.
- R4. Do not edit: `evals/plugin_ab/PROTOCOL*.md`, `prereg*`, `evals/reports/`, the README evidence
  table, `docs/FIDELITY-METRICS.md` definitions, `docs/engineering/NUMBERS.md`, frozen-bar files,
  golden files (unless your WP says so, and then the diff is reviewed line by line).
- R5. Never run a formatter over `evals/plugin_ab/fixture_v2/`.

**Privacy (P3 content classes)**
- R6. Anything that reads `~/.claude/projects`, `~/.codex` or any real log prints aggregates only:
  no paths, file names, prompts, commands, tool output, repo names, or timestamps finer than a day.
- R7. Nothing written to the state dir holds excerpt text, prompt text, tool-output text or secrets.
  Allowed: counts, sizes, hashes, model families, relative-path indexes.
- R8. Share payloads are built field by field from an allowlist. Never filter a richer object.

**Honesty**
- R9. No user-facing string says "saves", "saved", "reduces cost" or gives a saving figure unless
  the WP is W1-08 and the label is *observed*. Every number shown carries its evidence class:
  *measured*, *observed*, *estimated*.
- R10. Tool output never writes intent, constraint or decision fields.
- R11. Hook paths: no I/O beyond the state append, no blocking calls. Added p99 budget 2 ms.

**Tests**
- R12. Test first for any policy or measurement: the falsifying test lands in the same commit.
- R13. Flag-off must be byte-identical to today's behaviour; add the test when you add a flag.

**Done means** (run chained, stop at the first failure):
```
uv run ruff check . && uv run ruff format --check . && uv run mypy cfxd && \
uv run pytest -q <your test files> && uv run python scripts/check_docs_numbers.py
```
G-FULL (orchestrator, after each merge): the same chain with `uv run pytest -q` (whole suite).

**Commit** only if all pass. One commit per WP, message `WP-ID: <what changed>`. Do not push or
merge.

**Final message** (exactly these headings): Files changed · Tests added · Commands run (each with
its last 3 output lines) · Deviations from the WP · Open questions.
