"""cfxd coach tests: thresholds, message text, tail read, settings, receipts, fail-open."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from cfxd import coach
from cfxd.cli import main as cli_main
from cfxd.config import read_config, set_flag
from cfxd.handlers import clear
from cfxd.stats import collect_stats, format_human

ROOT = Path(__file__).resolve().parents[1]
LAUNCH_SH = ROOT / "cfxd" / "launch.sh"
HOOKS_JSON = ROOT / "hooks" / "hooks.json"


def _find_sh() -> str | None:
    found = shutil.which("sh")
    if found:
        return found
    if os.name == "nt":
        for candidate in (
            r"C:\Program Files\Git\bin\sh.exe",
            r"C:\Program Files\Git\usr\bin\sh.exe",
        ):
            if Path(candidate).exists():
                return candidate
    return None


SH = _find_sh()

EXACT_182K_OPUS = (
    "Context Fabric: this session is at ~182K tokens; each turn now re-reads it "
    "(about $0.04 at Opus 5.5 cache-read prices, API-equivalent). If this is a natural "
    "break: /context-fabric:checkpoint, then /clear. Turn this off: cfxd set coach off"
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Any:
    clear()
    coach._STATE.clear()
    monkeypatch.delenv("CFX_COACH", raising=False)
    yield
    clear()
    coach._STATE.clear()


def _on(tmp_path: Path) -> None:
    set_flag(tmp_path, "coach", True)


def _usage(context: int, model: str = "claude-opus-5-5") -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "model": model,
                "usage": {
                    "input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": context,
                },
            },
        }
    )


def _transcript(tmp_path: Path, context: int, model: str = "claude-opus-5-5") -> Path:
    path = tmp_path / "session.jsonl"
    path.write_text(_usage(context, model) + "\n", encoding="utf-8")
    return path


def _payload(path: Path, session: str = "s1") -> dict[str, Any]:
    return {"session_id": session, "transcript_path": str(path)}


def _receipts(tmp_path: Path) -> list[dict[str, Any]]:
    text = (tmp_path / coach.COACH_LOG).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def test_context_threshold_is_exact(tmp_path: Path) -> None:
    _on(tmp_path)
    transcript = _transcript(tmp_path, 149_999)
    for _ in range(30):
        assert coach.handle_stop(_payload(transcript), tmp_path) == {}
    assert {r["outcome"] for r in _receipts(tmp_path)} == {"below_threshold"}


def test_turn_threshold_is_exact(tmp_path: Path) -> None:
    _on(tmp_path)
    transcript = _transcript(tmp_path, 150_000)
    for _ in range(29):
        assert coach.handle_stop(_payload(transcript), tmp_path) == {}
    assert coach.handle_stop(_payload(transcript), tmp_path) != {}


def test_message_is_exact(tmp_path: Path) -> None:
    _on(tmp_path)
    transcript = _transcript(tmp_path, 182_345)
    for _ in range(29):
        coach.handle_stop(_payload(transcript), tmp_path)
    assert coach.handle_stop(_payload(transcript), tmp_path) == {"systemMessage": EXACT_182K_OPUS}


def test_second_nudge_needs_100k_growth(tmp_path: Path) -> None:
    _on(tmp_path)
    transcript = _transcript(tmp_path, 150_000)
    for _ in range(30):
        last = coach.handle_stop(_payload(transcript), tmp_path)
    assert last != {}
    _transcript(tmp_path, 249_999)
    for _ in range(10):
        assert coach.handle_stop(_payload(transcript), tmp_path) == {}
    assert _receipts(tmp_path)[-1]["outcome"] == "suppressed_recent"
    _transcript(tmp_path, 250_000)
    assert coach.handle_stop(_payload(transcript), tmp_path) != {}


def test_ten_turn_cooldown(tmp_path: Path) -> None:
    _on(tmp_path)
    transcript = _transcript(tmp_path, 150_000)
    for _ in range(30):
        coach.handle_stop(_payload(transcript), tmp_path)
    _transcript(tmp_path, 400_000)
    for _ in range(9):
        assert coach.handle_stop(_payload(transcript), tmp_path) == {}
    assert _receipts(tmp_path)[-1]["outcome"] == "suppressed_recent"
    assert coach.handle_stop(_payload(transcript), tmp_path) != {}


def test_unknown_model_omits_dollars(tmp_path: Path) -> None:
    assert coach.nudge_text(182_345, "claude-made-up-9") == (
        "Context Fabric: this session is at ~182K tokens; each turn now re-reads it. "
        "If this is a natural break: /context-fabric:checkpoint, then /clear. "
        "Turn this off: cfxd set coach off"
    )
    _on(tmp_path)
    transcript = _transcript(tmp_path, 150_000, model="claude-made-up-9")
    for _ in range(30):
        last = coach.handle_stop(_payload(transcript), tmp_path)
    assert last != {}
    assert "$" not in last["systemMessage"]


def test_tail_only_read_of_large_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = tmp_path / "big.jsonl"
    with open(transcript, "w", encoding="utf-8") as fh:
        fh.write(_usage(999) + "\n")
        for _ in range(20_000):
            fh.write(json.dumps({"type": "user", "message": {"content": "x" * 200}}) + "\n")
        fh.write(_usage(150_000) + "\n")
    assert transcript.stat().st_size > 2 * coach.TAIL_BYTES

    read_total = 0
    real_open = open

    class Recorder:
        def __init__(self, fh: Any) -> None:
            self._fh = fh

        def __enter__(self) -> Recorder:
            self._fh.__enter__()
            return self

        def __exit__(self, *exc: Any) -> Any:
            return self._fh.__exit__(*exc)

        def seek(self, *args: Any) -> int:
            return self._fh.seek(*args)

        def tell(self) -> int:
            return self._fh.tell()

        def read(self, size: int = -1) -> bytes:
            nonlocal read_total
            data = self._fh.read(size)
            read_total += len(data)
            return data

    def fake_open(*args: Any, **kwargs: Any) -> Recorder:
        return Recorder(real_open(*args, **kwargs))

    monkeypatch.setattr(coach, "open", fake_open, raising=False)
    assert coach._tail_usage(transcript) == (150_000, "claude-opus-5-5")
    assert read_total <= coach.TAIL_BYTES


def test_receipt_is_content_free(tmp_path: Path) -> None:
    _on(tmp_path)
    canary = "CANARY-1a2b3c4d5e6f-NEVER-STORE"
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "message": {"content": canary}})
        + "\n"
        + _usage(150_000)
        + "\n",
        encoding="utf-8",
    )
    for _ in range(30):
        coach.handle_stop(_payload(transcript), tmp_path)
    text = (tmp_path / coach.COACH_LOG).read_text(encoding="utf-8")
    assert canary not in text
    assert str(transcript) not in text
    receipt = _receipts(tmp_path)[-1]
    assert receipt["event"] == "coach_nudge"
    assert receipt["context_tokens"] == 150_000
    assert receipt["turns"] == 30
    assert receipt["model"] == "claude-opus-5-5"
    assert receipt["outcome"] == "shown"
    assert receipt["session_id"] == "s1"


def test_setting_off_and_env_opt_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    transcript = _transcript(tmp_path, 150_000)
    for _ in range(30):
        assert coach.handle_stop(_payload(transcript), tmp_path) == {}
    assert not (tmp_path / coach.COACH_LOG).exists()

    _on(tmp_path)
    monkeypatch.setenv("CFX_COACH", "0")
    for _ in range(30):
        assert coach.handle_stop(_payload(transcript), tmp_path) == {}
    assert coach._STATE == {}


def test_malformed_payload_fails_open(tmp_path: Path) -> None:
    _on(tmp_path)
    bad_file = tmp_path / "bad.jsonl"
    bad_file.write_text("not json\n", encoding="utf-8")
    payloads: list[Any] = [
        {},
        {"session_id": "s1"},
        {"session_id": None, "transcript_path": str(bad_file)},
        {"session_id": "s1", "transcript_path": None},
        {"session_id": "s1", "transcript_path": 42},
        {"session_id": "s1", "transcript_path": str(tmp_path / "missing.jsonl")},
        {"session_id": "s1", "transcript_path": str(bad_file)},
        {"session_id": "s1", "transcript_path": str(tmp_path)},
    ]
    for payload in payloads:
        assert coach.handle_stop(payload, tmp_path) == {}


def test_set_coach_creates_and_removes_flag(tmp_path: Path) -> None:
    assert cli_main(["set", "coach", "on", "--state-dir", str(tmp_path)]) == 0
    assert (tmp_path / "coach.on").exists()
    assert read_config(tmp_path)["coach"] is True
    assert cli_main(["set", "coach", "off", "--state-dir", str(tmp_path)]) == 0
    assert not (tmp_path / "coach.on").exists()
    assert read_config(tmp_path)["coach"] is False


def test_stats_counts_nudges(tmp_path: Path) -> None:
    _on(tmp_path)
    transcript = _transcript(tmp_path, 150_000)
    for _ in range(30):
        coach.handle_stop(_payload(transcript), tmp_path)
    stats = collect_stats(tmp_path)
    assert stats["nudges"] == 1
    assert "Nudges:" in format_human(stats)


def test_hooks_json_registers_stop() -> None:
    hooks = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))["hooks"]
    entry = hooks["Stop"][0]["hooks"][0]
    assert entry["command"].endswith("forward Stop")
    assert entry["timeout"] == 2


@pytest.mark.skipif(SH is None, reason="sh is not available")
def test_launch_fast_exit_when_flag_absent(tmp_path: Path) -> None:
    state_dir = tmp_path / "cfx"
    state_dir.mkdir()
    payload = json.dumps({"session_id": "s1", "transcript_path": "x.jsonl"})
    proc = subprocess.run(
        [str(SH), str(LAUNCH_SH), "forward", "Stop"],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env={**os.environ, "CFX_HOME": str(state_dir), "CFX_COACH": ""},
        timeout=30,
    )
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert not (state_dir / "state.json").exists()
