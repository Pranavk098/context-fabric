# v4 hidden tests and reference solutions

This directory is **not** part of the fixture an agent sees. It holds one hidden test
directory per task in `evals/plugin_ab/tasks_v4.json`.

```
fixture_v4_hidden/
  conftest.py                  # puts fixture_v4/src on sys.path
  verify_fixture.py            # runs the three acceptance checks
  <task-id>/
    test_behavior.py           # the feature contract; fails on the untouched fixture
    test_planted.py            # one test per planted fact
```

Reference solutions live outside the fixture in
`evals/plugin_ab/fixture_v4_reference/<task-id>/`:

- `files/` is a file set applied to the untouched fixture: every file under it replaces
  the same relative path.
- `variant.json` is a small patch applied **after** `files/` and deliberately ignores the
  planted facts while keeping the feature contract, so the planted tests must fail and
  the behavior tests must still pass.

`variant.json` uses the patch shape from `evals/plugin_ab/patch.py`: a map of relative
path to a list of `{"old"|"new"}`, `{"create"}`, or `{"append"}` operations.
`verify_fixture.py` imports that applier instead of shelling out to `patch`.

## Task-file fields used by v4

`tasks_v4.json` mirrors `tasks_v2.json` where the shape still fits (`id`, `kind`,
`prompt`, `oracle`-style hidden test path, `needs_read_files`, `stratum`, `fixture`) and
adds:

| Field | Meaning |
| :--- | :--- |
| `hidden_tests` | Directory under `fixture_v4_hidden/` holding behavior and planted tests. |
| `reference` | Patch that implements the task on the untouched fixture. |
| `reference_variant` | Patch applied after `reference`, ignoring planted facts. |
| `planted_facts` | List of `{id, fact, test}`; `fact` is stated only in the parent prompt. |
| `max_turns` | Expected Haiku budget (all tasks are sized for under 25). |
| `timeout` | Per-task harness timeout in seconds (H2 uses it). |

## Run it

From the repo root:

```sh
uv run python evals/plugin_ab/fixture_v4_hidden/verify_fixture.py
```

Exit status 0 means all eight tasks passed all three checks. `--task <id>` runs one task
and `--keep` keeps the temporary tree for inspection.

## Planted-fact convention

A planted fact is something the parent prompt states and the repository cannot reveal:
a required constant/function name, an exact message, or a section/heading decision. Each
fact has exactly one test in `test_planted.py`. The variant patch proves the test is tied
to the fact: it keeps the same observable feature behavior but changes the planted detail.
