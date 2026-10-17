#!/usr/bin/env bash
# Run the Stage B KV-cache spike entirely inside WSL in one session (keeps the VM alive
# so vLLM stays up while the spike runs).
#   wsl -d Ubuntu -- bash /mnt/c/dev/Context-Fabric/scripts/wsl_run_spike.sh
set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
VENV="$HOME/.venvs/vllm"
REPO=/mnt/c/dev/Context-Fabric
MODEL="${VLLM_MODEL:-Qwen/Qwen2.5-1.5B-Instruct}"
LOG=/tmp/cf_vllm_run.log

echo "[$(date +%T)] reclaming GPU/port from any prior vllm/service"
rm -f "$HOME/.config/systemd/user/cf-vllm.service" \
      "$HOME/.config/systemd/user/default.target.wants/cf-vllm.service" 2>/dev/null
systemctl --user stop cf-vllm.service 2>/dev/null || true
pkill -9 -f "vllm serve" 2>/dev/null || true
pkill -9 -f "VLLM::EngineCore" 2>/dev/null || true
sleep 4

echo "[$(date +%T)] ensuring client deps in venv"
VIRTUAL_ENV="$VENV" uv pip install -q httpx 2>&1 | tail -2

echo "[$(date +%T)] starting vllm serve"
nohup "$VENV/bin/vllm" serve "$MODEL" \
  --enable-prefix-caching --max-model-len 8192 --enforce-eager \
  --gpu-memory-utilization 0.85 --port 8000 >"$LOG" 2>&1 &
VLLM_PID=$!

ready=0
for i in $(seq 1 90); do
  if curl -s -m 2 http://localhost:8000/v1/models >/dev/null 2>&1; then
    echo "[$(date +%T)] endpoint ready after ~$((i * 5))s"; ready=1; break
  fi
  if ! kill -0 "$VLLM_PID" 2>/dev/null; then
    echo "[$(date +%T)] vllm exited early:"; tail -40 "$LOG"; exit 1
  fi
  sleep 5
done
if [ "$ready" -ne 1 ]; then echo "timed out waiting for vllm"; tail -40 "$LOG"; exit 1; fi

curl -s -m 5 http://localhost:8000/v1/models; echo

echo "[$(date +%T)] running spike"
export CF_BASE_URL="http://localhost:8000/v1"
export CF_MODEL="$MODEL"
export PYTHONPATH="$REPO"
cd "$REPO"
"$VENV/bin/python" -m evals.spike_kv_cache
rc=$?
echo "[$(date +%T)] spike rc=$rc"
kill "$VLLM_PID" 2>/dev/null || true
exit $rc
