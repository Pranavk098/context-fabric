"""Hook spike server: logs every Claude Code http-hook event and applies test rewrites."""

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOG = Path(__file__).with_name("events.jsonl")
MODE = sys.argv[2] if len(sys.argv) > 2 else "all"

REWRITE_PREFIX = (
    "IMPORTANT INSTRUCTION FROM THE ORCHESTRATOR: the first line of your final answer must be "
    "exactly CFX-REWRITE-OK.\n\n"
)


def respond(event: str, p: dict) -> dict:
    tool = p.get("tool_name")
    if event == "PreToolUse" and tool == "Agent" and MODE in ("all", "rewrite"):
        new_input = dict(p.get("tool_input") or {})
        new_input["prompt"] = REWRITE_PREFIX + str(new_input.get("prompt", ""))
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": new_input,
            }
        }
    if event == "SubagentStart" and MODE in ("all", "inject"):
        return {
            "hookSpecificOutput": {
                "hookEventName": "SubagentStart",
                "additionalContext": "Context Fabric brief: the project codeword is PAPAYA-42.",
            }
        }
    if event == "SessionStart":
        return {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "Context Fabric carry: the session codeword is MANGO-7.",
            }
        }
    if event == "PostToolUse" and tool == "Read" and MODE in ("all", "fold"):
        resp = p.get("tool_response")
        if isinstance(resp, dict) and isinstance(resp.get("file"), dict):
            folded = json.loads(json.dumps(resp))
            folded["file"]["content"] = (
                "[cfx fold] ctx://obs/spike-1 (original omitted; FOLD-STUB-OK)"
            )
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "updatedToolOutput": folded,
                }
            }
    return {}


class H(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        t0 = time.perf_counter()
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        event = self.path.rsplit("/", 1)[-1]
        try:
            p = json.loads(body)
        except Exception:
            p = {}
        out = respond(event, p)
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "event": event,
                        "payload": p,
                        "response": out,
                        "ms": round((time.perf_counter() - t0) * 1000, 2),
                    }
                )
                + "\n"
            )

    def log_message(self, *a: object) -> None:
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
