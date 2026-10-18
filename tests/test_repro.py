"""``cfx repro`` — the offline reproduction entry point (no GPU, no network)."""

from __future__ import annotations

from sidecar.repro import run_repro


def test_repro_passes_on_the_real_tree() -> None:
    lines: list[str] = []
    assert run_repro(out=lines.append) == 0
    assert any("cfx repro: OK" in line for line in lines)


def test_repro_names_the_live_engine_tier_it_does_not_run() -> None:
    lines: list[str] = []
    run_repro(out=lines.append)
    assert any("live-engine tier" in line for line in lines)
    assert any("cfx demo --scenario fleet" in line for line in lines)
