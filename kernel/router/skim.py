"""Question-guided hand-off skimmer ("skim") — deterministic, no LLM.

Given a task/objective string and a set of source files, produce a compact hand-off
packet of REAL source lines — verbatim, with ``file:line`` anchors — that fits a token
budget, in three deterministic steps:

1. **coarse** — chunk each file the same way the frozen B4 v3 retrieval arm does
   (``evals.b4_v3.build_ast_chunks``): one module-header chunk (docstring + import
   statements) and one chunk per top-level function/class (full body via
   ``ast.get_source_segment``). Rank chunks by Okapi BM25 against the objective using the
   shared ``kernel.router.recall.tokenize`` contract.
2. **fine** — inside each kept chunk, split the body into contiguous non-blank line blocks
   and keep the top BM25-ranked blocks, always keeping the def/class signature line and the
   import lines that bind names used in the kept lines.
3. **pack** — greedily under the token budget, preserving real source lines verbatim with
   ``===== FILE: <path>:<line> =====`` anchors.

Deterministic (ADR-0003): no RNG, no wall clock, no unordered iteration, stable float
accumulation order. Pure stdlib plus ``kernel.cartridge.serializer.count_tokens``
(tiktoken ``o200k_base``). No LLM, no network.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from kernel.cartridge.serializer import count_tokens
from kernel.router._bm25 import bm25_core
from kernel.router.recall import tokenize

TokenCounter = Callable[[str], int]

_IDENT = re.compile(r"[A-Za-z_]\w*")

_ANCHOR = "===== FILE: {}:{} ====="


@dataclass(frozen=True)
class _Block:
    """One anchored, verbatim source segment ready to render."""

    rel: str
    start_line: int
    lines: tuple[str, ...]

    def render(self) -> str:
        return "\n".join((_ANCHOR.format(self.rel, self.start_line), *self.lines))


@dataclass(frozen=True)
class _Chunk:
    """A coarse candidate: one module header or one top-level function/class."""

    rel: str
    name: str  # "" for the module-header chunk
    start_line: int
    lines: tuple[str, ...]

    @property
    def is_header(self) -> bool:
        return self.name == ""

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@dataclass(frozen=True)
class _Import:
    text: str
    start_line: int
    bound: tuple[str, ...]


@dataclass(frozen=True)
class _FileInfo:
    rel: str
    doc_line: int
    doc_lines: tuple[str, ...]
    imports: tuple[_Import, ...]


def bm25_scores(query: str, docs: Sequence[str]) -> list[float]:
    """Okapi BM25 (k1=1.2, b=0.75) over raw strings, shared tokenizer contract.

    Shares ``kernel.router._bm25.bm25_core`` with ``kernel.router.recall.bm25_scores`` but
    over plain strings (the skim chunks are not ``ContextBlock`` records). Deterministic:
    query terms summed in sorted order.
    """
    scores, _ = bm25_core(sorted(set(tokenize(query))), [tokenize(d) for d in docs])
    return scores


def _module_docstring(tree: ast.Module, source: str) -> tuple[int, tuple[str, ...]]:
    """Verbatim source lines of the module docstring, or ``(1, ())`` when absent."""
    first = tree.body[0] if tree.body else None
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        segment = ast.get_source_segment(source, first)
        if segment:
            return first.lineno, tuple(segment.splitlines())
    return 1, ()


def _imports(tree: ast.Module, source: str) -> tuple[_Import, ...]:
    """Module-level import statements, verbatim, with the names each one binds."""
    out: list[_Import] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            segment = ast.get_source_segment(source, node)
            if not segment:
                continue
            bound = tuple(alias.asname or alias.name.split(".")[0] for alias in node.names)
            out.append(_Import(segment, node.lineno, bound))
        elif isinstance(node, ast.ImportFrom):
            segment = ast.get_source_segment(source, node)
            if not segment:
                continue
            bound = tuple(alias.asname or alias.name for alias in node.names if alias.name != "*")
            out.append(_Import(segment, node.lineno, bound))
    return tuple(out)


def _build(root: Path, files: Sequence[str]) -> tuple[list[_Chunk], dict[str, _FileInfo]]:
    chunks: list[_Chunk] = []
    infos: dict[str, _FileInfo] = {}
    for rel in sorted(files):
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        doc_line, doc_lines = _module_docstring(tree, text)
        imports = _imports(tree, text)
        header_lines = list(doc_lines)
        for imp in imports:
            header_lines.extend(imp.text.splitlines())
        header_start = doc_line if doc_lines else (imports[0].start_line if imports else 1)
        if header_lines:
            chunks.append(_Chunk(rel, "", header_start, tuple(header_lines)))
        infos[rel] = _FileInfo(rel, doc_line, doc_lines, imports)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                segment = ast.get_source_segment(text, node)
                if not segment:
                    continue
                chunks.append(_Chunk(rel, node.name, node.lineno, tuple(segment.splitlines())))
    return chunks, infos


def _rank_chunks(objective: str, chunks: Sequence[_Chunk]) -> list[_Chunk]:
    scores = bm25_scores(objective, [c.text for c in chunks])
    scored = sorted(
        zip(chunks, scores, strict=True),
        key=lambda pair: (-pair[1], pair[0].rel, pair[0].start_line, pair[0].name),
    )
    return [chunk for chunk, _ in scored]


def _split_blocks(rel: str, lines: Sequence[str], start_line: int) -> list[_Block]:
    """Contiguous non-blank runs of ``lines``, anchored at their first line number."""
    blocks: list[_Block] = []
    current: list[str] = []
    current_start = start_line
    for i, line in enumerate(lines):
        if line.strip() == "":
            if current:
                blocks.append(_Block(rel, current_start, tuple(current)))
                current = []
            continue
        if not current:
            current_start = start_line + i
        current.append(line)
    if current:
        blocks.append(_Block(rel, current_start, tuple(current)))
    return blocks


def _header_blocks(info: _FileInfo) -> list[_Block]:
    # Imports are not emitted wholesale here: the binding rule adds only the import lines
    # that bind names used in kept lines (the "always keep" clause of the fine step).
    if info.doc_lines:
        return [_Block(info.rel, info.doc_line, info.doc_lines)]
    return []


def _symbol_blocks(objective: str, chunk: _Chunk) -> list[_Block]:
    lines = chunk.lines
    signature = _Block(chunk.rel, chunk.start_line, (lines[0],))
    body = _split_blocks(chunk.rel, lines[1:], chunk.start_line + 1)
    if not body:
        return [signature]
    scores = bm25_scores(objective, [b.render() for b in body])
    order = sorted(range(len(body)), key=lambda i: (-scores[i], body[i].start_line))
    return [signature, *(body[i] for i in order)]


def _identifiers(lines: Sequence[str]) -> set[str]:
    return {m.group(0) for line in lines for m in _IDENT.finditer(line)}


def skim(
    objective: str,
    files: Sequence[str],
    *,
    root: str | Path,
    budget: int,
    counter: TokenCounter = count_tokens,
) -> str:
    """Build the skimmed hand-off packet (verbatim real lines + anchors) under ``budget``.

    ``files`` are repo-relative source paths (e.g. ``httpx/_config.py``); ``root`` is the
    checkout they resolve against. The returned text never exceeds ``budget`` tokens under
    ``counter``.
    """
    if budget <= 0:
        return ""
    chunks, infos = _build(Path(root), files)
    ranked = _rank_chunks(objective, chunks)

    parts: list[str] = []
    spent = 0
    kept_lines: dict[str, list[str]] = {}
    included: set[tuple[str, int]] = set()

    def add(block: _Block) -> None:
        nonlocal spent
        text = block.render()
        cost = counter(text)
        sep = counter("\n\n") if parts else 0
        if spent + sep + cost > budget:
            return
        parts.append(text)
        spent += sep + cost
        kept_lines.setdefault(block.rel, []).extend(block.lines)
        for j in range(len(block.lines)):
            included.add((block.rel, block.start_line + j))

    for chunk in ranked:
        blocks = (
            _header_blocks(infos[chunk.rel])
            if chunk.is_header
            else _symbol_blocks(objective, chunk)
        )
        for block in blocks:
            add(block)

    # Import-binding rule: always keep import lines that bind names used in kept lines.
    for rel in sorted(infos):
        used = _identifiers(kept_lines.get(rel, []))
        for imp in infos[rel].imports:
            if not (set(imp.bound) & used):
                continue
            if (rel, imp.start_line) in included:
                continue
            add(_Block(rel, imp.start_line, tuple(imp.text.splitlines())))

    return "\n\n".join(parts)


def trim_to_budget(text: str, budget: int, counter: TokenCounter = count_tokens) -> str:
    """Drop trailing lines until ``text`` fits ``budget`` tokens (whole lines only)."""
    if budget <= 0:
        return ""
    if counter(text) <= budget:
        return text
    lines = text.splitlines()
    lo, hi = 0, len(lines)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if counter("\n".join(lines[:mid])) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return "\n".join(lines[:lo])


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "m.py").write_text(
            "import os\n"
            "from ._config import DEFAULT_MAX_REDIRECTS\n\n"
            "def get_max() -> int:\n"
            "    # default redirects\n"
            "    return DEFAULT_MAX_REDIRECTS\n"
            "def unused():\n"
            "    return os.getpid()\n",
            encoding="utf-8",
        )
        out = skim("default max redirects", ["m.py"], root=d, budget=1000)
        assert "===== FILE: m.py:" in out
        assert "DEFAULT_MAX_REDIRECTS" in out  # verbatim line kept
        assert "from ._config import DEFAULT_MAX_REDIRECTS" in out  # import-binding rule
        assert count_tokens(out) <= 1000
        assert skim("default max redirects", ["m.py"], root=d, budget=1000) == out
        # a budget too small for any block yields empty output, never partial lines
        assert skim("default max redirects", ["m.py"], root=d, budget=1) == ""
    print("skim.py self-check: OK")
