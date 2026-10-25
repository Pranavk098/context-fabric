# Testing guide

Thank you for testing Context Fabric. This page says exactly what to do, how long it takes and what to send back.
Everything you send is counts and sizes. Nothing leaves your machine unless you paste it yourself.

**What is being tested.** A Claude Code plugin (alpha) that records which files and outputs a session touched, hands
that to sub-agents (brief) and restores it after `/compact` (carry). Whether it saves tokens is **unknown**, which is
why we need real sessions. Do not expect your usage to drop.

**Time.** About 15 minutes to set up. After that, use Claude Code as you normally do for 3 to 7 days, then 5 minutes to
send results.

**Use your own computer only.** The plugin runs a local helper on `127.0.0.1`. Do not install it on a shared machine.

## 1. Set up (once)

Pick your system. Commands marked *terminal* go in a normal terminal, not inside Claude Code.

**macOS / Linux** (terminal)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh        # uv, the only prerequisite
uv python install 3.12
curl -fsSL https://claude.ai/install.sh | bash          # Claude Code, if you do not have it
```

**Windows** (PowerShell)

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
winget install --id Git.Git -e          # Git for Windows, if you do not have it
uv python install 3.12
irm https://claude.ai/install.ps1 | iex # Claude Code, if you do not have it
```

Open a **new** terminal so the tools are found. Then install the plugin (terminal):

```bash
claude plugin marketplace add Pranavk098/context-fabric
claude plugin install context-fabric@context-fabric
```

The same thing inside Claude Code is `/plugin marketplace add Pranavk098/context-fabric` followed by
`/plugin install context-fabric@context-fabric`. Desktop app (Code tab, local sessions): **+ → Add plugins**, then add
the marketplace `Pranavk098/context-fabric`. The CLI is the tested path; hooks in the desktop app and VS Code are not
yet verified.

Restart Claude Code. Open any project and run one normal task. Then check (terminal):

```bash
uvx --from git+https://github.com/Pranavk098/context-fabric cfxd doctor
```

You should see `healthy` and `identity: verified`. If not, see [Troubleshooting](#troubleshooting).

## 2. Use it normally (3 to 7 days)

Work as you always do. Two things make the data more useful, if they fit your work:

- Run at least one task that starts a **sub-agent** (for example ask Claude to "use an agent to review these files").
- Run `/compact` once in a long session.

You do not need to do anything special. Please do not turn it off to "help".

## 3. Send results

After a few days (terminal):

```bash
uvx --from git+https://github.com/Pranavk098/context-fabric cfxd audit
uvx --from git+https://github.com/Pranavk098/context-fabric cfxd stats
```

Both print counts and sizes only: no file paths, commands, prompts or code. **Read the output before you share it.**
Open an [alpha feedback issue](https://github.com/Pranavk098/context-fabric/issues/new?template=alpha-feedback.md) and
paste it, or send it to the person who gave you this link. `audit` works even if you only want to look at your own
history and skip the plugin: it needs no install.

## 4. Questions we want answered

1. Did anything break, error or feel slower?
2. After `/compact`, did work feel better, the same or worse?
3. Did a sub-agent seem confused by a note it was given (a block named `context-fabric-brief`)?
4. What surprised you in the `audit` output?
5. Would you act on what it shows (for example disabling skills or connectors you never use)? What would you want it to
   tell you instead?

## What it keeps on your computer

Under `~/.cfx/daemon/` (Windows: `C:\Users\<you>\.cfx\daemon\`): logs of event names, sizes, counts and timings, and
one small note per session for carry (file paths, line counts, recent commands with obvious secrets masked, to-do
text). It does not store file contents or tool output. The helper does see tool inputs and outputs in memory while
Claude Code runs, and sends nothing over the network. Details: [`INSTALL-ALPHA.md`](INSTALL-ALPHA.md).

## Troubleshooting

- **A warning says `uv` cannot be found.** Install uv (above), open a new terminal, restart Claude Code.
- **`doctor` says not running.** Start a new Claude Code session, then run `doctor` again.
- **Port 47831 in use.** Another program holds the port. Tell us; it is fixed in this alpha.
- **Windows: nothing happens.** The plugin's start-up script runs in Git Bash, so Git for Windows must be installed.
- Anything else: run `doctor` and paste its output in an issue.

## Remove it completely

```bash
claude plugin uninstall context-fabric@context-fabric
uvx --from git+https://github.com/Pranavk098/context-fabric cfxd stop
```

Then delete the data folder: macOS / Linux `rm -rf ~/.cfx`; Windows PowerShell `Remove-Item -Recurse -Force $HOME\.cfx`.
