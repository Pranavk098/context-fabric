# Install and reference

Context Fabric is an alpha Claude Code plugin. Its objective is the same work with fewer tokens; whether it achieves
that is **not yet proven** (see [Evidence](../README.md#evidence-so-far)). If you are testing it, follow
[`TESTING-GUIDE.md`](TESTING-GUIDE.md); this page is the reference.

## 1. Requirements

| Need | Why | Check |
| :--- | :--- | :--- |
| Claude Code | host agent | `claude --version` |
| [uv](https://docs.astral.sh/uv/) | runs the plugin's Python; fetches Python if missing | `uv --version` |
| Python 3.12 available to uv | avoids a download inside a hook, which can time out | `uv python list --only-installed` |
| Windows only: Git for Windows | the plugin's start-up script runs in Git Bash | `bash --version` |

Install what is missing (run once, then open a **new** terminal):

**macOS / Linux**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv python install 3.12
curl -fsSL https://claude.ai/install.sh | bash        # Claude Code
```

**Windows (PowerShell)**

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
winget install --id Git.Git -e
uv python install 3.12
irm https://claude.ai/install.ps1 | iex               # Claude Code
```

## 2. Add the plugin

Either form installs the same plugin at user scope.

| Where | How |
| :--- | :--- |
| Terminal (any system) | `claude plugin marketplace add Pranavk098/context-fabric` then `claude plugin install context-fabric@context-fabric` |
| Inside Claude Code (CLI) | `/plugin marketplace add Pranavk098/context-fabric` then `/plugin install context-fabric@context-fabric` |
| VS Code extension | type `/plugins`, Marketplaces tab, add `Pranavk098/context-fabric`, then Install |
| Desktop app, Code tab (local sessions) | **+ → Add plugins**, add marketplace `Pranavk098/context-fabric`, install |
| JetBrains | use the terminal forms above in the IDE terminal |
| Claude Code on the web | not supported (cloud sessions do not load plugins) |

The CLI is the path we test. Hook execution in the desktop app and VS Code is expected but **not verified** by us. The
terminal, desktop app (local sessions) and VS Code share one `~/.claude`, so installing in one makes it available in
the others.

Restart Claude Code. The first session starts the local helper on `127.0.0.1:47831`.

**Pin a version** (once releases exist): `claude plugin marketplace add Pranavk098/context-fabric#<tag>`.

## 3. Verify

From any terminal (this runs `cfxd` from GitHub; nothing needs to be cloned):

```bash
uvx --from git+https://github.com/Pranavk098/context-fabric cfxd doctor
```

Expect `healthy` and `identity: verified`. `identity` confirms the program on port 47831 is the daemon that owns your
token file.

## 4. Commands

Each command is `uvx --from git+https://github.com/Pranavk098/context-fabric cfxd <command>`.

| Command | Output |
| :--- | :--- |
| `doctor` | health, daemon identity, feature flags, hook latency |
| `status` | running or not, pid, port, version |
| `stats [--since 7d] [--session ID] [--json]` | folds and hook timings from the daemon's logs |
| `audit [--since 7d\|30d\|all] [--json]` | where your sessions' tokens go (reads local transcripts; works without the plugin) |
| `diet [--since 30d] [--share] [--json] [--apply [--yes] \| --undo [--all]]` | your starting-prefix items, which were never used, per-turn cost; `--share` hashes all names so it can be pasted; `--apply` disables fully-unused local plugins (dry run unless `--yes`), `--undo` restores them |
| `waste [--since 30d] [--json]` | cost share of loops, retry streaks, tail sessions and repeated discovery |
| `routing [--since 30d] [--prices FILE] [--json]` | tokens and API-equivalent dollars by model and role; what-if for cheaper sub-agent tiers (price only) |
| `agent-audit --agent codex\|opencode [--since all]` | counts-only breakdown of Codex or OpenCode session logs (Codex needs the optional `zstandard` decoder) |
| `replay [--since 30d] [--json]` | simulated saving from shrinking old tool output, with cache effects |
| `set brief\|carry\|fold\|fold_read\|bound on\|off` | feature flags (brief and carry on; fold, fold_read and bound off) |
| `stop` | stop the daemon (the next session starts it again) |

`audit`, `replay` and `stats` print counts and sizes only: no paths, commands, prompts or text.

## 5. What it stores

All under `~/.cfx/daemon/` (Windows: `C:\Users\<you>\.cfx\daemon\`). Nothing is sent over the network.

| Path | Content | Kept |
| :--- | :--- | :--- |
| `events.jsonl`, `briefs.jsonl`, `carry.jsonl`, `folds.jsonl`, `bound.jsonl` | event names, sizes, counts, hashes, latencies | rotated by size |
| `carry/<session>.json` | one note per compacted session: file paths, line counts, recent commands with obvious secrets masked, to-do text | 7 days |
| `obs/` | full copies of folded output (**only if you turn fold on**; may contain code or command output) | 7 days |
| `token` | per-install secret that authenticates hook calls (readable only by you) | until deleted |
| `config.json`, `state.json`, `daemon.log` | flags, pid and port, daemon messages | until deleted |

The helper receives each tool call's input and output in memory while Claude Code runs; it needs them to build brief
and carry notes. It does not write that content to disk, except the opt-in fold copies.

## 6. Security model

Fail-open hooks, loopback-only daemon, per-install token, daemon identity checked before any payload is sent. Any
process running **as you** can read the token, so use it on a personal account, not a shared login. Details and
reporting: [`SECURITY.md`](../SECURITY.md).

## 7. Troubleshooting

| Symptom | Fix |
| :--- | :--- |
| Warning that `uv` cannot be found | install uv (above), open a new terminal, restart Claude Code |
| `doctor` says not running | start a new Claude Code session, run `doctor` again |
| `doctor` says identity NOT verified | something else holds port 47831, or an old daemon is running: `cfxd stop`, restart Claude Code |
| Port 47831 in use | another program holds it; the port is fixed in this alpha, please report it |
| Windows: nothing happens | install Git for Windows; the start-up script needs Git Bash |
| First session slow | run `uv python install 3.12` once |

## 8. Uninstall

```bash
claude plugin uninstall context-fabric@context-fabric
uvx --from git+https://github.com/Pranavk098/context-fabric cfxd stop
```

Then delete the data: macOS / Linux `rm -rf ~/.cfx`; Windows PowerShell `Remove-Item -Recurse -Force $HOME\.cfx`.
