"""Brief v1 selection (ADR-0011, WP B1): target model, budgets, and relevance ranking.

``select`` ranks the parent's working set against the delegation prompt. It may read
working-set files from disk to find definitions and imports (Python via ``ast``, never
imported or executed; other languages via line regex) but never copies file text into the
``Selection``. The prompt is untrusted: it is only ever matched against, never echoed
except as bounded, sanitised terms.
"""

from __future__ import annotations

import ast
import os
import posixpath
import re
from collections.abc import Mapping
from typing import Any

from cfxd.brief import _cap, _sanitize
from cfxd.brief_types import (
    REASONS,
    SECTIONS,
    Budgets,
    FileItem,
    Selection,
    TargetModel,
)
from cfxd.session_state import FileFact, SessionFacts

BUDGET_BYTES: dict[str, int] = {
    "haiku": 6_000,
    "sonnet": 12_000,
    "opus": 20_000,
    "fable": 20_000,
    "unknown": 4_000,
}
FIXED_MAX_BYTES = 800

# max_files, max_excerpts, max_folds, max_commands
_CAPS: dict[str, tuple[int, int, int, int]] = {
    "haiku": (8, 3, 3, 3),
    "sonnet": (14, 5, 5, 5),
    "opus": (20, 8, 8, 5),
    "fable": (20, 8, 8, 5),
    "unknown": (6, 2, 2, 3),
}
_SHARES: dict[str, int] = {
    "task": 6,
    "constraints": 12,
    "decisions": 8,
    "tried": 6,
    "files": 10,
    "excerpts": 48,
    "folds": 5,
    "commands": 5,
}
_FAMILIES = ("haiku", "sonnet", "opus", "fable")
_RAW_MAX_BYTES = 64

# Bounds for adversarial input and disk work.
_PROMPT_MAX_CHARS = 100_000
_TERM_MAX_BYTES = 200
_MAX_PATH_TERMS = 64
_MAX_MODULE_TERMS = 64
_MAX_SYMBOL_TERMS = 64
_MAX_FACTS = 400
_FILE_READ_MAX_BYTES = 1_000_000
_TOTAL_READ_MAX_BYTES = 4_000_000
_MAX_IMPORT_NAMES = 200

_SOURCE_EXTS = (
    ".py",
    ".pyi",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".c",
    ".h",
    ".cc",
    ".cpp",
    ".hpp",
    ".cs",
    ".rb",
    ".php",
    ".swift",
    ".sh",
    ".md",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".txt",
    ".cfg",
    ".ini",
    ".sql",
    ".html",
    ".css",
)
_JS_EXTS = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")

