"""Offline tests for the B4 exploration levers (no model, no network).

Guards the "default behaviour unchanged" contract: ``build_blocks`` still embeds the same
cartridge hologram, ``compile_packet``/``compile_node_packet`` default to the frozen output
contract, and the new query-aware ``build_rich_blocks`` only adds content (full imports +
top-k bodies) behind the lever. Also locks the explore harness's dry-run determinism.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from adapters.langgraph import compile_node_packet
from evals.b4_explore import _ARMS, _build_arms
from evals.mech_fabric import build_blocks, build_project_cartridge, build_rich_blocks
from kernel.router.compiler import compile_packet
from protocol.models import Recipient, Scope

AS_OF = datetime(2026, 1, 1, tzinfo=UTC)
SCOPE = Scope(tenant_id="default", project_id="b4x", workflow_id="w", task_id="t")
RECIPIENT = Recipient(agent_id="a", role="planner", stage="plan")

_FROZEN_CONTRACT = "reply with the required structured output only."

_SRC = {
    "_api.py": (
        "from ._client import Client\n"
        "from ._config import DEFAULT_TIMEOUT_CONFIG\n"
        "from ._urls import URL\n"
        "\n"
        "def request(method, url, timeout=DEFAULT_TIMEOUT_CONFIG):\n"
        '    """Sends an HTTP request."""\n'
        "    return Client().request(method, url)\n"
    ),
    "_auth.py": (
        "class Auth:\n"
        '    """Base auth."""\n'
        "    requires_request_body = False\n"
        "\n"
        "class FunctionAuth(Auth):\n"
        '    """Wraps a callable."""\n'
        "    def __init__(self, func):\n"
        "        self._func = func\n"
    ),
    "_status_codes.py": (
        "class codes:\n"
        '    IM_A_TEAPOT = 418, "I\'m a teapot"\n'
        '    TOO_MANY_REQUESTS = 429, "Too Many Requests"\n'
        "    def is_error(self):\n"
        "        return True\n"
    ),
    "_types.py": (
        "URLTypes = str\nHeaderTypes = dict\nRequestContent = bytes\nResponseContent = bytes\n"
    ),
}


def _fixture(root: Path) -> None:
    pkg = root / "httpx"
    pkg.mkdir()
    for name, text in _SRC.items():
        (pkg / name).write_text(text, encoding="utf-8")


def test_build_blocks_embeds_the_same_cartridge_hologram(tmp_path: Path) -> None:
    _fixture(tmp_path)
    files = ["httpx/_api.py", "httpx/_auth.py"]
    blocks = build_blocks(tmp_path, files)
    cartridge = build_project_cartridge(tmp_path, files)
    artifact = next(b for b in blocks if b.ref_uri is None)
    assert artifact.content == cartridge.hologram_markdown
    assert cartridge.token_count > 0


def test_build_rich_blocks_is_query_aware_and_deterministic(tmp_path: Path) -> None:
    _fixture(tmp_path)
    files = ["httpx/_api.py", "httpx/_auth.py"]
    first = build_rich_blocks(tmp_path, files, "request client timeout")
    second = build_rich_blocks(tmp_path, files, "request client timeout")
    assert first == second

    text = "\n".join(b.content or "" for b in first)
    # full import statement (symbol -> module mapping), not root names
    assert "from ._config import DEFAULT_TIMEOUT_CONFIG" in text
    assert "from ._urls import URL" in text
    # a full body for a relevant symbol is present
    assert "def request(method, url" in text
    assert "return Client().request" in text


def test_compile_packet_default_output_contract_is_frozen() -> None:
    default = compile_packet(
        scope=SCOPE, recipient=RECIPIENT, objective="x", stage="plan", blocks=(), as_of=AS_OF
    )
    rendered = default.packet.tiers.tier3.recency_anchor.output_contract or ""
    assert _FROZEN_CONTRACT in rendered.lower()

    overridden = compile_packet(
        scope=SCOPE,
        recipient=RECIPIENT,
        objective="x",
        stage="plan",
        blocks=(),
        as_of=AS_OF,
        output_contract="ONLY the answer.",
    )
    assert overridden.packet.tiers.tier3.recency_anchor.output_contract == "ONLY the answer."
    # default and overridden differ only in the contract line, not block selection
    assert [b.block_id for b in default.packet.tiers.tier3.observations] == [
        b.block_id for b in overridden.packet.tiers.tier3.observations
    ]


def test_cartridge_pins_a_shared_tier0_across_roles(tmp_path: Path) -> None:
    _fixture(tmp_path)
    from evals.mech_fabric import build_project_cartridge as _cart

    files = ["httpx/_api.py", "httpx/_auth.py"]
    cartridge = _cart(tmp_path, files)
    blocks = build_blocks(tmp_path, files)
    for role in ("planner", "implementer"):
        result = compile_node_packet(
            {"objective": "x", "blocks": blocks},
            scope=SCOPE,
            recipient=Recipient(agent_id=role, role=role, stage="plan"),
            as_of=AS_OF,
            cartridge=cartridge,
        )
        assert result.packet.tiers.tier0.content_markdown == cartridge.hologram_markdown
    # both roles share the same Tier-0 bytes (the shared prefix), which is non-empty
    assert cartridge.hologram_markdown.startswith("# Project Hologram")


def test_explore_dry_run_arms_are_deterministic(tmp_path: Path) -> None:
    _fixture(tmp_path)
    rendered = "\n".join(f"{name}\n{text}" for name, text in _SRC.items())
    first = _build_arms(tmp_path, rendered)
    second = _build_arms(tmp_path, rendered)
    assert first == second
    names = {name for name, _mt, _pairs in first}
    assert names == {name for name, _mt in _ARMS}
