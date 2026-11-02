# Founder Checklist: Timed Cold-Install Rehearsal (P9)

Run this rehearsal on a tagged tester build before inviting external testers. It verifies that a developer can go from a clean user profile to a healthy daemon, first report, first diet inspection, and clean uninstall in **under 10 minutes to first report** using only the published guides.

- **No saving is proven.** All token and dollar figures shown by the tools carry their evidence class (*measured*, *observed*, or *estimated*); active hook features such as Brief are experimental and opt-in.
- **Retired features:** `fold`, `fold_read`, `bound`, and `checkpoint` are retired and must not appear in help output or rehearsal steps.

---

## 1. Rehearsal Rules

1. **Guide-only rule (strict):** Follow **only** the commands and instructions printed in [`docs/INSTALL-ALPHA.md`](../INSTALL-ALPHA.md), [`docs/TESTING-GUIDE.md`](../TESTING-GUIDE.md), and [`docs/testers/README.md`](README.md) ([`checkup.md`](checkup.md), [`diet.md`](diet.md), [`fidelity.md`](fidelity.md), [`brief-opt-in.md`](brief-opt-in.md)). If you have to leave the guide—search the repo, read source code, pass an undocumented flag, or fix a path by hand—**that is a bug**.
2. **Question rule:** Every question you have to ask yourself or look up outside the guide is a bug. Log it immediately in the [Question Log](#5-question-log) with the doc line that should have answered it.
3. **Target time:** Elapsed time from **`start`** to **`first report`** must be **under 10 minutes (`< 10:00`)** on both Windows and Linux/macOS.
4. **Two required environments:**
   - **Run A — Clean Windows local user profile:** A second local non-admin Windows account on the rehearsal machine. This profile starts with no user-scoped `%USERPROFILE%\.claude` plugins, no `%USERPROFILE%\.cfx` state, and **will hit any Windows Application Control, SmartScreen, or execution-policy block that external testers hit** (including blocks on `cfxd.exe` console launcher shims).
   - **Run B — Clean Linux or macOS user environment:** A clean non-root account, VM, container, or isolated `$HOME` with no existing `~/.cfx` directory or installed `context-fabric` plugin.

---

## 2. Environment Setup (Before Starting the Stopwatch)

### 2.1 Run A: Create the Clean Windows Local User Profile

Run once from an elevated (Administrator) PowerShell session on your main account before starting the clock, or via **Windows Settings → Accounts → Other users → Add account → I don't have this person's sign-in information → Add a user without a Microsoft account**:

```powershell
# Run in an elevated PowerShell window on the host account (before the stopwatch starts):
New-LocalUser -Name "cfx-rehearsal" -NoPassword -Description "Context Fabric cold-install rehearsal"
Add-LocalGroupMember -Group "Users" -Member "cfx-rehearsal"
```

1. Switch user and sign in interactively to Windows as `cfx-rehearsal` so Windows initializes `C:\Users\cfx-rehearsal\`.
2. Open a standard (non-admin) PowerShell window as `cfx-rehearsal` and confirm `$HOME` is `C:\Users\cfx-rehearsal` and `Test-Path $HOME\.cfx` is `False`.
3. Keep Application Control / SmartScreen at the machine's normal enforcement level so the profile hits the exact launcher policies a tester hits.

### 2.2 Run B: Prepare the Clean Linux or macOS Profile

Sign in to a fresh non-root user account (or in a clean shell session, isolate `HOME` the same way [`.github/workflows/install-smoke.yml`](../../.github/workflows/install-smoke.yml) does before starting the clock):

```bash
# Confirm no prior ~/.cfx state exists in the rehearsal home:
test ! -e "$HOME/.cfx" && echo "clean home ready: $HOME"
```

---

## 3. Stopwatch Table

Target: **`start` to `first report` under 10 minutes (`< 10:00`)** on both runs.

| Milestone | Definition | Windows (`cfx-rehearsal`) Elapsed (`MM:SS`) | Linux / macOS Elapsed (`MM:SS`) | Target | Status (`PASS` / `FAIL`) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`start`** | Clean terminal open in rehearsal profile; clock starts at Step 4.1 | `00:00` | `00:00` | `00:00` | |
| **`install done`** | Prerequisites verified (`INSTALL-ALPHA.md` §1) and `context-fabric@context-fabric` installed (§2) | | | `< 05:00` | |
| **`daemon healthy`** | First Claude Code session started; `cfxd doctor` (or `python -m cfxd doctor`) prints `healthy` and `identity: verified` (§3) | | | `< 07:00` | |
| **`first report`** | `cfxd checkup` and `cfxd week --share` finish and print `safe to paste: counts and percentages only` | | | **`< 10:00` (hard gate)** | |
| **`first diet look`** | `cfxd diet --since 30d`, `cfxd diet --since 30d --share`, and dry-run `cfxd diet --apply` inspected (`docs/testers/diet.md`) | | | `< 13:00` | |

---

## 4. Timed Rehearsal Steps (Exact Guide Command Order Only)

Start the stopwatch (`00:00`) and run the steps below in exact guide order without consulting any file outside `docs/INSTALL-ALPHA.md`, `docs/TESTING-GUIDE.md`, and `docs/testers/*.md`.

### Step 4.1 — Check and Install Prerequisites ([`docs/INSTALL-ALPHA.md` §1](../INSTALL-ALPHA.md#1-requirements))

Run the checks from `docs/INSTALL-ALPHA.md` §1:

```bash
claude --version
uv --version
uv python list --only-installed
```

On Windows, also run the Git Bash checks from `docs/INSTALL-ALPHA.md` §1 (the plugin's hooks run `sh launch.sh`):

```powershell
bash --version
sh -c "echo ok"
```

If any prerequisite is missing in the clean profile, run only the exact install commands from `docs/INSTALL-ALPHA.md` §1 and open a **new** terminal:

**Windows (PowerShell):**

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
winget install --id Git.Git -e
uv python install 3.12
irm https://claude.ai/install.ps1 | iex               # Claude Code
```

**macOS / Linux:**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv python install 3.12
curl -fsSL https://claude.ai/install.sh | bash        # Claude Code
```

### Step 4.2 — Add the Marketplace and Install the Plugin (`install done`) ([`docs/INSTALL-ALPHA.md` §2](../INSTALL-ALPHA.md#2-add-the-plugin))

Install via the terminal commands in `docs/INSTALL-ALPHA.md` §2:

```bash
claude plugin marketplace add Pranavk098/context-fabric
claude plugin install context-fabric@context-fabric
```

*(Or, if rehearsing the one-liner installer scripts [`scripts/install.ps1`](../../scripts/install.ps1) / [`scripts/install.sh`](../../scripts/install.sh) which run `cfxd doctor` then the two `claude plugin` commands above:*
- *Windows PowerShell:* `irm https://raw.githubusercontent.com/Pranavk098/context-fabric/main/scripts/install.ps1 | iex` *(or from a repo checkout: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install.ps1`)*
- *macOS / Linux:* `curl -fsSL https://raw.githubusercontent.com/Pranavk098/context-fabric/main/scripts/install.sh | sh` *(or from a repo checkout: `sh scripts/install.sh`))*

Record the **`install done`** time in the [Stopwatch Table](#3-stopwatch-table).

### Step 4.3 — Start Claude Code and Verify Daemon Health (`daemon healthy`) ([`docs/INSTALL-ALPHA.md` §2–3](../INSTALL-ALPHA.md#3-verify), [`docs/TESTING-GUIDE.md` §2](../TESTING-GUIDE.md#2-install-and-check))

1. Start (or restart) Claude Code in the rehearsal profile so the first session starts the local helper on `127.0.0.1:47831`.
2. From a terminal, run `cfxd doctor` exactly as shown in `docs/INSTALL-ALPHA.md` §3:

```bash
uvx --from git+https://github.com/Pranavk098/context-fabric cfxd doctor
```

*(If running from a local repo checkout as in [`docs/testers/brief-opt-in.md`](brief-opt-in.md): `uv run cfxd doctor`, or `uv run --python 3.12 cfxd doctor --help` / `uv run --python 3.12 cfxd doctor [--state-dir STATE_DIR]`.)*

3. Confirm `doctor` exits `0` and prints:
   - `cfxd doctor: http://127.0.0.1:47831 (pid ..., version ...)`
   - `health:      ok`
   - `identity:    verified`
   - `sh:          found (...)`
   - `cfxd doctor: healthy`

#### Windows `python -m cfxd` Fallback ([`docs/TESTING-GUIDE.md` §2](../TESTING-GUIDE.md#2-install-and-check))

If Windows Application Control, SmartScreen, or group policy on the `cfx-rehearsal` profile blocks the `cfxd.exe` console script launcher, test and use the documented module fallback from `docs/TESTING-GUIDE.md` §2:

```powershell
python -m cfxd doctor
```

*(Or inside the `uv` environment: `uv run --python 3.12 python -m cfxd doctor`.)*

- Confirm `python -m cfxd doctor` prints `health: ok`, `identity: verified`, and `cfxd doctor: healthy`.
- Even if `cfxd.exe` was **not** blocked on your machine, run `uv run --python 3.12 python -m cfxd doctor` once on Windows during this step to verify the fallback path works in the tester build.
- Wherever the remaining steps say `uv run cfxd ...` or `cfxd ...`, a Windows tester hitting an Application Control block runs `python -m cfxd ...` instead.

Record the **`daemon healthy`** time in the [Stopwatch Table](#3-stopwatch-table).

### Step 4.4 — Run the First Reports (`first report`) ([`docs/testers/README.md`](README.md), [`docs/testers/checkup.md`](checkup.md), [`docs/INSTALL-ALPHA.md` §4](../INSTALL-ALPHA.md#4-commands))

Run the first-command sequence from `docs/testers/checkup.md` (using `uvx --from git+https://github.com/Pranavk098/context-fabric cfxd ...`, `uv run cfxd ...`, or `python -m cfxd ...`):

```bash
uv run cfxd checkup
uv run cfxd checkup --since 7d
uv run cfxd checkup --since all
uv run cfxd week --share
```

Verify:
- [ ] `cfxd checkup` prints the three sections in fixed order: `1. Diet`, `2. Fidelity`, `3. Sub-agents`.
- [ ] `cfxd checkup` ends with `safe to paste: counts and percentages only`.
- [ ] `cfxd week --share` prints a version header and ends with `safe to paste: counts and percentages only`.
- [ ] Output contains counts, percentages, and model family ids only—no file paths, prompts, commands, or skill names.

Record the **`first report`** time in the [Stopwatch Table](#3-stopwatch-table). **Must be under 10:00.**

### Step 4.5 — Run the First Diet Inspection (`first diet look`) ([`docs/testers/diet.md`](diet.md))

Run the command sequence from `docs/testers/diet.md`:

```bash
uv run cfxd diet --since 30d
uv run cfxd diet --since 30d --share
uv run cfxd diet --apply
```

If testing `--apply --yes` and `--undo` from `docs/testers/diet.md`:

```bash
uv run cfxd diet --apply --yes
uv run cfxd diet --undo
```

Verify:
- [ ] `cfxd diet --since 30d` shows first-turn context, itemized prefix estimates (labelled as an estimate, not a bill), and used vs. never-used items.
- [ ] `cfxd diet --since 30d --share` replaces all item names with numbered labels and prints aggregate numbers only.
- [ ] `cfxd diet --apply` acts as a dry run unless `--yes` is passed, and `cfxd diet --undo` restores any disabled local plugins.

Record the **`first diet look`** time in the [Stopwatch Table](#3-stopwatch-table).

### Step 4.6 — Verify Remaining Guide Commands ([`docs/testers/fidelity.md`](fidelity.md), [`docs/testers/brief-opt-in.md`](brief-opt-in.md))

Run the remaining commands from `docs/testers/fidelity.md` and `docs/testers/brief-opt-in.md` to confirm every documented flag exists and exits cleanly:

```bash
# From docs/testers/fidelity.md (note: no --share flag on fidelity; default output is numbers only)
uv run cfxd fidelity --since 30d
uv run cfxd fidelity --since all

# From docs/testers/brief-opt-in.md
uv run cfxd handoff-stats --share
uv run cfxd set brief_v1 on
uv run cfxd brief
uv run cfxd brief --share
uv run cfxd set brief_v1 off
uv run cfxd ledger --days 30 --share
```

---

## 5. Question Log

**Every question you had to ask is a bug.** Whenever you pause to wonder what to type, which directory to run from, what an output line means, or how to recover from an error, add a row below. A rehearsal with any unresolved row fails until the target doc line is updated.

| When (`MM:SS` / Step) | What (the exact question or confusion) | Which doc line should have answered it (`file:line` or section) | Fixed before tester outreach? (`YES` / `NO`) |
| :--- | :--- | :--- | :--- |
| *Example: `03:40` / Step 4.3* | *Example: `cfxd doctor` printed `no state file` before I opened a Claude Code session* | *`docs/INSTALL-ALPHA.md:56` (§2 "Restart Claude Code. The first session starts the local helper")* | |
| | | | |
| | | | |
| | | | |

---

## 6. Uninstall and Disk-Clean Verification ([`docs/INSTALL-ALPHA.md` §5, §8](../INSTALL-ALPHA.md#5-what-it-stores))

### 6.1 Inspect What Was Stored on Disk Before Deleting ([`docs/INSTALL-ALPHA.md` §5](../INSTALL-ALPHA.md#5-what-it-stores))

Before removing `~/.cfx`, inspect `~/.cfx/daemon/` (Windows: `C:\Users\<you>\.cfx\daemon\`) and confirm only the files documented in `docs/INSTALL-ALPHA.md` §5 exist:

| Path under `~/.cfx/daemon/` | Expected Content per `docs/INSTALL-ALPHA.md` §5 | Verified (counts/hashes only, no raw tool output or file contents) |
| :--- | :--- | :--- |
| `events.jsonl`, `briefs.jsonl`, `carry.jsonl`, `coach.jsonl`, `resume.jsonl` | Event names, sizes, counts, hashes, latencies (rotated by size) | [ ] |
| `carry/<session>.json` | One note per compacted session: file paths, line counts, recent commands with obvious secrets masked, to-do text (7-day retention) | [ ] |
| `token` | Per-install secret that authenticates hook calls (readable only by current user) | [ ] |
| `config.json`, `state.json`, `daemon.log` | Feature flags, daemon pid and port, daemon messages | [ ] |
| `history/week-YYYY-MM-DD.json` | Numbers-only snapshot written only if `cfxd week --save` was run; no names, paths, or text | [ ] |

### 6.2 Run the Exact Uninstall Commands ([`docs/INSTALL-ALPHA.md` §8](../INSTALL-ALPHA.md#8-uninstall))

Run the two commands from `docs/INSTALL-ALPHA.md` §8:

```bash
claude plugin uninstall context-fabric@context-fabric
uvx --from git+https://github.com/Pranavk098/context-fabric cfxd stop
```

*(Or `python -m cfxd stop` / `uv run cfxd stop` if using the Windows module fallback.)*

Then delete the state directory using the exact command from `docs/INSTALL-ALPHA.md` §8:

- **macOS / Linux:**
  ```bash
  rm -rf ~/.cfx
  ```
- **Windows PowerShell:**
  ```powershell
  Remove-Item -Recurse -Force $HOME\.cfx
  ```

### 6.3 Confirm Nothing Is Left Behind

Run all three checks on both Windows and Linux/macOS:

1. **Plugin removed from Claude Code:**
   ```bash
   claude plugin list
   ```
   - [ ] `context-fabric@context-fabric` is **not** listed.

2. **Daemon stopped and port `47831` freed:**
   ```bash
   uv run cfxd status
   ```
   *(Or `python -m cfxd status` / `uvx --from git+https://github.com/Pranavk098/context-fabric cfxd status`.)*
   - [ ] Exits non-zero with `cfxd status: not running`.

3. **Data directory `~/.cfx` completely gone:**
   - **macOS / Linux:**
     ```bash
     test ! -e ~/.cfx && echo "CLEAN: ~/.cfx removed"
     ```
   - **Windows PowerShell:**
     ```powershell
     if (-not (Test-Path $HOME\.cfx)) { Write-Host "CLEAN: $HOME\.cfx removed" }
     ```
   - [ ] `~/.cfx` (and `C:\Users\cfx-rehearsal\.cfx`) does not exist.

4. **Optional post-rehearsal account cleanup (Windows host admin PowerShell):**
   After signing out of `cfx-rehearsal`, remove the temporary local user from an elevated PowerShell window on your main account:
   ```powershell
   Remove-LocalUser -Name "cfx-rehearsal"
   ```

---

## 7. Final Pass / Fail Box

All gates below must be checked `YES` for the rehearsal to pass before tester outreach begins.

| Gate | Requirement | Windows (`cfx-rehearsal`) | Linux / macOS |
| :--- | :--- | :--- | :--- |
| **G1. Clean profile** | Rehearsed on a second local user account (Windows) and a clean user profile (Linux/macOS) | `[ ] YES  [ ] NO` | `[ ] YES  [ ] NO` |
| **G2. Time to first report** | Stopwatch `start` → `first report` is **under 10 minutes (`< 10:00`)** | `[ ] YES  [ ] NO` | `[ ] YES  [ ] NO` |
| **G3. Guide-only rule** | Every command came verbatim from `INSTALL-ALPHA.md`, `TESTING-GUIDE.md`, or `docs/testers/*.md` | `[ ] YES  [ ] NO` | `[ ] YES  [ ] NO` |
| **G4. Question log clear** | Every row in the Question Log is either empty or fixed in the docs before outreach | `[ ] YES  [ ] NO` | `[ ] YES  [ ] NO` |
| **G5. Windows fallback** | `python -m cfxd doctor` verified on the Windows local profile (`TESTING-GUIDE.md` §2) | `[ ] YES  [ ] NO` | `N/A` |
| **G6. Clean uninstall** | `claude plugin uninstall context-fabric@context-fabric`, `cfxd stop`, and `~/.cfx` deletion leave nothing behind | `[ ] YES  [ ] NO` | `[ ] YES  [ ] NO` |

```text
+-----------------------------------------------------------------------+
| COLD-INSTALL REHEARSAL VERDICT                                        |
|                                                                       |
| Build tag / commit : ________________________________________________ |
| Rehearsal date     : ________________________________________________ |
| Windows time       : ________ MM:SS to first report (target < 10:00)  |
| Linux/macOS time   : ________ MM:SS to first report (target < 10:00)  |
| Questions logged   : ________ (must be 0 unfixed to pass)             |
|                                                                       |
| Overall result     : [ ] PASS — ready for tester outreach             |
|                      [ ] FAIL — fix guide/install bugs and re-run     |
+-----------------------------------------------------------------------+
```
