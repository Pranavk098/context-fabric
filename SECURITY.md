# Security policy

Context Fabric is an alpha. It runs a small local daemon (`127.0.0.1:47831`) that Claude Code talks to through hooks.

## Reporting a vulnerability

Please report privately: on GitHub go to **Security → Report a vulnerability**. Do not post exploit details in a public
issue. We aim to acknowledge within a few days.

## What we already know (alpha limits)

- The hook endpoint is a fixed local port with no shared secret, because Claude Code's HTTP hooks cannot send one yet.
  On a **shared multi-user machine** another local user could read hook traffic or answer in the daemon's place.
  The launcher verifies the daemon's identity at session start and warns, but cannot block this. Use the alpha on
  single-user computers.
- Logs hold event names, sizes, counts and hashes, never file contents. Saved copies of folded output (only if you turn
  fold on) and session notes for carry live under `~/.cfx/daemon/` and can contain parts of your code or commands
  (obvious secrets in commands are masked, but masking is pattern-based and incomplete).

## Scope

In scope: the `cfxd/` package, `hooks/hooks.json`, `cfxd/launch.sh`, the plugin manifests. Out of scope: Claude Code
itself, third-party tools.
