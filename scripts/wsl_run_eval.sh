#!/usr/bin/env bash
# Generic Stage B runner: start vLLM, run a given eval module in the same WSL session.
#   wsl -d Ubuntu -- bash /mnt/c/dev/Context-Fabric/scripts/wsl_run_eval.sh evals.stage_b2
set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
VENV="$HOME/.venvs/vllm"
REPO=/mnt/c/dev/Context-Fabric
MODULE="${1:-evals.spike_kv_cache}"
MODEL="${VLLM_MODEL:-Qwen/Qwen2.5-1.5B-Instruct}"
LOG=/tmp/cf_vllm_run.log

echo "[$(date +%T)] reclaiming GPU/port"
rm -f "$HOME/.config/systemd/user/cf-vllm.service" \
      "$HOME/.config/systemd/user/default.target.wants/cf-vllm.service" 2>/dev/null
systemctl --user stop cf-vllm.service 2>/dev/null || true
pkill -9 -f "vllm serve" 2>/dev/null || true
pkill -9 -f "VLLM::EngineCore" 2>/dev/null || true
sleep 4

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
  if ! kill -0 "$VLLM_PID" 2>/dev/null; then echo "vllm exited:"; tail -40 "$LOG"; exit 1; fi
  sleep 5
done
[ "$ready" -eq 1 ] || { echo "timeout"; tail -40 "$LOG"; exit 1; }

echo "[$(date +%T)] running $MODULE"
export CF_BASE_URL="http://localhost:8000/v1" CF_MODEL="$MODEL" PYTHONPATH="$REPO"
cd "$REPO"
"$VENV/bin/python" -m "$MODULE"
rc=$?
echo "[$(date +%T)] $MODULE rc=$rc"
kill "$VLLM_PID" 2>/dev/null || true
exit $rc
