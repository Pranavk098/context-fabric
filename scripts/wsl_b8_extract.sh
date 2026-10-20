#!/usr/bin/env bash
# B8 smoke: pull the task image and extract /testbed for the hand-off builders.
set -uo pipefail
IMAGE="swebench/sweb.eval.x86_64.astropy_1776_astropy-7606:latest"
DEST="/mnt/c/dev/Context-Fabric/evals/b8_repos/astropy__astropy-7606"
echo "[extract] pulling $IMAGE"
docker pull "$IMAGE"
mkdir -p "$DEST"
echo "[extract] extracting /testbed to $DEST"
# shortcut: image is pinned by tag (not digest); --no-same-owner stops a hostile archive
# from chowning files, and GNU tar strips leading '/' and '..' path entries by default.
# If the swebench image is ever swapped for an untrusted one, pin it by SHA first.
docker run --rm "$IMAGE" tar -c -C /testbed . | tar -x --no-same-owner -C "$DEST"
echo "[extract] done; file count:"
find "$DEST" -name "*.py" | wc -l
echo "[extract] has .git:"; [ -d "$DEST/.git" ] && echo yes || echo no
