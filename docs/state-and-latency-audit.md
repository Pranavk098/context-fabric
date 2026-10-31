# State and latency audit (E-6, E-7)

Generated 2026-10-31 from the cfxd state dir (`CFX_HOME` or `~/.cfx/daemon`) by
`scripts/hook_latency.py` and `scripts/state_audit.py`. Aggregates only: the scripts
never print a path, prompt, command, tool name, session id or field value.

## E-6 · Hook latency

Source: `events.jsonl` (plus its rotated predecessor), one line per hook call,
written by `cfxd/daemon.py`. `latency_ms` is server-side: request accepted to
response written. Per-call deadline is 150 ms, or 1300 ms when `tool_name` is
`Agent`; a missed deadline returns `{}` (fail open).

| Hook event | Calls | p50 ms | p95 ms | p99 ms | Over deadline |
|---|---:|---:|---:|---:|---:|
| PreToolUse | 1,202 | 0.22 | 0.43 | 0.77 | 0 |
| PostToolUse | 131 | 0.70 | 3.14 | 7.00 | 0 |
| SessionStart | 32 | 0.99 | 1.72 | 1.81 | 0 |
| SubagentStop | 6 | 0.15 | 0.21 | 0.22 | 0 |
| SubagentStart | 5 | 0.23 | 0.36 | 0.37 | 0 |
| PreCompact | 1 | 10.10 | 10.10 | 10.10 | 0 |
| **All receipts** | **1,377** | **0.23** | **0.92** | **2.43** | **0** |

Over the handler timeout: **0 of 1,377 (0.0%)**. Errors 0, rejected 0.

**Reading.** On this machine the daemon handled every recorded hook call in under
10.1 ms at the tail; the aggregate p99 is 2.43 ms against a 150 ms deadline. No
receipt shows a timeout or an error, so the server-side hook cost did not delay a
turn in this sample.

**What this does not measure.** These are daemon-side receipts. The client-side
launcher process that a hook spawns, and its process startup, are outside the
receipt and can dominate end-to-end hook wall time. This also does not show whether
any call used the 1300 ms `Agent` deadline, nor whether the distribution holds on
another machine. One machine, 1,377 receipts.

## E-7 · Stored text and ACL

### JSONL logs

Every `*.jsonl` file under the state dir was checked against the documented
content-free schema of its receipt writer. A line is flagged when it has a top-level
field outside that schema, a content-like field with a non-empty value, or a string
value that looks like source code or tool output.

| Log | Lines | Flagged |
|---|---:|---:|
| `briefs.jsonl` | 6 | 0 |
| `carry.jsonl` | 28 | 0 |
| `events.jsonl` | 1,377 | 0 |
| `folds.jsonl` | 19 | 0 |
| **Total** | **1,430** | **0** |

4 files, 1,430 lines, 0 malformed, 0 lines with unknown fields, 0 with content-like
fields, 0 with source-like values, **0 flagged**. The JSONL logs store no excerpt or
tool text on this machine.

### Non-JSONL files

The state dir is not text-free. It also holds documented stores that are not logs:

| Kind | Files | Bytes |
|---|---:|---:|
| Folded-output blobs (`obs/`) | 7 | 299,630 |
| Carry snapshots (`carry/`) | 1 | 605 |
| Other (`token`, `state.json`, `daemon.log`, …) | 4 | 28,685 |

`obs/` is the documented fold store: full tool output is deliberately saved there,
content-addressed, and pruned after 7 days. Carry snapshots hold bounded prose
(todos, constraints) and paths by design. E-7 therefore shows that **the logs are
content-free**, not that the state directory contains no user text. The
encryption-at-rest question is not closed by log cleanliness alone; it rests on the
ACL below plus the single-user-machine assumption documented in `SECURITY.md`.

### Windows ACL

`icacls` on the state dir: **PASS** — no access for Everyone, Users or
Authenticated Users. Only principal types and masks are reported:

| Principal type | Rights |
|---|---|
| owner | (I)(OI)(CI)(F) |
| system | (I)(OI)(CI)(F) |
| administrators | (I)(OI)(CI)(F) |

The `I` flag means the entries are inherited. On this machine, a non-owner,
non-administrator account cannot read the state dir, so the plaintext `obs/` blobs
are not exposed to other users. The check is Windows-only; on other platforms the
script reports UNKNOWN.

## Reproduce

```sh
uv run python scripts/hook_latency.py          # add --json for the raw aggregates
uv run python scripts/state_audit.py           # add --json for the raw aggregates
```

## Limitations

- One machine and one state directory; no second-machine sample.
- Latency is server-side and excludes client/launcher startup.
- The text scan is a schema and heuristic check, not a proof: it inspects JSONL
  fields and string shapes, not the non-JSONL blob stores it counts.
- ACL parsing depends on the local `icacls` output format; an unparseable result is
  reported as UNKNOWN, never as PASS.
