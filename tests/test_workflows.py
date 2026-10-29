"""CI hygiene (F13): pinned actions, locked syncs, and a tag-on-main release gate."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
FILES = sorted(WORKFLOWS.glob("*.yml"))
USES = re.compile(r"^\s*-?\s*uses:\s*(\S+)", re.MULTILINE)
SHA = re.compile(r"^[0-9a-f]{40}$")
UV_SYNC = re.compile(r"\buv sync\b")


def test_workflows_present() -> None:
    assert FILES


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_every_action_pinned_to_a_sha(path: Path) -> None:
    for ref in USES.findall(path.read_text(encoding="utf-8")):
        assert "@" in ref, f"{path.name}: {ref} has no ref"
        assert SHA.match(ref.rsplit("@", 1)[1]), f"{path.name}: {ref} is not a 40-hex SHA"


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_every_uv_sync_is_locked(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        if UV_SYNC.search(line):
            assert "--locked" in line, f"{path.name}: {line.strip()}"


def test_release_requires_a_tag_on_main() -> None:
    text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert "git fetch origin main" in text
    assert "merge-base --is-ancestor" in text


def test_release_has_a_manual_dry_run() -> None:
    text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in text
    assert "dry_run:" in text
    assert "github.event_name == 'push'" in text


def test_security_scan_is_weekly_and_read_only() -> None:
    text = (WORKFLOWS / "security.yml").read_text(encoding="utf-8")
    assert "cron:" in text
    assert "workflow_dispatch:" in text
    assert "pull_request_target" not in text
    permissions = text.split("permissions:", 1)[1].split("jobs:", 1)[0]
    assert "contents: read" in permissions
    assert "write" not in permissions
    assert "uvx bandit -r cfxd -ll -q -b .bandit-allow" in text
    assert "uv export --locked --no-hashes" in text
    assert "uvx pip-audit -r" in text


def test_bandit_allowlist_is_documented_and_narrow() -> None:
    import json

    allow = json.loads(
        (Path(__file__).resolve().parents[1] / ".bandit-allow").read_text(encoding="utf-8")
    )
    assert allow["results"]
    assert all(item["test_id"] == "B608" for item in allow["results"])
    assert all(item["_why"] for item in allow["results"])
