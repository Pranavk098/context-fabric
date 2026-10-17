"""Deterministic symbol extractor (stdlib ``ast``; zero dependencies).

Extracts exported symbols (classes/functions with signatures) and referenced names from
each source file. Deterministic: outputs are sorted. Non-Python files are ignored (a
tree-sitter backend can be dropped in behind the same interface later).
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: str  # class | function | async_function
    signature: str
    lineno: int


@dataclass(frozen=True)
class FileSymbols:
    path: str
    exported: tuple[Symbol, ...]
    referenced: tuple[str, ...]


def _func_sig(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    kind = "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"
    prefix = "async def" if kind == "async_function" else "def"
    args = ", ".join(a.arg for a in node.args.args)
    ret = ""
    if node.returns is not None:
        try:
            ret = " -> " + ast.unparse(node.returns)
        except Exception:
            ret = ""
    return f"{prefix} {node.name}({args}){ret}"


def extract_source(path: str, source: str) -> FileSymbols:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return FileSymbols(path=path, exported=(), referenced=())

    exported: list[Symbol] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            exported.append(Symbol(node.name, "function", _func_sig(node), node.lineno))
        elif isinstance(node, ast.ClassDef):
            methods = ", ".join(
                b.name for b in node.body if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
            sig = f"class {node.name}" + (f": {methods}" if methods else "")
            exported.append(Symbol(node.name, "class", sig, node.lineno))

    referenced: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name):
            referenced.add(n.id)
        elif isinstance(n, ast.Import):
            referenced.update(alias.name.split(".")[0] for alias in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            referenced.add(n.module.split(".")[0])
        elif isinstance(n, ast.Attribute):
            base: ast.expr = n
            while isinstance(base, ast.Attribute):
                base = base.value
            if isinstance(base, ast.Name):
                referenced.add(base.id)

    exported.sort(key=lambda s: (s.lineno, s.name))
    return FileSymbols(path=path, exported=tuple(exported), referenced=tuple(sorted(referenced)))


def extract_tree(
    root: str | Path, ignore: Sequence[str] = ("__pycache__", ".venv", "tests")
) -> tuple[FileSymbols, ...]:
    root_path = Path(root)
    out: list[FileSymbols] = []
    for f in sorted(root_path.rglob("*.py")):
        if any(part in ignore for part in f.parts):
            continue
        rel = f.relative_to(root_path).as_posix()
        out.append(extract_source(rel, f.read_text(encoding="utf-8", errors="replace")))
    return tuple(out)


if __name__ == "__main__":
    fs = extract_source(
        "m.py",
        "import os\nclass A:\n    def m(self): ...\ndef f(x, y) -> int:\n    return os.getpid()\n",
    )
    names = [s.name for s in fs.exported]
    assert names == ["A", "f"], names
    assert "os" in fs.referenced
    assert fs.exported[1].signature == "def f(x, y) -> int"
    print("ast_extractor.py self-check: OK")
