#!/usr/bin/env bash
# Install-only stage for vLLM in WSL. Run synchronously with a long timeout:
#   wsl -d Ubuntu -- bash -lc "bash /mnt/c/dev/Context-Fabric/scripts/wsl_install_vllm.sh"
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo "[$(date +%T)] installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
echo "[$(date +%T)] uv: $(uv --version)"

echo "[$(date +%T)] installing python 3.12"
uv python install 3.12

echo "[$(date +%T)] creating venv ~/.venvs/vllm"
mkdir -p "$HOME/.venvs"
uv venv "$HOME/.venvs/vllm" --python 3.12

echo "[$(date +%T)] installing vllm (large download)"
VIRTUAL_ENV="$HOME/.venvs/vllm" uv pip install vllm

echo "[$(date +%T)] verifying"
"$HOME/.venvs/vllm/bin/python" - <<'PY'
import torch, vllm
print("torch", torch.__version__, "cuda_available", torch.cuda.is_available())
if torch.cuda.is_available():
    print("gpu", torch.cuda.get_device_name(0), "cap", torch.cuda.get_device_capability(0))
print("vllm", vllm.__version__)
PY
echo "[$(date +%T)] INSTALL_OK"
