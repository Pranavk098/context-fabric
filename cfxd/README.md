# cfxd — Context Fabric daemon

A resident, fail-open hook endpoint. Claude Code points command hooks at the
daemon through `cfxd/launch.sh forward <Event>`; `SessionStart`/`PreCompact`
use the same launcher. The launcher reads the daemon's token file and sends it
as `X-Cfx-Token`. Any handler error, slow handler, or daemon outage is
invisible to the agent — every failure degrades to `{}`.

## Run

```sh
uv run python -m cfxd serve                 # bind 127.0.0.1:47831
uv run python -m cfxd serve --port 4321     # or pick the port (CFX_PORT env also works)
uv run python -m cfxd status
uv run python -m cfxd doctor                # health + latency report
uv run python -m cfxd stop
```

State lives in `%USERPROFILE%/.cfx/daemon` by default; override with
`--state-dir` or the `CFX_HOME` env var. The daemon writes `state.json`
(`port`, `pid`, `started_at`, `version`) and appends request metadata to
`events.jsonl` — never payload content, which may hold user code.

## Security model

The daemon binds `127.0.0.1` only. A `/hooks/*` POST is rejected (403) with an
`Origin` header, a non-loopback `Host` (DNS-rebinding defence), or a non-JSON
`Content-Type`, and rejected (401) without an `X-Cfx-Token` header equal to the
value in `<state_dir>/token` (0600). `/health` stays open but proves identity
only to a caller that holds the token (HMAC nonce challenge).

## Install the plugin

1. Install `uv` — Windows PowerShell:
   `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
   or macOS/Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`
2. Add the marketplace: `/plugin marketplace add <owner>/Context-Fabric`
   (or add the local path of this repository).
3. `/plugin install context-fabric@context-fabric`
4. Restart Claude Code.
5. Verify the daemon with `uv run python -m cfxd doctor`.

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
round trips: n=200 p50=15.00 ms p95=17.30 ms (200/200 ok)
shim cold:   n=20   p50=108.91 ms p95=129.55 ms (0 failed)
```

p95 HTTP round trip stays under the 50 ms budget; shim cold-start is dominated
by interpreter startup and is reported, not gated.
