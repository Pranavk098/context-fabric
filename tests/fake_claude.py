"""Deterministic stand-in for ``claude`` used by the plugin A/B tests.

The runner calls this instead of the real ``claude`` binary when the
``PLUGIN_AB_CLAUDE`` env var points at it. It writes a realistic Claude Code
transcript (parent plus, for sub-agent tasks, a sub-agent file) under
``CLAUDE_CONFIG_DIR/projects`` and, in the solving step, applies the task's
reference patch (handed over in ``PLUGIN_AB_SOLUTION``) so the run resolves the
oracle. It spends nothing and never calls a model.

Run directly (no harness on the path): the script inserts the repo root so it
can reuse the harness's patch applier.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from evals.plugin_ab.patch import apply_patch  # noqa: E402

MODEL = "claude-haiku-4-5-20251001"


def _parse(argv: list[str]) -> dict[str, str | None]:
    out: dict[str, str | None] = {"prompt": "", "session_id": None, "resume": None}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-p", "--prompt") and i + 1 < len(argv):
            out["prompt"] = argv[i + 1]
            i += 2
            continue
        if a in ("--session-id", "--resume") and i + 1 < len(argv):
            out[a[2:].replace("-", "_")] = argv[i + 1]
            i += 2
            continue
        i += 1
    return out


def _slug(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", os.path.abspath(cwd)).strip("-") or "project"


def _config_dir() -> Path:
    value = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(value) if value else Path.home() / ".claude"


def _projects_root() -> Path:
    return _config_dir() / "projects"


def _session_id(args: dict[str, str | None]) -> str:
    return str(args["session_id"] or args["resume"] or "fake-session")


def _project_dir(cwd: str) -> Path:
    d = _projects_root() / _slug(cwd)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _usage(input_tokens: int, output_tokens: int, cc: int, cr: int) -> dict[str, int]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": cc,
        "cache_read_input_tokens": cr,
    }


def _assistant(text: str, usage: dict[str, int]) -> dict[str, object]:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}], "usage": usage, "model": MODEL},
    }


def _user_text(text: str) -> dict[str, object]:
    return {"type": "user", "message": {"content": [{"type": "text", "text": text}]}}


def _write(path: Path, obj: dict[str, object]) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj) + "\n")


def _apply_solution(cwd: str) -> None:
    solution = os.environ.get("PLUGIN_AB_SOLUTION")
    if not solution:
        return
    patch = json.loads(Path(solution).read_text(encoding="utf-8"))
    apply_patch(Path(cwd), patch)


def _write_answers(cwd: str) -> None:
    source = os.environ.get("PLUGIN_AB_FAKE_ANSWERS")
    if not source:
        return
    Path(cwd, "answers.json").write_text(Path(source).read_text(encoding="utf-8"), encoding="utf-8")


def _write_subagent(project_dir: Path, session_id: str) -> None:
    sub_dir = project_dir / session_id / "subagents"
    sub_dir.mkdir(parents=True, exist_ok=True)
    path = sub_dir / f"{session_id}-sub.jsonl"
    _write(path, _assistant("reading", _usage(500, 40, 900, 0)))
    _write(path, _assistant("done", _usage(200, 30, 0, 100)))


def main(argv: list[str] | None = None) -> int:
    args = _parse(sys.argv[1:] if argv is None else argv)
    step = os.environ.get("PLUGIN_AB_FAKE_STEP", "run")
    session_id = _session_id(args)
    if os.environ.get("PLUGIN_AB_FAKE_ERROR") == "1":
        # what the real claude prints (and exits 1 with) when it cannot run, e.g. not logged in
        print(json.dumps({"type": "result", "is_error": True, "result": "Not logged in"}))
        return 1
    cwd = os.getcwd()
    project_dir = _project_dir(cwd)
    parent = project_dir / f"{session_id}.jsonl"

    if step in ("run", "implement"):
        _apply_solution(cwd)
        _write_answers(cwd)

    prompt = str(args["prompt"] or "")
    _write(parent, _user_text(prompt))
    if step == "run":
        _write(parent, _assistant("reading files", _usage(400, 40, 800, 0)))
        _write(parent, _user_text("<tool_result>"))
        if os.environ.get("PLUGIN_AB_FAKE_SUBAGENT") == "1":
            _write(parent, _assistant("delegating", _usage(600, 60, 1200, 100)))
            _write(parent, _user_text("<tool_result>"))
            _write_subagent(project_dir, session_id)
        text = os.environ.get("PLUGIN_AB_FAKE_TEXT") or "done"
        _write(parent, _assistant(text, _usage(300, 30, 0, 200)))
    else:
        _write(parent, _assistant(f"step {step}", _usage(300, 25, 500, 100)))

    _write(parent, {"type": "result", "subtype": "success", "session_id": session_id})
    print(json.dumps({"type": "result", "subtype": "success", "result": "done"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
