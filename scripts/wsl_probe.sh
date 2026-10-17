#!/usr/bin/env bash
# Stage B runtime probe — run inside WSL:  bash /mnt/c/dev/Context-Fabric/scripts/wsl_probe.sh
set +e
echo "== python =="
python3 --version
echo "== pip =="
pip3 --version 2>&1 | tail -1
echo "== venvs =="
ls -d ~/.venvs/* 2>/dev/null
echo "== torch =="
python3 - <<'PY' 2>&1 | tail -3
try:
    import torch
    print("torch", torch.__version__, "cuda:", torch.cuda.is_available())
    if torch.cuda.is_available():
        print("gpu:", torch.cuda.get_device_name(0), "cap:", torch.cuda.get_device_capability(0))
except Exception as e:
    print("torch not available:", e)
PY
echo "== vllm =="
python3 - <<'PY' 2>&1 | tail -2
try:
    import vllm
    print("vllm", vllm.__version__)
except Exception as e:
    print("vllm not available:", e)
PY
echo "== disk =="
df -h / | tail -1
echo "== nvcc =="
nvcc --version 2>/dev/null | tail -2 || echo "no nvcc"
