"""cfxd tour: five ordered steps, counts-only output, and no blocking without a TTY."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

HEADINGS = [
    "1. Doctor",
    "2. Where your tokens go",
    "3. Long sessions",
    "4. Your starting context",
    "5. What to try next",
]

_CANARY = "CANARYtourname9f3"
_CANARY_PATH = f"/private/{_CANARY}/secret.txt"
_CANARY_TEXT = "SECRETSENTENCEtour9f3"


def _run_tour(*args: str, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cfxd", "tour", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )


def _write(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


def _session(root: Path, session_id: str, turns: int) -> Path:
    lines: list[dict] = [
        {
            "type": "attachment",
            "attachment": {
                "type": "skill_listing",
                "names": [f"{_CANARY}:skill"],
                "content": f"- {_CANARY}:skill: secret skill\n",
            },
        },
        {"type": "user", "message": {"content": [{"type": "text", "text": _CANARY_TEXT}]}},
    ]
    for turn in range(turns):
        lines.append(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"t{turn}",
                            "name": "Read",
                            "input": {"file_path": _CANARY_PATH},
                        }
                    ],
                    "usage": {"input_tokens": 100, "output_tokens": 10},
                },
            }
        )
        lines.append(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": f"t{turn}",
                            "content": _CANARY_TEXT,
                        }
                    ]
                },
            }
        )
    path = root / f"{_CANARY}-project" / f"{_CANARY}-{session_id}.jsonl"
    _write(path, lines)
    return path


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "transcripts"
    _session(root, "long", 31)
    _session(root, "short", 2)
    return root, tmp_path / "cfx-empty"


def test_five_steps_in_order(tmp_path: Path) -> None:
    root, state_dir = _fixture(tmp_path)
    result = _run_tour("--root", str(root), "--yes", "--state-dir", str(state_dir))
    assert result.returncode == 0, result.stderr
    found = [line for line in result.stdout.splitlines() if line in HEADINGS]
    assert found == HEADINGS
    assert "at most 30 turns: 1" in result.stdout
    assert "over 30 turns: 1" in result.stdout
    assert "cfxd diet --apply" in result.stdout
    assert "cfxd set bound on" in result.stdout
    assert "cfxd set coach on" in result.stdout
    assert "/context-fabric:checkpoint" in result.stdout
    assert "cfxd week --share" in result.stdout
    assert "docs/TESTING-GUIDE.md" in result.stdout
    assert "Nothing leaves your machine." in result.stdout


def test_output_has_no_names_or_paths(tmp_path: Path) -> None:
    root, state_dir = _fixture(tmp_path)
    result = _run_tour("--root", str(root), "--yes", "--state-dir", str(state_dir))
    assert result.returncode == 0, result.stderr
    combined = result.stdout + result.stderr
    assert _CANARY not in combined
    assert _CANARY_TEXT not in combined
    assert str(root) not in combined
    assert str(state_dir) not in combined


def test_no_transcripts_still_prints_all_steps(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    result = _run_tour("--root", str(empty), "--yes", "--state-dir", str(tmp_path / "cfx"))
    assert result.returncode == 0, result.stderr
    found = [line for line in result.stdout.splitlines() if line in HEADINGS]
    assert found == HEADINGS
    assert result.stdout.count("No transcripts found") == 3


def test_doctor_failure_still_prints_remaining_steps(tmp_path: Path) -> None:
    root, state_dir = _fixture(tmp_path)
    result = _run_tour("--root", str(root), "--state-dir", str(state_dir))
    assert result.returncode == 0, result.stderr
    found = [line for line in result.stdout.splitlines() if line in HEADINGS]
    assert found == HEADINGS
    assert "did not answer" in result.stdout
    assert "Fix hints:" in result.stdout
    assert "cfxd diet --apply" in result.stdout
