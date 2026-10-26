---
description: Save this session's working state, then start fresh with /clear (experimental)
allowed-tools: Bash(sh "${CLAUDE_PLUGIN_ROOT}/cfxd/launch.sh" checkpoint-save)
disable-model-invocation: true
---

!`sh "${CLAUDE_PLUGIN_ROOT}/cfxd/launch.sh" checkpoint-save`

Print the line above unchanged, exactly once. Then say:
"Now run /clear to start fresh from this checkpoint."

Nothing was sent anywhere; the checkpoint stays in the local Context Fabric state directory.
