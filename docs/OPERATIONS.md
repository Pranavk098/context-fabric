# Operations

How the project stays healthy between releases: the weekly scan, the drift checks, and the
rules that keep builder agents from silently spending real money.

## Weekly routine

Run this on the maintainer machine at the start of the week (Monday), before dispatching work.

1. **Security scan.** `.github/workflows/security.yml` runs every Monday at 06:17 UTC, and can be
   run on demand from the Actions tab (`workflow_dispatch`). It has `contents: read` only and runs:
   - `uvx bandit -r cfxd -ll -q -b .bandit-allow` — fails on any medium-or-higher finding that is
     not in `.bandit-allow`. The allowlist is a small bandit baseline of two confirmed-benign
     `B608` cases in `cfxd/agents/opencode.py`: a table name read from `sqlite_master` (a closed
     set) and a `ts_expr` chosen from two static constants. Both are duplicated with `/` and
     backslash paths so the same file works on Linux and Windows. Anything new fails the job —
     fix it or add a documented, reviewed entry; never widen the scan to `-s B608`.
   - `uvx pip-audit` against the locked dependency set (`uv export --locked --no-hashes`).
2. **Drift checks.** `uv run pytest -q tests/test_readme_up_to_date.py tests/test_workflows.py`
   catches a command missing from the README/install guide and workflow hygiene regressions.
   `uv run python scripts/check_docs_numbers.py` catches stale headline numbers.
3. **Maintainer measurement.** `cfxd week --save` records the week's counts for the next report.
4. **Release.** Follow the checklist in `docs/engineering/RELEASE-v0.2.md` (and later release
   notes) before tagging.

The full gate (`uv run pytest -q`, ruff, mypy) still runs in `.github/workflows/ci.yml` on every
push; this routine is the scheduled outer loop, not a replacement.

## Builder cost rules

Builder agents run on the founder's metered OpenCode account. No builder may spend unbounded.

- **Cheap model only.** Use the cheapest model that can finish the task; never a premium model
  for routine edits, searches or test runs.
- **Budget before and after every wave.** `uv run python scripts/opencode_budget.py` prints one
  line and exits `0` (OK), `1` (WARN) or `2` (STOP). Run it before dispatching a wave and after it
  finishes. On WARN, keep the wave small and stay on the cheap model; on STOP, pause builders.
- **At most 4 concurrent builders.** More does not finish the work faster and multiplies the
  burn when a wave goes wrong.
- **Thresholds** (USD, inclusive): warn at `$5`/day or `$15`/week; stop at `$10`/day or `$30`/week.
  Override without editing code: `OPENCODE_BUDGET_WARN_DAY`, `OPENCODE_BUDGET_WARN_WEEK`,
  `OPENCODE_BUDGET_STOP_DAY`, `OPENCODE_BUDGET_STOP_WEEK`.
- If the `opencode` binary is absent the script prints `opencode not found` and exits `0`, so a
  machine that does not use OpenCode is not blocked.

## Incidents

A suspected vulnerability is **not** a public issue. Report it privately as described in
[`SECURITY.md`](../SECURITY.md) (GitHub **Security → Report a vulnerability**), and do not paste
prompts, paths, commands, tokens or transcripts into issues. The alpha's known limits and the
best-effort masking rules are listed there; re-read them before asking testers for logs.
