"""Offline tests for the model-based skimmer assembler (``kernel/router/skim_model.py``).

No torch, no GPU: a stub scorer drives ``ModelSkimmer`` so the chunking, knapsack, and
source-order emission are exercised without any model. Structural-preservation and budget
respect are asserted on the same surface the eval uses.
"""

from __future__ import annotations

from pathlib import Path

from kernel.cartridge.serializer import count_tokens
from kernel.router.skim_model import ModelSkimmer, _knapsack_select


def _stub(objective: str, blocks: list[tuple[str, tuple[str, ...]]]) -> list[float]:
    """1.0 for a block sharing any objective word, else 0.1."""
    terms = {w for w in objective.lower().split() if len(w) >= 2}
    return [1.0 if terms & set("\n".join(lines).lower().split()) else 0.1 for _rel, lines in blocks]


def _write_area_module(root: Path) -> None:
    (root / "m.py").write_text(
        "import math as _m\n"
        "import zlib\n\n"
        "RED = 10\n"
        "BLUE = 20\n\n"
        "def area(radius):\n"
        "    # circle area\n"
        "    return _m.pi * radius * radius\n"
        "def color():\n"
        "    return RED\n",
        encoding="utf-8",
    )


def test_knapsack_selects_unique_optimum() -> None:
    values = [6.0, 5.0, 5.0, 3.0]
    weights = [4, 3, 3, 2]
    chosen = _knapsack_select(values, weights, 6)
    assert sum(weights[i] for i in chosen) <= 6
    assert sum(values[i] for i in chosen) == 10.0
    assert set(chosen) == {1, 2}


def test_knapsack_shifts_negative_values() -> None:
    chosen = _knapsack_select([-1.0, 3.0], [1, 2], 2)
    assert set(chosen) == {1}


def test_knapsack_no_fit_is_empty() -> None:
    assert _knapsack_select([1.0], [10], 5) == []


def test_model_skim_respects_budget_and_is_deterministic(tmp_path: Path) -> None:
    _write_area_module(tmp_path)
    sk = ModelSkimmer(tmp_path, ["m.py"], scorer=_stub)
    a = sk.skim("pick the red value", budget=200)
    b = sk.skim("pick the red value", budget=200)
    assert a == b
    for budget in (1, 10, 50, 200, 1000):
        out = sk.skim("pick the red value", budget=budget)
        assert count_tokens(out) <= budget


def test_model_skim_keeps_signature_and_return_lines(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text("def f(x):\n    return x + 1\n", encoding="utf-8")
    out = ModelSkimmer(tmp_path, ["m.py"], scorer=_stub).skim("add one", budget=1000)
    assert "def f(x):" in out  # signature always kept
    assert "return x + 1" in out  # return line always kept


def test_model_skim_import_binding_rule(tmp_path: Path) -> None:
    _write_area_module(tmp_path)
    out = ModelSkimmer(tmp_path, ["m.py"], scorer=_stub).skim("pick the red value", budget=1000)
    assert "def color():" in out
    assert "return RED" in out
    assert "RED = 10" in out  # module constant visible + bound to the query
    assert "import zlib" not in out  # unbound import dropped


def test_model_skim_empty_budget_is_empty(tmp_path: Path) -> None:
    _write_area_module(tmp_path)
    assert ModelSkimmer(tmp_path, ["m.py"], scorer=_stub).skim("red", budget=0) == ""
