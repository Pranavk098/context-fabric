"""Golden report suite (P12): the four tester-facing reports over fixed transcripts.

Each file under ``tests/golden/reports/`` is the exact stdout of one command on the
synthetic transcripts under ``tests/golden/``. A number changing here is a deliberate
change, reviewed line by line against the fixture, never a regenerated snapshot.

The root is passed explicitly and ``CFX_HOME`` points at a temporary directory, so no
report can read this machine's real transcripts or state.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cfxd.cli import main as cli_main

GOLDEN = Path(__file__).with_name("golden")
REPORTS = GOLDEN / "reports"

# ``week`` filters by file mtime, and a checkout stamps the fixtures with "now". The
# window is wide enough that any clone is inside it; 7 days would make this test depend
# on when the repo was cloned.
_CASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("audit", ("audit", "--since", "all")),
    ("week", ("week", "--days", "36500", "--state-dir", "{state}")),
    ("fidelity", ("fidelity", "--since", "all")),
    ("handoff-stats", ("handoff-stats",)),
)


@pytest.mark.parametrize(("name", "argv"), _CASES, ids=[case[0] for case in _CASES])
def test_golden_report_is_exact(
    name: str,
    argv: tuple[str, ...],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # diet and week read their state through CFX_HOME; keep a developer's own state out.
    monkeypatch.setenv("CFX_HOME", str(tmp_path))
    args = [arg.format(state=tmp_path) for arg in (*argv, "--root", str(GOLDEN))]
    assert cli_main(args) == 0
    actual = capsys.readouterr().out.replace("\r\n", "\n")
    expected = (REPORTS / f"{name}.txt").read_text(encoding="utf-8")
    assert actual == expected


def test_every_golden_report_has_an_expected_file() -> None:
    assert {path.stem for path in REPORTS.glob("*.txt")} == {name for name, _ in _CASES}
