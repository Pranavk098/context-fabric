"""cfxd/launch.sh: finds uv outside PATH, warns once when it is missing, never fails."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "cfxd" / "launch.sh"
SH = shutil.which("sh")

pytestmark = pytest.mark.skipif(SH is None, reason="needs a POSIX sh (Git Bash on Windows)")


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    assert SH is not None
    return subprocess.run(
        [SH, str(SCRIPT), *args],
        input="{}",
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def _bare_env(tmp_path: Path, extra_dirs: list[Path] | None = None) -> dict[str, str]:
    """An environment where uv is not on PATH and home dirs are empty."""
    assert SH is not None
    tools = {Path(SH).parent, Path(shutil.which("dirname") or SH).parent}
    path = os.pathsep.join(str(p) for p in [*tools, *(extra_dirs or [])])
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return {
        "PATH": path,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "LOCALAPPDATA": str(home / "local"),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
    }


def _uv_in_fixed_dirs() -> bool:
    return any(
        Path(d, name).exists()
        for d in ("/opt/homebrew/bin", "/usr/local/bin")
        for name in ("uv", "uv.exe")
    )


@pytest.mark.skipif(
    _uv_in_fixed_dirs(), reason="a real uv sits in a dir the script always searches"
)
def test_missing_uv_session_start_warns_visibly_and_exits_zero(tmp_path: Path) -> None:
    proc = _run(["session-start"], _bare_env(tmp_path))
    assert proc.returncode == 0
    message = json.loads(proc.stdout)["systemMessage"]
    assert "uv" in message
    assert "docs.astral.sh/uv" in message


@pytest.mark.skipif(
    _uv_in_fixed_dirs(), reason="a real uv sits in a dir the script always searches"
)
def test_missing_uv_other_events_stay_silent(tmp_path: Path) -> None:
    proc = _run(["forward", "PreCompact"], _bare_env(tmp_path))
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {}


def test_finds_uv_in_home_local_bin_when_path_lacks_it(tmp_path: Path) -> None:
    env = _bare_env(tmp_path)
    bindir = Path(env["HOME"]) / ".local" / "bin"
    bindir.mkdir(parents=True)
    fake = bindir / "uv"
    fake.write_text(
        '#!/bin/sh\nprintf \'{"fake_uv": "%s"}\' "$1"\n', encoding="utf-8", newline="\n"
    )
    fake.chmod(0o755)
    proc = _run(["session-start"], env)
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {"fake_uv": "run"}


def test_broken_uv_still_exits_zero(tmp_path: Path) -> None:
    env = _bare_env(tmp_path)
    bindir = Path(env["HOME"]) / ".local" / "bin"
    bindir.mkdir(parents=True)
    fake = bindir / "uv"
    fake.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8", newline="\n")
    fake.chmod(0o755)
    assert _run(["session-start"], env).returncode == 0


@pytest.mark.skipif(
    _uv_in_fixed_dirs(), reason="a real uv sits in a dir the script always searches"
)
def test_empty_path_does_not_search_cwd(tmp_path: Path) -> None:
    # With an empty PATH the script must not produce an empty element (which POSIX
    # treats as the cwd) and so must not pick up a uv sitting in the project repo.
    assert SH is not None
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    fake = cwd / "uv"
    fake.write_text('#!/bin/sh\nprintf "CWD-UV-FOUND"\n', encoding="utf-8", newline="\n")
    fake.chmod(0o755)

    home = tmp_path / "home"
    bindir = home / ".local" / "bin"
    bindir.mkdir(parents=True)

    env = {
        "PATH": "",
        "HOME": str(home),
        "USERPROFILE": str(home),
        "LOCALAPPDATA": str(home / "local"),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
    }
    proc = subprocess.run(
        [SH, str(SCRIPT), "session-start"],
        input="{}",
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd),
        timeout=30,
    )
    assert proc.returncode == 0
    assert "CWD-UV-FOUND" not in proc.stdout
    message = json.loads(proc.stdout)["systemMessage"]
    assert "uv" in message
