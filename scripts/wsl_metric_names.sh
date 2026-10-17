#!/usr/bin/env bash
# Discover vLLM cache/usage metric names (no server needed).
SP="$HOME/.venvs/vllm/lib/python3.12/site-packages/vllm"
echo "== metric-ish strings =="
grep -rhoE 'vllm:[a-zA-Z_]+' "$SP" 2>/dev/null | sort -u | head -40
echo "== usage_perc references =="
grep -rhoE '[a-zA-Z_]*usage_perc' "$SP" 2>/dev/null | sort -u | head -20
echo "== kv_cache metrics =="
grep -rhoE 'name="[a-zA-Z_]+"' "$SP/v1/metrics" 2>/dev/null | sort -u | head -40
