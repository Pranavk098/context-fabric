# Claude Code hook spike (2026-10-21)

Throwaway test rig behind `docs/engineering/HOOK-SPIKE-2026-10-21.md`. Not product code.

1. `python spike_server.py 8765 all` (logs every event to `events.jsonl` next to it, applies the test rewrites).
2. Copy `settings.example.json` to `<scratch project>/.claude/settings.json` and fix the `ss_hook.py` path.
3. Run `claude -p "..."` in the scratch project; read `events.jsonl` and `ss_cmd.log`.
