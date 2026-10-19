#!/usr/bin/env bash
# B8 WSL venv setup: separate venv for the agent + grader (mini-swe-agent + swebench + repo).
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"

VENV="$HOME/.venvs/swe"
REPO="/mnt/c/dev/Context-Fabric"

echo "[setup] creating venv at $VENV (python 3.12)"
uv venv --clear --python 3.12 "$VENV" 2>&1

PY="$VENV/bin/python"
echo "[setup] python: $($PY --version 2>&1)"

echo "[setup] installing mini-swe-agent, swebench, and the repo (editable)"
uv pip install --python "$PY" --quiet "mini-swe-agent" "swebench" 2>&1 | tail -8
uv pip install --python "$PY" --quiet -e "$REPO" 2>&1 | tail -5

echo "[setup] versions:"
"$PY" -m pip show mini-swe-agent swebench context-fabric 2>/dev/null | grep -E "^(Name|Version):"

echo "[setup] done"
