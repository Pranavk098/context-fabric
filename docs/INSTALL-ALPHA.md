# Context Fabric alpha: install and try it

Context Fabric is a Claude Code plugin that cuts the tokens a coding session spends. This alpha does one thing so far:
**fold**. When a tool returns a very large output (a big file, a long test log, a wide search), it shows Claude a short
version plus the path to the full copy, so Claude reads the rest only if it needs to. Nothing is deleted.

It is an early alpha. It works on Windows, macOS and Linux, and it is built to get out of the way: if anything goes
wrong, Claude Code carries on exactly as it would without the plugin.

## What you need

- **Claude Code**, signed in.
- **uv** (a small tool that runs the plugin's Python; it downloads Python itself if needed).
  - macOS / Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`
  - Windows (PowerShell): `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
- **Windows only:** Git for Windows (Claude Code uses its Git Bash; the plugin's start-up script runs there).

Then open a new terminal so `uv` is found.

## Install

In Claude Code:

```
/plugin marketplace add Pranavk098/context-fabric
/plugin install context-fabric@context-fabric
```

Restart Claude Code. That's it: the first session starts a small background helper that listens on `127.0.0.1:47831`.

## Check it is working

Use Claude Code normally, for example ask it to read a large file. Then, in a terminal, from the plugin folder:

- macOS / Linux: `cd ~/.claude/plugins/cache/context-fabric/context-fabric/*/`
- Windows (PowerShell): `cd $env:USERPROFILE\.claude\plugins\cache\context-fabric\context-fabric\0.1.0-alpha.1`

```
uv run --no-project --quiet --python ">=3.12" python -m cfxd status
uv run --no-project --quiet --python ">=3.12" python -m cfxd stats
```

`status` should say `running`. `stats` shows how many outputs were folded and roughly how many bytes and tokens Claude
did not have to read. Those are estimates of what was *not shown*, not a measurement of your bill.

## What it keeps on your computer

Everything stays on your machine. The helper only listens on your own computer (`127.0.0.1`) and the plugin makes no
network calls.

- `~/.cfx/daemon/events.jsonl` and `folds.jsonl`: event names, sizes and timings. No file contents.
- `~/.cfx/daemon/obs/`: the **full copy of each folded output**, so Claude can read the rest. This can include code,
  command output or web pages from your session. Files are deleted after 7 days.

To remove all of it at any time: stop the helper (`... python -m cfxd stop`, from the plugin folder) and delete the
`.cfx` folder in your home directory.

## If something looks wrong

- Claude Code warns that `uv` can't be found: install uv (above), then restart Claude Code.
- `status` says `not running`: start a new Claude Code session, or run `... python -m cfxd doctor`.
- Port `47831` already in use by another program: tell me; it is a fixed port in this alpha.
- Anything else: run `... python -m cfxd doctor` and send me its output (it contains no file contents).

## Uninstall

```
/plugin uninstall context-fabric@context-fabric
```

Then stop the helper and delete `~/.cfx` as above.

## What I'd like to know

Did Claude ever seem confused by a folded output? Did anything feel slower? Did a session fail or behave differently
from usual? Any of that, even vague, helps.
