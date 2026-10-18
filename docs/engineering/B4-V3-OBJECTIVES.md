# B4 v3 Blind Objectives

This file documents the question-writing protocol for `evals/fixtures/b4v3_objectives.json`.

## Blind protocol

- Only the httpx source package was read: `C:\Users\<user>\AppData\Local\Temp\opencode\httpx_pilot\httpx` (pinned commit `b5addb64f0161ff6bfe94c124ef76f6a1fba5254`).
- No Context Fabric system code, docs, evals, kernel/sidecar/adapters code, or reports were consulted.
- No models or vLLM instances were run.
- Questions are short, identifier/value-style answers that a real agent working on httpx would need to know.
- Every required and forbidden string was verified to exist in the httpx source; forbidden entries are plausible same-shaped neighbours (sibling classes, methods, constants) that really appear in the code.

## Coverage

- 20 objectives across 4 roles: planner, implementer, tester, reviewer (5 each).
- Facts span 14 distinct files of the `httpx/` package.
- 9 objectives require combining facts from two different source files.

## Verification

Run:

```bash
python evals/fixtures/verify_b4v3_objectives.py
```

It asserts that all recorded excerpts match the actual httpx lines and that every required/forbidden string exists in the referenced source files.

## Artifact

- File: `evals/fixtures/b4v3_objectives.json`
- SHA-256: `5b2c11725195d19a5c34e8625d71cd96fb147a711ce80856ab37765092eeeefb`
- Written (UTC): `2026-10-18T23:54:08+00:00`
