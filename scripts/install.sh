#!/bin/sh
# Context Fabric alpha installer (macOS / Linux). Documented one-liner:
#   curl -fsSL https://raw.githubusercontent.com/Pranavk098/context-fabric/main/scripts/install.sh | sh
# Read it first if you prefer: it only checks prerequisites, runs `cfxd doctor`, and adds the plugin.
# Usage: sh install.sh [--dry-run]
# Never uses sudo. Changes only the Claude Code plugin install and (by you, later) cfxd config.
# Safe to re-run: an already-installed plugin is left alone.

REPO="Pranavk098/context-fabric"
CFXD="uvx --from git+https://github.com/${REPO} cfxd"
dry=0
for a in "$@"; do
  case "$a" in
    --dry-run) dry=1 ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

say() { echo "[context-fabric] $*"; }
fail() { echo "[context-fabric] ERROR: $1" >&2; exit 1; }
run() {
  if [ "$dry" = 1 ]; then say "dry-run: would run: $*"; else say "running: $*"; "$@"; fi
}

[ "$dry" = 1 ] && say "dry-run: nothing will be changed"

missing=0
if command -v uv >/dev/null 2>&1; then say "found uv"; else
  echo "[context-fabric] missing: uv (install: https://docs.astral.sh/uv/getting-started/installation/)" >&2; missing=1; fi
if command -v claude >/dev/null 2>&1; then say "found claude"; else
  echo "[context-fabric] missing: claude (Claude Code: https://docs.claude.com/en/docs/claude-code)" >&2; missing=1; fi
if [ "$missing" = 1 ]; then
  if [ "$dry" = 1 ]; then say "dry-run: would stop here until the prerequisites above are installed"; else exit 1; fi
fi

say "step 1/3: cfxd doctor (informational; 'not running' is normal before the first session)"
if [ "$dry" = 1 ]; then
  say "dry-run: would run: $CFXD doctor"
else
  $CFXD doctor || say "doctor reported a problem; continuing, see docs/INSTALL-ALPHA.md section 7"
fi

say "step 2/3: add the marketplace and install the plugin"
if [ "$dry" = 0 ] && claude plugin list 2>/dev/null | grep -q "context-fabric@context-fabric"; then
  say "plugin already installed; nothing to do"
else
  run claude plugin marketplace add "$REPO" || say "marketplace may already be added; continuing"
  run claude plugin install context-fabric@context-fabric || fail "plugin install failed"
fi

say "step 3/3: next steps"
cat <<MSG
Restart Claude Code; the first session starts the local helper.
Optional and EXPERIMENTAL (effect unproven): turn on Brief for sub-agent handoffs:
  $CFXD set brief on
  $CFXD set brief_v1 on
After a session that used sub-agents, see the receipts:
  $CFXD brief
MSG
