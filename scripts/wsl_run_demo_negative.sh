#!/usr/bin/env bash
# Fresh negative-knowledge demo rerun (1.5B main + 3B probe) in ONE WSL session.
# Replicates the superseded set's frozen protocol: 1.5B -> trap `cache`; 3B -> traps
# `queue,metrics`; 2 repeats per arm; no pilot re-selection (--freeze-traps).
#   wsl -d Ubuntu -- bash /mnt/c/dev/Context-Fabric/scripts/wsl_run_demo_negative.sh
set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
VENV="$HOME/.venvs/vllm"
REPO=/mnt/c/dev/Context-Fabric
OUTDIR="$REPO/evals/reports"
LOG=/tmp/cf_vllm_demo.log

reclaim() {
  rm -f "$HOME/.config/systemd/user/cf-vllm.service" \
        "$HOME/.config/systemd/user/default.target.wants/cf-vllm.service" 2>/dev/null
  systemctl --user stop cf-vllm.service 2>/dev/null || true
  pkill -9 -f "vllm serve" 2>/dev/null || true
  pkill -9 -f "VLLM::EngineCore" 2>/dev/null || true
  sleep 4
}

run_model() {
  local model="$1" traps="$2" prefix="$3"
  echo "[$(date +%T)] starting vllm serve $model"
  nohup "$VENV/bin/vllm" serve "$model" \
    --enable-prefix-caching --max-model-len 8192 --enforce-eager \
    --gpu-memory-utilization 0.85 --port 8000 >"$LOG" 2>&1 &
  local pid=$!
  local ready=0 i
  for i in $(seq 1 90); do
    if curl -s -m 2 http://localhost:8000/v1/models >/dev/null 2>&1; then
      echo "[$(date +%T)] endpoint ready after ~$((i * 5))s"; ready=1; break
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "[$(date +%T)] vllm exited early:"; tail -40 "$LOG"; return 1
    fi
    sleep 5
  done
  if [ "$ready" -ne 1 ]; then echo "timed out waiting for vllm"; tail -40 "$LOG"; return 1; fi

  echo "[$(date +%T)] warming engine (smoke request)"
  curl -s -m 300 http://localhost:8000/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"$model\",\"messages\":[{\"role\":\"user\",\"content\":\"warmup\"}],\"max_tokens\":1,\"temperature\":0.0}" \
    >/dev/null || echo "warmup request failed; continuing"

  local ts out rc
  ts=$(date +%Y%m%d_%H%M%S)
  out="$OUTDIR/${prefix}_${ts}.json"
  echo "[$(date +%T)] running negative demo -> $out"
  ( cd "$REPO" && PYTHONPATH="$REPO" "$VENV/bin/python" -m sidecar.demo \
      --upstream http://localhost:8000 --model "$model" --scenario negative \
      --freeze-traps "$traps" --out "$out" )
  rc=$?
  echo "[$(date +%T)] demo rc=$rc out=$out"
  kill "$pid" 2>/dev/null || true
  pkill -9 -f "VLLM::EngineCore" 2>/dev/null || true
  sleep 3
  return $rc
}

echo "[$(date +%T)] reclaiming GPU/port"
reclaim
"$VENV/bin/python" -c "import httpx" 2>/dev/null || VIRTUAL_ENV="$VENV" uv pip install -q httpx

run_model "Qwen/Qwen2.5-1.5B-Instruct" "cache" "demo_negative" || exit 1
run_model "Qwen/Qwen2.5-3B-Instruct" "queue,metrics" "demo_negative_3b" || exit 1
echo "[$(date +%T)] all negative runs complete"
