# Vendored fixture source

| Field | Value |
| :--- | :--- |
| Upstream repo | https://github.com/pallets/click |
| Commit | `8b19813f2bfca99f1018a587a8cf54fc959f2e5d` (tag `8.5.0`) |
| License | BSD-3-Clause (upstream `LICENSE.txt` kept at the fixture root) |
| Files | 166 upstream files plus this `SOURCE.md` (167 total), `.git` stripped |
| Language | pure Python, no compiled extensions |
| Python | `>=3.10` (upstream pin) |

The tree is the upstream tag unmodified except for this `SOURCE.md`; no source file was
edited, so any diff against the upstream commit is limited to this file.

## Test command

Run from `evals/plugin_ab/fixture_v4/` with any environment that has `pytest` (the repo
`.venv` works). The upstream suite is offline: it only uses local files, sockets bound to
loopback where marked, and no external network.

```sh
PYTHONPATH=src python -m pytest tests --ignore=tests/test_deprecations.py -q
```

`tests/test_deprecations.py` is skipped because it reads installed distribution metadata
via `importlib.metadata.version("click")`; the fixture is imported from `src/` and is not
installed. Every other test runs. Observed on Windows: `1879 passed, 108 skipped, 1 xfailed`
in about five seconds.

## Why this repo

Click is small enough to stay inside the 150-400 file budget, is pure Python with no
runtime dependencies, has a fast offline suite, and has several orthogonal modules
(`core`, `types`, `termui`, `formatting`, `utils`, `_compat`) so each v4 task can require
edits in two or more modules without inventing fake structure.
