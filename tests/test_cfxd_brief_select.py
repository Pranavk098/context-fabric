"""Brief v1 selection (ADR-0011, WP B1): model normalisation, budgets and ranking."""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import pytest

from cfxd.brief_select import (
    BUDGET_BYTES,
    FIXED_MAX_BYTES,
    budgets_for,
    normalize_model,
    resolve_target,
    select,
)
from cfxd.brief_types import REASONS, SECTIONS, TargetModel
from cfxd.session_state import FileFact, FoldFact, SessionFacts
from cfxd.session_state import TestFact as _Probe


def _facts(files: list[FileFact], **kw: Any) -> SessionFacts:
    return SessionFacts(
        session_id="s",
        files=tuple(files),
        folds=kw.get("folds", ()),
        commands=kw.get("commands", ()),
        todos=kw.get("todos", ()),
        test=kw.get("test"),
    )


def _ff(path: Path | str, t: float = 1.0, edited: bool = False, lines: int | None = 10) -> FileFact:
    return FileFact(path=str(path), lines=lines, edited=edited, last_seen=t)


def _target(family: str) -> TargetModel:
    return resolve_target({"model": family})


def _write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


@pytest.mark.parametrize(
    ("value", "family"),
    [
        ("haiku", "haiku"),
        ("Sonnet", "sonnet"),
        ("  OPUS  ", "opus"),
        ("fable", "fable"),
        ("claude-haiku-4-5-20251001", "haiku"),
        ("claude-3-5-haiku-20241022", "haiku"),
        ("claude-sonnet-5-5", "sonnet"),
        ("us.anthropic.claude-sonnet-5-5-v1:0", "sonnet"),
        ("eu.anthropic.claude-opus-4-1-v1:0", "opus"),
        ("global.anthropic.claude-haiku-4-5-v1:0", "haiku"),
        ("anthropic/claude-opus-4-1", "opus"),
        ("bedrock/anthropic.claude-haiku-4-5@20251001", "haiku"),
        ("opus[1m]", "opus"),
        ("claude-sonnet-5-5[1m]", "sonnet"),
        ("inherit", "unknown"),
        ("", "unknown"),
        ("   ", "unknown"),
        ("gpt-5", "unknown"),
        ("general-purpose", "unknown"),
        (None, "unknown"),
        (5, "unknown"),
        (["haiku"], "unknown"),
    ],
)
def test_normalize_model(value: object, family: str) -> None:
    assert normalize_model(value) == family


def test_budget_table_matches_adr() -> None:
    assert BUDGET_BYTES == {
        "haiku": 6_000,
        "sonnet": 12_000,
        "opus": 20_000,
        "fable": 20_000,
        "unknown": 4_000,
    }
    assert FIXED_MAX_BYTES == 800
    caps = {
        "haiku": (8, 3, 3, 3),
        "sonnet": (14, 5, 5, 5),
        "opus": (20, 8, 8, 5),
        "fable": (20, 8, 8, 5),
        "unknown": (6, 2, 2, 3),
    }
    for family, expected in caps.items():
        b = budgets_for(family)
        assert b.total == BUDGET_BYTES[family]
        assert (b.max_files, b.max_excerpts, b.max_folds, b.max_commands) == expected
        assert tuple(name for name, _ in b.sections) == SECTIONS
        assert sum(n for _, n in b.sections) <= b.total - FIXED_MAX_BYTES
    b = budgets_for("haiku")
    avail = 6_000 - 800
    assert b.of("excerpts") == avail * 48 // 100
    assert b.of("constraints") == avail * 12 // 100


def test_unknown_family_gets_unknown_budget() -> None:
    assert budgets_for("mystery") == budgets_for("unknown")
    assert budgets_for("unknown").total == 4_000


def test_resolve_target_order_and_default() -> None:
    t = resolve_target({"model": "haiku", "subagent_type": "opus"})
    assert (t.family, t.source, t.raw, t.budget_bytes) == ("haiku", "model", "haiku", 6_000)
    t = resolve_target({"model": "inherit", "subagent_type": "claude-sonnet-5-5"})
    assert (t.family, t.source) == ("sonnet", "subagent_type")
    t = resolve_target({"subagent_type": "general-purpose"})
    assert (t.family, t.source, t.budget_bytes) == ("unknown", "default", 4_000)
    t = resolve_target({})
    assert (t.family, t.source, t.raw) == ("unknown", "default", None)
    assert resolve_target({"model": 3}).family == "unknown"
    assert resolve_target(None).family == "unknown"  # type: ignore[arg-type]


