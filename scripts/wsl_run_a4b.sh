#!/usr/bin/env bash
# A4B exploratory concurrency sweep — vLLM serving parameters, both arms, N in 16,32.
#
# For each serving config (label -> extra `vllm serve` flags) it restarts vLLM once per
# arm (raw, then compile) and runs `evals.a4b_concurrency` at N=16,32 × 2 repeats, stamping
# the config label into each receipt. EXPLORATORY results (not a pre-registered claim).
#
#   CF_A4_FIXTURE=/mnt/c/Users/<user>/AppData/Local/Temp/opencode/httpx_pilot \
#     wsl -d Ubuntu -- bash /mnt/c/Users/<user>/orca/workspaces/Context-Fabric/track-t3-concurrency/scripts/wsl_run_a4b.sh
set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
VENV="$HOME/.venvs/vllm"
REPO="${CF_REPO:-/mnt/c/dev/Context-Fabric}"
FIXTURE="${CF_A4_FIXTURE:?set CF_A4_FIXTURE to the httpx clone (WSL path)}"
MODEL="${VLLM_MODEL:-Qwen/Qwen2.5-1.5B-Instruct}"
MAX_LEN="${VLLM_MAX_LEN:-32768}"
NLEVELS="${CF_A4B_N:-16,32}"
REPEATS="${CF_A4B_REPEATS:-2}"
LOG=/tmp/cf_a4b_vllm.log
VLLM_PID=""

# Serving configs: label|extra `vllm serve` flags (appended after the shared base flags).
CONFIGS=(
  "baseline|"
  "b8192|--max-num-batched-tokens 8192"
  "b16384|--max-num-batched-tokens 16384"
  "b32768|--max-num-batched-tokens 32768"
  "b8192s256|--max-num-batched-tokens 8192 --max-num-seqs 256"
  "nochunk|--no-enable-chunked-prefill --max-num-batched-tokens 32768"
)

reclaim() {
  rm -f "$HOME/.config/systemd/user/cf-vllm.service" \
        "$HOME/.config/systemd/user/default.target.wants/cf-vllm.service" 2>/dev/null
  systemctl --user stop cf-vllm.service 2>/dev/null || true
  pkill -9 -f "vllm serve" 2>/dev/null || true
  pkill -9 -f "VLLM::EngineCore" 2>/dev/null || true
  sleep 4
}

start_vllm() {
  local label="$1"; shift
  echo "[$(date +%T)] starting vllm serve ($MODEL, config=$label, extra flags: $*)"
  nohup "$VENV/bin/vllm" serve "$MODEL" \
    --enable-prefix-caching --max-model-len "$MAX_LEN" --enforce-eager \
    --gpu-memory-utilization 0.85 --port 8000 "$@" >"$LOG" 2>&1 &
  VLLM_PID=$!
  local ready=0
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

rc=0
for entry in "${CONFIGS[@]}"; do
  label="${entry%%|*}"
  flags="${entry#*|}"
  # shellcheck disable=SC2086  # flags are intentionally word-split into `vllm serve`
  echo "[$(date +%T)] ===== config=$label ====="

  reclaim
  start_vllm "$label" $flags
  "$VENV/bin/python" -m evals.a4b_concurrency --arm raw --config "$label" --n "$NLEVELS" --repeats "$REPEATS" --fixture "$FIXTURE"
  raw_rc=$?
  echo "[$(date +%T)] config=$label raw rc=$raw_rc"
  stop_vllm

  start_vllm "$label" $flags
  "$VENV/bin/python" -m evals.a4b_concurrency --arm compile --config "$label" --n "$NLEVELS" --repeats "$REPEATS" --fixture "$FIXTURE"
  cmp_rc=$?
  echo "[$(date +%T)] config=$label compile rc=$cmp_rc"
  stop_vllm

  [ "$raw_rc" -eq 0 ] && [ "$cmp_rc" -eq 0 ] || rc=1
done

stop_vllm
echo "[$(date +%T)] done rc=$rc"
exit "$rc"
