"""PostToolUse fold handler: shrink large tool outputs to a header + kept excerpt.

The full output is saved, content-addressed, to ``<state_dir>/obs/``; the model
gets a self-explaining header plus a small deterministic excerpt and can use its
normal ``Read``/``Grep`` tools on the saved file to see more. Nothing is lost.

Stdlib only (this module runs under ``uv run --no-project``): no pydantic,
tiktoken, or other third-party imports.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cfxd import session_state
from cfxd.config import read_config
from cfxd.fsutil import ensure_dir, open_log, rotate
from cfxd.handlers import register

FOLD_MIN_BYTES = 20_000
EXCERPT_MAX_BYTES = 4_000
OBS_DIR = "obs"
FOLDS_LOG = "folds.jsonl"
FOLDS_ROTATE_BYTES = 5 * 1024 * 1024
_SAVE_TTL_SECONDS = 7 * 24 * 3600

_FOLD_TOOLS = ("Read", "Bash", "Grep", "WebFetch")

_CODE_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".c",
    ".h",
    ".cpp",
    ".cs",
    ".rb",
    ".php",
    ".swift",
    ".scala",
}

_DEF_KEYWORDS = (
    "async def ",
    "pub fn ",
    "function ",
    "interface ",
    "class ",
    "struct ",
    "impl ",
    "enum ",
    "export ",
    "func ",
    "fn ",
    "def ",
    "type ",
    "public ",
    "private ",
    "protected ",
)

_WHAT = {
    "test": "failing tests, error lines and the last 15 lines",
    "diff": "diff headers and changed lines (unchanged context dropped)",
    "code": "an outline of definitions with line numbers",
    "other": "the first 40 and last 40 lines",
}

_fold_log_lock = threading.Lock()


def handle_post_tool_use(payload: dict[str, Any], state_dir: Path) -> dict[str, Any]:
    """Fold a large ``PostToolUse`` output; ``{}`` means "leave it alone" (never raise)."""
    if not isinstance(payload, dict):
        return {}
    tool_name = payload.get("tool_name")
    if tool_name not in _FOLD_TOOLS:
        return {}
    response = payload.get("tool_response")
    if not isinstance(response, dict):
        return {}
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    session_id = payload.get("session_id")
    if not isinstance(session_id, str):
        session_id = None

    source = _fold_source(tool_name, tool_input)

    updated = copy.deepcopy(response)
    folded = False

    if tool_name == "Read":
        if not _fold_read_enabled(state_dir):
            return {}
        if response.get("type") != "text":
            return {}
        file = response.get("file")
        if not isinstance(file, dict):
            return {}
        content = file.get("content")
        if not isinstance(content, str):
            return {}
        if "offset" in tool_input or "limit" in tool_input:
            return {}
        if _path_in_obs(tool_input.get("file_path"), state_dir):
            return {}
        replacement = _fold(
            content,
            tool_name,
            "file.content",
            session_id,
            state_dir,
            _code_ext(tool_input),
            _read_range_note(file),
            source,
        )
        if replacement is None:
            return {}
        updated["file"]["content"] = replacement
        folded = True
    elif tool_name == "Bash":
        for field in ("stdout", "stderr"):
            content = response.get(field)
            if not isinstance(content, str):
                continue
            replacement = _fold(content, tool_name, field, session_id, state_dir, None, "", source)
            if replacement is not None:
                updated[field] = replacement
                folded = True
    elif tool_name == "Grep":
        content = response.get("content")
        if not isinstance(content, str) or not content:
            return {}
        if _path_in_obs(tool_input.get("path"), state_dir):
            return {}
        replacement = _fold(content, tool_name, "content", session_id, state_dir, None, "", source)
        if replacement is None:
            return {}
        updated["content"] = replacement
        folded = True
    elif tool_name == "WebFetch":
        content = response.get("result")
        if not isinstance(content, str):
            return {}
        replacement = _fold(content, tool_name, "result", session_id, state_dir, None, "", source)
        if replacement is None:
            return {}
        updated["result"] = replacement
        folded = True

    if not folded:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "updatedToolOutput": updated,
        }
    }


def _fold_read_enabled(state_dir: Path) -> bool:
    return bool(read_config(state_dir).get("fold_read", False))


def install(state_dir: Path) -> None:
    """Register the ``PostToolUse`` handler and prune old saved outputs (best effort)."""
    register("PostToolUse", lambda payload: handle_post_tool_use(payload, state_dir))
    _prune(state_dir)


def _fold(
    content: str,
    tool_name: str,
    field: str,
    session_id: str | None,
    state_dir: Path,
    code_ext: str | None,
    note: str = "",
    source: str = "",
) -> str | None:
    """Fold one string field; return the replacement text, or ``None`` to skip folding."""
    content_bytes = content.encode("utf-8")
    if len(content_bytes) < FOLD_MIN_BYTES:
        return None
    lines = content.split("\n")
    obs_id = hashlib.sha256(content_bytes).hexdigest()[:16]
    obs_path = state_dir / OBS_DIR / f"{obs_id}.txt"

    kind = "code" if code_ext and _code_outline(lines) else _classify(content)
    replacement = _replacement(tool_name, content_bytes, lines, kind, obs_path, note)
    if replacement is None:
        replacement = _replacement(tool_name, content_bytes, lines, "other", obs_path, note)
        if replacement is None:
            return None
        kind = "other"

    _save(obs_path, content)
    _log_fold(
        state_dir,
        session_id,
        tool_name,
        field,
        kind,
        len(content_bytes),
        len(replacement.encode("utf-8")),
        obs_id,
    )
    try:
        session_state.record_fold(
            session_id,
            tool_name,
            source,
            obs_id,
            obs_path.resolve().as_posix(),
            len(content_bytes),
            kind,
        )
    except Exception:
        pass
    return replacement


def _fold_source(tool_name: str, tool_input: dict[str, Any]) -> str:
    """The fact source for a fold: file path, redacted command, bare URL, or ``Grep``."""
    if tool_name == "Bash":
        command = tool_input.get("command")
        return session_state.redact_command(command) if isinstance(command, str) else ""
    if tool_name == "WebFetch":
        url = tool_input.get("url")
        if isinstance(url, str):
            return url.split("?", 1)[0].split("#", 1)[0]
        return ""
    if tool_name == "Read":
        file_path = tool_input.get("file_path")
        return file_path if isinstance(file_path, str) else ""
    return "Grep"


def _replacement(
    tool_name: str,
    content_bytes: bytes,
    lines: list[str],
    kind: str,
    obs_path: Path,
    note: str = "",
) -> str | None:
    excerpt = _cap_excerpt(_excerpt(kind, lines))
    header = (
        f"[Context Fabric folded this {tool_name} output to save context: "
        f"{len(content_bytes):,} bytes, {len(lines):,} lines{note}. Shown below: {_WHAT[kind]}.\n"
        f"Full original, unchanged: {obs_path.resolve().as_posix()}\n"
        "To see more, Grep that path for what you need, or Read it with offset/limit for a "
        "range; reading all of it costs its full size. Nothing was deleted.]"
    )
    replacement = header + "\n\n" + excerpt
    if len(replacement.encode("utf-8")) * 2 <= len(content_bytes):
        return replacement
    return None


def _classify(content: str) -> str:
    """Classify as ``diff`` / ``test`` / ``other`` (the ``code`` kind is chosen elsewhere)."""
    lines = content.split("\n")
    has_hunk = any(line.startswith("@@") for line in lines)
    has_change = any(line[:1] in "+-" and not line.startswith(("+++", "---")) for line in lines)
    if has_hunk and has_change:
        return "diff"
    if any(
        marker in content
        for marker in (
            "= FAILURES =",
            "= ERRORS =",
            "short test summary info",
            "Traceback (most recent call last)",
        )
    ) or any(_is_test_line(line, line.strip()) for line in lines):
        return "test"
    return "other"


def _excerpt(kind: str, lines: list[str]) -> str:
    if kind == "test":
        kept = _test_kept_indices(lines)
        return "\n".join(line for i, line in enumerate(lines) if i in kept)
    if kind == "diff":
        return "\n".join(line for line in lines if _diff_keep(line))
    if kind == "code":
        return _code_outline(lines)
    return _other_excerpt(lines)


def _is_test_line(line: str, stripped: str) -> bool:
    return (
        "FAILED" in line
        or "ERROR" in line
        or stripped.startswith("E ")
        or "AssertionError" in line
        or "Error:" in line
        or _failure_header(stripped)
    )


def _failure_header(stripped: str) -> bool:
    """Pytest ``____ test_name ____`` failure-section header."""
    return (
        stripped.startswith("_")
        and stripped.endswith("_")
        and "test" in stripped
        and len(stripped) >= 8
    )


def _test_kept_indices(lines: list[str]) -> set[int]:
    kept: set[int] = set()
    for i, line in enumerate(lines):
        if _is_test_line(line, line.strip()):
            kept.add(i)
    kept.update(range(max(0, len(lines) - 15), len(lines)))
    return kept


def _diff_keep(line: str) -> bool:
    return line.startswith(("@@", "diff --git", "---", "+++")) or line[:1] in "+-"


def _code_outline(lines: list[str]) -> str:
    outline: list[str] = []
    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith(_DEF_KEYWORDS):
            outline.append(f"L{i}: {stripped}")
    return "\n".join(outline)


def _other_excerpt(lines: list[str]) -> str:
    total = len(lines)
    head = lines[:40]
    tail = lines[-40:] if total > 40 else []
    n_folded = max(0, total - len(head) - len(tail))
    return "\n".join([*head, f"… {n_folded} lines folded …", *tail])


def _cap_excerpt(excerpt: str) -> str:
    if len(excerpt.encode("utf-8")) <= EXCERPT_MAX_BYTES:
        return excerpt
    marker = "… (excerpt truncated)"
    budget = EXCERPT_MAX_BYTES - len(marker.encode("utf-8"))
    kept_lines: list[str] = []
    size = 0
    for line in excerpt.split("\n"):
        add = len(line.encode("utf-8")) + (1 if kept_lines else 0)
        if size + add > budget:
            break
        kept_lines.append(line)
        size += add
    kept = "\n".join(kept_lines)
    return kept + "\n" + marker if kept else marker


def _read_range_note(file: dict[str, Any]) -> str:
    """Say which lines a partial Read covered, so the header isn't taken for the whole file."""
    start, count, total = file.get("startLine"), file.get("numLines"), file.get("totalLines")
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (start, count, total)):
        return ""
    assert isinstance(start, int) and isinstance(count, int) and isinstance(total, int)
    if count <= 0 or count >= total:
        return ""
    return f" (this Read returned lines {start:,}-{start + count - 1:,} of {total:,} in the file)"


