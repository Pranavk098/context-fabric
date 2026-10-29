# Fidelity metrics v1

Frozen on 2026-10-29; changes require a dated deviation entry below.

All three metrics are computed per compaction boundary from the transcript lines
before that boundary. The report contains counts and rates only: no paths,
names, summary text or hashes.

## Strict path mention

A whitespace-separated token is a path mention only if, after stripping
surrounding quotes, brackets and punctuation, it satisfies one of:

1. It starts with `/`, `~/`, `./`, `../`, or a drive letter (`X:\` or `X:/`).
2. It contains `/` or `\` and its last segment ends in one of these extensions:
   `py js jsx ts tsx json md toml yaml yml sh go rs java kt c cc cpp h hpp css
   scss html sql txt cfg ini lock xml ipynb rb php cs swift`.
3. It is a bare filename with one of those extensions and at least three
   characters before the dot.

Rejected: URLs (`://`), tokens containing `@`, `and/or`, `w/`, pure
numbers/versions, tokens with spaces, and tokens that are only punctuation.
Tokens are normalised to lower case with forward slashes and no trailing slash.

## M1: unsupported path rate

`(strict path mentions in the summary never seen in any earlier tool input or
tool result of the same transcript) / (all strict path mentions)`.

The report also gives the count per event; an unseen mention may be an
invention or may come from knowledge outside the transcript.

## M2: dropped edit rate

`(files edited or written before the boundary that the summary does not name) /
(files edited or written before the boundary)`.

A path counts as edited for `Edit`, `Write`, `MultiEdit` and `NotebookEdit`
calls. Naming uses the same path matching as recall (full path, separator-
aligned suffix, or basename). Events with no edits are excluded from M2 and
counted separately.

## M3: unsupported pass claims

An event is a pass-claim event when the summary contains, case-insensitively,
one of: `tests pass`, `tests passed`, `all tests pass`, `passing`, `all green`,
`0 failed`, `no failures`.

The claim is unsupported when the most recent test-runner call before the
boundary did not succeed, or no such call exists. Test runners are the
`bound.py` test keys: `pytest`, `npm test`, `yarn test`, `pnpm test`, `go test`,
`cargo test`, `dotnet test`, `mvn test`, `gradle test`, `make test` (plus
`npm run test`). A result did not succeed when its text begins with `Exit code`
followed by a non-zero number, it is flagged `is_error`, or the call was
interrupted or timed out. The report gives the number of pass-claim events and
how many of those are unsupported.

## Deviations

None.
