import json
import pathlib
import sys

p = json.load(sys.stdin)
pathlib.Path(__file__).with_name("ss_cmd.log").open("a").write(
    json.dumps(
        {"source": p.get("source"), "event": p.get("hook_event_name"), "trigger": p.get("trigger")}
    )
    + "\n"
)
ev = p.get("hook_event_name")
if ev == "SessionStart":
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": "Context Fabric carry: the session codeword is MANGO-7.",
                }
            }
        )
    )
