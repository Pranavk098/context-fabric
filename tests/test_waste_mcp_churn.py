"""cfxd waste MCP churn rule: repeated deferred-tool/instruction delta attachments."""

from __future__ import annotations

import json
from pathlib import Path

from cfxd.cli import _mcp_churn_warning
from cfxd.waste import format_waste, waste


def _attachment(kind: str, added: list[str], blocks: list[str] | None = None) -> dict:
    att: dict[str, object] = {"type": kind, "addedNames": added}
    if blocks is not None:
        att["addedBlocks"] = blocks
    return {"type": "attachment", "attachment": att}


def _assistant() -> dict:
    return {
        "type": "assistant",
        "message": {"content": [], "usage": {"input_tokens": 1, "output_tokens": 0}},
    }


def _session(root: Path, session_id: str, lines: list[dict]) -> Path:
    path = root / "proj" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def test_churn_fires_on_synthetic_transcript(tmp_path: Path) -> None:
    lines = [_attachment("deferred_tools_delta", [f"mcp__srv__tool{i}"]) for i in range(27)] + [
        _attachment("mcp_instructions_delta", ["srv"], ["server instructions"]) for _ in range(26)
    ]
    lines.append(_assistant())
    _session(tmp_path, "churn", lines)

    report = waste(tmp_path, "all")
    churn = report["mcp_churn"]
    assert churn["sessions_with_churn"] == 1
    assert churn["deferred_attachments"] == 27
    assert churn["instruction_attachments"] == 26
    assert churn["attachments"] == 53
    assert churn["est_tokens"] > 0

    out = format_waste(report, "proj")
    assert "MCP churn:" in out
    assert "--strict-mcp-config" in out

    warning = _mcp_churn_warning(tmp_path, "all")
    assert warning is not None
    assert "--strict-mcp-config" in warning
    assert "53" in warning


def test_clean_transcript_is_silent(tmp_path: Path) -> None:
    lines = [
        _attachment("deferred_tools_delta", ["mcp__srv__tool"]),
        _attachment("mcp_instructions_delta", ["srv"], ["server instructions"]),
        _assistant(),
    ]
    _session(tmp_path, "clean", lines)

    report = waste(tmp_path, "all")
    assert report["mcp_churn"]["sessions_with_churn"] == 0
    assert report["mcp_churn"]["attachments"] == 2
    assert "MCP churn:" not in format_waste(report, "proj")
    assert _mcp_churn_warning(tmp_path, "all") is None


def test_second_delta_of_either_kind_fires(tmp_path: Path) -> None:
    _session(
        tmp_path,
        "deferred",
        [
            _attachment("deferred_tools_delta", ["mcp__srv__a"]),
            _attachment("deferred_tools_delta", ["mcp__srv__b"]),
            _assistant(),
        ],
    )
    _session(
        tmp_path,
        "instructions",
        [
            _attachment("mcp_instructions_delta", ["srv"], ["x"]),
            _attachment("mcp_instructions_delta", ["srv"], ["y"]),
            _assistant(),
        ],
    )
    assert waste(tmp_path, "all")["mcp_churn"]["sessions_with_churn"] == 2


def test_tokens_estimated_from_attachment_bytes(tmp_path: Path) -> None:
    first = _attachment("deferred_tools_delta", ["mcp__srv__a"])
    extra = _attachment("deferred_tools_delta", ["mcp__srv__tool"])
    _session(tmp_path, "one", [first, extra, _assistant()])
    expected_bytes = len(json.dumps(extra["attachment"]["addedNames"]).encode("utf-8"))
    assert waste(tmp_path, "all")["mcp_churn"]["est_tokens"] == round(expected_bytes / 4.0, 1)


def test_first_delta_of_each_kind_is_not_waste(tmp_path: Path) -> None:
    _session(
        tmp_path,
        "clean",
        [
            _attachment("deferred_tools_delta", ["mcp__srv__tool"]),
            _attachment("mcp_instructions_delta", ["srv"], ["server instructions"]),
            _assistant(),
        ],
    )
    assert waste(tmp_path, "all")["mcp_churn"]["est_tokens"] == 0.0
