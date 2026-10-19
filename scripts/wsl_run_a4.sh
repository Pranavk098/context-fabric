#!/usr/bin/env bash
# A4 concurrency runner: raw arm, restart vLLM, compile arm, stop vLLM — one WSL session.
#   wsl -d Ubuntu -- bash /mnt/c/Users/<user>/orca/workspaces/Context-Fabric/wave-a4-concurrency/scripts/wsl_run_a4.sh
set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
VENV="$HOME/.venvs/vllm"
REPO="${CF_REPO:-/mnt/c/Users/<user>/orca/workspaces/Context-Fabric/wave-a4-concurrency}"
FIXTURE="${CF_A4_FIXTURE:-/mnt/c/Users/<user>/AppData/Local/Temp/opencode/httpx_pilot}"
MODEL="${VLLM_MODEL:-Qwen/Qwen2.5-1.5B-Instruct}"
MAX_LEN="${VLLM_MAX_LEN:-32768}"
LOG=/tmp/cf_a4_vllm.log
VLLM_PID=""

reclaim() {
  rm -f "$HOME/.config/systemd/user/cf-vllm.service" \
        "$HOME/.config/systemd/user/default.target.wants/cf-vllm.service" 2>/dev/null
  systemctl --user stop cf-vllm.service 2>/dev/null || true
  pkill -9 -f "vllm serve" 2>/dev/null || true
  pkill -9 -f "VLLM::EngineCore" 2>/dev/null || true
  sleep 4
}

start_vllm() {
  echo "[$(date +%T)] starting vllm serve ($MODEL, max-len $MAX_LEN)"
  nohup "$VENV/bin/vllm" serve "$MODEL" \
    --enable-prefix-caching --max-model-len "$MAX_LEN" --enforce-eager \
    --gpu-memory-utilization 0.85 --port 8000 >"$LOG" 2>&1 &
  VLLM_PID=$!
  ready=0
  for i in $(seq 1 90); do
    if curl -s -m 2 http://localhost:8000/v1/models >/dev/null 2>&1; then
      echo "[$(date +%T)] endpoint ready after ~$((i * 5))s"; ready=1; break
    fi
    if ! kill -0 "$VLLM_PID" 2>/dev/null; then echo "[$(date +%T)] vllm exited:"; tail -40 "$LOG"; exit 1; fi
    sleep 5
  done
  [ "$ready" -eq 1 ] || { echo "[$(date +%T)] timeout"; tail -40 "$LOG"; exit 1; }

  echo "[$(date +%T)] warming engine"
  curl -s -m 300 http://localhost:8000/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"warmup\"}],\"max_tokens\":1,\"temperature\":0.0}" \
    >/dev/null || echo "warmup request failed; continuing"
  echo "[$(date +%T)] engine warm"
}

stop_vllm() {
  [ -n "$VLLM_PID" ] && kill "$VLLM_PID" 2>/dev/null || true
  pkill -9 -f "vllm serve" 2>/dev/null || true
  pkill -9 -f "VLLM::EngineCore" 2>/dev/null || true
  sleep 4
}

export CF_BASE_URL="http://localhost:8000/v1" CF_MODEL="$MODEL" PYTHONPATH="$REPO"
cd "$REPO" || exit 1

echo "[$(date +%T)] ===== A4 raw arm ====="
reclaim
start_vllm
"$VENV/bin/python" -m evals.a4_concurrency --arm raw --fixture "$FIXTURE"
RAW_RC=$?
echo "[$(date +%T)] raw arm rc=$RAW_RC"
stop_vllm

echo "[$(date +%T)] ===== A4 compile arm ====="
start_vllm
"$VENV/bin/python" -m evals.a4_concurrency --arm compile --fixture "$FIXTURE"
CMP_RC=$?
echo "[$(date +%T)] compile arm rc=$CMP_RC"
stop_vllm

echo "[$(date +%T)] done raw_rc=$RAW_RC compile_rc=$CMP_RC"
[ "$RAW_RC" -eq 0 ] && [ "$CMP_RC" -eq 0 ]
