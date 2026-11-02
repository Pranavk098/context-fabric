# Research: Other-CLI Session Log Formats (Codex CLI, Gemini CLI, OpenCode)

**Date:** 2026-11-01  
**Work Package:** W1-09  
**Scope:** Session log locations per OS, file formats, per-message token usage fields (input, cached, output), tool-result persistence, hook surfaces, and log-reader feasibility verdicts for OpenAI Codex CLI, Google Gemini CLI, and OpenCode.

---

## Summary Table

| CLI | Default Session Log Path (Linux / macOS / Windows) | File Format | Per-Message Usage Recorded? | Tool Results Recorded? | Hook Surface? | Verdict |
|---|---|---|---|---|---|---|
| **Codex CLI** (`openai/codex`) | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` (Linux/macOS)<br>`%USERPROFILE%\.codex\sessions\YYYY\MM\DD\rollout-*.jsonl` (Windows)<br>(Overridable via `$CODEX_HOME`) | JSONL (`.jsonl`) | **Yes** (`event_msg` with `payload.type == "token_count"`: `input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_output_tokens`, `total_tokens`) | **Yes** (`response_item` with `payload.type == "function_call_output"` / `"custom_tool_call_output"`) | **Yes** (`notify` command in `config.toml`; lifecycle hooks in `config.toml` / `hooks.json`) | **`reader feasible`** |
| **Gemini CLI** (`google-gemini/gemini-cli`) | `~/.gemini/tmp/<project_hash>/chats/session-*.jsonl` (or `.json`) (Linux/macOS)<br>`%USERPROFILE%\.gemini\tmp\<project_hash>\chats\session-*.jsonl` (or `.json`) (Windows)<br>(Overridable via `$GEMINI_CLI_HOME`) | JSONL (`.jsonl`, current) or JSON (`.json`, legacy ≤ v0.38) | **Yes** (`tokens` on `"gemini"` messages: `input`, `output`, `cached`, `thoughts`, `tool`, `total`) | **Yes** (`toolCalls[].result` and `toolCalls[].resultDisplay` on `"gemini"` messages) | **Yes** (`hooks` in `settings.json` or extension `hooks/hooks.json`: `BeforeTool`, `AfterTool`, `SessionStart`, `PreCompress`, etc.) | **`reader feasible`** |
| **OpenCode** (`anomalyco/opencode`, formerly `sst/opencode`) | `~/.local/share/opencode/opencode.db` (Linux/macOS via `xdg-basedir`)<br>`%LOCALAPPDATA%\opencode\Data\opencode.db` or `~/.local/share/opencode/opencode.db` (Windows, `unverified` exact native fallback when `XDG_DATA_HOME` is unset)<br>Legacy: `storage/{session,message,part}/**/*.json` | SQLite (`opencode.db`, v1.2+) or hierarchical JSON files (`.json`, legacy) | **Yes** (`AssistantMessage.tokens`: `input`, `output`, `reasoning`, `cache.read`, `cache.write`, plus `cost`) | **Yes** (`ToolPart.state.output` when `state.status == "completed"`) | **Yes** (In-process JS/TS plugin API: `tool.execute.before`, `tool.execute.after`, `chat.message`, `experimental.session.compacting`) | **`reader feasible`** |

---

## 1. OpenAI Codex CLI (`openai/codex`)

### 1.1 Where Session Logs Live per OS
- **Base Directory (`CODEX_HOME`):**
  - **Linux:** `~/.codex` (or `$CODEX_HOME` if set)
  - **macOS:** `~/.codex` (or `$CODEX_HOME` if set)
  - **Windows:** `%USERPROFILE%\.codex` (e.g., `C:\Users\<user>\.codex`, or `%CODEX_HOME%` if set)
- **Active Session Rollouts:** Stored in date-sharded subdirectories under `sessions/`:
  ```text
  $CODEX_HOME/sessions/YYYY/MM/DD/rollout-<YYYY-MM-DDThh-mm-ss>-<session-uuid>.jsonl
  ```
- **Archived Sessions:** `$CODEX_HOME/archived_sessions/`
- **Flat Input History:** `$CODEX_HOME/history.jsonl` (stores user prompt history lines only, not full turn transcripts).
- **Primary Sources:**
  - [`codex-rs/core/src/config/mod.rs`](https://github.com/openai/codex/blob/main/codex-rs/core/src/config/mod.rs) (`find_codex_home()` resolving `CODEX_HOME` or the user home directory `~/.codex`).
  - [`codex-rs/core/src/rollout/mod.rs`](https://github.com/openai/codex/blob/main/codex-rs/core/src/rollout/mod.rs) (`SESSIONS_SUBDIR = "sessions"`, `ARCHIVED_SESSIONS_SUBDIR = "archived_sessions"`).
  - [`codex-rs/core/src/rollout/recorder.rs`](https://github.com/openai/codex/blob/main/codex-rs/core/src/rollout/recorder.rs) (`RolloutRecorder` creating `YYYY/MM/DD/rollout-<timestamp>-<uuid>.jsonl`).

### 1.2 File Format
- **Format:** Newline-delimited JSON (**JSONL**, `.jsonl`).
- **Structure:** Each line serializes a `RolloutLine` struct with an ISO-8601 `timestamp` and a flattened `RolloutItem` variant (`type` and `payload`):
  - `session_meta` (`SessionMetaLine`: session `id`, `timestamp`, `cwd`, `originator`, `cli_version`, `model_provider`, git info)
  - `turn_context` (`TurnContextItem`: `cwd`, `approval_policy`, `sandbox_policy`, `model`, `effort`, `summary`)
  - `response_item` (`ResponseItem`: `message`, `reasoning`, `function_call`, `function_call_output`, `custom_tool_call`, `custom_tool_call_output`, `local_shell_call`, `web_search_call`)
  - `compacted` (`CompactedItem`: compaction summary message)
  - `event_msg` (`EventMsg`: `token_count`, `user_message`, `agent_message`, `exec_command_begin`, `exec_command_end`, `patch_apply_begin`, `patch_apply_end`, `mcp_tool_call_begin`, `mcp_tool_call_end`, `turn_aborted`, `task_complete`, etc.)
- **Primary Sources:**
  - [`codex-rs/protocol/src/protocol.rs`](https://github.com/openai/codex/blob/main/codex-rs/protocol/src/protocol.rs) (`RolloutLine`, `RolloutItem`, `SessionMetaLine`, `TurnContextItem`, `CompactedItem`, `EventMsg`).
  - [`codex-rs/protocol/src/models.rs`](https://github.com/openai/codex/blob/main/codex-rs/protocol/src/models.rs) (`ResponseItem`).

### 1.3 Per-Message Usage (Input, Cached, Output)
- **Recorded:** **Yes**, per model sampling step / turn via `event_msg` lines where `payload.type == "token_count"` (`TokenCountEvent`).
- **Schema (`TokenUsageInfo` / `TokenUsage`):**
  ```json
  {
    "timestamp": "2026-11-01T12:00:00.000Z",
    "type": "event_msg",
    "payload": {
      "type": "token_count",
      "info": {
        "total_token_usage": {
          "input_tokens": 18400,
          "cached_input_tokens": 14200,
          "output_tokens": 620,
          "reasoning_output_tokens": 256,
          "total_tokens": 19020
        },
        "last_token_usage": {
          "input_tokens": 9500,
          "cached_input_tokens": 8100,
          "output_tokens": 310,
          "reasoning_output_tokens": 128,
          "total_tokens": 9810
        },
        "model_context_window": 200000
      },
      "rate_limits": null
    }
  }
  ```
- **Accounting Semantics:**
  - `last_token_usage` provides the per-response delta; `total_token_usage` provides the running session sum.
  - `cached_input_tokens` is a **subset** of `input_tokens` (so uncached new input tokens = `input_tokens - cached_input_tokens`).
  - `reasoning_output_tokens` is a **subset** of `output_tokens`.
  - OpenAI's API does not charge a separate cache-write token tier, so `TokenUsage` has no `cache_write_tokens` field. Whether non-OpenAI providers configured in Codex CLI report cache-write tokens anywhere in `rollout-*.jsonl` is `unverified`.
  - Duplicate/consecutive `token_count` events can be emitted around turn completion; a reader must deduplicate consecutive identical `total_token_usage` snapshots or sum `last_token_usage` only when `total_token_usage` advances (`unverified` whether every intermediate streaming event emits a distinct `token_count` line across all wire APIs).
- **Primary Sources:**
  - [`codex-rs/protocol/src/protocol.rs`](https://github.com/openai/codex/blob/main/codex-rs/protocol/src/protocol.rs) (`TokenUsage`, `TokenUsageInfo`, `TokenCountEvent`).
  - [OpenAI Prompt Caching Guide](https://platform.openai.com/docs/guides/prompt-caching).

### 1.4 Tool Results Recorded
- **Recorded:** **Yes.** Full tool outputs that enter model context are recorded as `response_item` lines with:
  - `payload.type == "function_call_output"` (`call_id`, `output: { content: string, success?: bool }`)
  - `payload.type == "custom_tool_call_output"` (`call_id`, `output: string`)
- Additionally, execution telemetry is recorded in `event_msg` lines (`exec_command_end` with `stdout`, `stderr`, `exit_code`, `aggregated_output`, `formatted_output`; `mcp_tool_call_end` with `result`).
- **Primary Sources:**
  - [`codex-rs/protocol/src/models.rs`](https://github.com/openai/codex/blob/main/codex-rs/protocol/src/models.rs) (`ResponseItem::FunctionCallOutput`, `FunctionCallOutputPayload`, `ResponseItem::CustomToolCallOutput`).
  - [`codex-rs/protocol/src/protocol.rs`](https://github.com/openai/codex/blob/main/codex-rs/protocol/src/protocol.rs) (`ExecCommandEndEvent`, `McpToolCallEndEvent`).

### 1.5 Hook Surface
- **Turn-Completion Notification (`notify`):** Configured in `~/.codex/config.toml` via `notify = ["program", ...]`. Invokes the external executable with a single JSON argv payload on `agent-turn-complete` (fire-and-forget notification; cannot modify context or intercept tools).
- **Lifecycle Hooks (`config.toml` / `hooks.json`):** Newer Codex CLI releases support lifecycle hooks (`SessionStart`, `PreToolUse`, `PostToolUse`, `Stop`, etc.) in `config.toml` or `.codex/hooks.json`. However, whether `PostToolUse` supports replacing tool output in-flight across all built-in tools (`exec_command`, `apply_patch`) with semantics identical to Claude Code's `updatedToolOutput` is `unverified`.
- **Primary Sources:**
  - [`docs/config.md`](https://github.com/openai/codex/blob/main/docs/config.md).
  - [`codex-rs/core/src/config/mod.rs`](https://github.com/openai/codex/blob/main/codex-rs/core/src/config/mod.rs).

### 1.6 Verdict for Codex CLI
- **Verdict:** **`reader feasible`**
- **Rationale:** Session logs are plain JSONL files at a deterministic per-OS path (`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`), containing both per-turn token usage (`input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_output_tokens`) and full tool-call outputs (`function_call_output`) plus compaction boundaries (`compacted`). This satisfies Decision D-5 for building a `--source codex` beta reader.

---

## 2. Google Gemini CLI (`google-gemini/gemini-cli`)

### 2.1 Where Session Logs Live per OS
- **Base Directory:**
  - **Linux:** `~/.gemini` (or `$GEMINI_CLI_HOME/.gemini` if `GEMINI_CLI_HOME` is set)
  - **macOS:** `~/.gemini` (or `$GEMINI_CLI_HOME/.gemini` if `GEMINI_CLI_HOME` is set)
  - **Windows:** `%USERPROFILE%\.gemini` (`C:\Users\<user>\.gemini`, or `%GEMINI_CLI_HOME%\.gemini`)
- **Session Transcript Path:**
  ```text
  ~/.gemini/tmp/<project_hash>/chats/session-<YYYY-MM-DDThh-mm>-<short-uuid>.jsonl
  ```
  (or `.json` in versions prior to the JSONL streaming migration).
  Here `<project_hash>` is the SHA-256 hex digest of the absolute project root path (`crypto.createHash('sha256').update(projectRoot).digest('hex')`).
- **Primary Sources:**
  - [`packages/core/src/config/storage.ts`](https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/config/storage.ts) (`GEMINI_DIR = ".gemini"`, `getProjectTempDir()`, SHA-256 project directory hashing).
  - [`packages/core/src/services/chatRecordingService.ts`](https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/services/chatRecordingService.ts) (`ChatRecordingService` writing under `<projectTempDir>/chats/session-...`).
  - [Gemini CLI Session Management Documentation](https://geminicli.com/docs/cli/session-management/).

### 2.2 File Format
- **Format:**
  - **Legacy (≤ v0.38):** Single monolithic **JSON (`.json`)** file serializing a `ConversationRecord` object (`sessionId`, `projectHash`, `startTime`, `lastUpdated`, `messages: MessageRecord[]`).
  - **Current:** Append-only **JSON Lines (`.jsonl`)** file (`session-*.jsonl`) where session metadata and `MessageRecord` additions/updates are appended line by line to avoid rewriting large transcripts in memory.
- **Primary Sources:**
  - [`packages/core/src/services/chatRecordingService.ts`](https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/services/chatRecordingService.ts) (`ConversationRecord`, `MessageRecord`, `ToolCallRecord`, `TokensSummary`).

### 2.3 Per-Message Usage (Input, Cached, Output)
- **Recorded:** **Yes**, on each assistant message (`MessageRecord` with `type: "gemini"`) in the `tokens: TokensSummary` field:
  ```json
  {
    "id": "msg-uuid",
    "timestamp": "2026-11-01T12:00:00.000Z",
    "type": "gemini",
    "content": "...",
    "Model": "gemini-2.5-pro",
    "tokens": {
      "input": 12400,
      "output": 450,
      "cached": 9800,
      "thoughts": 180,
      "tool": 0,
      "total": 13030
    }
  }
  ```
- **Accounting Semantics:**
  - `input` corresponds to Gemini API `promptTokenCount` (total prompt tokens, **inclusive** of `cached`).
  - `cached` corresponds to `cachedContentTokenCount` (subset of `input` served from context cache; uncached input = `input - cached`).
  - `output` corresponds to `candidatesTokenCount`.
  - `thoughts` corresponds to `thoughtsTokenCount` (reasoning tokens).
  - `tool` corresponds to `toolUsePromptTokenCount`.
  - `total` corresponds to `totalTokenCount`.
  - Note: Google Gemini `usageMetadata` does not report a separate cache-write token count in `TokensSummary`. Additionally, personal/enterprise OAuth sessions via Code Assist may report `cached: 0` if backend context caching is not active (`unverified` across all OAuth tiers).
- **Primary Sources:**
  - [`packages/core/src/services/chatRecordingService.ts`](https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/services/chatRecordingService.ts) (`TokensSummary`).
  - [`packages/core/src/telemetry/types.ts`](https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/telemetry/types.ts).

### 2.4 Tool Results Recorded
- **Recorded:** **Yes.** Each assistant `MessageRecord` contains `toolCalls?: ToolCallRecord[]`, where each `ToolCallRecord` stores:
  - `id` (string), `name` (string), `args` (object), `status` (string), `timestamp` (string)
  - `result`: `PartListUnion | null` (the raw `functionResponse` payload returned to the model)
  - `resultDisplay`: `ResultDisplay | undefined` (rendered text or diff string)
- **Primary Sources:**
  - [`packages/core/src/services/chatRecordingService.ts`](https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/services/chatRecordingService.ts) (`ToolCallRecord`).

### 2.5 Hook Surface
- **Exists:** **Yes.** Gemini CLI provides a command-hook system configured in `~/.gemini/settings.json`, `<repo>/.gemini/settings.json`, `/etc/gemini-cli/settings.json`, or extension `hooks/hooks.json`.
- **Supported Hook Events:** `SessionStart`, `SessionEnd`, `BeforeAgent`, `AfterAgent`, `BeforeModel`, `AfterModel`, `BeforeToolSelection`, `BeforeTool`, `AfterTool`, `PreCompress`, `Notification`.
- **Protocol:** Executes external commands, passing event JSON on `stdin` and parsing JSON from `stdout` (exit code `0` to allow/modify, exit code `2` to block).
- **Primary Sources:**
  - [Gemini CLI Hooks Documentation](https://geminicli.com/docs/hooks/) and [Writing Hooks Guide](https://geminicli.com/docs/hooks/writing-hooks/).
  - [`schemas/settings.schema.json`](https://github.com/google-gemini/gemini-cli/blob/main/schemas/settings.schema.json).
  - [`packages/core/src/hooks/`](https://github.com/google-gemini/gemini-cli/tree/main/packages/core/src/hooks).

### 2.6 Verdict for Gemini CLI
- **Verdict:** **`reader feasible`**
- **Rationale:** Both legacy `.json` and current `.jsonl` session files under `~/.gemini/tmp/<project_hash>/chats/` record per-message token usage (`input`, `cached`, `output`, `thoughts`, `total`) and full tool results (`toolCalls[].result`), though a reader must support both `.json` and `.jsonl` formats and cannot reverse `<project_hash>` to a project path without scanning known local workspaces.

---

## 3. OpenCode (`anomalyco/opencode`, formerly `sst/opencode`)

### 3.1 Where Session Logs Live per OS
- **Base Data Directory (via `xdg-basedir`):**
  - **Linux:** `~/.local/share/opencode/` (or `$XDG_DATA_HOME/opencode/`)
  - **macOS:** `~/.local/share/opencode/` (or `$XDG_DATA_HOME/opencode/` — note that `xdg-basedir` uses `~/.local/share` on macOS rather than `~/Library/Application Support`)
  - **Windows:** `xdg-basedir` resolves `$XDG_DATA_HOME` or falls back to `%LOCALAPPDATA%\opencode\Data` (or `~/.local/share/opencode` in POSIX/WSL environments; exact Windows path across standalone binaries when `LOCALAPPDATA` vs `XDG_DATA_HOME` is used is `unverified` without running the Windows binary).
- **Storage Layout:**
  - **Current (v1.2+ SQLite backend):** `~/.local/share/opencode/opencode.db` (SQLite database managed via Drizzle ORM with `session`, `message`, and `part` tables).
  - **Legacy (pre-SQLite JSON file backend):**
    - `~/.local/share/opencode/storage/session/<project_id>/<session_id>.json`
    - `~/.local/share/opencode/storage/message/<session_id>/<message_id>.json`
    - `~/.local/share/opencode/storage/part/<message_id>/<part_id>.json`
- **Primary Sources:**
  - [`packages/opencode/src/global/index.ts`](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/global/index.ts) (`Global.Path.data` using `xdgData` from `xdg-basedir`).
  - [`packages/opencode/src/session/session.sql.ts`](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/session.sql.ts) (SQLite schema for `session`, `message`, `part`).
  - [`packages/opencode/src/storage/storage.ts`](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/storage/storage.ts) (file-based JSON storage layout).

### 3.2 File Format
- **Format:**
  - **Current:** **SQLite 3 database (`opencode.db`)** storing structured JSON columns (`data`) in the `message` and `part` tables alongside relational metadata columns on `session`.
  - **Legacy / Export:** **JSON (`.json`)** files sharded across `storage/session/`, `storage/message/`, and `storage/part/`, or single-file JSON emitted by `opencode export [sessionID]`.
- **Primary Sources:**
  - [`packages/opencode/src/session/session.sql.ts`](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/session.sql.ts).
  - [OpenCode CLI Documentation](https://opencode.ai/docs/cli/).

### 3.3 Per-Message Usage (Input, Cached, Output)
- **Recorded:** **Yes**, on every `MessageV2.Assistant` record (`role: "assistant"`):
  ```json
  {
    "id": "msg_01...",
    "sessionID": "ses_01...",
    "role": "assistant",
    "modelID": "claude-sonnet-4-20250514",
    "providerID": "anthropic",
    "cost": 0.0142,
    "tokens": {
      "input": 2150,
      "output": 480,
      "reasoning": 0,
      "cache": {
        "read": 18400,
        "write": 1200
      }
    }
  }
  ```
- **Accounting Semantics:**
  - `tokens.input`, `tokens.output`, `tokens.reasoning`, `tokens.cache.read`, and `tokens.cache.write` are all recorded per assistant message (`MessageV2.Assistant`) and rolled up into session columns (`tokens_input`, `tokens_output`, `tokens_reasoning`, `tokens_cache_read`, `tokens_cache_write`, `cost`) in `session.sql.ts`.
  - Because OpenCode routes to multiple providers (Anthropic, OpenAI, Google, etc.) via the Vercel AI SDK, whether `tokens.input` excludes or includes `tokens.cache.read` depends on how the provider adapter normalizes usage in `session/processor.ts` (`unverified` whether every provider adapter normalizes `tokens.input` identically).
- **Primary Sources:**
  - [`packages/opencode/src/session/message-v2.ts`](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/message-v2.ts) (`MessageV2.Assistant` Zod schema defining `cost` and `tokens: { input, output, reasoning, cache: { read, write } }`).
  - [`packages/opencode/src/session/session.sql.ts`](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/session.sql.ts).

### 3.4 Tool Results Recorded
- **Recorded:** **Yes.** Tool invocations are stored as `MessageV2.ToolPart` records (`type: "tool"` in the `part` table or `storage/part/<message_id>/<part_id>.json`):
  - `callID`: string
  - `tool`: string (e.g., `"bash"`, `"read"`, `"edit"`, `"grep"`)
  - `state`: discriminated union on `status` (`"pending" | "running" | "completed" | "error"`). When `status == "completed"`, `state` contains `input` (tool arguments object), `output` (full string output returned by the tool), `title`, `metadata`, and `time: { start, end, compacted? }`.
- **Primary Sources:**
  - [`packages/opencode/src/session/message-v2.ts`](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/message-v2.ts) (`ToolPart`, `ToolStateCompleted`, `ToolStateError`).

### 3.5 Hook Surface
- **Exists:** **Yes**, as an **in-process JavaScript/TypeScript plugin API** (`@opencode-ai/plugin` / `@opencode/plugin`), not an external shell-command hook runner.
- **Configuration Locations:** `.opencode/plugins/`, `~/.config/opencode/plugins/`, or npm package names in `opencode.json` / `opencode.jsonc`.
- **Supported Hooks:**
  - `tool.execute.before`: `(input: { tool, sessionID, callID }, output: { args }) => Promise<void>` (can mutate `output.args` or throw to abort tool execution)
  - `tool.execute.after`: `(input: { tool, sessionID, callID, args }, output: { title, output, metadata }) => Promise<void>` (can mutate `output.output` before it enters context)
  - `chat.message`, `chat.params`, `chat.headers`, `permission.ask`, `command.execute.before`, `shell.env`, `experimental.chat.messages.transform`, `experimental.chat.system.transform`, `experimental.session.compacting`, `event`.
- **Primary Sources:**
  - [OpenCode Plugins Documentation](https://opencode.ai/docs/plugins/).
  - [`packages/plugin/src/index.ts`](https://github.com/anomalyco/opencode/blob/dev/packages/plugin/src/index.ts) (`Hooks` interface definition).

### 3.6 Verdict for OpenCode
- **Verdict:** **`reader feasible`**
- **Rationale:** OpenCode persists full per-message usage (`input`, `output`, `reasoning`, `cache.read`, `cache.write`, `cost`) and full tool outputs (`ToolPart.state.output`) in `~/.local/share/opencode/opencode.db` (and legacy `storage/{session,message,part}/*.json`), accessible via Python's standard-library `sqlite3` module in read-only mode without extra dependencies.
