#!/bin/sh
# One-command check that the Context Fabric plugin works on this machine (macOS, Linux, Git Bash).
# Runs a tiny Claude Code session with the plugin loaded, then checks the daemon saw it.
# Usage: sh scripts/smoke_plugin.sh [path-to-repo-or-plugin-dir]   (default: the repo this script lives in)
# Prints PASS or FAIL with the reason. Makes no changes to your Claude Code settings.

here=$(cd "$(dirname "$0")/.." && pwd)
plugin_dir=${1:-$here}
cfx="uvx --from git+https://github.com/Pranavk098/context-fabric cfxd"
[ -f "$plugin_dir/cfxd/__main__.py" ] && cfx="uv run --no-project --quiet --python >=3.12 python -m cfxd"

fail() { echo "FAIL: $1"; exit 1; }

command -v uv >/dev/null 2>&1 || fail "uv not found (install: https://docs.astral.sh/uv/)"
command -v claude >/dev/null 2>&1 || fail "claude not found (install Claude Code first)"

work=$(mktemp -d 2>/dev/null || echo "${TMPDIR:-/tmp}/cfx-smoke-$$"); mkdir -p "$work"
echo "1/4 stopping any running helper"
(cd "$plugin_dir" && $cfx stop >/dev/null 2>&1)
rm -f "${HOME}/.cfx/daemon/welcomed"

echo "2/4 running a tiny Claude Code session with the plugin loaded (about 20 seconds)"
(cd "$work" && MSYS_NO_PATHCONV=1 claude -p "Reply with just: ok" --plugin-dir "$plugin_dir" --output-format json >"$work/out.json" 2>"$work/err.txt") \
  || fail "claude session failed: $(head -c 300 "$work/err.txt")"

echo "3/4 checking the helper"
out=$(cd "$plugin_dir" && $cfx doctor 2>&1)
echo "$out" | grep -q "identity:.*verified" || fail "daemon identity not verified: $(echo "$out" | tail -3 | tr '\n' ' ')"
echo "$out" | grep -q "healthy" || fail "doctor did not report healthy"

echo "4/4 checking the plugin saw the session"
ev="${HOME}/.cfx/daemon/events.jsonl"
[ -f "$ev" ] || fail "no events log at $ev (hooks did not reach the daemon)"
grep -q '"event": "SessionStart"' "$ev" || fail "no SessionStart event logged (hooks did not run)"
grep -q '"outcome": "rejected"' "$ev" && echo "note: some requests were rejected; run 'cfxd doctor' for details"

echo "PASS: plugin loaded, helper healthy with verified identity, SessionStart handled."
echo "(If a welcome message appeared in your session, that was the first-run notice.)"
