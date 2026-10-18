"""Guard against silently drifting headline numbers in the docs.

Reads the fenced ```json block in ``docs/engineering/NUMBERS.md`` (the canonical registry,
one source for humans and this checker) and verifies, per entry:

* ``artifact``: the listed file exists under the repo root;
* ``recompute``: ``a``/``b`` name dotted paths into that artifact's JSON; ``expr`` is one
  of ``a/b``, ``a-b``, ``a+b`` (hand-rolled, no ``eval()``) and
  ``round(value, round) == expect``;
* ``must_appear_in``: the ``display`` string occurs in every listed doc after normalizing
  ``×``/``x`` and Unicode minus/ASCII minus;
* ``baseline``: every ``strict`` entry must name the comparison it is measured against, so a
  headline number can never ship without its strongest baseline.

``stale_patterns`` must not occur in their scoped docs (or, unscoped, anywhere under the
repo except docs listed in ``historical_ok`` and the registry itself).

Exit status 0 when clean, 1 with a report of every failed check. ``--verbose`` prints all.
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "docs" / "engineering" / "NUMBERS.md"
_JSON_BLOCK = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)
_SKIP_DIRS = frozenset(
    {".git", ".venv", ".mypy_cache", ".pytest_cache", ".ruff_cache", "node_modules", "__pycache__"}
)

# Capability phrases the docs may only assert when the named code symbol (and optionally
# test) exists. Keep the list short and explicit; the guard fails when a phrase is found
# in the docs but its symbol/test is missing.
CAPABILITY_REGISTRY: list[dict[str, Any]] = [
    {
        "phrase": "lambda-MMR",
        "symbol": "kernel.router.packer:_mmr_order",
        "test": "tests.test_packer:test_mmr_lowers_redundancy_at_equal_utility",
    },
    {
        "phrase": "purpose compartments",
        "symbol": "kernel.router.policy_engine:apply_policy",
        "test": "tests.test_policy_engine:test_matching_purpose_is_allowed",
    },
    {
        "phrase": "prefix sharing",
        "symbol": "adapters.raw_llm:shared_prefix",
        "test": "tests.test_adapters",
    },
    {
        "phrase": "signable",
        "symbol": "protocol.signing:Signer",
        "test": "tests.test_signing:test_sign_and_verify_roundtrip",
    },
    {
        "phrase": "Copy-on-Write",
        "symbol": "kernel.mmu.cow_forker:fork_packet",
        "test": "tests.test_mmu",
    },
]


def normalize(text: str) -> str:
    """Compare '×'/'x' and Unicode/en-dash minus variants alike."""
    return text.replace("\u00d7", "x").replace("\u2212", "-").replace("\u2013", "-")


def dig(data: Any, path: str) -> Any:
    """Resolve a dotted path (list indices allowed) into artifact JSON."""
    value = data
    for part in path.split("."):
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def evaluate(recompute: dict[str, Any], artifact: Any) -> float:
    """Tiny safe evaluator over two named artifact fields. No ``eval()``."""
    expr = recompute["expr"]
    a = dig(artifact, recompute["a"])
    b = dig(artifact, recompute["b"])
    if expr == "a/b":
        value = a / b
    elif expr == "a-b":
        value = a - b
    elif expr == "a+b":
        value = a + b
    else:
        raise ValueError(f"unsupported expression {expr!r}: use a/b, a-b or a+b")
    return round(value, int(recompute.get("round", 2)))


def load_registry(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    match = _JSON_BLOCK.search(text)
    if match is None:
        raise ValueError(f"{path}: no fenced ```json block found")
    registry = json.loads(match.group(1))
    if not isinstance(registry, dict):
        raise ValueError(f"{path}: json block must be an object")
    return registry


def check_entry(entry: dict[str, Any], root: Path, report: list[tuple[bool, str]]) -> None:
    eid = entry["id"]
    if entry.get("kind") == "strict" and not entry.get("baseline"):
        report.append((False, f"{eid}: strict entry must name its baseline"))
    artifact = entry.get("artifact")
    if artifact:
        path = root / artifact
        if not path.is_file():
            report.append((False, f"{eid}: artifact missing: {artifact}"))
        elif "recompute" in entry:
            try:
                value = evaluate(entry["recompute"], json.loads(path.read_text(encoding="utf-8")))
            except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError) as exc:
                report.append((False, f"{eid}: recompute failed: {exc!r}"))
            else:
                expect = entry["recompute"]["expect"]
                expr = entry["recompute"]["expr"]
                report.append(
                    (value == expect, f"{eid}: recompute {expr} = {value} (expect {expect})")
                )
        else:
            report.append((True, f"{eid}: artifact present ({artifact})"))
    display = normalize(entry["display"])
    for doc in entry.get("must_appear_in", []):
        path = root / doc
        if not path.is_file():
            report.append((False, f"{eid}: doc missing: {doc}"))
        elif display in normalize(path.read_text(encoding="utf-8")):
            report.append((True, f"{eid}: display {entry['display']!r} present in {doc}"))
        else:
            report.append((False, f"{eid}: display {entry['display']!r} NOT FOUND in {doc}"))


def _all_docs(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*.md")
        if not _SKIP_DIRS.intersection(path.relative_to(root).parts)
    )


def check_stale(
    registry: dict[str, Any], root: Path, registry_path: Path, report: list[tuple[bool, str]]
) -> None:
    historical = {Path(str(path)).as_posix() for path in registry.get("historical_ok", [])}
    for item in registry.get("stale_patterns", []):
        pattern = normalize(item["pattern"])
        scoped = item.get("docs")
        candidates = [root / doc for doc in scoped] if scoped else _all_docs(root)
        for path in candidates:
            rel = path.relative_to(root).as_posix()
            if rel in historical or path.resolve() == registry_path.resolve():
                continue
            if not path.is_file():
                report.append((False, f"stale {pattern!r}: scoped doc missing: {rel}"))
                continue
            found = pattern in normalize(path.read_text(encoding="utf-8"))
            report.append((not found, f"stale pattern {pattern!r} absent from {rel}"))


def _symbol_exists(spec: str) -> bool:
    """Import ``module.path:name`` and verify ``name`` exists."""
    if ":" not in spec:
        module_name, attr = spec, None
    else:
        module_name, attr = spec.split(":", 1)
    try:
        module = importlib.import_module(module_name)
    except Exception:
        return False
    if attr is None:
        return True
    return getattr(module, attr, None) is not None


def _check_capabilities(root: Path, report: list[tuple[bool, str]]) -> None:
    docs = _all_docs(root)
    for item in CAPABILITY_REGISTRY:
        phrase = item["phrase"]
        symbol = item.get("symbol")
        test = item.get("test")
        found_in: list[str] = []
        for path in docs:
            if phrase in normalize(path.read_text(encoding="utf-8")):
                found_in.append(path.relative_to(root).as_posix())
        if not found_in:
            continue
        symbol_ok = symbol is None or _symbol_exists(symbol)
        test_ok = test is None or _symbol_exists(test)
        if symbol_ok and test_ok:
            report.append(
                (True, f"capability {phrase!r} backed by {symbol or test} in {', '.join(found_in)}")
            )
        else:
            missing = []
            if not symbol_ok:
                missing.append(f"symbol {symbol}")
            if not test_ok:
                missing.append(f"test {test}")
            where = ", ".join(found_in)
            what = ", ".join(missing)
            report.append((False, f"capability {phrase!r} claimed in {where} but missing {what}"))


def run(registry_path: Path = REGISTRY, root: Path = ROOT) -> list[tuple[bool, str]]:
    """Return every check as ``(ok, message)``; failures are the ``ok=False`` entries."""
    report: list[tuple[bool, str]] = []
    try:
        registry = load_registry(registry_path)
    except (OSError, ValueError) as exc:
        return [(False, f"registry unreadable: {exc}")]
    for entry in registry.get("entries", []):
        check_entry(entry, root, report)
    check_stale(registry, root, registry_path, report)
    _check_capabilities(root, report)
    return report


def _safe(text: str) -> str:
    """ASCII-escape messages so any Windows console encoding can print them."""
    return text.encode("ascii", "backslashreplace").decode("ascii")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify docs headline numbers against NUMBERS.md.")
    parser.add_argument("--verbose", "-v", action="store_true", help="print every check")
    args = parser.parse_args(argv)
    report = run()
    for ok, message in report:
        if ok and not args.verbose:
            continue
        print(_safe(f"{'ok  ' if ok else 'FAIL'} {message}"))
    failures = [message for ok, message in report if not ok]
    if failures:
        print(f"docs numbers: FAIL - {len(failures)} of {len(report)} checks failed")
        return 1
    print(f"docs numbers: OK - {len(report)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
