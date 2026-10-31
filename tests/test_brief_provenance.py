"""M-4: Brief excerpts come only from ranges the parent read, inside the work tree, opt-in.

``brief_excerpts`` is off by default, so the Brief a tester gets is pointers only. When it is
on, an excerpt line must be one the parent's own Read/Edit calls recorded, the file must
resolve inside the session's work tree, and secret-like names never get content.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from cfxd.brief_render import HEADERS, render
from cfxd.brief_select import allowed_ranges, resolve_target, select
from cfxd.brief_types import SECTIONS, Budgets, FileItem, Intent, Selection, TargetModel
from cfxd.config import read_config, set_flag
from cfxd.session_state import FileFact, SessionFacts

NOW = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)
EMPTY = Intent(goal=None, constraints=(), decisions=(), tried=(), deduped=0)
SHARES = {
    "task": 6,
    "constraints": 12,
    "decisions": 8,
    "tried": 6,
    "files": 10,
    "excerpts": 48,
    "folds": 5,
    "commands": 5,
}


def _budgets() -> Budgets:
    total = 12_000
    avail = total - 800
    return Budgets(
        total=total,
        sections=tuple((name, avail * SHARES[name] // 100) for name in SECTIONS),
        max_files=14,
        max_excerpts=5,
        max_folds=5,
        max_commands=5,
    )


def _selection(files: tuple[FileItem, ...]) -> Selection:
    budgets = _budgets()
    return Selection(
        target=TargetModel(
            family="sonnet", raw="sonnet", source="model", budget_bytes=budgets.total
        ),
        budgets=budgets,
        files=files,
        folds=(),
        commands=(),
        todos=(),
        test=None,
        terms=(),
    )


def _file(
    path: Path | str,
    ranges: tuple[tuple[int, int, str, str], ...] = (),
    *,
    excerpt: bool = True,
    lines: int | None = 30,
) -> FileItem:
    return FileItem(
        path=str(path),
        lines=lines,
        edited=False,
        last_seen=1.0,
        reasons=("recent",),
        matched=(),
        ranges=ranges,
        excerpt=excerpt,
    )


def _numbered(path: Path, count: int) -> None:
    path.write_text("".join(f"row {i}\n" for i in range(1, count + 1)), encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _render(sel: Selection, cwd: Path, excerpts: bool) -> object:
    return render(sel, EMPTY, cwd=str(cwd), session_id="s", now=NOW, excerpts=excerpts)


def test_excerpt_from_a_read_range_appears_when_brief_excerpts_is_on(tmp_path: Path) -> None:
    p = tmp_path / "a.py"
    _numbered(p, 10)
    sel = _selection((_file(p, ((3, 4, _sha(p), "read"),)),))

    state = tmp_path / "state"
    assert read_config(state)["brief_excerpts"] is False
    set_flag(state, "brief_excerpts", True)
    assert read_config(state)["brief_excerpts"] is True

    out = _render(sel, tmp_path, read_config(state)["brief_excerpts"])
    assert HEADERS["excerpts"] in out.text  # type: ignore[attr-defined]
    assert "=== a.py lines 3-4" in out.text  # type: ignore[attr-defined]
    assert "    3| row 3" in out.text and "    4| row 4" in out.text  # type: ignore[attr-defined]


def test_range_the_parent_did_not_read_never_appears(tmp_path: Path) -> None:
    p = tmp_path / "a.py"
    lines = [f"row {i}" for i in range(1, 31)]
    lines[19] = "CANARY-IN-A-GREP-RANGE"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    sha = _sha(p)
    # Only (1, 2) came from a Read; (18, 21) claims to be a range but was not a Read/Edit call.
    item = _file(p, ((1, 2, sha, "read"), (18, 21, sha, "grep")))
    assert allowed_ranges(item.ranges) == ((1, 2, sha, "read"),)

    out = _render(_selection((item,)), tmp_path, True)
    assert "CANARY-IN-A-GREP-RANGE" not in out.text  # type: ignore[attr-defined]
    assert out.items[0].excerpt_lines == ((1, 2),)  # type: ignore[attr-defined]


def test_file_outside_work_tree_and_secret_names_never_get_excerpts(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    outside = tmp_path / "outside.py"
    _numbered(outside, 20)
    outside.write_text(outside.read_text(encoding="utf-8") + "OUTSIDE-CANARY\n", encoding="utf-8")
    env = repo / ".env"
    env.write_text("ENVCANARY=1\n", encoding="utf-8")
    app = repo / "app.py"
    _numbered(app, 5)

    sel = _selection(
        (
            _file("../outside.py", ((1, 21, _sha(outside), "read"),)),
            _file(env, ((1, 1, _sha(env), "read"),), lines=1),
            _file(app),
        )
    )
    out = _render(sel, repo, True)

    assert "OUTSIDE-CANARY" not in out.text  # type: ignore[attr-defined]
    assert "ENVCANARY" not in out.text  # type: ignore[attr-defined]
    assert "=== " not in out.text  # type: ignore[attr-defined]
    for pointer in ("- ../outside.py | ", "- .env | ", "- app.py | "):
        assert pointer in out.text  # type: ignore[attr-defined]


def test_default_off_renders_pointers_only(tmp_path: Path) -> None:
    p = tmp_path / "a.py"
    _numbered(p, 10)
    state = tmp_path / "state"
    flag = read_config(state)["brief_excerpts"]
    assert flag is False

    out = _render(_selection((_file(p, ((1, 2, _sha(p), "read"),)),)), tmp_path, flag)
    assert HEADERS["excerpts"] not in out.text  # type: ignore[attr-defined]
    assert "=== " not in out.text  # type: ignore[attr-defined]
    assert "row 1" not in out.text  # type: ignore[attr-defined]
    assert "- a.py | 30 lines | read" in out.text  # type: ignore[attr-defined]
    assert out.items[0].excerpt_lines == () and out.items[0].degraded is False  # type: ignore[attr-defined]


def test_canary_outside_the_read_ranges_never_appears(tmp_path: Path) -> None:
    p = tmp_path / "a.py"
    lines = [f"row {i}" for i in range(1, 41)]
    lines[4] = "CANARY-BETWEEN-RANGES"
    lines[29] = "CANARY-AFTER-RANGES"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    sha = _sha(p)
    item = _file(p, ((1, 3, sha, "read"), (10, 12, sha, "read")))

    out = _render(_selection((item,)), tmp_path, True)
    text = out.text  # type: ignore[attr-defined]
    assert "CANARY-BETWEEN-RANGES" not in text and "CANARY-AFTER-RANGES" not in text
    assert "   10| row 10" in text and "   12| row 12" in text


def test_select_keeps_only_read_and_edit_ranges(tmp_path: Path) -> None:
    p = tmp_path / "a.py"
    p.write_text("x = 1\n", encoding="utf-8")
    sha = _sha(p)
    facts = SessionFacts(
        "s",
        (
            FileFact(
                str(p),
                1,
                False,
                1.0,
                ((1, 1, sha, "read"), (1, 1, sha, "grep"), (0, 0, sha, "edit")),
            ),
        ),
        (),
        (),
        (),
    )
    sel = select(facts, "", resolve_target({"model": "sonnet"}))
    assert sel.files[0].ranges == ((1, 1, sha, "read"),)