def _code_ext(tool_input: dict[str, Any]) -> str | None:
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str):
        return None
    suffix = Path(file_path).suffix.lower()
    return suffix if suffix in _CODE_EXTENSIONS else None


def _path_in_obs(path_value: Any, state_dir: Path) -> bool:
    """True when ``path_value`` resolves inside the saved-output dir (Windows: case-insensitive)."""
    if not isinstance(path_value, str):
        return False
    try:
        candidate = Path(path_value).resolve()
        obs_dir = (state_dir / OBS_DIR).resolve()
    except OSError:
        return False
    if os.name == "nt":
        candidate = Path(str(candidate).lower())
        obs_dir = Path(str(obs_dir).lower())
    try:
        candidate.relative_to(obs_dir)
        return True
    except ValueError:
        return False


def _save(path: Path, content: str) -> None:
    """Atomically write ``content`` byte-exact (content-addressed, so idempotent)."""
    if path.exists():
        # Refresh its age so pruning doesn't delete a file a new fold points to.
        os.utime(path)
        return
    ensure_dir(path.parent)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".obs.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _log_fold(
    state_dir: Path,
    session_id: str | None,
    tool_name: str,
    field: str,
    kind: str,
    bytes_in: int,
    bytes_out: int,
    obs_id: str,
) -> None:
    entry = {
        "ts": datetime.now(UTC).isoformat(),
        "session_id": session_id,
        "tool_name": tool_name,
        "field": field,
        "kind": kind,
        "bytes_in": bytes_in,
        "bytes_out": bytes_out,
        "obs_id": obs_id,
    }
    with _fold_log_lock:
        rotate(state_dir / FOLDS_LOG, FOLDS_ROTATE_BYTES)
        with open_log(state_dir / FOLDS_LOG) as fh:
            fh.write(json.dumps(entry) + "\n")


def _prune(state_dir: Path) -> None:
    """Delete saved outputs older than 7 days; best effort, never raises."""
    obs_dir = state_dir / OBS_DIR
    if not obs_dir.is_dir():
        return
    cutoff = time.time() - _SAVE_TTL_SECONDS
    for path in obs_dir.iterdir():
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue
