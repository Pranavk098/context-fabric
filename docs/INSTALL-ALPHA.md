# Context Fabric alpha: install and try it

Context Fabric is a Claude Code plugin that tries to cut the tokens a coding session spends. **Whether it actually
does is the question this alpha is for.** Our first small tests (a few tasks, a few runs each) showed no measurable
saving yet, and one setting we first tried made some tasks cost more, so we turned it off. Please treat it as an
experiment, not a money-saver. This alpha has three parts:

- **Fold (off by default).** When a search, command or web fetch returns a very large output, Claude can be shown a short
  version plus the path to the full copy. In our first tests this saved nothing measurable (Claude Code already
  shortens very large outputs itself) and folding file reads cost more, so it is switched off. `cfxd set fold on`
  turns it on, `cfxd set fold_read on` adds file reads.
- **Brief.** When Claude starts a sub-agent, a short note lists the files the main session already opened and where
  the full copies of large outputs are saved, so the sub-agent doesn't start from zero.
- **Carry.** Before `/compact`, it notes which files and outputs the session was working with; afterwards it puts that
  note back so Claude doesn't lose its place.

Brief and carry use only paths, sizes and names it saw; never the contents of files or tool output.

It is an early alpha. It works on Windows, macOS and Linux, and it is built to get out of the way: if anything goes
wrong, Claude Code carries on exactly as it would without the plugin.

**Please use it on your own computer only.** The helper listens on a fixed local port with no password (Claude Code
can't send one yet), so on a shared machine another user could read what passes through it. It is fine on a personal
laptop or desktop.

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

Use Claude Code normally for a session or two. Then, in a terminal, from the plugin folder:

- macOS / Linux: `cd ~/.claude/plugins/cache/context-fabric/context-fabric/*/`
- Windows (PowerShell): `cd $env:USERPROFILE\.claude\plugins\cache\context-fabric\context-fabric\0.1.0-alpha.1`

```
uv run --no-project --quiet --python ">=3.12" python -m cfxd status
uv run --no-project --quiet --python ">=3.12" python -m cfxd doctor
uv run --no-project --quiet --python ">=3.12" python -m cfxd stats
```

`status` should say `running`. `doctor` should end with `healthy` and `identity: verified`. `stats` says `No folds yet.`
unless you turned fold on; it then shows how many outputs were folded and roughly how many bytes Claude did not have to
read (an estimate of what was *not shown*, not a measurement of your bill).

To switch a feature off or on: `... python -m cfxd set brief off` (or `carry`, `on`).

## What it keeps on your computer

Everything stays on your machine. The helper only listens on your own computer (`127.0.0.1`) and the plugin makes no
network calls.

- `~/.cfx/daemon/events.jsonl`, `folds.jsonl`, `briefs.jsonl`, `carry.jsonl`: event names, sizes, counts and timings. No file contents.
- `~/.cfx/daemon/carry/`: one small note per session (file paths, line counts, recent commands with obvious secrets masked, todo text).
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

Did Claude ever seem confused by a note it didn't expect (a brief block in a sub-agent, or the note after `/compact`)? Did anything feel slower? Did a session fail or behave differently
from usual? Any of that, even vague, helps.