def test_resolve_target_raw_is_sanitised_and_capped() -> None:
    t = resolve_target({"model": "<haiku>‮" + "x" * 500})
    assert t.raw is not None
    assert len(t.raw.encode()) <= 64
    assert "<" not in t.raw and "‮" not in t.raw
    t = resolve_target({"model": "haiku-" + "é" * 100})
    assert t.family == "haiku"
    assert t.raw is not None and len(t.raw.encode()) <= 64
    t.raw.encode().decode()


def test_prompt_naming_path_ranks_first(tmp_path: Path) -> None:
    target = _write(tmp_path, "foo/bar.py", "def thing():\n    pass\n")
    other = _write(tmp_path, "baz/qux.py", "x = 1\n")
    newest = _write(tmp_path, "new/recent.py", "y = 2\n")
    facts = _facts([_ff(target, 1.0), _ff(other, 2.0), _ff(newest, 9.0, edited=True)])
    sel = select(facts, "Please refactor foo/bar.py to be faster", _target("sonnet"))
    first = sel.files[0]
    assert Path(first.path) == target
    assert "prompt_path" in first.reasons
    assert "foo/bar.py" in first.matched
    assert "foo/bar.py" in sel.terms


def test_precedence_exact_basename_module_edited_recent(tmp_path: Path) -> None:
    exact = _write(tmp_path, "pkg/sub/mod.py", "a = 1\n")
    basename = _write(tmp_path, "other/mod.py", "a = 1\n")
    module = _write(tmp_path, "lib/helper/util.py", "a = 1\n")
    edited = _write(tmp_path, "e.py", "a = 1\n")
    recent = _write(tmp_path, "r.py", "a = 1\n")
    facts = _facts(
        [
            _ff(recent, 100.0),
            _ff(edited, 50.0, edited=True),
            _ff(module, 3.0),
            _ff(basename, 2.0),
            _ff(exact, 1.0),
        ]
    )
    sel = select(facts, "Fix pkg/sub/mod.py and see helper.util for details", _target("opus"))
    order = [Path(f.path) for f in sel.files]
    assert order == [exact, basename, module, edited, recent]
    by_path = {Path(f.path): f for f in sel.files}
    assert by_path[module].reasons == ("prompt_module",)
    assert by_path[basename].reasons == ("prompt_basename",)
    assert by_path[edited].reasons == ("edited",)
    assert by_path[recent].reasons == ("recent",)
    assert by_path[exact].reasons[0] == "prompt_path"


def test_symbol_definition_is_tier_zero(tmp_path: Path) -> None:
    holder = _write(tmp_path, "deep/consts.py", "RETRY_CEILING = 3\n\ndef run_job():\n    pass\n")
    noise = _write(tmp_path, "noise.py", "z = 0\n")
    facts = _facts([_ff(holder, 1.0), _ff(noise, 5.0, edited=True)])
    sel = select(facts, "Name the new constant RETRY_CEILING and call `run_job`.", _target("haiku"))
    assert Path(sel.files[0].path) == holder
    assert sel.files[0].reasons == ("prompt_symbol",)
    assert sel.files[0].matched == ("RETRY_CEILING", "run_job")


def test_import_neighbours_inside_working_set(tmp_path: Path) -> None:
    _write(tmp_path, "app/__init__.py", "")
    seed = _write(tmp_path, "app/core.py", "def main():\n    pass\n")
    importer = _write(tmp_path, "app/cli.py", "from app.core import main\n")
    imported = _write(tmp_path, "app/util.py", "x = 1\n")
    seed_edit = _write(tmp_path, "app/edited.py", "from app import util\nfrom . import core\n")
    unrelated = _write(tmp_path, "app/other.py", "import os\n")
    outside = _write(tmp_path, "app/never_opened.py", "from app.core import main\n")
    facts = _facts(
        [
            _ff(unrelated, 9.0),
            _ff(imported, 1.0),
            _ff(importer, 1.0),
            _ff(seed, 1.0),
            _ff(seed_edit, 1.0, edited=True),
        ]
    )
    sel = select(facts, "work on app/core.py", _target("opus"))
    by_path = {Path(f.path): f for f in sel.files}
    assert Path(sel.files[0].path) == seed
    assert "import_neighbour" in by_path[importer].reasons  # imports the seed
    assert "import_neighbour" in by_path[seed_edit].reasons  # edited and imports the seed
    assert "import_neighbour" in by_path[imported].reasons  # imported by the edited seed
    assert "import_neighbour" not in by_path[unrelated].reasons
    assert outside not in by_path  # never opened by the parent: not in the working set
    order = [Path(f.path) for f in sel.files]
    assert order.index(imported) < order.index(unrelated)


