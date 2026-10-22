# cfxd — Context Fabric daemon

A resident, fail-open hook endpoint. Claude Code points its `"type": "http"`
hooks at the daemon (no per-call process spawn); command-hook clients
(OpenCode/Codex) forward through a tiny shim. Any handler error, slow handler,
or daemon outage is invisible to the agent — every failure degrades to `{}`.

## Run

```sh
uv run python -m cfxd serve                 # bind 127.0.0.1, pick a free port
uv run python -m cfxd serve --port 4321     # or pick the port
uv run python -m cfxd status
uv run python -m cfxd doctor                # health + latency report
uv run python -m cfxd stop
```

State lives in `%USERPROFILE%/.cfx/daemon` by default; override with
`--state-dir` or the `CFX_HOME` env var. The daemon writes `state.json`
(`port`, `pid`, `token`, `started_at`, `version`) and appends request metadata
to `events.jsonl` — never payload content, which may hold user code.

## Claude Code http hook

`state.json` holds the bearer token. Point the hook at the daemon:

```jsonc
{
  "hooks": {
    "PreToolUse": [
      {
        "type": "http",
        "url": "http://127.0.0.1:52840/hooks/PreToolUse",
        "headers": { "Authorization": "Bearer <token from state.json>" }
      }
    ]
  }
}
```

Expose the token through an environment variable and list it in
`allowedEnvVars` rather than hard-coding it in the config. Repeat the block for
`PostToolUse`, `SubagentStart`, `PreCompact`, `SessionStart`, etc.

## Command-hook clients (OpenCode/Codex)

Run the shim as the hook command; it reads the event from stdin and writes the
daemon's response to stdout, failing open in under a second:

```sh
python -I -S /path/to/context-fabric/cfxd/shim.py PreToolUse
```

The shim imports only the stdlib (`json`, `os`, `sys`, `urllib.request`) and
never imports the `cfxd` package, so it runs under `python -I -S`.

## Measured (this machine)

`uv run python -m cfxd doctor` on Windows, Python 3.13:

```
round trips: n=200 p50=15.27 ms p95=16.74 ms (200/200 ok)
shim cold:   n=20   p50=123.88 ms p95=128.22 ms (0 failed)
```

p95 HTTP round trip stays under the 50 ms budget; shim cold-start is dominated
by interpreter startup and is reported, not gated.
