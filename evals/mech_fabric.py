"""Deterministic mechanical fabric ingester — build ``ContextBlock`` memory from source.

This is the reusable ingester behind Order B's "mechanical memory" runs (B4 v2 today, the
``httpx`` pilot rerun next). It reads **only** the source files it is given (plus the
shared AST/PageRank/cartridge helpers) and **never** any objective, answer key, or
hand-written fact block. The output is fully determined by ``(repo_root, files)``:

* one ``fact`` block per top-level symbol (class/function signature from
  ``kernel.cartridge.ast_extractor``, plus the symbol's first docstring line),
* one ``fact`` block per assignment (module-level and class-level ``name = value``,
  rendered with ``ast.unparse``),
* one ``fact`` block per file listing its import roots (``import``/``from ... import``),
* one ``artifact`` block carrying the project ``.cfx`` cartridge hologram
  (``kernel.cartridge.serializer.build_cartridge``).

Each source-derived block records its origin file in ``ref_uri`` so a caller can apply an
ownership map (role -> files it owns) without re-reading the source; the cartridge block is
project-wide (``ref_uri=None``). No RNG, no wall clock, no unordered iteration.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from kernel.cartridge.ast_extractor import FileSymbols, extract_source
from kernel.cartridge.serializer import Cartridge, build_cartridge, count_tokens
from kernel.router.recall import tokenize
from protocol.canonical import sha256_hex
from protocol.models import (
    BlockKind,
    ContextBlock,
    Creator,
    CreatorType,
    Fidelity,
    MemoryTier,
)

_AS_OF = datetime(2026, 1, 1, tzinfo=UTC)
_CREATOR = Creator(type=CreatorType.tool, id="b4-mech")

_DocNode = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef


def _doc_first_line(node: _DocNode) -> str:
    """First line of a node's docstring, or ``""`` when absent/unparseable."""
    doc = ast.get_docstring(node)
    return doc.strip().splitlines()[0] if doc else ""


def _docstrings(tree: ast.Module) -> dict[str, str]:
    """Name -> first docstring line for every top-level class/function."""
    docs: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            line = _doc_first_line(node)
            if line:
                docs[node.name] = line
    return docs


def _assignments(tree: ast.Module) -> list[tuple[str, str]]:
    """``(qualified_name, unparse(value))`` for module- and class-level assignments."""
    out: list[tuple[str, str]] = []

    def visit(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        out.append((f"{prefix}{target.id}", ast.unparse(node.value)))
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name):
                    value = ast.unparse(node.value) if node.value is not None else "None"
                    out.append((f"{prefix}{node.target.id}", value))
            elif isinstance(node, ast.ClassDef):
                visit(node.body, f"{node.name}.")

    visit(tree.body, "")
    return out


def _import_roots(tree: ast.Module) -> list[str]:
    """Ordered, de-duplicated root module names of every import in the file."""
    roots: list[str] = []
    seen: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in seen:
                    seen.add(root)
                    roots.append(root)
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            if root not in seen:
                seen.add(root)
                roots.append(root)
    return roots


def _import_statements(tree: ast.Module) -> list[str]:
    """Ordered full ``from <module> import <names>`` statements (module-level only).

    Unlike :func:`_import_roots`, this keeps the symbol -> module mapping (e.g.
    ``from ._config import DEFAULT_TIMEOUT_CONFIG``), which the B4 planner objectives need.
    """
    out: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            names = ", ".join(alias.name for alias in node.names)
            dots = "." * node.level
            module = f"{dots}{node.module}" if node.module else dots
            out.append(f"from {module} import {names}")
    return out


def _block(
    block_id: str,
    kind: BlockKind,
    content: str,
    seq: int,
    rel: str | None,
    exported: tuple[str, ...],
) -> ContextBlock:
    return ContextBlock(
        block_id=block_id,
        block_kind=kind,
        memory_tier=MemoryTier.ledger,
        content=content,
        ref_uri=rel,
        content_hash="sha256:" + sha256_hex(content.encode("utf-8")),
        token_estimate=count_tokens(content),
        source_event_seq=seq,
        creator=_CREATOR,
        fidelity=Fidelity.extract,
        trust_domain="default",
        symbols_exported=exported,
        observed_at=_AS_OF,
        valid_from=_AS_OF,
    )


def build_blocks(
    repo_root: str | Path,
    files: Sequence[str],
    *,
    import_statements: bool = False,
) -> list[ContextBlock]:
    """Build mechanical memory blocks for ``files`` under ``repo_root`` (see module docstring).

    Reads only ``(repo_root, files)`` — no objectives, answer keys, or hand-written content
    enter the output. ``ref_uri`` carries the source file for source-derived blocks; the
    trailing cartridge block has ``ref_uri=None``. With ``import_statements=True`` the
    per-file imports block carries full ``from <module> import <names>`` statements (symbol
    -> module mapping, the B4 v3 lever) instead of root module names; the default is
    unchanged.
    """
    root = Path(repo_root)
    blocks: list[ContextBlock] = []
    seq = 0
    for rel in files:
        text = (root / rel).read_text(encoding="utf-8", errors="replace")
        fs = extract_source(rel, text)
        tree = ast.parse(text)
        docs = _docstrings(tree)
        for sym in fs.exported:
            doc = docs.get(sym.name)
            content = f"{rel}: {sym.signature}" + (f" — {doc}" if doc else "")
            seq += 1
            blocks.append(
                _block(
                    f"blk_{sha256_hex(f'{rel}:{sym.name}'.encode())[:16]}",
                    BlockKind.fact,
                    content,
                    seq,
                    rel,
                    (sym.name,),
                )
            )
        for name, value in _assignments(tree):
            seq += 1
            blocks.append(
                _block(
                    f"blk_{sha256_hex(f'{rel}:{name}'.encode())[:16]}",
                    BlockKind.fact,
                    f"{rel}: {name} = {value}",
                    seq,
                    rel,
                    (name,),
                )
            )
        imports = _import_statements(tree) if import_statements else _import_roots(tree)
        if imports:
            seq += 1
            blocks.append(
                _block(
                    f"blk_{sha256_hex(f'{rel}:imports'.encode())[:16]}",
                    BlockKind.fact,
                    (
                        f"{rel}: {', '.join(imports)}"
                        if import_statements
                        else f"{rel}: imports {', '.join(imports)}"
                    ),
                    seq,
                    rel,
                    (),
                )
            )

    cartridge = build_project_cartridge(root, files)
    seq += 1
    blocks.append(
        _block("blk_cartridge", BlockKind.artifact, cartridge.hologram_markdown, seq, None, ())
    )
    return blocks


