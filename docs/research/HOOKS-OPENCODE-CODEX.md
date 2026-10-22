# Hooks & MCP plugin — OpenCode / Codex capability facts

Purpose: map Claude Code's hook surface (already known) onto the two other targets for the
Context Fabric hooks + MCP plugin, so `fold` / `brief` / `carry` can be designed once.

Versions read (cloned `--depth 1` into `%TEMP%/cfx-research`, 2026-10-21):

- **OpenCode** `sst/opencode` @ `c93aaae37157d785f76a8e9a9243a6560e286468` (repo tags itself
  `2.1.2`; local CLI is `2.0.32`). Plugin package `@opencode/plugin` `2.1.2`.
- **Codex** `openai/codex` @ `c2d5ea52e1b7ab11ee2d71dc44ea1ef00f9c8eb3` (repo version
  `0.0.0-dev`).
- **Claude Code** facts taken as given from `code.claude.com/docs/en/hooks` (not re-read).

Every cell cites file path + line (or a doc URL). `unverified` = no evidence found in source.

---

## 1. Capability matrix

| Capability | Claude Code | OpenCode | Codex |
|---|---|---|---|
| Block a tool call | **yes** — PreToolUse deny / `updatedInput` block (docs) | **yes** — `tool.execute.before` throws → call rejected; "only execute.before may fail" [O1] `packages/plugin/src/effect/tool.ts:48-52`; example [O2] `packages/web/src/content/docs/plugins.mdx:243-252` | **yes** — PreToolUse `permissionDecision:"deny"` (needs `permissionDecisionReason`) [C4] `codex-rs/hooks/src/engine/output_parser.rs:461-469`; also `decision:"block"` |
| Rewrite tool input | **yes** — PreToolUse `updatedInput` (full replacement) | **yes** — `tool.execute.before` mutates `input` (args) [O1] `:27`; example `output.args.command=…` [O2] `plugins.mdx:91-99` | **yes** — PreToolUse `updatedInput` + required `permissionDecision:"allow"` [C3] `pre-tool-use.command.output.schema.json:35`; [C4] `output_parser.rs:441-456` |
| Replace tool output | **yes** — PostToolUse `updatedToolOutput` | **yes** — `tool.execute.after` mutates `result` on `status:"completed"` [O1] `:29-45` | **no** — `updatedMCPToolOutput` is parsed but rejected, fails open [C4] `output_parser.rs:431-439`; test [C5] `events/post_tool_use.rs:412-452` |
| Add context to main agent | **yes** — PreToolUse `additionalContext` | **yes** — `session` `context` hook mutates `event.system`/`event.messages` [O3] `packages/plugin/src/effect/session.ts:33-36`; example [O4] `migrate-v1.mdx:203-207` | **yes** — `additionalContext` on PreToolUse / PostToolUse / SessionStart [C3] `:15`; [C5] `post_tool_use.rs:377-410` |
| Inject into sub-agent at spawn | **yes** — SubagentStart `additionalContext` | **partial** — no SubagentStart hook; rewrite the `subagent` tool's `input.prompt` via `execute.before` [O5] `packages/core/src/tool/plugin/subagent.ts:29-48` | **yes** — SubagentStart `additionalContext` [C6] `subagent-start.command.output.schema.json:8` |
| Rewrite sub-agent prompt | **partial** — via PreToolUse `updatedInput` on `Agent` tool | **yes** — `execute.before` on `subagent` tool mutates `input.prompt` (prefixed "You are a subagent spawned by another session.") [O5] `:205-213` | **no** — SubagentStart input has `agent_id/agent_type/model/…`, no prompt field [C6] `subagent-start.command.input.schema.json` |
| Pre-compaction hook | **yes** — PreCompact (manual\|auto) can block | **yes** — `session` `compaction` hook (mutable `system`/`messages`, set `result.summary` to skip model) [O3] `session.ts:38-49,142`; V1 `experimental.session.compacting` [O2] `plugins.mdx:334-385` | **yes** — PreCompact `continue:false` blocks; matcher `manual\|auto` [C8] `pre-compact.command.output.schema.json`; `events/common.rs:312-318` |
| Post-compaction / session-start re-injection | **yes** — SessionStart `source:compact` adds context | **partial** — compaction hook sets `result.summary`/`result.native.messages` (replay) [O3] `:38-49`; `context` hook re-runs on next request; event `session.compaction.ended` [O6] `packages/schema/src/session-event.ts:596-647` | **yes** — PostCompact [C8] `post-compact.command.output.schema.json` + SessionStart `source:compact` [C7] `session-start.command.input.schema.json:36-45` |
| HTTP hook transport | **yes** — `type:"http"` | **no** — plugins are in-process JS/TS; plugin must `fetch()` itself | **no** — handler kinds are `command`/`mcp_tool` only [C2] `codex-rs/config/src/hook_config.rs:180-218` |
| Command hook transport | **yes** — `type:"command"` | **partial** — no command hook type; plugin shells out via Bun `$` [O2] `plugins.mdx:107-122` | **yes** — `type:"command"` (`command`, `commandWindows`, `timeout`, `async`) [C2] `hook_config.rs:181-202` |
| MCP server support | **yes** | **yes** — `mcp` config + MCP tools [O] `packages/core/src/tool/mcp.ts`; `packages/web/src/content/docs/config.mdx` | **yes** — `mcp_servers` config; hooks may dispatch to `mcp_tool` handlers [C2] `hook_config.rs:203-213` |
| Plugin distribution / install | hooks in `settings.json` / `.claude/settings.json` | **npm package or local file** — `.opencode/plugins/`, `~/.config/opencode/plugins/`, `"plugin":[…]`; npm auto-installed via Bun [O7] `plugins.mdx:14-63` | `hooks.json` + `[hooks]` in `config.toml`/`requirements.toml`; handlers reference commands/MCP tools (no separate install) [C2] `hook_config.rs:10-25,180-218` |

