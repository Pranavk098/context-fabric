# ADR-0009 — Authenticated hook transport: command hooks + per-install token

- **Status:** Accepted
- **Date:** 2026-10-24
- **Evidence:** spike memo `hook-secret-spike.md` (Claude Code 2.1.310, Windows/Git Bash, 3 runs); Claude Code hooks
  reference (command hooks support `async`, read 2026-10-24); `tests/test_cfxd_daemon.py`, `test_cfxd_identity.py`,
  `test_cfxd_launch.py`, `test_cfxd_launch_sh.py`; latency runs on this machine (2026-10-24).

## Context

The external audit's top finding: the local hook port has no authentication, so any local process — or a port
squatter — can read or forge hook traffic. ADR-0006 accepted a "no token" design because Claude Code's `http` hooks
could not send one. The spike confirms that limit precisely: an `http` hook's URL and headers interpolate only
`$VAR` from Claude Code's own environment (gated by `allowedEnvVars`); `${user_config.*}`, `${CLAUDE_PLUGIN_OPTION_*}`
and `${CLAUDE_PLUGIN_ROOT}` stay literal or empty there. A plugin-managed secret therefore cannot reach an `http`
hook. A `command` hook runs our code, so it can read the daemon's 0600 token file and call the daemon with it.

## Decision

1. **The hot-path events move from `http` to `command` hooks.** `PreToolUse` becomes a command hook with
   `matcher: "Agent"` (brief only needs the Agent tool) and runs `sh "${CLAUDE_PLUGIN_ROOT}/cfxd/launch.sh" forward
   PreToolUse`. `PostToolUse` becomes a command hook with `matcher: "*"` and `async: true` (the session-state
   observer returns nothing when fold is off, so it need not block). `SubagentStart`/`SubagentStop` had no handlers
   and are dropped. `SessionStart`/`PreCompact` keep their existing command hooks.
2. **The daemon requires a token on `POST /hooks/*`:** the `X-Cfx-Token` header must equal the value in
   `<state_dir>/token` (compared with `hmac.compare_digest`). Missing or wrong → `401 {"error":"unauthorized"}`
   logged as outcome `rejected` reason `token`, without reading the body. The existing Host/Origin/Content-Type and
   size checks still run first; `/health` stays unauthenticated and returns only its nonce proof as before.
3. **The launcher verifies before it sends.** `launch.py forward` and `session-start` prove the daemon's identity
   (the existing HMAC challenge against the token file) *before* posting any payload. If the token file is missing or
   identity fails, they send nothing and print `{}` (`session-start` keeps its visible squatter warning). `post_hook`
   gains an optional `token` argument. A non-`SessionStart` event never spawns the daemon: if it is not running, the
   hook prints `{}`. The OpenCode/Codex `shim.py` reads the same token file and sends the header.
4. **Speed.** Cold `sh launch.sh forward PostToolUse` on this machine (Windows): p50 ≈ 406 ms, p95 ≈ 424 ms
   (20 runs). The suggested optimisation — prefer an already-installed Python ≥ 3.12 on PATH over `uv run` — was
   measured and rejected here: the only ≥ 3.12 Python on this machine's PATH is the Windows app-execution alias
   (`python3`, ≈ 284 ms to run `launch.py`), which is slower than `uv run`'s resolved CPython 3.12 (≈ 254 ms), and the
   version probe adds another ≈ 160 ms. On macOS/Linux a direct `python3` would win; revisit if testers report
   latency. `PostToolUse` is async, so the blocking cost falls only on `PreToolUse` (Agent) and `PreCompact`.

## Consequences

- On a shared machine, other users can no longer read the token (0600, 0700 dir) or forge hook traffic; a port
  squatter that answers `/health` without a valid proof receives nothing. The remaining limit is **same-user
  processes**, which can always read the user's files. Use the alpha on a single account.
- Every hot-path tool event now spawns a process (~400 ms here), versus the old in-process `http` hook. `PostToolUse`
  is async so it does not block the loop; `PreToolUse` fires only on the Agent tool, not every tool call.
- The token is minted once at daemon start and reused; it never appears in logs or event records.

## Supersedes

- ADR-0006, decision 4 ("No per-start token on `/hooks/*`") and the hot-path part of decision 5. The fixed-port and
  `uv`-prerequisite decisions stand.
