"""ADR-0013: fold, bound and the checkpoint commands are retired from the tester surface."""

from __future__ import annotations

import http.client
import json
import re
from pathlib import Path

import pytest

from cfxd import __version__
from cfxd.cli import _RETIRED_DOC, _build_parser
from cfxd.cli import main as cli_main
from cfxd.config import read_config
from cfxd.daemon import OBS_PURGE_FILE, Daemon
from cfxd.handlers import clear
from cfxd.tour import _step_long, _step_next

ROOT = Path(__file__).resolve().parent.parent
RETIRED = ("fold", "fold_read", "bound")
NOTICE = f"is retired, see {_RETIRED_DOC}"


def _names(text: str) -> set[str]:
    return {name for name in (*RETIRED, "checkpoint") if re.search(rf"\b{name}\b", text)}


@pytest.fixture(autouse=True)
def _clean() -> None:
    clear()
    yield
    clear()


@pytest.mark.parametrize("flag", RETIRED)
def test_set_on_prints_the_notice_and_does_not_turn_it_on(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flag: str
) -> None:
    assert cli_main(["set", flag, "on", "--state-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert f"{flag} {NOTICE}" in out
    assert read_config(tmp_path)[flag] is False
    assert not (tmp_path / f"{flag}.on").exists()


def test_set_off_still_works(tmp_path: Path) -> None:
    assert cli_main(["set", "bound", "off", "--state-dir", str(tmp_path)]) == 0
    assert read_config(tmp_path)["bound"] is False


def test_old_config_with_retired_flags_reads_off(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text(
        '{"fold": true, "fold_read": true, "bound": true, "carry": true}', encoding="utf-8"
    )
    config = read_config(tmp_path)
    assert [config[name] for name in RETIRED] == [False, False, False]
    assert config["carry"] is True


@pytest.mark.parametrize("action", ("save", "show", "clear", "import"))
def test_checkpoint_actions_print_the_notice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], action: str
) -> None:
    state = tmp_path / "cfx"
    assert cli_main(["checkpoint", action, "--state-dir", str(state)]) == 0
    assert f"checkpoint {NOTICE}" in capsys.readouterr().out
    assert not state.exists()


def test_checkpoint_command_doc_is_a_retired_notice() -> None:
    lines = (ROOT / "commands" / "checkpoint.md").read_text(encoding="utf-8").splitlines()
    assert len([line for line in lines if line.strip()]) == 1
    assert "retired" in lines[0]
    assert _RETIRED_DOC in lines[0]


def test_daemon_purges_obs_once_and_never_writes_new_blobs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "cfx"
    obs = state / "obs"
    obs.mkdir(parents=True)
    (obs / "old.txt").write_text("full tool output", encoding="utf-8")
    (state / "config.json").write_text('{"fold": true, "fold_read": true}', encoding="utf-8")

    daemon = Daemon(state, 0)
    daemon.start()
    try:
        assert not (obs / "old.txt").exists()
        assert (state / OBS_PURGE_FILE).read_text(encoding="utf-8") == __version__

        content = "def a():\n" + ("# pad\n" * 20_000)
        payload = json.dumps(
            {
                "tool_name": "Read",
                "tool_input": {"file_path": "mod.py"},
                "tool_response": {"type": "text", "file": {"content": content}},
                "session_id": "s1",
            }
        ).encode("utf-8")
        conn = http.client.HTTPConnection("127.0.0.1", daemon.port, timeout=5)
        conn.request(
            "POST",
            "/hooks/PostToolUse",
            body=payload,
            headers={"Content-Type": "application/json", "X-Cfx-Token": daemon.token},
        )
        resp = conn.getresponse()
        body = json.loads(resp.read())
        conn.close()
        assert resp.status == 200
        assert body == {}
        assert not any(obs.glob("*"))
    finally:
        daemon.stop()
    # A second start of the same version is a no-op, and the receipt stays counts-only.
    daemon2 = Daemon(state, 0)
    daemon2.start()
    try:
        events = (state / "events.jsonl").read_text(encoding="utf-8")
        assert events.count('"event": "obs_purged"') == 1
        assert "old.txt" not in events
    finally:
        daemon2.stop()


def test_top_level_help_names_none_of_them() -> None:
    help_text = _build_parser().format_help()
    assert _names(help_text) == set(), _names(help_text)


def test_tour_names_none_of_them() -> None:
    report = {"sessions": 1, "turns": {"sessions_over_30": 0, "median": 2.0, "p90": 3.0}}
    text = "\n".join([*_step_long(report), *_step_next()])
    assert _names(text) == set(), _names(text)