Notes:

- OpenCode has two plugin API generations. V1 (string-key returned object,
  `@opencode-ai/plugin`, e.g. `"tool.execute.before"`) is what `plugins.mdx` documents; V2 at HEAD
  is `@opencode/plugin` `Plugin.define({ id, setup(ctx) })` with `ctx.tool.hook("execute.before", …)`.
  Same underlying hooks, renamed [O4] `migrate-v1.mdx:142-164`. Local CLI 2.0.32 sits between the
  two — **verify which generation 2.0.32 actually loads before coding against it** (unverified).
- OpenCode sub-agent tool is `subagent` (formerly `task`); input `{agent, description, prompt,
  model?, sessionID?, background?}` [O5] `subagent.ts:29-48`. The `Agent`-style prompt is
  `prompt`; there is no `subagent_type`.
- OpenCode events are `session.compaction.started/ended/failed` [O6]; the docs event list still
  says `session.compacted` (`plugins.mdx:183`) — treat the schema as authoritative.
- Codex PostToolUse output replacement is the biggest gap vs Claude: the field is present in the
  wire schema but the engine returns "unsupported … fails open" [C4][C5].
- Codex `PermissionRequest` also exists (`updatedInput`/`updatedPermissions` unsupported;
  `allow`/`deny` only) [C4] `output_parser.rs:401-429`.

---

## 2. Forward event JSON to a local HTTP daemon (fails open)

Target: `POST http://127.0.0.1:<PORT>/hooks/<Event>` with the event JSON as body; a hook that
cannot reach the daemon must **not** block/break the agent.

### Claude Code (command hook, `|| true` = fail open)

```jsonc
// .claude/settings.json
{
  "hooks": {
    "PreToolUse":  [{ "matcher": "*", "hooks": [{ "type": "command",
      "command": "curl -s -X POST -H 'Content-Type: application/json' --data-binary @- http://127.0.0.1:8765/hooks/PreToolUse || true" }] }],
    "PostToolUse": [{ "matcher": "*", "hooks": [{ "type": "command",
      "command": "curl -s -X POST -H 'Content-Type: application/json' --data-binary @- http://127.0.0.1:8765/hooks/PostToolUse || true" }] }]
  }
}
```

