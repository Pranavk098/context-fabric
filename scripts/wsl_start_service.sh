#!/usr/bin/env bash
# Start vLLM as a persistent systemd user service (WSL2 has systemd=true).
set -euo pipefail
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
MODEL="${VLLM_MODEL:-Qwen/Qwen2.5-1.5B-Instruct}"

mkdir -p "$HOME/.config/systemd/user"
cat >"$HOME/.config/systemd/user/cf-vllm.service" <<EOF
[Unit]
Description=Context Fabric vLLM (Stage B spike)
After=network-online.target

[Service]
Type=simple
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=HF_HUB_ENABLE_HF_TRANSFER=0
ExecStart=$HOME/.venvs/vllm/bin/vllm serve $MODEL --enable-prefix-caching --max-model-len 4096 --gpu-memory-utilization 0.85 --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

loginctl enable-linger "$(id -un)" || true
systemctl --user daemon-reload
systemctl --user enable --now cf-vllm.service
sleep 3
systemctl --user --no-pager status cf-vllm.service | head -12 || true
echo "--- service created; model download + load may take minutes ---"