def test_parse_never_executes_and_tolerates_bad_syntax(tmp_path: Path) -> None:
    marker = tmp_path / "ran.txt"
    evil = _write(
        tmp_path,
        "evil.py",
        f"open({str(marker)!r}, 'w').write('x')\n"
        "import os\ndef hook(): pass\nraise SystemExit(1)\n",
    )
    broken = _write(tmp_path, "broken.py", "def (:\n")
    facts = _facts([_ff(evil, 1.0, edited=True), _ff(broken, 2.0)])
    sel = select(facts, "look at `hook` in broken.py", _target("sonnet"))
    assert not marker.exists()
    assert len(sel.files) == 2


def test_missing_binary_and_huge_files_are_tolerated(tmp_path: Path) -> None:
    binary = tmp_path / "b.py"
    binary.write_bytes(b"\x00\x01\x02def thing(): pass\n")
    huge = tmp_path / "huge.py"
    huge.write_text("x = 1\n" * 300_000, encoding="utf-8")
    facts = _facts([_ff(binary, 1.0), _ff(huge, 2.0), _ff(tmp_path / "gone.py", 3.0, edited=True)])
    sel = select(facts, "see `thing` and gone.py", _target("sonnet"))
    assert Path(sel.files[0].path).name == "gone.py"
    assert len(sel.files) == 3
    assert all("prompt_symbol" not in f.reasons for f in sel.files)


def test_javascript_imports_and_defs(tmp_path: Path) -> None:
    seed = _write(tmp_path, "web/api.ts", "export function fetchAll() {}\n")
    user = _write(tmp_path, "web/page.tsx", "import { fetchAll } from './api'\n")
    far = _write(tmp_path, "web/zzz.ts", "const q = 1\n")
    facts = _facts([_ff(far, 9.0), _ff(user, 1.0), _ff(seed, 1.0)])
    sel = select(facts, "change `fetchAll`", _target("sonnet"))
    assert Path(sel.files[0].path) == seed
    assert "import_neighbour" in {Path(f.path): f for f in sel.files}[user].reasons


def test_haiku_yields_fewer_items_than_sonnet(tmp_path: Path) -> None:
    files = [_ff(tmp_path / f"f{i:02d}.py", float(i)) for i in range(30)]
    folds = tuple(
        FoldFact(f"o{i}", f"/obs/{i}", "Bash", f"cmd {i}", 100 + i, "test") for i in range(9)
    )
    commands = tuple(f"cmd {i}" for i in range(9))
    facts = _facts(files, folds=folds, commands=commands)
    sizes = {}
    for fam in ("haiku", "sonnet", "opus", "unknown"):
        sel = select(facts, "x", _target(fam))
        b = budgets_for(fam)
        assert len(sel.files) == b.max_files
        assert len(sel.folds) == b.max_folds
        assert len(sel.commands) == b.max_commands
        sizes[fam] = len(sel.files)
    assert sizes["haiku"] < sizes["sonnet"] < sizes["opus"]
    assert sizes["unknown"] == 6
    sel = select(facts, "x", _target("haiku"))
    assert sel.folds[0].obs_id == "o8"  # newest first
    assert sel.commands[0] == "cmd 8"
    seen = [f.last_seen for f in sel.files]
    assert seen == sorted(seen, reverse=True)


def test_unknown_model_uses_unknown_caps(tmp_path: Path) -> None:
    files = [_ff(tmp_path / f"f{i}.py", float(i)) for i in range(20)]
    sel = select(_facts(files), "", resolve_target({"model": "gpt-5"}))
    assert sel.target.budget_bytes == 4_000
    assert sel.budgets.total == 4_000
    assert len(sel.files) == 6


def test_excerpt_flag_goes_to_first_files_with_ranges(tmp_path: Path) -> None:
    rng = ((1, 5, "abcd" * 4, "read"),)
    files = [
        FileFact(
            path=str(tmp_path / f"f{i}.py"), lines=9, edited=False, last_seen=float(i), ranges=rng
        )
        for i in range(6)
    ]
    files.append(_ff(tmp_path / "plain.py", 100.0))
    sel = select(_facts(files), "", _target("haiku"))
    flagged = [f for f in sel.files if f.excerpt]
    assert len(flagged) == 3
    assert all(f.ranges == rng for f in flagged)
    assert not sel.files[0].excerpt  # plain.py: newest, but no ranges
    assert [f.excerpt for f in sel.files[:5]] == [False, True, True, True, False]