### OpenCode (in-process plugin; `try/catch` = fail open)

```ts
// .opencode/plugins/http-forward.ts
export const HttpForward = async () => {
  const fwd = async (event: string, body: unknown) => {
    try {
      await fetch(`http://127.0.0.1:8765/hooks/${event}`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      })
    } catch { /* fail open */ }
  }
  return {
    "tool.execute.before": (input: unknown) => fwd("tool.execute.before", input),
    "tool.execute.after":  (input: unknown) => fwd("tool.execute.after", input),
    // note: `event` / `ctx.session.hook("compaction", …)` forward compaction & session events
  }
}
```

### Codex (command hook, `|| true` = fail open)

```jsonc
// ~/.codex/hooks.json
{
  "hooks": {
    "PreToolUse":  [{ "matcher": "*", "hooks": [{ "type": "command",
      "command": "curl -s -X POST -H 'Content-Type: application/json' --data-binary @- http://127.0.0.1:8765/hooks/PreToolUse || true" }] }],
    "PostToolUse": [{ "matcher": "*", "hooks": [{ "type": "command",
      "command": "curl -s -X POST -H 'Content-Type: application/json' --data-binary @- http://127.0.0.1:8765/hooks/PostToolUse || true" }] }]
  }
}
```

All three feed the event JSON on **stdin** (`--data-binary @-`). Codex also allows an `mcp_tool`
handler instead of `command` if the daemon is exposed as an MCP server [C2] `hook_config.rs:203-213`.

---

## 3. Gaps & risks for the three features

- **fold (tool-output folding):** needs PostToolUse output *replacement*.
  Claude ✓, OpenCode ✓ (`execute.after` → `event.result`). **Codex ✗** — `updatedMCPToolOutput`
  is rejected and fails open today [C4][C5], so Codex fold must fall back to *front-of-response
  additionalContext* (PostToolUse `additionalContext` works [C5] `:377-410`) or wait for the
  field to be implemented. Risk: output is still shown to the user un-folded.

- **brief (sub-agent hand-off packet):** needs sub-agent-spawn context injection.
  Claude ✓ (SubagentStart), Codex ✓ (SubagentStart `additionalContext`). **OpenCode partial** —
  no SubagentStart hook; you must rewrite `subagent` `input.prompt`/`input.description` in
  `execute.before` [O5]. Risk: mutating a prompt the model wrote is fragile; and OpenCode's
  sub-agent is a *child session* (`parentID`), not a fresh `Agent` tool call, so "hand-off packet"
  has to be serialized into a single `prompt` string.

- **carry (state re-injection after compaction):** needs post-compaction re-injection.
  Claude ✓ (SessionStart `source:compact`), Codex ✓ (PostCompact + SessionStart `source:compact`).
  **OpenCode partial** — `compaction` hook can *supply* the summary (`result.summary`) or replay
  native messages (`result.native.messages`) [O3] `:38-49`, and `context` re-runs on the next
  request; but there is no clean "after compaction, append state" hook — you must watch
  `session.compaction.ended` [O6] and re-inject via the next `context` hook. Risk: ordering
  between the compaction summary and your re-injected state is not contractually defined.

Cross-cutting: only Codex has a **native MCP hook transport** (handlers can be MCP tools), which
fits the plugin's "MCP server" ambition; OpenCode needs an in-process JS/TS shim, Claude/Codex
need a `command` shim. No client supports a plain `http://` hook transport out of the box.

---

## 4. Unverified

- Which OpenCode plugin API generation local CLI `2.0.32` actually loads (repo HEAD is `2.1.2`).
- Whether Codex `PermissionRequest` `ask`/`updatedInput` will be wired (currently unsupported [C4]).
- Whether Codex `updatedMCPToolOutput` is on any near-term roadmap (schema exists, engine rejects).
- Exact `additionalContext` size limits: OpenCode unbounded (plugin-controlled), Codex has a
  spilling threshold (`additionalContextLimit`, default ~2500 tokens) [C2] `hook_config.rs:192-201` — not re-measured.
