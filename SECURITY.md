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
- Logs hold event names, sizes, counts and hashes, never file contents or the token. Saved copies of folded output
  (only if you turn fold on) and session notes for carry live under `~/.cfx/daemon/` and can contain parts of your
  code or commands (obvious secrets in commands are masked, but masking is pattern-based and incomplete).

## Scope

In scope: the `cfxd/` package, `hooks/hooks.json`, `cfxd/launch.sh`, the plugin manifests. Out of scope: Claude Code
itself, third-party tools.
