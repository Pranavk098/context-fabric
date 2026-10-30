"""Deterministic stand-in for ``claude`` for the v4 delegation benchmark (WP H2).

Like ``tests/fake_claude.py`` it spends nothing, but it plays a Sonnet parent that reads
files and delegates to a Haiku sub-agent. Environment switches set by the v4 runner:

- ``PLUGIN_AB_V4_REFERENCE``: directory of files copied over the workspace (the solution).
- ``PLUGIN_AB_V4_VARIANT``: optional ``patch.py`` patch applied after it (a solution that
  ignores the planted facts, as a child that never received them would write).
- ``PLUGIN_AB_V4_ARM``: arm label. ``brief_v0`` / ``brief_v1`` write a ``briefs.jsonl`` entry
  and put a packet in the child's first message (the real PreToolUse hook does not run here).
- ``PLUGIN_AB_V4_READS``: comma-separated workspace files the parent reads before delegating.
- ``PLUGIN_AB_V4_NOSPAWN=1``: the parent does the work itself (no sub-agent transcript).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))

from fake_claude import (  # noqa: E402
    MODEL,
    _parse,
    _project_dir,
    _session_id,
    _usage,
    _user_text,
    _write,
)

from evals.plugin_ab.patch import apply_patch  # noqa: E402

AGENT_ID = "a1b2c3d4"
TOOL_USE_ID = "toolu_FAKE_AGENT"


def _msg(mid: str, content: list[dict[str, object]], usage: dict[str, int]) -> dict[str, object]:
    return {
        "type": "assistant",
        "message": {
            "id": mid,
            "role": "assistant",
            "model": MODEL,
            "content": content,
            "usage": usage,
        },
    }


def _read_use(uid: str, path: str) -> dict[str, object]:
    return {"type": "tool_use", "id": uid, "name": "Read", "input": {"file_path": path}}


def _solve(cwd: str) -> None:
    reference = os.environ.get("PLUGIN_AB_V4_REFERENCE")
    if reference and Path(reference).is_dir():
        for source in sorted(Path(reference).rglob("*")):
            if source.is_file():
                target = Path(cwd) / source.relative_to(reference)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
    variant = os.environ.get("PLUGIN_AB_V4_VARIANT")
    if variant:
        apply_patch(Path(cwd), json.loads(Path(variant).read_text(encoding="utf-8")))


def main(argv: list[str] | None = None) -> int:
    args = _parse(sys.argv[1:] if argv is None else argv)
    session_id = _session_id(args)
    cwd = os.getcwd()
    arm = os.environ.get("PLUGIN_AB_V4_ARM", "none")
    reads = [
        Path(cwd, r).as_posix() for r in os.environ.get("PLUGIN_AB_V4_READS", "").split(",") if r
    ]
    spawn = os.environ.get("PLUGIN_AB_V4_NOSPAWN") != "1"
    project_dir = _project_dir(cwd)
    parent = project_dir / f"{session_id}.jsonl"
    prompt = str(args["prompt"] or "")

    _solve(cwd)
    _write(parent, _user_text(prompt))
    _write(
        parent,
        _msg("p1", [_read_use(f"r{i}", p) for i, p in enumerate(reads)], _usage(900, 80, 2000, 0)),
    )
    if not spawn:
        _write(parent, _msg("p2", [{"type": "text", "text": "done"}], _usage(400, 60, 0, 2500)))
        print(json.dumps({"type": "result", "subtype": "success", "result": "done"}))
        return 0

    _write(
        parent,
        _msg(
            "p2",
            [{"type": "tool_use", "id": TOOL_USE_ID, "name": "Agent", "input": {"prompt": "go"}}],
            _usage(500, 120, 0, 2600),
        ),
    )
    packet = ""
    if arm in ("brief_v0", "brief_v1") and reads:
        packet = (
            f'\n\n<context-fabric-brief v="{1 if arm == "brief_v1" else 0}">\n'
            "Files the parent session already opened (path | lines | status):\n"
            f"- {reads[0]} | 10 lines | read\n"
            "</context-fabric-brief>"
        )
        entry = {
            "ts": "2026-10-30T00:00:00+00:00",
            "session_id": session_id,
            "tool_use_id": TOOL_USE_ID,
            "subagent_type": "general-purpose",
            "decision": "briefed",
            "skip_reason": None,
            "prompt_bytes": 300,
            "packet_bytes": len(packet.encode("utf-8")),
            "counts": {"files": 1},
            "packet_sha16": hashlib.sha256(packet.strip().encode("utf-8")).hexdigest()[:16],
        }
        cfx = Path(os.environ["CFX_HOME"])
        with open(cfx / "briefs.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    sub = project_dir / session_id / "subagents" / f"agent-{AGENT_ID}.jsonl"
    sub.parent.mkdir(parents=True, exist_ok=True)

    def child(obj: dict[str, object]) -> None:
        obj["agentId"] = AGENT_ID
        obj["isSidechain"] = True
        _write(sub, obj)

    child({"type": "user", "message": {"role": "user", "content": "do the task" + packet}})
    if arm == "fork":  # a fork replays the parent history, then compacts under the weight of it
        child(_msg("p1", [_read_use("f0", reads[0])] if reads else [], _usage(900, 80, 2000, 0)))
        child({"type": "system", "subtype": "compact_boundary"})
    # Unbriefed children re-read what the parent read; a v0 pointer list is re-read anyway.
    child_reads = [] if arm == "brief_v1" else reads[:1]
    child(_msg("c1", [_read_use("k0", p) for p in child_reads], _usage(700, 90, 1500, 0)))
    child(_msg("c2", [{"type": "text", "text": "implemented"}], _usage(300, 50, 0, 1800)))

    _write(
        parent,
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": TOOL_USE_ID, "content": "ok"}],
            },
            "toolUseResult": {"status": "completed", "agentId": AGENT_ID},
        },
    )
    _write(parent, _msg("p3", [{"type": "text", "text": "done"}], _usage(400, 60, 0, 2700)))
    print(json.dumps({"type": "result", "subtype": "success", "result": "done"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
