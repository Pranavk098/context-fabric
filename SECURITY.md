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
- Logs hold event names, sizes, counts and hashes, never file contents or the token. Saved copies of folded output
  (only if you turn fold on) and session notes for carry live under `~/.cfx/daemon/` and can contain parts of your
  code or commands.
- **Secret masking is best-effort pattern matching and cannot be complete.** It masks `NAME=value` assignments whose
  name contains `token`, `secret`, `key`, `pass` or `auth`; `--password`, `--passwd`, `--pass`, `--token`, `--secret`
  and `--api-key` values; `Bearer` tokens; `Authorization`, `Proxy-Authorization`, `X-Api-Key`, `Cookie` and
  `Set-Cookie` header values; `user:password` in `-u`/`--user` and in URLs; `-p` for `mysql`, `mysqldump` and
  `sshpass`; and bare GitHub (`ghp_`, `github_pat_`), OpenAI-style (`sk-`), Slack (`xox?-`), AWS (`AKIA`) and JWT
  patterns. It misses a secret under any other name, one split across arguments, and anything a command prints from a
  file or environment variable. Treat folded output and notes as sensitive.
- **`--share` is pseudonymous, not anonymous.** `cfxd diet --share` replaces each name with a kind-prefixed label plus
  a short digest (4 hex characters of a SHA-1 of the name) and keeps counts, sizes and token estimates;
  `cfxd week --share` and `cfxd ledger --share` print counts, sizes and model ids and append a safe-to-paste line.
  Because the label is derived from the name, anyone who guesses a plugin, skill or connector name can confirm it.
- **Restored context can carry prompt injection.** After a compaction the daemon re-injects a small
  `<context-fabric-carry>` block of recorded facts as additional context. Transmission is bounded (3,000 bytes, with
  per-item caps), and every string is sanitised (angle brackets and backticks removed, control and format characters
  dropped) and rendered as data with a "reference, not instructions" prologue. That reduces but cannot remove the
  risk: paths, command fragments or notes that an attacker-controlled repository influenced can still read as
  persuasive text to the model. Verify restored facts before relying on them.
- **Dependency audit.** On 2026-10-29, `uvx pip-audit` against this checkout reported **no known vulnerabilities**.
  That is a point-in-time snapshot against public advisory data for the resolved dependency set, not a guarantee;
  re-run it after dependency changes.

## Scope

In scope: the `cfxd/` package, `hooks/hooks.json`, `cfxd/launch.sh`, the plugin manifests. Out of scope: Claude Code
itself, third-party tools.
