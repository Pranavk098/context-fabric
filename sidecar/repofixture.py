"""Repo fixture builder — turn any local Python repository into a deterministic demo fixture.

``cfx demo --repo PATH`` runs the before/after receipt on real repository content; this
module derives the whole fixture automatically, with no hand-written objectives:

- the raw arm bundle: real file bytes in stable sorted-path order, each prefixed with
  ``===== FILE: <path> =====`` (the same rendering the pilot used);
- objectives: six roles, each asking "In which file is `<symbol>` defined?", where the
  symbols come from the real AST extractor (``kernel.cartridge.ast_extractor``) and the
  answer key is the real relative file path;
- fact blocks: one per objective symbol, built from the module docstring's first line and
  the symbol's real signature and path, plus the Tier-0 hologram as a routable artifact
  block, for the sidecar's fabric store;
- the ``.cfx`` cartridge: the real PageRank + serializer path, carried on the fixture.

Determinism: same repo and same parameters produce a byte-identical fixture. No RNG, no
wall clock, no unordered iteration; the cartridge's commit hash is the content hash of the
selected files.

Scan and selection rules: ``*.py`` below ``repo_path``; any path part in ``_IGNORE`` is
skipped (``.git``, ``.venv``/``venv``/``site-packages``, ``__pycache__``, ``tests``,
``build``/``dist``, tool caches, ``node_modules``), so virtualenvs, vendored dependencies,
build output, and tests never enter the fixture. Candidates that resolve outside
``repo_path`` (symlinks) or cannot be read are skipped silently. A candidate over
``_MAX_FILE_TOKENS`` (20000 o200k tokens) is skipped as a vendored/generated outlier and
reported in ``RepoFixture.skipped_large``. The remaining files are taken in sorted
relative-path order until the running per-file token sum reaches ``raw_token_target``
(the crossing file is included) or ``max_files`` files are taken, whichever comes first.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

from kernel.cartridge.ast_extractor import FileSymbols, Symbol, extract_source
from kernel.cartridge.serializer import Cartridge, build_cartridge, count_tokens
from kernel.store import SqliteStore
from protocol.canonical import sha256_hex
from protocol.models import (
    BlockKind,
    ContextBlock,
    Creator,
    CreatorType,
    Fidelity,
    LifecycleStatus,
    MemoryTier,
)

_IGNORE = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "site-packages",
        "tests",
    }
)
_MAX_FILE_TOKENS = 20_000
_ROLES = ("architect", "implementer", "reviewer", "tester", "auditor", "ops")
_AS_OF = datetime(2026, 1, 1, tzinfo=UTC)


class RepoObjective(NamedTuple):
    role: str
    objective: str
    keys: tuple[str, ...]


@dataclass(frozen=True)
class RepoFixture:
    repo_path: str
    files: tuple[str, ...]
    bundle: str
    blocks: tuple[ContextBlock, ...]
    cartridge: Cartridge
    objectives: tuple[RepoObjective, ...]
    raw_tokens: int
    skipped_large: tuple[str, ...]


class _ChosenFile(NamedTuple):
    rel: str
    text: str
    tokens: int


def _scan(root: Path) -> list[tuple[str, Path]]:
    if not root.is_dir():
        raise ValueError(f"not a directory: {root}")
    resolved_root = root.resolve()
    candidates: list[tuple[str, Path]] = []
    for path in root.rglob("*.py"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if any(part in _IGNORE for part in rel.split("/")):
            continue
        try:
            path.resolve().relative_to(resolved_root)
        except (OSError, ValueError):
            continue  # symlink pointing outside repo_path: never read outside the repo
        candidates.append((rel, path))
    candidates.sort(key=lambda item: item[0])
    if not candidates:
        raise ValueError(
            f"no Python files found under {root} (skipped: {', '.join(sorted(_IGNORE))})"
        )
    return candidates


def _select(
    candidates: Sequence[tuple[str, Path]], *, raw_token_target: int, max_files: int
) -> tuple[list[_ChosenFile], tuple[str, ...]]:
    chosen: list[_ChosenFile] = []
    skipped_large: list[str] = []
    total = 0
    for rel, path in candidates:
        if len(chosen) >= max_files:
            break
        try:
            text = path.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue  # unreadable (permissions, vanished mid-scan): skip gracefully
        tokens = count_tokens(text)
        if tokens > _MAX_FILE_TOKENS:
            skipped_large.append(rel)
            continue
        chosen.append(_ChosenFile(rel, text, tokens))
        total += tokens
        if total >= raw_token_target:
            break
    if not chosen:
        raise ValueError(
            f"no usable Python files: every candidate exceeded the {_MAX_FILE_TOKENS}-token cap"
        )
    return chosen, tuple(skipped_large)


def _render_bundle(chosen: Sequence[_ChosenFile]) -> str:
    return "\n\n".join(f"===== FILE: {item.rel} =====\n{item.text}" for item in chosen)


def _module_doc_line(source: str) -> str:
    try:
        doc = ast.get_docstring(ast.parse(source))
    except (SyntaxError, ValueError):
        return ""
    return doc.strip().splitlines()[0] if doc else ""


def _pick_symbols(
    files: Sequence[_ChosenFile], symbols: dict[str, FileSymbols]
) -> list[tuple[str, Symbol]]:
    per_file: list[tuple[str, list[Symbol]]] = []
    for chosen in files:
        exported = list(symbols[chosen.rel].exported)
        public = [s for s in exported if not s.name.startswith("_")]
        per_file.append((chosen.rel, public or exported))
    picked: list[tuple[str, Symbol]] = []
    used: set[str] = set()
    index = 0
    while len(picked) < len(_ROLES):
        progressed = False
        for rel, syms in per_file:
            if len(picked) >= len(_ROLES):
                break
            if index >= len(syms) or syms[index].name in used:
                continue
            progressed = True
            used.add(syms[index].name)
            picked.append((rel, syms[index]))
        if not progressed:
            break
        index += 1
    return picked


def _objectives(picked: Sequence[tuple[str, Symbol]]) -> tuple[RepoObjective, ...]:
    return tuple(
        RepoObjective(
            role,
            f"In which file is `{symbol.name}` defined? Answer with the file path only.",
            (rel,),
        )
        for role, (rel, symbol) in zip(_ROLES, picked, strict=False)
    )


def _block(block_id: str, kind: BlockKind, content: str, seq: int) -> ContextBlock:
    return ContextBlock(
        block_id=block_id,
        block_kind=kind,
        memory_tier=MemoryTier.ledger,
        content=content,
        content_hash="sha256:" + sha256_hex(content.encode("utf-8")),
        token_estimate=count_tokens(content),
        source_event_seq=seq,
        creator=Creator(type=CreatorType.tool, id="repo-fixture"),
        fidelity=Fidelity.extract,
        lifecycle_status=LifecycleStatus.routable,
        trust_domain="default",
        observed_at=_AS_OF,
        valid_from=_AS_OF,
    )


def _fact_blocks(
    picked: Sequence[tuple[str, Symbol]], doc_lines: dict[str, str]
) -> tuple[ContextBlock, ...]:
    blocks: list[ContextBlock] = []
    for seq, (rel, symbol) in enumerate(picked, start=1):
        doc = doc_lines.get(rel) or "(no module docstring)"
        content = f"Module {rel}: {doc} Defines {symbol.kind} {symbol.signature}."
        blocks.append(_block(f"blk_repo_sym_{seq}_{symbol.name}", BlockKind.fact, content, seq))
    return tuple(blocks)


def build_repo_fixture(
    repo_path: Path, *, raw_token_target: int = 6000, max_files: int = 40
) -> RepoFixture:
    """Build the deterministic demo fixture for ``repo_path`` (see the module docstring)."""
    if raw_token_target <= 0 or max_files <= 0:
        raise ValueError("raw_token_target and max_files must be positive")
    root = Path(repo_path)
    candidates = _scan(root)
    chosen, skipped_large = _select(
        candidates, raw_token_target=raw_token_target, max_files=max_files
    )
    symbols = {item.rel: extract_source(item.rel, item.text) for item in chosen}
    picked = _pick_symbols(chosen, symbols)
    if not picked:
        raise ValueError(f"no top-level symbols found in the selected files of {root}")
    bundle = _render_bundle(chosen)
    fixture_hash = sha256_hex(
        "\n".join(f"{item.rel} {sha256_hex(item.text.encode('utf-8'))}" for item in chosen).encode(
            "utf-8"
        )
    )
    cartridge = build_cartridge(
        [symbols[item.rel] for item in chosen],
        tenant_id="default",
        project_id="repo",
        commit_hash=fixture_hash,
    )
    doc_lines = {item.rel: _module_doc_line(item.text) for item in chosen}
    hologram = _block(
        "blk_repo_hologram", BlockKind.artifact, cartridge.hologram_markdown, len(picked) + 1
    )
    return RepoFixture(
        repo_path=str(root),
        files=tuple(item.rel for item in chosen),
        bundle=bundle,
        blocks=(*_fact_blocks(picked, doc_lines), hologram),
        cartridge=cartridge,
        objectives=_objectives(picked),
        raw_tokens=count_tokens(bundle),
        skipped_large=skipped_large,
    )


def build_repo_store(fixture: RepoFixture, path: Path) -> Path:
    """Append the fixture's blocks through the normal store path (routable, trusted)."""
    store = SqliteStore(path)
    for block in fixture.blocks:
        store.append(block)
    return path
