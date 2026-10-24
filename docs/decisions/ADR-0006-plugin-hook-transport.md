# ADR-0006 — Plugin hook transport, daemon discovery, and packaging

- **Status:** Accepted
- **Date:** 2026-10-22
- **Evidence:** `docs/engineering/HOOK-SPIKE-2026-10-21.md`, `docs/research/HOOKS-OPENCODE-CODEX.md`,
  Claude Code docs (hooks, plugins reference) read 2026-10-22.

## Context

The plugin's hooks must reach the resident `cfxd` daemon within 50 ms p95, fail open, and work on
Windows, macOS and Linux (first testers: mostly Windows, some macOS, a few Ubuntu). Facts that constrain it:

- Claude Code `http` hooks have a static `url`; `${CLAUDE_PLUGIN_ROOT}` / `${user_config.*}` are not documented
  as substituted there. Header values interpolate only env vars already in Claude Code's own environment and
  listed in `allowedEnvVars`. A daemon started after Claude Code can't put a fresh token there.
- `SessionStart` accepts only `command` and `mcp_tool` hooks (docs; matches the spike).
- Command hooks in exec form (`command` + `args`) run without a shell on every OS; `${CLAUDE_PLUGIN_ROOT}` is
  substituted (forward slashes on Windows). `python` vs `python3` differs by OS.
- A marketplace install copies the plugin's source directory into Claude Code's plugin cache.

## Decision

1. **The repository root is the plugin and its own marketplace** (`.claude-plugin/plugin.json`,
   `.claude-plugin/marketplace.json` with `"source": "./"`, `hooks/hooks.json`). The tracked tree is ~4.4 MB,
   so the cache copy is acceptable, and `cfxd/` is inside the plugin with no build step.
2. **One prerequisite: `uv`.** Hook commands are exec form: `uv run --no-project --quiet --python ">=3.12"
   ${CLAUDE_PLUGIN_ROOT}/cfxd/launch.py <subcommand>`. `uv` is a single binary on all three OSes and fetches
   Python if needed. `cfxd` stays **standard library only**, so nothing else is installed.
3. **Fixed port `47831` on `127.0.0.1`**, hard-coded in `hooks/hooks.json` (`CFX_PORT` overrides for tests and
   `cfxd serve` only). If the port is taken by something else, the daemon refuses to start, hooks fail open,
   and `cfxd doctor` says so.
4. **No per-start token on `/hooks/*`.** Instead: bind 127.0.0.1 only; reject any request with an `Origin`
   header; require `Host` to be `127.0.0.1:<port>` or `localhost:<port>` (DNS-rebinding defence); require
   `Content-Type: application/json`; send no CORS headers. Rationale: browsers are the realistic remote
   attacker against a loopback service, and all three checks stop them; a local process running as the user
   can already read anything the daemon could protect.
5. **Lifecycle:** a `SessionStart` command hook runs `launch.py session-start`: if `/health` fails it spawns
   `cfxd serve` detached (Windows `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`; POSIX
   `start_new_session=True`), waits up to 3 s for health, forwards the `SessionStart` event, prints the
   daemon's answer or `{}`, and always exits 0. `PreCompact` uses the same launcher (`forward PreCompact`).
   Hot-path events (`PreToolUse`, `PostToolUse`, `SubagentStart`, `SubagentStop`) are native `http` hooks.
   One daemon per machine, shared by all sessions.

## Consequences

- Install for a tester: install `uv`, then `/plugin marketplace add <repo>` and `/plugin install`.
- `cfxd` is in the wheel with a `cfxd` command (added 2026-10-24, after the B8 freeze ended), so
  `uvx --from git+https://github.com/Pranavk098/context-fabric cfxd audit` works without a clone. The plugin hooks
  still use `uv run --no-project` and need nothing installed.
- OpenCode gets an in-process TS plugin that calls the same daemon with `fetch()`; Codex uses the command
  shim (≈110 ms per call), so it gets brief and carry first, not per-call fold.
- Revisit the fixed port if testers report clashes; revisit tokens if Claude Code adds substitution in `http`
  hook headers.
