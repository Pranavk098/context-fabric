# Testing guide

This guide is for people trying the Context Fabric alpha. It takes about 30 minutes to set up, then about a week of
normal work. It says what to run, what to look for, and what to send back.

In the commands below, `cfxd` stands for `uvx --from git+https://github.com/Pranavk098/context-fabric cfxd` unless you
installed `cfxd` another way.

## 1. What we are testing

The idea: when a big model hands work to a smaller sub-agent, the handoff is where context gets wasted. A sub-agent
that is told too little re-reads what its parent already knew. Context Fabric's **Brief** adds a short note to the
sub-agent's prompt: which files the parent read or edited, the constraints and decisions the parent was given, and
short excerpts of the files that matter. We want to know whether that note is sensible and whether the sub-agent still
re-reads the same files.

Be clear about what is and is not known:

- **Brief v1 is experimental and off by default.** It has no measured benefit. You turn it on yourself (section 3).
  Do not expect your usage to drop.
- **No saving is proven for anything here.** `/checkpoint` is experimental too, with no proven saving.
- **Carry-back is unverified and off by default.** It would append a short list of what the sub-agent touched to the
  parent. We have not confirmed that sub-agent events can be attributed to one sub-agent. Leave it off unless we ask.
- **Codex and OpenCode children receive pointers only:** file paths and edit flags, saved-output metadata, masked
  commands, open to-dos and a test result. They do not receive the constraints, decisions or excerpts. See
  [`demos/brief-handoff.md`](demos/brief-handoff.md).
- **What works today is measurement.** `doctor`, `brief`, `audit`, `stats`, `diet` and `week` show what happened in your
  own sessions.

Your data matters because only real sessions can show whether any of this helps, or whether the note confuses a
sub-agent. A report that says "it did nothing" or "it got in the way" is as useful as a good one.

## 2. Install and check

Install the plugin with [`INSTALL-ALPHA.md`](INSTALL-ALPHA.md). Use your own computer, not a shared machine.

Start one Claude Code session, then check the helper from a terminal:

```bash
cfxd doctor
```

