#!/usr/bin/env bash
# Diagnose how vLLM reports prefix-cache hits (usage vs /metrics).
set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"
VENV="$HOME/.venvs/vllm"
MODEL=Qwen/Qwen2.5-1.5B-Instruct
LOG=/tmp/cf_vllm_debug.log
echo "[$(date +%T)] starting server"
systemctl --user stop cf-vllm.service 2>/dev/null || true
sleep 1
nohup "$VENV/bin/vllm" serve "$MODEL" --enable-prefix-caching --max-model-len 8192 \
  --enforce-eager --gpu-memory-utilization 0.85 --port 8000 >"$LOG" 2>&1 &
PID=$!
for i in $(seq 1 90); do
  curl -s -m 2 http://localhost:8000/v1/models >/dev/null 2>&1 && { echo "[$(date +%T)] ready"; break; }
  kill -0 "$PID" 2>/dev/null || { echo "died:"; tail -30 "$LOG"; exit 1; }
  sleep 5
done

"$VENV/bin/python" - <<'PY'
import json, httpx
base = "http://localhost:8000/v1"
stable = "STABLE PREFIX " + " ".join(f"token{i}" for i in range(1500))
def ask(user):
    r = httpx.post(base + "/chat/completions", json={
        "model": "Qwen/Qwen2.5-1.5B-Instruct",
        "messages": [{"role": "system", "content": stable}, {"role": "user", "content": user}],
        "max_tokens": 8, "temperature": 0.0}, timeout=120)
    return r.json()
r1 = ask("hello")
r2 = ask("hello again")
print("REQ1 usage:", json.dumps(r1.get("usage"), indent=2))
print("REQ2 usage:", json.dumps(r2.get("usage"), indent=2))
# also test the non-streaming presence of prompt_tokens_details keys
print("REQ2 keys:", list((r2.get("usage") or {}).keys()))
PY
echo "=== metrics (prefix_cache) ==="
curl -s http://localhost:8000/metrics | grep -iE "prefix_cache" | head -20
kill "$PID" 2>/dev/null || true
