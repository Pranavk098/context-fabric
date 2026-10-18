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
from kernel.cartridge.serializer import build_cartridge, count_tokens
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


def build_blocks(repo_root: str | Path, files: Sequence[str]) -> list[ContextBlock]:
    """Build mechanical memory blocks for ``files`` under ``repo_root`` (see module docstring).

    Reads only ``(repo_root, files)`` — no objectives, answer keys, or hand-written content
    enter the output. ``ref_uri`` carries the source file for source-derived blocks; the
    trailing cartridge block has ``ref_uri=None``.
    """
    root = Path(repo_root)
    blocks: list[ContextBlock] = []
    symbols_per_file: list[FileSymbols] = []
    seq = 0
    for rel in files:
        text = (root / rel).read_text(encoding="utf-8", errors="replace")
        fs = extract_source(rel, text)
        symbols_per_file.append(fs)
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
        imports = _import_roots(tree)
        if imports:
            seq += 1
            blocks.append(
                _block(
                    f"blk_{sha256_hex(f'{rel}:imports'.encode())[:16]}",
                    BlockKind.fact,
                    f"{rel}: imports {', '.join(imports)}",
                    seq,
                    rel,
                    (),
                )
            )

    commit = sha256_hex(
        "\n".join(f"{rel} {sha256_hex((root / rel).read_bytes())}" for rel in files).encode("utf-8")
    )
    cartridge = build_cartridge(
        symbols_per_file, tenant_id="default", project_id="httpx-b4", commit_hash=commit
    )
    seq += 1
    blocks.append(
        _block("blk_cartridge", BlockKind.artifact, cartridge.hologram_markdown, seq, None, ())
    )
    return blocks
