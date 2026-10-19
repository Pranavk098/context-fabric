#!/usr/bin/env bash
# B8 smoke: pull the task image and extract /testbed for the hand-off builders.
set -uo pipefail
IMAGE="swebench/sweb.eval.x86_64.astropy_1776_astropy-7606:latest"
DEST="/mnt/c/dev/Context-Fabric/evals/b8_repos/astropy__astropy-7606"
echo "[extract] pulling $IMAGE"
docker pull "$IMAGE"
mkdir -p "$DEST"
echo "[extract] extracting /testbed to $DEST"
docker run --rm "$IMAGE" tar -c -C /testbed . | tar -x -C "$DEST"
echo "[extract] done; file count:"
find "$DEST" -name "*.py" | wc -l
echo "[extract] has .git:"; [ -d "$DEST/.git" ] && echo yes || echo no