Expect `healthy` and `identity: verified`. The `flags:` line shows `brief`, `carry`, `fold`, `brief_v1` and
`carry_back`. If `doctor` fails, see [Known limits](#9-known-limits-and-things-that-may-look-wrong) or the troubleshooting
table in [`INSTALL-ALPHA.md`](INSTALL-ALPHA.md#7-troubleshooting).

**Windows blocks `cfxd.exe`?** Some Windows setups (Application Control, SmartScreen, a managed laptop) refuse to run
the `cfxd` launcher. Run the same command through Python instead: `python -m cfxd doctor`. Use `python -m cfxd ...`
wherever this guide says `cfxd ...`, from the environment where cfxd is installed.

## 3. Turn Brief on

Brief v1 needs two switches:

```bash
cfxd set brief on
cfxd set brief_v1 on
```

Check it with `cfxd brief`. Before any handoff has happened it prints `Brief v1 is on` (or `off`) and says no briefs
have been logged yet. Leave `carry_back` and the other switches alone.

## 4. Work normally for a week

Use Claude Code as you normally do for 3 to 7 days. The one thing we need is **sub-agents**: ask Claude to delegate
work (for example, "use a sub-agent to ..."), or use tasks where it does so on its own. A Brief only appears when the
parent starts a sub-agent. Some handoffs are skipped on purpose, for example a very short prompt, a resumed
sub-agent or one that was already briefed; that is normal and is recorded with a reason.

You do not need to change how you work or write longer prompts. If a task goes badly, note whether the sub-agent
seemed to ignore, repeat or misread something from the brief.

## 5. Look at what happened

### 5.1 `cfxd brief`

After a few delegations, run:

```bash
cfxd brief
```

(`cfxd brief --session SESSION_ID` narrows it to one session; `--json` prints one JSON object.) It prints one entry per
handoff. What to look for:

- **Target model.** The model family the brief was sized for, such as `haiku`, `sonnet`, `opus` or `unknown`. `unknown`
  means the delegation did not name a model, so a default size was used. Does it match the sub-agent you meant?
- **Packet size.** Bytes sent, the budget for that model and the percentage used, and how many items. Is it tiny, or
  pressed against the limit? Did a brief that was nearly full seem to help or crowd the prompt?
- **Re-read anyway.** How many of the briefed files the sub-agent opened again. This is the number we care about most:
  a file listed here was in the brief and the sub-agent read it regardless. It is a count, not a verdict; a sub-agent
  may reasonably open an edited file.
- **Left out, re-read.** Files the parent had read, that were not in the brief, and that the sub-agent read. These
  suggest what the brief missed.
- **Child tokens, compactions, context overflows, outcome.** Context for the above.
- **Top items and why.** The files that ranked highest and the reason (named in the prompt, edited, import neighbour,
  recent). Does the ranking look right for the task?

If `cfxd brief` says a match could not be made, it prints the reason under `unmatched`; include that in a report.

`cfxd brief` prints relative file paths. Read it before you paste anything from it (see section 7).

### 5.2 `cfxd week --share`

```bash
cfxd week --share
```

It prints your weekly summary with counts, sizes and model ids only. Every number is a count from your own logs, not a
claim that the plugin saved anything. In brief:

- **Total API-equivalent dollars:** what these tokens would cost at list API prices, not your subscription bill.
- **Sessions by length, cache-read share, model switches:** where the cost sits and how much of it was cached history.
- **Plugin receipts:** counts from the plugin's local logs, including hook calls and errors.
- **First-turn context:** median tokens already in the window before you type.
- **Refetch after compaction:** how much of the dropped context was read again afterwards.
- **Diet:** skills and connectors you never used and their estimated cost per turn.

Read the output before you paste it. Counts, sizes and model ids are safe; names from `diet` without `--share` are not.

### 5.3 Fill in the template

Use [`FEEDBACK-TEMPLATE.md`](FEEDBACK-TEMPLATE.md), or a Brief feedback issue (section 6). Paste the whole
`week --share` output. For Brief, also answer in your own words:

- Did any sub-agent behave worse or oddly after a `context-fabric-brief` note appeared?
- Did the target model and packet size look right?
- Did you see the `Re-read anyway` count go up or down compared with what you expected?
- Would you leave Brief on?

## 6. Send results

Use [`FEEDBACK-TEMPLATE.md`](FEEDBACK-TEMPLATE.md). Paste your `week --share` output, then send the filled template to
the person who gave you this guide, or open a GitHub issue:

- [Brief feedback](https://github.com/Pranavk098/context-fabric/issues/new?template=brief-feedback.yml)
- [Brief bug report](https://github.com/Pranavk098/context-fabric/issues/new?template=brief-bug.yml)

### What a useful bug report contains

1. OS and version, Claude Code version (`claude --version`), and the cfxd version (first line of `week --share`).
2. The output of `cfxd doctor`.
3. Which switches were on: `brief`, `brief_v1`, `carry_back`, and anything else from section 8 that you turned on.
4. What you did, what you expected, and what happened instead. One handoff, described in words: the sub-agent type,
   the target model and packet size shown by `cfxd brief`, and the re-read and outcome lines.
5. Whether it happens every time, and whether it stops when you run `cfxd set brief_v1 off`.
6. Do not paste prompts, file paths, commands, brief text or secrets. If a path matters, say what kind of file it is.
   Vulnerabilities go to the private channel in [`SECURITY.md`](../SECURITY.md), not a public issue.

## 7. Privacy and what stays on your machine

Nothing is sent anywhere by Context Fabric's own tools. The helper runs on your machine and writes under `~/.cfx`.

- **Receipts** (`briefs.jsonl`): the decision, target model, sizes and counts, and a hash per file. They do not hold
  file paths, prompt text or brief text.
- **Brief packets:** for each brief, a small JSON file under `~/.cfx/daemon/briefs/` holding file paths, line counts,
  masked commands and to-do text. They are deleted after seven days. File contents are not stored there.
- **`cfxd brief`** reads your Claude Code transcripts to see what each sub-agent opened, and prints relative paths on
  your screen.
- **What the sub-agent sees.** The brief itself is added to the sub-agent's prompt, so it travels to the model like
  the rest of your session. With Brief v1 it can include short excerpts of files you already read, and constraints and
  decisions taken from your prompts and the parent's messages. It is built from what you and the parent said and from
  file bytes read from disk, not from tool output.
- **`--share` output** is counts, sizes and model ids only. Read it before you paste it.

## 8. Turn everything off

```bash
cfxd set brief_v1 off
cfxd set brief off
cfxd set carry_back off
```

Other switches, if you turned them on: `cfxd set bound off`, `cfxd set coach off`, `cfxd set idle_notice off`.
`cfxd set carry off`, `cfxd set fold off` and `cfxd set fold_read off` turn off the older features. Undo experiments with
`cfxd diet --undo`, `cfxd checkpoint clear` and `cfxd packet install-agents-md --cwd . --remove`.

To remove the plugin and delete its data, follow [`INSTALL-ALPHA.md`](INSTALL-ALPHA.md#8-uninstall).

## 9. Known limits and things that may look wrong

- **Windows needs Git for Windows.** The plugin's start-up script runs in Git Bash.
- **The first hook call can be slow.** Python starts on the first call; later calls are fast. `uv python install 3.12`
  removes the download that can make it worse.
- **No briefs appear.** Brief runs only when the parent starts a sub-agent through the `Agent` tool, and only with both
  `brief` and `brief_v1` on. A skipped handoff still shows up in `cfxd brief` with its reason.
- **The re-read count is a signal, not a score.** It counts files the sub-agent opened that the brief had listed. It
  does not say whether the sub-agent needed them.
- **Whether a sub-agent's events are attributed correctly is not verified.** This is why carry-back is off.
- **Account-synced plugins cannot be disabled locally.** `diet --apply` lists them for the claude.ai settings page
  instead.
- **A checkpoint restores only after `/clear`, and only within 60 minutes.** A normal restart or `/compact` does not
  consume it.
- **Bound rewrites only a short list of simple test and log commands.** Pipes, chains, redirects and background runs
  are left exactly as they were.

## 10. Other experiments (optional)

Each of these is experimental and none has a proven saving. Try one at a time, after Brief, so you can tell what
changed.

- **Diet apply and undo.** Lists fully-unused local plugins, then can disable them. `cfxd diet --apply` is a dry run;
  `cfxd diet --apply --yes` disables what it lists; `cfxd diet --undo` restores them. Open a new session and run
  `/context` to compare the setup number.
- **Bound.** Keeps only the last 150 lines of a short list of simple test and log commands (`cfxd set bound on`, `off`).
  Report any command that was cut too short.
- **Coach.** After about 30 turns and 150K context, adds one visible line about cache-read cost (`cfxd set coach on`,
  `off`).
- **Checkpoint, then `/clear`.** Run `/checkpoint [note]`, then `/clear`; if the short name is not found, use
  `/context-fabric:checkpoint [note]`. The next session gets one `context-fabric-carry` block with your note, the last
  test result and files marked `[changed since checkpoint]` or `[missing]`. `cfxd checkpoint show` previews it and
  `cfxd checkpoint clear` deletes it.
- **Idle notice.** On a resumed session whose prompt cache expired, shows one line with the API-equivalent re-read
  cost (`cfxd set idle_notice on`, `off`).
- **Weekly trend.** `cfxd week --save` once a week writes a numbers-only snapshot; after two weeks, `cfxd trend --share`
  shows week-over-week change. Every change line is labelled OBSERVED: it is a before/after in your own data, not a
  counterfactual. Nothing is scheduled for you.
- **Ledger.** `cfxd ledger --days 30` lists what the plugin did, with every line labelled OBSERVED, ESTIMATED or
  PROVEN. Nothing is PROVEN yet. Add `--share` to print numbers and model ids only.
- **Tour.** `cfxd tour --yes` walks through five steps on your own transcripts and prints everything without pausing.
- **Fidelity.** `cfxd fidelity --since 30d` counts how many files each compaction summary names back and how many named
  paths never appeared before it.
- **Hook delivery.** `cfxd coverage` compares tool calls in your transcripts with the events the helper logged over
  7 days. A rate well below 100% means some hook events were dropped under load.
- **Hand off between agents.** `cfxd packet build`, `validate`, `render` and `install-agents-md` move observed facts
  between Claude Code, Codex and OpenCode as pointers only. Tested on hand-built logs only, not end to end with live
  Codex or OpenCode. See [`demos/brief-handoff.md`](demos/brief-handoff.md).
