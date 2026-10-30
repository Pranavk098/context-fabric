---
description: Show what each sub-agent handoff carried and what the child did with it
allowed-tools: Bash(sh:*)
disable-model-invocation: true
---

```!
sh -c 'r=$1; shift; PYTHONPATH="$r" exec uv run --no-config --no-project --quiet --python ">=3.12" python -m cfxd brief "$@"' sh "${CLAUDE_PLUGIN_ROOT}" $ARGUMENTS
```

Print the output above unchanged. Do not add commentary or read any files.

Nothing was sent anywhere; this reads the local Context Fabric brief log and your own transcripts, and prints paths and counts only.
