# Testing guide

This guide takes about 30 minutes to read and try, then about a week of normal work. It says what to run, what to look
for, and what to send back.

## 1. What we are testing

- **Objective:** the same work with fewer tokens. Whether Context Fabric achieves that is **not proven**.
- **What works today:** measurement. `doctor`, `audit`, `stats`, `diet` and `week` show where your tokens go.
- **What is experimental:** `bound`, `coach`, `/checkpoint`, the idle notice, and `diet --apply`. Their saving is not
  proven.
- **No saving is proven.** Do not expect your usage to drop.
- **Why your data matters:** only real sessions can show whether any of this helps.

## 2. Before you start

Install the plugin with [`INSTALL-ALPHA.md`](INSTALL-ALPHA.md). Use your own computer, not a shared machine.

Start one Claude Code session, then check the helper from a terminal:

```bash
uvx --from git+https://github.com/Pranavk098/context-fabric cfxd doctor
```

Expect `healthy` and `identity: verified`. If not, see [Known limits](#5-known-limits-and-things-that-may-look-wrong) or
the troubleshooting table in [`INSTALL-ALPHA.md`](INSTALL-ALPHA.md#7-troubleshooting).

**Privacy.** Nothing leaves your machine. The helper records event names, sizes, counts, timings and, for carry and
checkpoint notes, file paths, line counts, recent commands with obvious secrets masked, and to-do text. It does not store file
contents or tool output. `--share` prints counts, sizes and model ids only; read the output before you paste it.

## 3. The 10-minute path

**Step 1: take the tour.** It walks through five steps on your own transcripts and prints everything without pausing:

```bash
uvx --from git+https://github.com/Pranavk098/context-fabric cfxd tour --yes
```

**Step 2: get your weekly summary.** This is experimental, but it only counts:

```bash
uvx --from git+https://github.com/Pranavk098/context-fabric cfxd week --share
```

What each number means:

- **Total API-equivalent dollars:** what these tokens would cost at list API prices, not your subscription bill.
- **Cost is roughly turns x context size:** every turn re-reads the context, so long sessions keep paying for the same
  history.
- **Sessions by length:** how much of the cost sits in short, medium and long sessions. In our data, long sessions
  carry most of it.
- **Cache-read share:** the share of tokens read from the prompt cache. A high number means history was reused, not
  re-sent at full price.
- **Model switches:** how often you switched models, and the extra cache-write cost that caused.
- **Plugin receipts:** counts from the plugin's local logs: rewrites, hook calls and errors.
- **First-turn context:** median tokens already in the window before you type.
- **Refetch after compaction:** how much of the dropped context was read again afterwards.
- **Diet:** skills and connectors you never used and their estimated cost per turn.

**What to send back:** paste the whole `week --share` output into the template at
[`FEEDBACK-TEMPLATE.md`](FEEDBACK-TEMPLATE.md), fill in the rest, and send it as described in [section 6](#6-send-results).

**Optional:** `uvx --from git+https://github.com/Pranavk098/context-fabric cfxd fidelity --since 30d` is an experimental measurement of how many files each compaction summary names back and how many named paths never appeared before it; it prints counts only.

## 4. The one-week path

Use Claude Code as you normally do for 3 to 7 days. Run one experiment at a time so you can tell what changed. Each
one is optional and experimental; none has a proven saving.

### 4.1 Diet apply and undo

- **What it does:** experimental; saving not proven. Lists fully-unused local plugins, then can disable them; account-synced items cannot be changed locally.
- **Turn on:** `uvx --from git+https://github.com/Pranavk098/context-fabric cfxd diet --apply` is a dry run; `uvx --from git+https://github.com/Pranavk098/context-fabric cfxd diet --apply --yes` disables what it lists.
- **What to do:** open a **new** session and run `/context`; compare the setup number with the one you saw before.
- **What to look for:** a lower first-turn context, and nothing you needed going missing.
- **Turn off:** `uvx --from git+https://github.com/Pranavk098/context-fabric cfxd diet --undo`
- **What to report:** the before and after `/context` numbers, and anything that disappeared.

### 4.2 Bound

- **What it does:** experimental; saving not proven. Keeps only the last 150 lines of a short list of simple test and log commands (`pytest`, `npm test`, `go test`, `git log`, `docker logs` and similar).
- **Turn on:** `uvx --from git+https://github.com/Pranavk098/context-fabric cfxd set bound on`
- **What to do:** run your tests and log commands as usual.
- **What to look for:** whether Claude still finds the line it needs, and whether errors stay visible.
- **Turn off:** `uvx --from git+https://github.com/Pranavk098/context-fabric cfxd set bound off`
- **What to report:** any command that was cut too short, and whether the agent recovered.

### 4.3 Coach

- **What it does:** experimental; saving not proven. After about 30 turns and 150K context, adds one visible line with the session's cache-read cost and the checkpoint workflow.
- **Turn on:** `uvx --from git+https://github.com/Pranavk098/context-fabric cfxd set coach on`
- **What to do:** work normally through at least one long session.
- **What to look for:** whether the line helped you notice context growth, or just added noise.
- **Turn off:** `uvx --from git+https://github.com/Pranavk098/context-fabric cfxd set coach off`
- **What to report:** how often it appeared and what you did next.

### 4.4 Checkpoint, then `/clear`

- **What it does:** experimental; saving not proven. Saves the working state and an optional goal note, then restores it once after `/clear`.
- **Turn on:** after a normal task, run `/checkpoint [note]` (optional, up to 300 characters), then `/clear`; if the short name is not found, use `/context-fabric:checkpoint [note]`.
- **What to do:** start the next task in the new session.
- **What to look for:** a `context-fabric-carry` block with your note, the last test result and files marked `[changed since checkpoint]` or `[missing]`.
- **Turn off / undo:** the restore happens once; `uvx --from git+https://github.com/Pranavk098/context-fabric cfxd checkpoint show` previews the block and `uvx --from git+https://github.com/Pranavk098/context-fabric cfxd checkpoint clear` deletes a saved checkpoint.
- **What to report:** whether the block matched what you were doing, and any stale or wrong marks.

### 4.5 Idle notice

- **What it does:** experimental; saving not proven. On a resumed session whose prompt cache expired, shows one line with the API-equivalent re-read cost and notes that `/clear` starts cheap.
- **Turn on:** `uvx --from git+https://github.com/Pranavk098/context-fabric cfxd set idle_notice on`
- **What to do:** leave a session alone long enough for the cache to expire, then resume it.
- **What to look for:** whether the cost line changed what you did next.
- **Turn off:** `uvx --from git+https://github.com/Pranavk098/context-fabric cfxd set idle_notice off`
- **What to report:** what the line said and whether it was useful or confusing.

## 5. Known limits and things that may look wrong

- **Windows needs Git for Windows.** The plugin's start-up script runs in Git Bash.
- **The first hook call can be slow.** Python starts on the first call; later calls are fast. `uv python install 3.12`
  removes the download that can make it worse.
- **Account-synced plugins cannot be disabled locally.** `diet --apply` lists them for the claude.ai settings page
  instead.
- **A checkpoint restores only after `/clear`, and only within 60 minutes.** A normal restart or `/compact` does not
  consume it.
- **Bound rewrites only a short list of simple test and log commands.** Pipes, chains, redirects and background runs
  are left exactly as they were.

## 6. Send results

Use [`FEEDBACK-TEMPLATE.md`](FEEDBACK-TEMPLATE.md). Paste your `week --share` output, then send the filled template to
the person who gave you this guide or open an
[alpha feedback issue](https://github.com/Pranavk098/context-fabric/issues/new?template=alpha-feedback.md).

Read the output before you share it. Counts, sizes and model ids are safe; names from `diet` without `--share` are not.

To stop or remove the plugin, follow [`INSTALL-ALPHA.md`](INSTALL-ALPHA.md#8-uninstall).
