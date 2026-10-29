# Context Packet v0 test vectors

Each file is one serialized checkpoint packet. `valid-*` must pass every enabled
check; `invalid-<rule>-*` must fail exactly the named check.

Run them all:

```bash
for f in docs/specs/vectors/*.json; do cfxd packet validate "$f" || echo "FAILED $f"; done
```

Or in the repo: `uv run python -m pytest -q tests/test_cfxd_packet.py`.

## Valid

| File | What it covers |
| :--- | :--- |
| `valid-minimal.json` | The smallest packet the writer can emit: every required field, all arrays empty. |
| `valid-files.json` | Two files, one with a line count and one with `lines: null`, oldest first, read and edited. |
| `valid-folds.json` | Two saved outputs with 16-hex ids and the `test` / `code` kinds. |
| `valid-commands.json` | Masked commands only, including an already-redacted `API_TOKEN=***`. |
| `valid-todos-note.json` | Open todos, a goal note, a passed test, one file and one command. |
| `valid-test-unknown.json` | A note at the 300-character cap and `test.ok: null` (no exit status in the response). |

## Invalid

| File | Rule it breaks | Check that fails |
| :--- | :--- | :--- |
| `invalid-oversize-packet.json` | 20 long file paths: the render would have to drop file lines to fit 3,000 bytes. | `size` |
| `invalid-unknown-field-packet.json` | An `extra` top-level field; every object is closed. | `schema` |
| `invalid-bad-enum-kind.json` | Fold `kind: "binary"`; only `code`, `test` and `other` exist. | `schema` |
| `invalid-control-character-path.json` | A path containing U+0007 BEL. | `characters` |
| `invalid-unmasked-secret-command.json` | A command still carrying a `ghp_` token. | `masking` |
| `invalid-too-many-files.json` | 201 files; the writer keeps at most 200. | `caps` |
| `invalid-missing-required-cwd.json` | No `cwd`; it is required at restore. | `schema` |
| `invalid-wrong-version.json` | `v: 1`; any version but 0 is not a v0 packet. | `schema` |
| `invalid-note-too-long.json` | A 301-character note; the writer caps at 300. | `caps` |
| `invalid-stale-marker-without-file.json` | A file entry with an empty path, so no staleness mark or line can point at a file. | `provenance` |

The invalid packets are hand-edited on purpose: they are not produced by the
writer. A validator must reject them without crashing.
