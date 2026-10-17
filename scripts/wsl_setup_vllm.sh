#!/usr/bin/env bash
# Stage B runtime bring-up — run inside WSL (background):
#   wsl -d Ubuntu -- bash -lc "nohup bash /mnt/c/dev/Context-Fabric/scripts/wsl_setup_vllm.sh >/tmp/cf_vllm.out 2>&1 &"
# Logs: /tmp/cf_vllm_setup.log   Endpoint: http://localhost:8000/v1
set -euo pipefail
LOG=/tmp/cf_vllm_setup.log
exec >"$LOG" 2>&1
echo "[$(date +%T)] setup start"

export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo "[$(date +%T)] installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
uv --version

echo "[$(date +%T)] installing python 3.12"
uv python install 3.12

echo "[$(date +%T)] creating venv"
mkdir -p "$HOME/.venvs"
uv venv "$HOME/.venvs/vllm" --python 3.12
# shellcheck disable=SC1091
source "$HOME/.venvs/vllm/bin/activate"

echo "[$(date +%T)] installing vllm (large download; be patient)"
uv pip install vllm

echo "[$(date +%T)] starting vllm serve"
MODEL="${VLLM_MODEL:-Qwen/Qwen2.5-1.5B-Instruct}"
exec vllm serve "$MODEL" \
  --enable-prefix-caching \
  --max-model-len 4096 \
  --gpu-memory-utilization 0.85 \
  --port 8000
