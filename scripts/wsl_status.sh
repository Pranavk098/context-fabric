#!/usr/bin/env bash
# Poll vLLM service status + endpoint readiness.
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
echo "=== journal ==="
journalctl --user -u cf-vllm.service --no-pager -n 15 2>/dev/null | tail -15
echo "=== endpoint ==="
curl -s -m 5 http://localhost:8000/v1/models
echo
