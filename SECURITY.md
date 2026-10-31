# Security policy

Context Fabric is an alpha. It runs a small local daemon (`127.0.0.1:47831`) that Claude Code talks to through hooks.

## Reporting a vulnerability

Please report privately: on GitHub go to **Security → Report a vulnerability**. Do not post exploit details in a public
issue. We aim to acknowledge within a few days.

## What we already know (alpha limits)

- The hook endpoint is a fixed local port, authenticated by a per-install token the daemon writes to
  `<state_dir>/token` (0600). Hooks now run as commands that read that token and send it as `X-Cfx-Token`; a missing
  or wrong token gets `401`, and a port squatter that cannot prove the daemon's identity receives nothing. Because the
  token file is only readable by you, other users on a **shared machine** cannot read or forge hook traffic — but any
  process running **as you** can, so use the alpha on a single-user account.
- **Windows has no `0600`.** `os.chmod` is a no-op on Windows, so the daemon relies on the ACL of your user profile for
  the state directory (default `%USERPROFILE%\.cfx\daemon`): only your account (and administrators) can read the token
  and the logs. Moving the state directory to a shared, synced or world-readable location, or changing that ACL,
  removes this protection.
- **`/health` is unauthenticated.** `GET /health` needs no token and returns `{"ok": true, "pid": <daemon pid>,
  "version": "<cfxd version>"}`. It only answers a request whose `Host` header is `127.0.0.1:<port>` or
  `localhost:<port>` and that carries no `Origin`, so a web page cannot read it, but any local process can — it
  discloses the pid and version, no session data. Adding `?nonce=<value>` also returns an HMAC proof that the daemon
  holds the token.
- **The state directory is private to you, not content-free.** Daemon logs (`events`, `briefs`, `carry`, `folds`)
  hold event names, sizes, counts and hashes, never file contents or the token; `scripts/state_audit.py` scans them and
  found no content-like fields on the maintainer's machine (2026-10-31). The directory does also hold bounded prose:
  carry snapshots (paths, open todos, constraint sentences) and, from builds that had fold turned on, saved copies of
  full tool output under `obs/`. Fold is retired and pruned after 7 days; the tester build stops writing `obs/` and
  removes old blobs on upgrade. Treat the whole directory as sensitive and do not sync or share it.
- **Secret masking is best-effort pattern matching and cannot be complete.** It masks `NAME=value` assignments whose
  name contains `token`, `secret`, `key`, `pass` or `auth`; `--password`, `--passwd`, `--pass`, `--token`, `--secret`
  and `--api-key` values; `Bearer` tokens; `Authorization`, `Proxy-Authorization`, `X-Api-Key`, `Cookie` and
  `Set-Cookie` header values; `user:password` in `-u`/`--user` and in URLs; `-p` for `mysql`, `mysqldump` and
  `sshpass`; and bare GitHub (`ghp_`, `github_pat_`), OpenAI-style (`sk-`), Slack (`xox?-`), AWS (`AKIA`) and JWT
  patterns. It misses a secret under any other name, one split across arguments, and anything a command prints from a
  file or environment variable. Treat folded output and notes as sensitive.
- **`--share` is numeric by default.** `cfxd week`, `diet`, `ledger` and `brief` print counts, sizes, token estimates
  and model ids only, with ordinal labels (`skill 1`, `connector 2`) in place of names, and end with a "safe to paste"
  line. Name digests (4 hex characters of a SHA-1 of the name) appear only with the explicit `--share-digests` flag,
  intended for private use: anyone who guesses a plugin, skill or connector name can confirm it from the digest, so do
  not paste `--share-digests` output publicly.
- **Restored context can carry prompt injection.** After a compaction the daemon re-injects a small
  `<context-fabric-carry>` block of recorded facts as additional context. Transmission is bounded (3,000 bytes, with
  per-item caps), and every string is sanitised (angle brackets and backticks removed, control and format characters
  dropped) and rendered as data with a "reference, not instructions" prologue. That reduces but cannot remove the
  risk: paths, command fragments or notes that an attacker-controlled repository influenced can still read as
  persuasive text to the model. Verify restored facts before relying on them.
- **Brief excerpts are an opt-in surface.** Brief v1 (off by default) can include short excerpts of files the parent
  session read, so a sub-agent need not re-read them. Excerpts are off unless you set `brief_excerpts`; they are cut
  only from line ranges the parent actually read, never from files outside the project's git work tree, and never from
  secret-looking names (`.env*`, `*.pem`, `*.key`, `id_rsa*`, `credentials*`). Excerpts are sanitised and rendered as
  data, but they come from your repository, so a hostile repository can still try to shape them; see the prompt
  injection note above.
- **Carry v2 is experimental and off.** It re-injects up to 4,000 bytes (v1: 3,000) including constraint sentences and
  line ranges, and is gated by `carry_v2` (default off). The same sanitising and "reference, not instructions" framing
  apply. Constraint sentences come from user and assistant prose, so they can contain anything said in the session.
- **Windows access control is an assumption, not a guarantee.** The state directory relies on your profile ACL. On the
  maintainer's machine `icacls` showed access for the owner, SYSTEM and Administrators only; `scripts/state_audit.py` in
  the repository repeats that check. If your profile is shared, synced or ACL-modified, run the alpha on a single-user
  account.
- **Dependency audit.** On 2026-10-29, `uvx pip-audit` against this checkout reported **no known vulnerabilities**.
  That is a point-in-time snapshot against public advisory data for the resolved dependency set, not a guarantee;
  re-run it after dependency changes.

## Scope

In scope: the `cfxd/` package, `hooks/hooks.json`, `cfxd/launch.sh`, the plugin manifests. Out of scope: Claude Code
itself, third-party tools.
