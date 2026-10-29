---
description: Save this session's working state, then start fresh with /clear (experimental)
allowed-tools: Write, Bash(sh "${CLAUDE_PLUGIN_ROOT}/cfxd/launch.sh" checkpoint-save), Bash(sh "${CLAUDE_PLUGIN_ROOT}/cfxd/launch.sh" checkpoint-save --note-file *)
disable-model-invocation: true
---

The user's optional goal note is the text after the command name (it may be empty):

$ARGUMENTS

1. If that text is not empty, use the Write tool to write it verbatim to
   `${CLAUDE_PLUGIN_DATA}/checkpoint-note-${CLAUDE_SESSION_ID}.txt`. Do not add or change anything.
2. Run exactly this with the Bash tool:
   `sh "${CLAUDE_PLUGIN_ROOT}/cfxd/launch.sh" checkpoint-save --note-file "${CLAUDE_PLUGIN_DATA}/checkpoint-note-${CLAUDE_SESSION_ID}.txt"`
   If the note was empty, run instead:
   `sh "${CLAUDE_PLUGIN_ROOT}/cfxd/launch.sh" checkpoint-save`
3. Print that command's output line unchanged, exactly once. Only if it begins with
   "Checkpoint saved", then say: "Now run /clear to start fresh from this checkpoint."
   Otherwise say nothing more.

Nothing was sent anywhere; the checkpoint stays in the local Context Fabric state directory.
