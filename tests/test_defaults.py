"""Shipped on/off defaults, i.e. what a user gets before running ``cfxd set``.

Changing a default is a product decision: update this table, the docstring in
``cfxd/config.py`` and the defaults sentence in ``docs/TESTING-GUIDE.md`` together.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cfxd.config import read_config

# brief, brief_policy and carry are on so the alpha produces data; fold/fold_read are off
# (2026-10-24 A/Bs); brief_v1 and carry_back are opt-in until the v4 pilot.
_EXPECTED = {
    "brief": True,
    "brief_policy": True,
    "carry": True,
    "fold": False,
    "fold_read": False,
    "bound": False,
    "coach": False,
    "coach_control": True,
    "idle_notice": False,
    "brief_v1": False,
    "carry_back": False,
    "carry_v2": False,
    "brief_excerpts": False,
}


def test_every_shipped_default_is_exact(tmp_path: Path) -> None:
    # No config.json in tmp_path, so this is the shipped default (not user state).
    assert read_config(tmp_path) == _EXPECTED


def test_carry_v2_defaults_off_once_the_flag_lands(tmp_path: Path) -> None:
    config = read_config(tmp_path)
    if "carry_v2" not in config:
        pytest.skip("carry_v2 arrives with M-1; this goes strict when the flag exists")
    assert config["carry_v2"] is False
