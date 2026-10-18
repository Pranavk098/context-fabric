"""``sidecar/repofixture`` tests — deterministic fixture from a real temp repo.

The tiny repos are built on disk, so the AST extractor, the module-docstring reader, the
cartridge serializer, and the file bundle all run on real files, not mocks. Answer keys are
verified against the actual files with ``ast`` (no self-referential assertions).
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kernel.cartridge.serializer import count_tokens
from kernel.router.compiler import compile_packet
from kernel.store import SqliteStore
from protocol.models import Recipient, Scope
from sidecar.render import render_packet
from sidecar.repofixture import build_repo_fixture, build_repo_store


def make_tiny_repo(root: Path, *, files: int = 8, funcs_per_file: int = 2) -> Path:
    """A deterministic repo: ``files`` modules, each with a docstring and distinct defs."""
    root.mkdir(parents=True, exist_ok=True)
    for i in range(files):
        lines = [f'"""Module {i} for the tiny fixture repo."""', ""]
        for j in range(funcs_per_file):
            lines += [
                f"def fn_{i}_{j}(x):",
                f'    """Return x adjusted by {i} and {j}."""',
                f"    return x + {i * 10 + j}",
                "",
            ]
        path = root / f"pkg_{i}" / f"mod_{i}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")
    return root


def _symbol_defined_in(rel: str, symbol: str, repo: Path) -> bool:
    tree = ast.parse((repo / rel).read_text(encoding="utf-8"))
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.name == symbol
        for node in tree.body
    )


def test_fixture_builds_six_objectives_with_real_path_keys(tmp_path: Path) -> None:
    repo = make_tiny_repo(tmp_path / "repo")
    fixture = build_repo_fixture(repo)
    assert len(fixture.objectives) == 6
    assert [o.role for o in fixture.objectives] == [
        "architect",
        "implementer",
        "reviewer",
        "tester",
        "auditor",
        "ops",
    ]
    assert tuple(fixture.files) == tuple(sorted(fixture.files))
    assert len(fixture.files) == 8
    seen: set[str] = set()
    for objective in fixture.objectives:
        (rel,) = objective.keys
        symbol = objective.objective.split("`")[1]
        assert (repo / rel).is_file()
        assert _symbol_defined_in(rel, symbol, repo)
        assert symbol not in seen
        seen.add(symbol)
        assert any(
            block.content is not None and rel in block.content and symbol in block.content
            for block in fixture.blocks
        )
    assert fixture.raw_tokens == count_tokens(fixture.bundle)
    assert fixture.bundle.startswith("===== FILE: pkg_0/mod_0.py =====")
    assert fixture.cartridge.cartridge_id.startswith("cfx_repo_")
    assert fixture.cartridge.token_count > 0


def test_fixture_is_deterministic_across_builds(tmp_path: Path) -> None:
    repo = make_tiny_repo(tmp_path / "repo")
    first = build_repo_fixture(repo)
    second = build_repo_fixture(repo)
    assert first == second
    assert first.cartridge == second.cartridge
    assert first.bundle == second.bundle


def test_raw_token_target_and_max_files_cap(tmp_path: Path) -> None:
    repo = make_tiny_repo(tmp_path / "repo", files=10, funcs_per_file=40)
    target = build_repo_fixture(repo, raw_token_target=1000)
    assert target.raw_tokens >= 1000
    assert len(target.files) < 10
    capped = build_repo_fixture(repo, raw_token_target=10**9, max_files=3)
    assert len(capped.files) == 3
    assert capped.raw_tokens < 10**9
    with pytest.raises(ValueError, match="must be positive"):
        build_repo_fixture(repo, raw_token_target=0)


def test_huge_files_are_skipped_and_reported(tmp_path: Path) -> None:
    repo = make_tiny_repo(tmp_path / "repo")
    (repo / "aaa_big.py").write_text("x = 1\n" * 20000, encoding="utf-8")
    fixture = build_repo_fixture(repo)
    assert "aaa_big.py" in fixture.skipped_large
    assert "aaa_big.py" not in fixture.files


def test_ignored_directories_are_not_scanned(tmp_path: Path) -> None:
    repo = make_tiny_repo(tmp_path / "repo")
    venv = repo / ".venv" / "lib" / "site-packages"
    venv.mkdir(parents=True)
    (venv / "vendored.py").write_text("def vendored(): ...\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("def test_x(): ...\n", encoding="utf-8")
    fixture = build_repo_fixture(repo)
    assert not any("vendored" in rel or rel.startswith("tests/") for rel in fixture.files)


def test_zero_python_files_raises(tmp_path: Path) -> None:
    repo = tmp_path / "empty"
    repo.mkdir()
    (repo / "README.md").write_text("no python here", encoding="utf-8")
    with pytest.raises(ValueError, match="no Python files found"):
        build_repo_fixture(repo)


def test_non_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a directory"):
        build_repo_fixture(tmp_path / "missing")


def test_selected_files_without_symbols_raise(tmp_path: Path) -> None:
    repo = tmp_path / "symbols"
    repo.mkdir()
    (repo / "empty.py").write_text("# nothing defined here\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no top-level symbols"):
        build_repo_fixture(repo)


def test_build_repo_store_round_trips_blocks(tmp_path: Path) -> None:
    fixture = build_repo_fixture(make_tiny_repo(tmp_path / "repo"))
    db = build_repo_store(fixture, tmp_path / "repo.db")
    assert SqliteStore(db).load_blocks() == list(fixture.blocks)
    assert len(fixture.blocks) == len(fixture.objectives) + 1


def test_compiled_packets_carry_every_answer_key(tmp_path: Path) -> None:
    fixture = build_repo_fixture(make_tiny_repo(tmp_path / "repo"))
    for objective in fixture.objectives:
        result = compile_packet(
            scope=Scope(
                tenant_id="default", project_id="default", workflow_id="chat", task_id="chat"
            ),
            recipient=Recipient(agent_id="repo-agent", role=objective.role, stage="chat"),
            objective=objective.objective,
            stage="chat",
            blocks=list(fixture.blocks),
            as_of=datetime(2026, 1, 1, tzinfo=UTC),
        )
        rendered = render_packet(result.packet)
        assert objective.keys[0] in rendered
        assert objective.objective.split("`")[1] in rendered


def test_unreadable_files_are_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_tiny_repo(tmp_path / "repo", files=2)
    blocked = repo / "pkg_0" / "mod_0.py"
    real_read_bytes = Path.read_bytes

    def deny(self: Path) -> bytes:
        if self == blocked:
            raise PermissionError(13, "Access is denied")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", deny)
    fixture = build_repo_fixture(repo)
    assert "pkg_0/mod_0.py" not in fixture.files
    assert len(fixture.files) == 1
    assert fixture.objectives


def test_symlinked_files_outside_the_repo_are_skipped(tmp_path: Path) -> None:
    repo = make_tiny_repo(tmp_path / "repo", files=1)
    outside = tmp_path / "outside_secret.py"
    outside.write_text('"""SECRET."""\n\ndef secret():\n    ...\n', encoding="utf-8")
    try:
        (repo / "leak.py").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks not permitted on this platform")
    fixture = build_repo_fixture(repo)
    assert "leak.py" not in fixture.files
    assert "SECRET" not in fixture.bundle
