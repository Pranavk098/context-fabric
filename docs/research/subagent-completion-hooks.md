# Sub-agent completion hooks in Claude Code (R-3 research)

Research step for **R-3 · Carry-back on child completion** (`docs/plans/2026-10-31-feature-rebuild-plan.md`).

Questions to answer:
1. Which Claude Code hook fires when a sub-agent (including background / async) finishes?
2. What are its payload fields (agent id, transcript path, result), with the official doc URL for each field?
3. Does `PostToolUse` on `Agent` see async results?
4. Can the completion hook reliably attach a carry-back summary to the **parent** session?

---

## 1. Executive summary (verdict)

1. **Hook that fires on child completion:** **`SubagentStop`** fires when any Claude Code sub-agent (foreground or background/async) finishes responding ([`code.claude.com/docs/en/hooks#subagentstop`](https://code.claude.com/docs/en/hooks#subagentstop)).
2. **Does `PostToolUse` on `Agent` see async results?** **No.** When a sub-agent runs in the background (which is the default in Claude Code when fork mode is on, or when `run_in_background: true` is passed, or when a foreground sub-agent is backgrounded mid-run), the `Agent` tool returns immediately at launch/transition with `tool_response.status: "async_launched"`. `PostToolUse` on `Agent` fires at that launch moment and never fires again when the background sub-agent later completes ([`code.claude.com/docs/en/hooks#agent`](https://code.claude.com/docs/en/hooks#agent)). In our local Gate 1 corpus (`docs/gate1-rediscovery.md`), **256 of 424** joined children ran background/async, and in the v4 pilot (`docs/plans/v4-pilot-notes.md`), `carry_fired` was false in every run because the `Agent` calls returned `async_launched`.
3. **Can `SubagentStop` directly attach `additionalContext` to the parent?** **No.** Per the official decision-control specification for `SubagentStop`, returning `hookSpecificOutput.additionalContext` (or `decision: "block"`) on `SubagentStop` does **not** inject context into the parent session; instead, it prevents the sub-agent from stopping and feeds the context back to the **sub-agent** so the sub-agent keeps running ([`code.claude.com/docs/en/hooks#subagentstop`](https://code.claude.com/docs/en/hooks#subagentstop), [`code.claude.com/docs/en/hooks#stop-decision-control`](https://code.claude.com/docs/en/hooks#stop-decision-control)). The docs explicitly state: *"To inject context into the parent session after a subagent returns, use a `PostToolUse` hook on the `Agent` tool instead"* — which only works for synchronous foreground children.
4. **How background results reach the parent (and the only viable two-step workaround):** A finished background sub-agent delivers its result to the parent session as a `<task-notification>` prompt in a subsequent turn, which fires **`UserPromptSubmit`** on the parent session ([`code.claude.com/docs/en/hooks#userpromptsubmit`](https://code.claude.com/docs/en/hooks#userpromptsubmit), [`code.claude.com/docs/en/sub-agents#tools-permissions-and-results-of-background-subagents`](https://code.claude.com/docs/en/sub-agents#tools-permissions-and-results-of-background-subagents)). Therefore, no single hook on child completion can directly attach parent context for async children. Achieving carry-back for async children requires either:
   - **Two-hook staging:** record the summary in `cfxd` on `SubagentStop` (keyed by `session_id` + `agent_id`, reading `agent_transcript_path`), and inject it via `hookSpecificOutput.additionalContext` on the parent's `UserPromptSubmit` when the `<task-notification>` carrying `<task-id>` (= `agent_id`) arrives; or
   - **Synchronous `PostToolUse:Agent` when foreground:** reading `agentId` from `tool_response.agentId` and parsing the child transcript `<session_id>/subagents/agent-<agentId>.jsonl`.

---

## 2. Why `PostToolUse` on `Agent` misses async children

Source: [`https://code.claude.com/docs/en/hooks#agent`](https://code.claude.com/docs/en/hooks#agent) and [`https://code.claude.com/docs/en/sub-agents#which-subagents-run-in-the-background`](https://code.claude.com/docs/en/sub-agents#which-subagents-run-in-the-background).

When `PreToolUse` and `PostToolUse` match `"Agent"`, `PostToolUse` receives `tool_response` with one of two shapes:

| Launch mode | `tool_response.status` | Fields present in `tool_response` at `PostToolUse` | Sees child result? | Doc citation |
|---|---|---|---|---|
| **Foreground** (waited to completion) | `"completed"` | `status`, `agentId`, `content` (final text blocks, or a short note if delivered via `SubagentHandback`), `resolvedModel`, `modelsUsed`, `totalTokens`, `totalDurationMs`, `totalToolUseCount`, `usage` | **Yes** (except when `SubagentHandback` is used in auto mode, where the report is in `PreToolUse`/`PostToolUse` on `SubagentHandback` under `tool_input.message`) | [`code.claude.com/docs/en/hooks#agent`](https://code.claude.com/docs/en/hooks#agent) |
| **Background / async** (`run_in_background: true`, default fork mode, or Ctrl+B mid-run) | `"async_launched"` | `status` (`"async_launched"`), `agentId`, `description`, `prompt`, `outputFile`, `resolvedModel` | **No.** Fires when the task moves to the background, before the child finishes. No usage, no `content`, and no second `PostToolUse` when the child later finishes. | [`code.claude.com/docs/en/hooks#agent`](https://code.claude.com/docs/en/hooks#agent) |

Furthermore, `cfxd/brief.py` currently builds carry-back by diffing the parent's in-memory `SessionFacts` between `PreToolUse:Agent` and `PostToolUse:Agent`, and explicitly skips whenever `record.background` is true or another child is concurrent (`skip_reason: "background"` / `"concurrent"`). Even if `record.background` were not set on `tool_input`, any sub-agent that Claude Code launches asynchronously returns `status: "async_launched"` in milliseconds, before the child has executed a single tool call.

---

## 3. `SubagentStop` hook: when it fires and payload fields

Source: [`https://code.claude.com/docs/en/hooks#subagentstop`](https://code.claude.com/docs/en/hooks#subagentstop) and [`https://code.claude.com/docs/en/hooks#common-input-fields`](https://code.claude.com/docs/en/hooks#common-input-fields).

### When `SubagentStop` fires
- Fires whenever a Claude Code sub-agent has finished responding (both foreground and background/async sub-agents, as well as sub-agent frontmatter `Stop` hooks which Claude Code converts to `SubagentStop` at runtime: [`code.claude.com/docs/en/sub-agents#hooks-in-subagent-frontmatter`](https://code.claude.com/docs/en/sub-agents#hooks-in-subagent-frontmatter)).
- **Important caveat — internal agents:** `SubagentStop` also fires when Claude Code's own internal agents finish (such as prompt suggestions and `/btw` side questions). For those internal runs, `agent_type` is the session's `--agent` name or `""` (empty string) when the session runs without one ([`code.claude.com/docs/en/hooks#subagentstop-input`](https://code.claude.com/docs/en/hooks#subagentstop-input)). Any handler listening on `SubagentStop` with `"*"` or omitted matcher must filter out empty/internal `agent_type` or verify that `agent_id` / `agent_transcript_path` corresponds to a real sub-agent.

### `SubagentStop` JSON input payload fields

Every field below is passed on `stdin` (for `type: "command"`) or POST body (for `type: "http"`):

| Field | Type | Description & caveats | Official doc URL |
|---|---|---|---|
| `hook_event_name` | `string` | Always `"SubagentStop"`. | [`https://code.claude.com/docs/en/hooks#common-input-fields`](https://code.claude.com/docs/en/hooks#common-input-fields) |
| `session_id` | `string` | Identifier of the parent session. | [`https://code.claude.com/docs/en/hooks#common-input-fields`](https://code.claude.com/docs/en/hooks#common-input-fields) |
| `transcript_path` | `string` | Path to the **main (parent) session's** `.jsonl` transcript file. Written asynchronously and may lag in-memory state. | [`https://code.claude.com/docs/en/hooks#common-input-fields`](https://code.claude.com/docs/en/hooks#common-input-fields) · [`https://code.claude.com/docs/en/hooks#subagentstop-input`](https://code.claude.com/docs/en/hooks#subagentstop-input) |
| `cwd` | `string` | Current working directory when the hook is invoked. | [`https://code.claude.com/docs/en/hooks#common-input-fields`](https://code.claude.com/docs/en/hooks#common-input-fields) |
| `permission_mode` | `string` | `"default"`, `"plan"`, `"acceptEdits"`, `"auto"`, `"dontAsk"`, or `"bypassPermissions"`. | [`https://code.claude.com/docs/en/hooks#common-input-fields`](https://code.claude.com/docs/en/hooks#common-input-fields) |
| `prompt_id` | `string` (optional) | UUID identifying the user prompt currently being processed (matches OTel `prompt.id`). | [`https://code.claude.com/docs/en/hooks#common-input-fields`](https://code.claude.com/docs/en/hooks#common-input-fields) |
| `scratchpad_dir` | `string` (optional) | Session scratchpad directory (v2.1.257+). | [`https://code.claude.com/docs/en/hooks#common-input-fields`](https://code.claude.com/docs/en/hooks#common-input-fields) |
| `effort` | `object` (optional) | `{"level": "low"\|"medium"\|"high"\|"xhigh"\|"max"}` on models with effort. | [`https://code.claude.com/docs/en/hooks#common-input-fields`](https://code.claude.com/docs/en/hooks#common-input-fields) |
| **`agent_id`** | `string` | Unique identifier for the sub-agent instance (e.g. `"def456"`). Matches `tool_response.agentId` on the parent's `Agent` tool call and the filename `agent-<agent_id>.jsonl`. | [`https://code.claude.com/docs/en/hooks#common-input-fields`](https://code.claude.com/docs/en/hooks#common-input-fields) · [`https://code.claude.com/docs/en/hooks#subagentstop-input`](https://code.claude.com/docs/en/hooks#subagentstop-input) |
| **`agent_type`** | `string` | Agent type name used for matcher filtering (e.g. `"Explore"`, `"general-purpose"`, frontmatter `name`, or `"plugin:name"`). Empty string `""` for internal Claude Code agents when no `--agent` is set. | [`https://code.claude.com/docs/en/hooks#common-input-fields`](https://code.claude.com/docs/en/hooks#common-input-fields) · [`https://code.claude.com/docs/en/hooks#subagentstop-input`](https://code.claude.com/docs/en/hooks#subagentstop-input) |
| **`agent_transcript_path`** | `string` | Path to the **sub-agent's own transcript** stored in the nested `subagents/` folder (`<session_id>/subagents/agent-<agent_id>.jsonl`). Note: transcript files are written asynchronously and may slightly lag the final turn at hook time. | [`https://code.claude.com/docs/en/hooks#subagentstop-input`](https://code.claude.com/docs/en/hooks#subagentstop-input) · [`https://code.claude.com/docs/en/hooks#common-input-fields`](https://code.claude.com/docs/en/hooks#common-input-fields) |
| **`last_assistant_message`** | `string` | Text content of the sub-agent's final response, available directly without parsing `agent_transcript_path`. Caveat: on v2.1.271+ in auto mode, if the sub-agent delivered its report via the `SubagentHandback` tool, `last_assistant_message` holds only closing text (if any), while the report itself is in `SubagentHandback`'s `tool_input.message`. | [`https://code.claude.com/docs/en/hooks#subagentstop-input`](https://code.claude.com/docs/en/hooks#subagentstop-input) |
| `stop_hook_active` | `boolean` | `true` when the sub-agent is already continuing as a result of a previous `SubagentStop` hook block/feedback. | [`https://code.claude.com/docs/en/hooks#subagentstop-input`](https://code.claude.com/docs/en/hooks#subagentstop-input) · [`https://code.claude.com/docs/en/hooks#stop-input`](https://code.claude.com/docs/en/hooks#stop-input) |
| `background_tasks` | `array` | In-flight background tasks (`id`, `type`, `status`, `description`, etc.) scoped to the **parent** session. | [`https://code.claude.com/docs/en/hooks#subagentstop-input`](https://code.claude.com/docs/en/hooks#subagentstop-input) · [`https://code.claude.com/docs/en/hooks#stop-input`](https://code.claude.com/docs/en/hooks#stop-input) |
| `session_crons` | `array` | Scheduled wakeups scoped to the **parent** session. | [`https://code.claude.com/docs/en/hooks#subagentstop-input`](https://code.claude.com/docs/en/hooks#subagentstop-input) · [`https://code.claude.com/docs/en/hooks#stop-input`](https://code.claude.com/docs/en/hooks#stop-input) |

*(Note: `SubagentStop` does **not** include the parent `Agent` call's `tool_use_id`. Joining `SubagentStop` back to a `PreToolUse:Agent` receipt requires matching `agent_id` against the parent transcript's `toolUseResult.agentId`, or hashing the brief packet in the first user entry of `agent_transcript_path`, as `cfxd/brief_receipts.py` already does.)*

---

## 4. Why `SubagentStop` output cannot inject into the parent directly

Source: [`https://code.claude.com/docs/en/hooks#subagentstop-input`](https://code.claude.com/docs/en/hooks#subagentstop-input), [`https://code.claude.com/docs/en/hooks#stop-decision-control`](https://code.claude.com/docs/en/hooks#stop-decision-control), and [`https://code.claude.com/docs/en/hooks#add-context-for-claude`](https://code.claude.com/docs/en/hooks#add-context-for-claude).

`SubagentStop` shares the `Stop` decision-control contract:
- Returning `{"decision": "block", "reason": "..."}` or exiting with code `2` **prevents the sub-agent from stopping** and feeds `reason` into the **sub-agent** as its next instruction.
- Returning `{"hookSpecificOutput": {"hookEventName": "SubagentStop", "additionalContext": "..."}}` **also keeps the sub-agent running**:
  > *"SubagentStop hooks use the same decision control format as Stop hooks, including `hookSpecificOutput.additionalContext` with `hookEventName` set to `"SubagentStop"`, for non-error feedback that keeps the subagent running... To inject context into the parent session after a subagent returns, use a `PostToolUse` hook on the `Agent` tool instead."*
  > — [`https://code.claude.com/docs/en/hooks#subagentstop-input`](https://code.claude.com/docs/en/hooks#subagentstop-input)

Thus, returning `additionalContext` from `SubagentStop` would **not** hand a summary back to the parent; it would force the child to run an extra turn (burning extra child tokens up to the 8-continuation cap).

---

## 5. How child tool calls and completion notifications work in practice

1. **Tool hooks inside sub-agents share `session_id` but tag `agent_id`:**
   Session-wide `PreToolUse` and `PostToolUse` hooks in `settings.json` / plugins also fire for every tool call a sub-agent makes ([`code.claude.com/docs/en/sub-agents#define-hooks-for-subagents`](https://code.claude.com/docs/en/sub-agents#define-hooks-for-subagents)). When a hook fires inside a sub-agent, the common input includes `agent_id` and `agent_type` ([`code.claude.com/docs/en/hooks#common-input-fields`](https://code.claude.com/docs/en/hooks#common-input-fields)). Reading `agent_transcript_path` at completion time is cleaner and self-contained because the child transcript (`<session_id>/subagents/agent-<agent_id>.jsonl`) already has the child's isolated `Read`/`Edit`/`Write`/`Bash`/`TodoWrite` tool calls without concurrency ambiguity against the parent or sibling sub-agents.
2. **How async child completion reaches the parent (`UserPromptSubmit`):**
   When a background sub-agent finishes, Claude Code injects a `<task-notification>` message into the parent session (containing `<task-id>`, `<tool-use-id>`, `<output-file>`, `<status>`, `<summary>`, `<result>`, and `<usage>`). The Hooks reference specifies under [`UserPromptSubmit`](https://code.claude.com/docs/en/hooks#userpromptsubmit):
   > *"`UserPromptSubmit` hooks don't fire only on prompts you type. Claude Code also runs them on:*
   > - *A session-scoped scheduled task firing, including a `/loop` iteration*
   > - *A background subagent reporting back to the session that started it*
   > - *A message another session sends to your main conversation"*

---

## 6. Conclusion & recommendation for R-3

- **Is there a single hook that fires on async sub-agent completion AND attaches `additionalContext` to the parent?** **No.**
  - `PostToolUse` on `Agent` can attach `additionalContext` to the parent, but for background/async children (60% of children in Gate 1) it fires at launch (`status: "async_launched"`) when the child transcript is still empty.
  - `SubagentStop` fires reliably when any child (sync or async) finishes and provides `agent_id`, `agent_type`, `agent_transcript_path`, and `last_assistant_message`, but its `additionalContext` feeds back into the **child** (forcing continuation), not the parent.
- **If we build carry-back from `agent_transcript_path`, how must it be wired?**
  1. **Foreground children (`PostToolUse:Agent` where `tool_response.status == "completed"`):** `tool_response.agentId` is present at `PostToolUse` time, so `PostToolUse:Agent` can read `<transcript_dir>/<session_id>/subagents/agent-<agentId>.jsonl` directly and return `hookSpecificOutput.additionalContext` to the parent immediately—eliminating the in-flight `SessionFacts` diff and working even when multiple children overlapped.
  2. **Background/async children (`SubagentStop` → `UserPromptSubmit` queue):** `SubagentStop` builds the capped summary from `agent_transcript_path` and queues it in `cfxd` for `session_id` (returning `{}` from `SubagentStop` so the child stops cleanly). Then, either `UserPromptSubmit` (when the `<task-notification>` prompt arrives in the parent) or the next parent hook drains the queued carry-back block into the parent's `additionalContext`.
