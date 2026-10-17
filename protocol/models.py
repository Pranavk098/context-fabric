"""CPP v1.0 immutable models (see docs/adr/ADR-0005-packet-schema-v1.md).

Pydantic v2, frozen and ``extra="forbid"`` so the wire schema cannot drift silently.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum, StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

_FROZEN = ConfigDict(frozen=True, extra="forbid")


def _now() -> datetime:
    return datetime.now(tz=UTC)


class BlockKind(StrEnum):
    instruction = "instruction"
    decision = "decision"
    fact = "fact"
    artifact = "artifact"
    observation = "observation"
    summary = "summary"
    pointer = "pointer"
    negative_knowledge = "negative_knowledge"
    shadow_probe = "shadow_probe"
    cartridge_hologram = "cartridge_hologram"


class MemoryTier(int, Enum):
    cartridge = 0
    ledger = 1
    swap = 2
    register = 3


class Fidelity(StrEnum):
    raw = "raw"
    extract = "extract"
    summary = "summary"
    pointer = "pointer"


class LifecycleStatus(StrEnum):
    draft = "draft"
    validated = "validated"
    routable = "routable"
    superseded = "superseded"
    expired = "expired"
    redacted = "redacted"


class CreatorType(StrEnum):
    human = "human"
    agent = "agent"
    tool = "tool"
    deterministic_probe = "deterministic_probe"


class Creator(BaseModel):
    model_config = _FROZEN
    type: CreatorType
    id: str


class ContextBlock(BaseModel):
    """The atomic unit of context (stored in ``context_blocks``)."""

    model_config = _FROZEN

    block_id: str
    block_kind: BlockKind
    memory_tier: MemoryTier
    media_type: str = "text/markdown"
    content: str | None = None
    ref_uri: str | None = None
    content_hash: str
    token_estimate: int = Field(ge=0)
    tokenizer_name: str = "o200k_base"
    source_event_seq: int = Field(ge=0)
    creator: Creator
    confidence: float = 1.0
    fidelity: Fidelity
    verified: bool = False
    observed_at: datetime = Field(default_factory=_now)
    valid_from: datetime = Field(default_factory=_now)
    valid_to: datetime | None = None
    lifecycle_status: LifecycleStatus = LifecycleStatus.routable
    classification: str = "internal"
    trust_domain: str
    allowed_roles: tuple[str, ...] = ()
    denied_roles: tuple[str, ...] = ()
    allowed_purposes: tuple[str, ...] = ()
    symbols_exported: tuple[str, ...] = ()
    symbols_imported: tuple[str, ...] = ()
    counterfactual_meta: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _content_or_pointer(self) -> ContextBlock:
        if self.content is None and self.ref_uri is None:
            raise ValueError("block must carry content or a ref_uri pointer")
        return self


class SwapHandle(BaseModel):
    model_config = _FROZEN
    uri: str
    summary: str
    tokens_raw: int = Field(ge=0)
    sha256: str


class TokenBudget(BaseModel):
    model_config = _FROZEN
    hard_total_tokens: int = 4000
    tier0: int = 600
    tier1: int = 1000
    tier2: int = 200
    tier3: int = 1800
    reserve: int = 400

    @model_validator(mode="after")
    def _sums(self) -> TokenBudget:
        total = self.tier0 + self.tier1 + self.tier2 + self.tier3 + self.reserve
        if total != self.hard_total_tokens:
            raise ValueError(f"tier budgets ({total}) must sum to hard_total_tokens")
        return self

    @classmethod
    def from_base(cls, base: int) -> TokenBudget:
        """Derive tier budgets from the normative proportions (ADR-0001)."""
        if base % 20 != 0:
            raise ValueError("hard_total_tokens must be divisible by 20")
        return cls(
            hard_total_tokens=base,
            tier0=base * 15 // 100,
            tier1=base * 25 // 100,
            tier2=base * 5 // 100,
            tier3=base * 45 // 100,
            reserve=base * 10 // 100,
        )


class Scope(BaseModel):
    model_config = _FROZEN
    tenant_id: str
    project_id: str
    workflow_id: str
    task_id: str


class Recipient(BaseModel):
    model_config = _FROZEN
    agent_id: str
    role: str
    stage: str


class Tier0Cartridge(BaseModel):
    model_config = _FROZEN
    cartridge_id: str
    kv_prefix_hash: str
    content_markdown: str


class Tier1Ledger(BaseModel):
    model_config = _FROZEN
    blocks: tuple[ContextBlock, ...] = ()


class Tier2Swap(BaseModel):
    model_config = _FROZEN
    handles: tuple[SwapHandle, ...] = ()


class RecencyAnchor(BaseModel):
    model_config = _FROZEN
    output_contract: str
    prohibitions: tuple[str, ...] = ()


class Tier3Register(BaseModel):
    model_config = _FROZEN
    instruction: ContextBlock | None = None
    shadow_probes: tuple[ContextBlock, ...] = ()
    observations: tuple[ContextBlock, ...] = ()
    recency_anchor: RecencyAnchor | None = None


class Tiers(BaseModel):
    model_config = _FROZEN
    tier0: Tier0Cartridge
    tier1: Tier1Ledger = Tier1Ledger()
    tier2: Tier2Swap = Tier2Swap()
    tier3: Tier3Register = Tier3Register()


class ConflictAlert(BaseModel):
    model_config = _FROZEN
    a_block_id: str
    b_block_id: str
    reason: str


class Hashes(BaseModel):
    model_config = _FROZEN
    content_hash: str
    signature: str | None = None
    key_id: str | None = None


class RouteTraceRef(BaseModel):
    model_config = _FROZEN
    included: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()


class ContextPacket(BaseModel):
    model_config = _FROZEN

    cpp_version: str = "1.0"
    packet_id: str
    parent_packet_id: str | None = None
    route_id: str
    spawn_depth: int = Field(default=0, ge=0)
    epoch_number: int = Field(default=1, ge=1)
    watermark_event_seq: int = Field(ge=0)
    scope: Scope
    recipient: Recipient
    budget: TokenBudget = Field(default_factory=TokenBudget)
    tiers: Tiers
    conflict_alerts: tuple[ConflictAlert, ...] = ()
    total_tokens: int = Field(ge=0)
    hashes: Hashes
    route_trace_ref: RouteTraceRef = RouteTraceRef()


class WorkflowEvent(BaseModel):
    model_config = _FROZEN
    event_id: str
    idempotency_key: str
    tenant_id: str
    project_id: str
    workflow_id: str
    task_id: str | None = None
    agent_id: str | None = None
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    content_hash: str = ""
    observed_at: datetime = Field(default_factory=_now)


class AgentOutcome(BaseModel):
    model_config = _FROZEN
    packet_id: str
    task_id: str
    artifacts: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    negative_knowledge: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    cited_block_ids: tuple[str, ...] = ()
    task_succeeded: bool | None = None
