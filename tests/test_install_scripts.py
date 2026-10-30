"""--dry-run of the install scripts prints the plan and changes nothing."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_GIT_SH = Path(r"C:\Program Files\Git\bin\sh.exe")


def _sh() -> str | None:
    return shutil.which("sh") or (str(_GIT_SH) if _GIT_SH.exists() else None)


def _check(cmd: list[str]) -> None:
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    text = out.stdout + out.stderr
    assert "would run" in text and "cfxd doctor" in text
    assert text.index("cfxd doctor") < text.index("plugin install")  # doctor first
    assert "set brief_v1 on" in text and "EXPERIMENTAL" in text
    assert "running:" not in text  # nothing executed
    assert "sudo" not in text


def test_install_sh_dry_run() -> None:
    sh = _sh()
    if sh is None:
        pytest.skip("no POSIX sh")
    _check([sh, str(SCRIPTS / "install.sh"), "--dry-run"])


def test_install_sh_rejects_unknown_option() -> None:
    sh = _sh()
    if sh is None:
        pytest.skip("no POSIX sh")
    out = subprocess.run(
        [sh, str(SCRIPTS / "install.sh"), "--bogus"], capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 2


def test_install_ps1_dry_run() -> None:
    ps = shutil.which("pwsh") or shutil.which("powershell")
    if ps is None or os.name != "nt":
        pytest.skip("PowerShell run only on Windows")
    _check([ps, "-NoProfile", "-File", str(SCRIPTS / "install.ps1"), "-DryRun"])