_TOKEN_STRIP = "\"'`()[]{}<>,;!?"
_LINE_SUFFIX_RE = re.compile(r"(?::\d+){1,2}$")
_DOTTED_RE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+$")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,63}")
_BACKTICK_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]{2,63})`")
_CAPS_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")
_INNER_CAP_RE = re.compile(r"[a-z0-9][A-Z]")
_DEF_RE = re.compile(
    r"\b(?:def|class|function|func|fn|struct|enum|interface|trait|type|const|let|var)"
    r"\s+([A-Za-z_]\w{0,63})"
)
_JS_IMPORT_RE = re.compile(
    r"""(?:\bfrom\s*|\brequire\s*\(\s*|\bimport\s*\(?\s*)['"](\.{1,2}/[^'"\n]{1,200})['"]"""
)


def normalize_model(value: object) -> str:
    """Map a model alias, full id or provider-prefixed id to a family; else ``"unknown"``."""
    if not isinstance(value, str):
        return "unknown"
    text = value.strip().lower()
    if not text:
        return "unknown"
    bracket = text.find("[")
    if bracket != -1:
        text = text[:bracket]
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    for prefix in ("us.", "eu.", "apac.", "global."):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    if text.startswith("anthropic."):
        text = text[len("anthropic.") :]
    if text.startswith("claude-"):
        text = text[len("claude-") :]
    for token in re.split(r"[-._:@]", text):
        if token in _FAMILIES:
            return token
    return "unknown"


def resolve_target(tool_input: Mapping[str, Any]) -> TargetModel:
    """Read the child's model from ``model``, then ``subagent_type``, else the default."""
    model = tool_input.get("model") if isinstance(tool_input, Mapping) else None
    subagent = tool_input.get("subagent_type") if isinstance(tool_input, Mapping) else None
    for source, value in (("model", model), ("subagent_type", subagent)):
        family = normalize_model(value)
        if family != "unknown":
            return _target(family, value, source)
    return _target("unknown", model, "default")


def _target(family: str, raw: object, source: str) -> TargetModel:
    clean = _cap(_sanitize(raw), _RAW_MAX_BYTES) if isinstance(raw, str) else None
    return TargetModel(family=family, raw=clean, source=source, budget_bytes=BUDGET_BYTES[family])


def budgets_for(family: str) -> Budgets:
    """Total, per-section byte ceilings and item caps for a family (unknown -> ``unknown``)."""
    if family not in BUDGET_BYTES:
        family = "unknown"
    total = BUDGET_BYTES[family]
    avail = total - FIXED_MAX_BYTES
    max_files, max_excerpts, max_folds, max_commands = _CAPS[family]
    return Budgets(
        total=total,
        sections=tuple((name, avail * _SHARES[name] // 100) for name in SECTIONS),
        max_files=max_files,
        max_excerpts=max_excerpts,
        max_folds=max_folds,
        max_commands=max_commands,
    )


# --------------------------------------------------------------------------- prompt terms


def _key(path: str) -> str:
    """Comparison key: ``/``-separated and ``normcase``d."""
    return os.path.normcase(path).replace("\\", "/")


def _has_ext(token: str) -> bool:
    return token.lower().endswith(_SOURCE_EXTS)


def _prompt_terms(prompt: str) -> tuple[list[str], list[str], list[str]]:
    """(paths, modules, symbols) in first-appearance order, bounded and de-duplicated."""
    text = prompt[:_PROMPT_MAX_CHARS]
    paths: dict[str, None] = {}
    modules: dict[str, None] = {}
    symbols: dict[str, None] = {}

    for raw in text.split():
        if len(raw) > _TERM_MAX_BYTES * 2:
            continue
        token = raw.strip(_TOKEN_STRIP).rstrip(".:")
        token = _LINE_SUFFIX_RE.sub("", token)
        token = token.replace("\\", "/")
        while token.startswith("./"):
            token = token[2:]
        if not token or len(token) > _TERM_MAX_BYTES or "://" in token:
            continue
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in token):
            continue
        segments = token.split("/")
        if ".." in segments:
            continue
        if "/" in token or _has_ext(token):
            if _sanitize(token) == token and len(paths) < _MAX_PATH_TERMS:
                paths.setdefault(token, None)
            if not _has_ext(token) and "/" in token and len(modules) < _MAX_MODULE_TERMS:
                modules.setdefault(token, None)
        elif _DOTTED_RE.match(token) and len(modules) < _MAX_MODULE_TERMS:
            modules.setdefault(token, None)

    quoted = set(_BACKTICK_RE.findall(text))
    for match in _IDENT_RE.finditer(text):
        name = match.group(0)
        if len(symbols) >= _MAX_SYMBOL_TERMS:
            break
        if name in symbols:
            continue
        after = text[match.end() : match.end() + 1]
        if (
            name in quoted
            or after == "("
            or _CAPS_RE.match(name)
            or "_" in name
            or _INNER_CAP_RE.search(name)
        ):
            symbols.setdefault(name, None)
    return list(paths), list(modules), list(symbols)


# --------------------------------------------------------------------------- disk analysis


class _Reader:
    """Bounded, deterministic reads of working-set files."""

    def __init__(self) -> None:
        self.used = 0

    def read(self, path: str) -> str | None:
        try:
            size = os.stat(path).st_size
            if size > _FILE_READ_MAX_BYTES or self.used + size > _TOTAL_READ_MAX_BYTES:
                return None
            with open(path, "rb") as fh:
                data = fh.read(_FILE_READ_MAX_BYTES + 1)
        except (OSError, ValueError):
            return None
        if len(data) > _FILE_READ_MAX_BYTES or b"\x00" in data[:8192]:
            return None
        self.used += len(data)
        return data.decode("utf-8", "replace")


def _py_defs(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    body = getattr(tree, "body", [])
    for node in body:
        if isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _parse(text: str) -> ast.AST | None:
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None


def _defs(path: str, text: str, tree: ast.AST | None) -> set[str]:
    if path.lower().endswith((".py", ".pyi")):
        return _py_defs(tree) if tree is not None else set()
    return set(_DEF_RE.findall(text))


def _modkey(key: str) -> str:
    stem = key.rsplit(".", 1)[0] if "." in posixpath.basename(key) else key
    if posixpath.basename(stem) == "__init__":
        stem = posixpath.dirname(stem)
    return stem


def _imports(key: str, text: str, tree: ast.AST | None) -> list[str]:
    """Candidate module path strings (``a/b/c``); absolute paths for relative imports."""
    out: list[str] = []
    if key.endswith((".py", ".pyi")):
        if tree is None:
            return out
        here = posixpath.dirname(key)
        for node in ast.walk(tree):
            if len(out) >= _MAX_IMPORT_NAMES:
                break
            if isinstance(node, ast.Import):
                out.extend(a.name.replace(".", "/") for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                mod = (node.module or "").replace(".", "/")
                if node.level:
                    base = here
                    for _ in range(node.level - 1):
                        base = posixpath.dirname(base)
                    mod = posixpath.join(base, mod) if mod else base
                    mod = mod.rstrip("/")
                    if node.module:
                        out.append(mod)
                else:
                    out.append(mod)
                for alias in node.names:
                    if alias.name != "*" and mod:
                        out.append(f"{mod}/{alias.name}")
        return out
    if key.endswith(_JS_EXTS):
        here = posixpath.dirname(key)
        for spec in _JS_IMPORT_RE.findall(text)[:_MAX_IMPORT_NAMES]:
            out.append(posixpath.normpath(posixpath.join(here, spec)))
    return out


# --------------------------------------------------------------------------- select


def select(facts: SessionFacts | None, prompt: str, target_model: TargetModel) -> Selection:
    """Rank the working set for a delegation prompt; sized to ``target_model``."""
    family = target_model.family if target_model.family in BUDGET_BYTES else "unknown"
    budgets = budgets_for(family)
    if facts is None:
        return Selection(
            target=target_model,
            budgets=budgets,
            files=(),
            folds=(),
            commands=(),
            todos=(),
            test=None,
            terms=(),
        )

    paths, modules, symbols = _prompt_terms(prompt if isinstance(prompt, str) else "")
    items = _rank(_dedupe(facts.files), paths, modules, symbols, budgets)
    terms = tuple(sorted({*paths, *modules, *symbols}))
    return Selection(
        target=target_model,
        budgets=budgets,
        files=items,
        folds=tuple(reversed(facts.folds))[: budgets.max_folds],
        commands=tuple(reversed(facts.commands))[: budgets.max_commands],
        todos=tuple((s, t) for s, t in facts.todos if s.lower() != "completed"),
        test=facts.test,
        terms=terms,
    )


def _dedupe(files: tuple[FileFact, ...]) -> list[FileFact]:
    best: dict[str, FileFact] = {}
    for fact in files:
        key = _key(fact.path)
        old = best.get(key)
        if old is None or (fact.last_seen, fact.path) > (old.last_seen, old.path):
            best[key] = fact
    ordered = sorted(best.values(), key=lambda f: (-f.last_seen, _key(f.path)))
    return ordered[:_MAX_FACTS]


def _rank(
    files: list[FileFact],
    paths: list[str],
    modules: list[str],
    symbols: list[str],
    budgets: Budgets,
) -> tuple[FileItem, ...]:
    if not files:
        return ()
    keys = [_key(f.path) for f in files]
    path_terms = [(p, _key(p)) for p in paths]
    module_terms = [(m, _key(m.replace(".", "/"))) for m in modules]
    sym_set = set(symbols)

    # Read each needed file once, in sorted order so the byte budget is deterministic.
    # Seeds are known only after prompt tiers, so reading for imports happens in a second pass.
    reader = _Reader()
    parsed: dict[int, tuple[str, ast.AST | None] | None] = {}

    def load(i: int) -> tuple[str, ast.AST | None] | None:
        if i not in parsed:
            text = reader.read(files[i].path)
            if text is None:
                parsed[i] = None
            else:
                tree = _parse(text) if keys[i].endswith((".py", ".pyi")) else None
                parsed[i] = (text, tree)
        return parsed[i]

    order = sorted(range(len(files)), key=lambda i: keys[i])
    defs_hit: dict[int, list[str]] = {}
    if sym_set:
        for i in order:
            entry = load(i)
            if entry is not None:
                hit = sorted(sym_set & _defs(keys[i], entry[0], entry[1]))
                if hit:
                    defs_hit[i] = hit

    modkeys = [_modkey(k) for k in keys]
    tier: list[int] = []
    flags: list[dict[str, list[str]]] = []
    for i, fact in enumerate(files):
        key = keys[i]
        base = posixpath.basename(key)
        found: dict[str, list[str]] = {name: [] for name in REASONS}
        for term, pkey in path_terms:
            if "/" in pkey and (key == pkey or key.endswith("/" + pkey)):
                found["prompt_path"].append(term)
            if posixpath.basename(pkey) == base:
                found["prompt_basename"].append(term)
        if i in defs_hit:
            found["prompt_symbol"].extend(defs_hit[i])
        for term, mkey in module_terms:
            if modkeys[i] == mkey or modkeys[i].endswith("/" + mkey):
                found["prompt_module"].append(term)
        found["edited"] = ["edited"] if fact.edited else []
        if found["prompt_path"] or found["prompt_symbol"]:
            tier.append(0)
        elif found["prompt_basename"]:
            tier.append(1)
        elif found["prompt_module"]:
            tier.append(2)
        else:
            tier.append(3)
        flags.append(found)

    seeds = {i for i in range(len(files)) if tier[i] < 3 or files[i].edited}
    neighbour: set[int] = set()
    if seeds:
        suffix_index: dict[str, set[int]] = {}
        for i, mk in enumerate(modkeys):
            parts = mk.split("/")
            for j in range(len(parts)):
                suffix_index.setdefault("/".join(parts[j:]), set()).add(i)
        for i in order:
            entry = load(i)
            if entry is None:
                continue
            for cand in _imports(keys[i], entry[0], entry[1]):
                targets = suffix_index.get(_key(cand), set()) - {i}
                if i in seeds and targets:
                    neighbour.update(targets)
                if targets & seeds:
                    neighbour.add(i)

    ranked = sorted(
        range(len(files)),
        key=lambda i: (
            tier[i],
            0 if files[i].edited else 1,
            0 if i in neighbour else 1,
            -files[i].last_seen,
            keys[i],
        ),
    )[: budgets.max_files]

    out: list[FileItem] = []
    excerpts_left = budgets.max_excerpts
    for i in ranked:
        fact = files[i]
        found = flags[i]
        if i in neighbour:
            found["import_neighbour"] = ["import_neighbour"]
        applied = {name for name in REASONS if found[name]}
        if not applied:
            applied.add("recent")
        matched = sorted(
            {t for name in ("prompt_path", "prompt_basename", "prompt_module") for t in found[name]}
            | set(found["prompt_symbol"])
        )
        ranges = tuple(getattr(fact, "ranges", ()))
        excerpt = bool(ranges) and excerpts_left > 0
        if excerpt:
            excerpts_left -= 1
        out.append(
            FileItem(
                path=fact.path,
                lines=fact.lines,
                edited=fact.edited,
                last_seen=fact.last_seen,
                reasons=tuple(name for name in REASONS if name in applied),
                matched=tuple(matched),
                ranges=ranges,
                excerpt=excerpt,
            )
        )
    return tuple(out)
