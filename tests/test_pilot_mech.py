"""Offline tests for the pilot's mechanical-fabric mode (no model, no network).

Asserts the hand mode is unchanged (the 11 frozen hand-derived blocks are what the store
holds), that the mech mode is literally ``mech_fabric.build_blocks`` over the same 10-file
bundle (an ingester that never reads objectives or answer keys), and that the dry-run is
deterministic and reports a per-objective recall diagnostic. See
``docs/engineering/PILOT-MECH-PREREGISTRATION.md``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.mech_fabric import build_blocks
from evals.pilot import (
    _FACT_BLOCKS,
    BUNDLE_FILES,
    build_mech_store,
    build_pilot_store,
    dry_run,
    pilot_blocks,
    pilot_workload,
    render_bundle,
    verify_raw_answerable,
)
from kernel.store import SqliteStore


def _synthetic_httpx(root: Path) -> None:
    """Ten minimal httpx files (matching BUNDLE_FILES) that carry the 6 answer keys."""
    pkg = root / "httpx"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("from ._api import get\n", encoding="utf-8")
    (pkg / "__version__.py").write_text("__version__ = '0.28.1'\n", encoding="utf-8")
    (pkg / "_api.py").write_text(
        "def request(method, url):\n"
        '    """Send an HTTP request."""\n'
        "    return None\n"
        "\n"
        "def get(url):\n"
        '    """Perform a GET request."""\n'
        "    return request('GET', url)\n",
        encoding="utf-8",
    )
    (pkg / "_auth.py").write_text("class Auth:\n    pass\n", encoding="utf-8")
    (pkg / "_client.py").write_text(
        "class Client:\n"
        '    """Synchronous client."""\n'
        "    pass\n"
        "\n"
        "class AsyncClient:\n"
        '    """Async client."""\n'
        "    pass\n",
        encoding="utf-8",
    )
    (pkg / "_config.py").write_text(
        "DEFAULT_LIMITS = Limits(max_connections=100)\n", encoding="utf-8"
    )
    (pkg / "_content.py").write_text("class ByteStream:\n    pass\n", encoding="utf-8")
    (pkg / "_decoders.py").write_text(
        'class GZipDecoder:\n    """Decodes gzip."""\n    pass\n',
        encoding="utf-8",
    )
    (pkg / "_exceptions.py").write_text(
        "class TooManyRedirects(Exception):\n    pass\n", encoding="utf-8"
    )
    (pkg / "_main.py").write_text(
        "import click\n\n@click.command()\ndef main():\n    pass\n", encoding="utf-8"
    )


def test_hand_store_contents_unchanged(tmp_path: Path) -> None:
    """Hand mode still writes the 11 frozen hand-derived blocks, byte-for-byte."""
    store_path = build_pilot_store(tmp_path / "pilot.db")
    blocks = tuple(SqliteStore(store_path).load_blocks())
    assert [b.block_id for b in blocks] == [block_id for block_id, _ in _FACT_BLOCKS]
    assert [b.content for b in blocks] == [content for _, content in _FACT_BLOCKS]
    assert [b.source_event_seq for b in blocks] == list(range(1, len(_FACT_BLOCKS) + 1))
    assert all(b.block_kind.value == "fact" for b in blocks)
    assert all(b.lifecycle_status.value == "routable" for b in blocks)
    assert all(b.trust_domain == "default" for b in blocks)
    assert all(b.creator.id == "pilot-fixture" for b in blocks)


def test_mech_pilot_blocks_are_the_mechanical_ingester(tmp_path: Path) -> None:
    """Mech mode is ``build_blocks`` over the same 10-file bundle — nothing else."""
    _synthetic_httpx(tmp_path)
    assert pilot_blocks(tmp_path, "mech") == tuple(build_blocks(tmp_path, BUNDLE_FILES))


def test_mech_pilot_blocks_never_read_objectives(tmp_path: Path) -> None:
    """The ingester's only inputs are (repo_root, files): no objective text enters."""
    _synthetic_httpx(tmp_path)
    blocks = pilot_blocks(tmp_path, "mech")
    text = "\n".join(b.content or "" for b in blocks).lower()
    for spec in pilot_workload():
        assert spec.objective.lower() not in text
    # every block is derived from one of the 10 files or the project-wide cartridge
    assert {b.ref_uri for b in blocks} <= set(BUNDLE_FILES) | {None}


def test_verify_raw_answerable_only_checks_the_raw_bundle(tmp_path: Path) -> None:
    _synthetic_httpx(tmp_path)
    rendered, _ = render_bundle(tmp_path)
    verify_raw_answerable(rendered, pilot_workload())  # must not raise
    with pytest.raises(ValueError, match="missing from raw bundle"):
        verify_raw_answerable("no keys here", pilot_workload())


def test_mech_store_matches_pilot_blocks(tmp_path: Path) -> None:
    """The mech store path persists exactly the mechanical blocks."""
    _synthetic_httpx(tmp_path)
    store_path = build_mech_store(tmp_path / "pilot.db", tmp_path)
    assert tuple(SqliteStore(store_path).load_blocks()) == pilot_blocks(tmp_path, "mech")


def test_dry_run_is_deterministic_and_reports_recall(tmp_path: Path) -> None:
    _synthetic_httpx(tmp_path)
    first = dry_run(tmp_path, fabric="mech")
    second = dry_run(tmp_path, fabric="mech")
    assert first == second
    assert {row["role"] for row in first["recall"]} == {s.role for s in pilot_workload()}
    for row in first["recall"]:
        assert set(row["facts_in_packet"]) == set(row["required"])
        assert isinstance(row["all_in_packet"], bool)
    assert first["arms"]["raw"]["prompt_tokens_total"] > 0
    assert first["arms"]["compile"]["prompt_tokens_total"] > 0


def test_all_error_run_fails_every_criterion() -> None:
    """Regression: a run where every request errored once printed PASS for (a) and (b)."""
    from evals.pilot import _criteria

    arm = {"prompt_tokens_total": 0, "passed": 0}
    records = [
        {"arm": a, "error": "502 Bad Gateway", "compiled": False} for a in ("raw", "compile")
    ]
    verdict = _criteria({"raw": arm, "compile": arm}, records, {"compile": {"errors": 1}}, 6)
    assert not verdict["a_token_reduction"]["pass"]
    assert not verdict["b_accuracy_parity"]["pass"]
    assert not verdict["c_no_compile_errors"]["pass"]