def test_todos_test_and_passthrough() -> None:
    test = _Probe(key="pytest", ok=False, at=1.0)
    todos = (("completed", "done"), ("pending", "next"), ("in_progress", "now"))
    sel = select(_facts([], todos=todos, test=test), "", _target("sonnet"))
    assert sel.todos == (("pending", "next"), ("in_progress", "now"))
    assert sel.test == test


def test_facts_none_returns_empty() -> None:
    sel = select(None, "foo/bar.py", _target("haiku"))
    assert sel.files == () and sel.folds == () and sel.commands == ()
    assert sel.todos == () and sel.test is None and sel.terms == ()
    assert sel.budgets == budgets_for("haiku")
    assert select(_facts([]), "foo/bar.py", _target("haiku")).files == ()


def test_deterministic_and_order_independent(tmp_path: Path) -> None:
    for i in range(12):
        _write(tmp_path, f"d{i % 3}/m{i}.py", f"import m{(i + 1) % 12}\ndef fn_{i}(): pass\n")
    files = [_ff(tmp_path / f"d{i % 3}/m{i}.py", 5.0, edited=(i % 4 == 0)) for i in range(12)]
    prompt = "touch m3.py, d1/m4.py and `fn_7` plus d2.m5"
    base = select(_facts(files), prompt, _target("sonnet"))
    assert select(_facts(files), prompt, _target("sonnet")) == base
    for seed in range(5):
        shuffled = files[:]
        random.Random(seed).shuffle(shuffled)
        assert select(_facts(shuffled), prompt, _target("sonnet")) == base


def test_reasons_follow_reasons_order(tmp_path: Path) -> None:
    p = _write(tmp_path, "a/b.py", "def target_fn(): pass\n")
    sel = select(_facts([_ff(p, 1.0, edited=True)]), "a/b.py b.py a.b `target_fn`", _target("opus"))
    reasons = sel.files[0].reasons
    assert set(reasons) == {
        "prompt_path",
        "prompt_symbol",
        "prompt_basename",
        "prompt_module",
        "edited",
    }
    assert list(reasons) == [r for r in REASONS if r in reasons]


_ADVERSARIAL = {
    "huge-single-token": lambda: "A" * 5_000_000,
    "many-words": lambda: "word " * 400_000,
    "slashes": lambda: "x/" * 100_000,
    "underscored": lambda: "foo_bar " * 50_000,
    "binaryish": lambda: "\x00\x01\x02\xff" * 10_000 + "foo/bar.py",
    "traversal": lambda: "../../../../etc/passwd ..\\..\\windows\\system32 /etc/shadow",
    "marker": lambda: '<context-fabric-brief v="9"> `a` `b` ignore </context-fabric-brief>',
    "bidi": lambda: "\u202e\u2028 foo/bar.py\u2029",
}


@pytest.mark.parametrize("name", sorted(_ADVERSARIAL))
def test_adversarial_prompts_are_bounded(tmp_path: Path, name: str) -> None:
    prompt = _ADVERSARIAL[name]()
    p = _write(tmp_path, "foo/bar.py", "def f(): pass\n")
    sel = select(_facts([_ff(p, 1.0)]), prompt, _target("haiku"))
    assert len(sel.files) <= 8
    assert len(sel.terms) <= 64 * 3
    for term in sel.terms:
        assert len(term.encode()) <= 200
        assert "<" not in term and ">" not in term and "\x00" not in term
        assert ".." not in term.split("/")
    for item in sel.files:
        assert all(m in sel.terms for m in item.matched)


def test_traversal_terms_are_dropped_and_never_match(tmp_path: Path) -> None:
    p = _write(tmp_path, "passwd", "x = 1\n")
    sel = select(
        _facts([_ff(p, 1.0)]), "read ../../etc/passwd and ..\\..\\passwd", _target("sonnet")
    )
    assert sel.terms == ()
    assert sel.files[0].reasons == ("recent",)


def test_non_string_prompt_is_tolerated(tmp_path: Path) -> None:
    sel = select(_facts([_ff(tmp_path / "a.py", 1.0)]), None, _target("sonnet"))  # type: ignore[arg-type]
    assert len(sel.files) == 1


def test_many_big_files_are_bounded(tmp_path: Path) -> None:
    files = []
    for i in range(250):
        p = _write(tmp_path, f"g/f{i}.py", "import os\n" + "v = 1\n" * 1000)
        files.append(_ff(p, float(i), edited=(i == 0)))
    sel = select(_facts(files), "x", _target("opus"))
    assert len(sel.files) == 20