def build_project_cartridge(repo_root: str | Path, files: Sequence[str]) -> Cartridge:
    """Build the project ``.cfx`` cartridge (Tier-0 hologram) for ``files``.

    Extracted from :func:`build_blocks` so a caller can pin the same hologram as a Tier-0
    shared prefix across roles (exploration lever L1). Reads only ``(repo_root, files)`` and
    produces the byte-identical cartridge :func:`build_blocks` embeds as its artifact block.
    """
    root = Path(repo_root)
    symbols: list[FileSymbols] = []
    hashes: list[str] = []
    for rel in files:
        text = (root / rel).read_text(encoding="utf-8", errors="replace")
        symbols.append(extract_source(rel, text))
        hashes.append(f"{rel} {sha256_hex((root / rel).read_bytes())}")
    commit = sha256_hex("\n".join(hashes).encode("utf-8"))
    return build_cartridge(symbols, tenant_id="default", project_id="httpx-b4", commit_hash=commit)


def _rank_symbols(
    root: Path, files: Sequence[str], query: str
) -> list[tuple[float, str, str, str]]:
    """Top-level symbols ranked by lexical relevance to ``query``, with full source bodies.

    Returns ``(score, rel, name, source_segment)`` descending by score, ties broken by
    ``(rel, name)``. Score is the fraction of query tokens present in the symbol's
    signature + first docstring line + body.
    """
    q = set(tokenize(query))
    ranked: list[tuple[float, str, str, str]] = []
    for rel in files:
        text = (root / rel).read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(text)
        docs = _docstrings(tree)
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            segment = ast.get_source_segment(text, node)
            if not segment:
                continue
            doc = docs.get(node.name, "")
            probe = f"{node.name} {doc} {segment}"
            score = len(q & set(tokenize(probe))) / len(q) if q else 0.0
            ranked.append((score, rel, node.name, segment))
    ranked.sort(key=lambda t: (-t[0], t[1], t[2]))
    return ranked


def build_rich_blocks(
    repo_root: str | Path,
    files: Sequence[str],
    query: str,
    *,
    top_k: int = 8,
    body_budget_tokens: int = 1200,
) -> list[ContextBlock]:
    """Exploration lever L2: richer mechanical fabric, query-aware.

    Same base as :func:`build_blocks` (signatures + first docstring lines + assignments +
    cartridge) with two enrichments, both behind this function (``build_blocks`` itself is
    unchanged):

    * the per-file imports block carries full ``from <module> import <names>`` statements
      instead of root names, so a symbol's originating module is recoverable;
    * the top-``top_k`` symbols most relevant to ``query`` get a full source-body ``fact``
      block, added while ``body_budget_tokens`` lasts.

    Unlike :func:`build_blocks`, this reads ``query`` (the objective) by design.
    """
    root = Path(repo_root)
    blocks: list[ContextBlock] = []
    seq = 0
    for rel in files:
        text = (root / rel).read_text(encoding="utf-8", errors="replace")
        fs = extract_source(rel, text)
        tree = ast.parse(text)
        docs = _docstrings(tree)
        for sym in fs.exported:
            doc = docs.get(sym.name)
            content = f"{rel}: {sym.signature}" + (f" — {doc}" if doc else "")
            seq += 1
            blocks.append(
                _block(
                    f"blk_{sha256_hex(f'{rel}:{sym.name}'.encode())[:16]}",
                    BlockKind.fact,
                    content,
                    seq,
                    rel,
                    (sym.name,),
                )
            )
        for name, value in _assignments(tree):
            seq += 1
            blocks.append(
                _block(
                    f"blk_{sha256_hex(f'{rel}:{name}'.encode())[:16]}",
                    BlockKind.fact,
                    f"{rel}: {name} = {value}",
                    seq,
                    rel,
                    (name,),
                )
            )
        imports = _import_statements(tree)
        if imports:
            seq += 1
            blocks.append(
                _block(
                    f"blk_{sha256_hex(f'{rel}:imports'.encode())[:16]}",
                    BlockKind.fact,
                    f"{rel}: {', '.join(imports)}",
                    seq,
                    rel,
                    (),
                )
            )

    spent = 0
    for score, rel, name, segment in _rank_symbols(root, files, query)[:top_k]:
        if score <= 0.0:
            continue
        content = f"{rel}: {name} (body)\n{segment}"
        if spent + count_tokens(content) > body_budget_tokens:
            continue
        spent += count_tokens(content)
        seq += 1
        blocks.append(
            _block(
                f"blk_{sha256_hex(f'{rel}:{name}:body'.encode())[:16]}",
                BlockKind.fact,
                content,
                seq,
                rel,
                (name,),
            )
        )

    cartridge = build_project_cartridge(root, files)
    seq += 1
    blocks.append(
        _block("blk_cartridge", BlockKind.artifact, cartridge.hologram_markdown, seq, None, ())
    )
    return blocks
