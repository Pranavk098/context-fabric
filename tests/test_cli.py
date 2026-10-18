"""``cfx`` CLI tests — no network, no GPU; serve is mocked where needed."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

from kernel.store import SqliteStore
from sidecar.cli import main


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "block_kind": "decision",
        "content": "Use Postgres for canonical storage",
    }
    payload.update(overrides)
    return payload


def _write_block(tmp_path: Path, payload: dict[str, Any]) -> Path:
    path = tmp_path / "block.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_bare_flags_parse_as_serve(monkeypatch: Any) -> None:
    calls: list[dict[str, Any]] = []

    def fake_serve(*, upstream: str, port: int, fabric: Path | None, mode: str) -> None:
        calls.append({"upstream": upstream, "port": port, "fabric": fabric, "mode": mode})

    monkeypatch.setattr("sidecar.cli.serve", fake_serve)
    assert main(["--port", "0", "--mode", "passthrough"]) == 0
    assert calls == [
        {"upstream": "http://127.0.0.1:8000", "port": 0, "fabric": None, "mode": "passthrough"}
    ]


def test_serve_missing_fabric_exits_2(monkeypatch: Any, capsys: Any) -> None:
    def fake_serve(*, upstream: str, port: int, fabric: Path | None, mode: str) -> None:
        raise FileNotFoundError(f"fabric file not found: {fabric}")

    monkeypatch.setattr("sidecar.cli.serve", fake_serve)
    assert main(["serve", "--fabric", "missing.db"]) == 2
    assert "fabric file not found" in capsys.readouterr().err


def test_help_lists_subcommands(capsys: Any) -> None:
    assert main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "serve" in out
    assert "add" in out
    assert "verify" in out


def test_repro_recomputes_offline_claims(capsys: Any) -> None:
    assert main(["repro"]) == 0
    assert "cfx repro: OK" in capsys.readouterr().out


def test_add_writes_draft_by_default(tmp_path: Path, capsys: Any) -> None:
    db = tmp_path / "fabric.db"
    src = _write_block(tmp_path, _payload())
    assert main(["add", "--store", str(db), "--file", str(src)]) == 0
    out = capsys.readouterr().out
    assert "lifecycle_status: draft" in out
    assert "block_id: blk_" in out
    assert "content_hash: sha256:" in out
    (block,) = SqliteStore(db).load_blocks()
    assert block.lifecycle_status.value == "draft"
    assert block.content == "Use Postgres for canonical storage"
    assert block.source_event_seq == 1


def test_add_trusted_writes_routable(tmp_path: Path, capsys: Any) -> None:
    db = tmp_path / "fabric.db"
    src = _write_block(tmp_path, _payload())
    assert main(["add", "--store", str(db), "--file", str(src), "--trusted"]) == 0
    assert "lifecycle_status: routable" in capsys.readouterr().out
    (block,) = SqliteStore(db).load_blocks()
    assert block.lifecycle_status.value == "routable"


def test_add_is_idempotent(tmp_path: Path, capsys: Any) -> None:
    db = tmp_path / "fabric.db"
    src = _write_block(tmp_path, _payload())
    assert main(["add", "--store", str(db), "--file", str(src)]) == 0
    capsys.readouterr()
    assert main(["add", "--store", str(db), "--file", str(src)]) == 0
    assert "already present" in capsys.readouterr().out
    assert SqliteStore(db).count() == 1


def test_add_trusted_on_existing_draft_reports_stored_state(tmp_path: Path, capsys: Any) -> None:
    db = tmp_path / "fabric.db"
    src = _write_block(tmp_path, _payload())
    assert main(["add", "--store", str(db), "--file", str(src)]) == 0
    capsys.readouterr()
    assert main(["add", "--store", str(db), "--file", str(src), "--trusted"]) == 0
    out = capsys.readouterr().out
    assert "already present" in out
    assert "lifecycle_status: draft" in out
    assert SqliteStore(db).load_blocks()[0].lifecycle_status.value == "draft"


def test_add_conflict_exits_1(tmp_path: Path, capsys: Any) -> None:
    db = tmp_path / "fabric.db"
    first = _write_block(tmp_path, _payload(block_id="blk_fixed"))
    assert main(["add", "--store", str(db), "--file", str(first)]) == 0
    second = _write_block(tmp_path, _payload(block_id="blk_fixed", content="something else"))
    assert main(["add", "--store", str(db), "--file", str(second)]) == 1
    assert "blk_fixed" in capsys.readouterr().err


def test_add_invalid_payload_exits_2(tmp_path: Path, capsys: Any) -> None:
    db = tmp_path / "fabric.db"
    src = _write_block(tmp_path, {"block_kind": "decision"})
    assert main(["add", "--store", str(db), "--file", str(src)]) == 2
    assert "exactly one of content or ref_uri" in capsys.readouterr().err


def test_add_empty_content_exits_2(tmp_path: Path, capsys: Any) -> None:
    db = tmp_path / "fabric.db"
    src = _write_block(tmp_path, {"block_kind": "decision", "content": ""})
    assert main(["add", "--store", str(db), "--file", str(src)]) == 2
    assert "non-empty" in capsys.readouterr().err


def test_add_non_finite_confidence_exits_2(tmp_path: Path, capsys: Any) -> None:
    db = tmp_path / "fabric.db"
    src = _write_block(tmp_path, _payload(confidence=float("nan")))
    assert main(["add", "--store", str(db), "--file", str(src)]) == 2
    assert "finite" in capsys.readouterr().err


def test_add_unopenable_store_exits_2(tmp_path: Path, capsys: Any) -> None:
    bad = tmp_path / "fake.db"
    bad.write_text("not a database", encoding="utf-8")
    src = _write_block(tmp_path, _payload())
    assert main(["add", "--store", str(bad), "--file", str(src)]) == 2
    assert "cannot open store" in capsys.readouterr().err


def test_add_reads_stdin(tmp_path: Path, monkeypatch: Any) -> None:
    db = tmp_path / "fabric.db"
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_payload())))
    assert main(["add", "--store", str(db), "--file", "-"]) == 0
    assert SqliteStore(db).count() == 1


def test_verify_delegates_to_epoch_cli(monkeypatch: Any) -> None:
    seen: list[list[str]] = []

    def fake_epoch_main(argv: list[str] | None = None) -> int:
        seen.append(list(argv or []))
        return 0

    monkeypatch.setattr("protocol.epoch.main", fake_epoch_main)
    assert main(["verify", "epochs.json", "--pubkey", "ab12"]) == 0
    assert seen == [["verify", "epochs.json", "--pubkey", "ab12"]]
    assert main(["verify", "epochs.json"]) == 0
    assert seen[-1] == ["verify", "epochs.json"]
