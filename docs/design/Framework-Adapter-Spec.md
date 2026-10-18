# 05 — Sidecar REST API & Framework Adapter Specification

> **Status note (2026-10-18):** design of record. The framework adapters below are deferred
> to post-v1.0 (ADR-0004). The shipped surface is the OpenAI-compatible sidecar proxy
> (`sidecar/`, `../engineering/SIDECAR.md`), the `cfx` CLI, and the MCP stdio server
> (`../engineering/MCP.md`) — not these REST routes; those remain the v2 target.

## 1. Sidecar REST API Endpoints (`services/api/`)

All `POST` endpoints MUST require an `Idempotency-Key` HTTP header[cite: 36].

| Method & Path | Request Body | Response Body | Normative Behavior |
| :--- | :--- | :--- | :--- |
| `POST /v1/cartridges:build` | `{project_id, repo_path, commit_hash}` | `{cartridge_id, token_count, kv_prefix_hash, compile_ms}` | Runs Tree-sitter + PageRank indexer and stores Tier 0 `.cfx` hologram. |
| `POST /v1/events`[cite: 36] | `WorkflowEvent` JSON[cite: 31] | `{event_seq, event_id, blocks_indexed}` | Appends event to `canonical_events` and indexes derived blocks[cite: 23, 36]. |
| `POST /v1/packets:compile`[cite: 36] | `{workflow_id, task_id, recipient_agent_id, objective, stage, budget_tokens}` | `ContextPacket` (CPP v1.0 JSON) | Executes 9-stage router + Shadow Cortex and returns signed 4-Tier packet[cite: 22, 27, 36]. |
| `POST /v1/packets/{id}:fold` | `{packet_id, step_observations: [...]}` | `ContextPacket` (New Epoch) | Executes intra-turn `EPOCH_FOLD`, evicting raw tool outputs to Tier 2 `ctx://` swap handles. |
| `POST /v1/packets/{id}:fork` | `{parent_packet_id, child_agent_id, child_role, sub_objective, stage}` | `ContextPacket` (Child CoW Packet) | Executes `COW_FORK`, preserving Tier 0/1 prefix and creating an isolated Tier 3 frame. |
| `POST /v1/packets/{id}:expand`[cite: 36] | `{packet_id, ref_uri, reason, additional_tokens}`[cite: 28] | `ContextPacket` (Delta Expansion) | Executes `PAGE_FAULT_EXPAND` after verifying RBAC policy authorization[cite: 28, 32, 36]. |
| `POST /v1/receipts`[cite: 36] | `{packet_id, agent_id, kv_cache_hit_ratio, cited_block_ids}` | `{receipt_id, status}` | Records delivery and actual KV-cache / citation telemetry[cite: 22, 36]. |
| `POST /v1/outcomes`[cite: 36] | `{packet_id, task_id, artifacts, decisions, negative_knowledge, gaps}` | `{event_seq, merged_tier1_blocks}` | Validates return contract, commits new facts/dead-ends to Tier 1, GCs Tier 3[cite: 22, 29, 36]. |
| `GET /v1/routes/{id}/explain`[cite: 36] | Query param: `redact=true` | `RouteExplanationTrace` | Returns full audit trail: candidate scores, policy denials, dependency edges, and hashes[cite: 21, 32, 36]. |

---

## 2. Python Adapter Interface (`adapters/base.py`)

Every framework adapter (`LangGraphFabricAdapter`, `CrewAIFabricAdapter`, `AutoGenFabricAdapter`, `RawAPIFabricAdapter`) MUST implement this exact abstract base class and pass the 6-operation semantic parity test suite[cite: 31, 35, 37]:

```python
from abc import ABC, abstractmethod
from typing import Any, Dict, List
from protocol.models import (
    ContextPacket,
    DeliveryReceipt,
    RuntimeCapabilities,
    WorkflowEvent,
    AgentOutcome,
    CancellationReceipt,
)

class FabricAdapter(ABC):
    """
    Orthogonal boundary adapter[cite: 9, 31]. Translates native runtime events into Context Fabric
    syscalls and renders CPP v1.0 packets into provider/framework-native messages[cite: 25, 31].
    MUST NOT contain custom routing, scoring, or policy logic[cite: 25, 31].
    """

    @abstractmethod
    def describe_runtime(self) -> RuntimeCapabilities:
        """Return framework name, version, supported delivery modes, and KV-cache capabilities[cite: 31]."""

    @abstractmethod
    async def normalize_event(self, native_event: Any) -> WorkflowEvent:
        """Convert a LangGraph state transition, CrewAI task start, or A2A message into a WorkflowEvent[cite: 31]."""

    @abstractmethod
    async def deliver(self, packet: ContextPacket) -> Dict[str, Any]:
        """
        Render the 4-Tier ContextPacket into the runtime's native prompt/message array[cite: 25, 31].
        MUST place provider cache_control breakpoints at the exact end of Tier 0 and Tier 1.
        """

    @abstractmethod
    async def intercept_tool_step(
        self, packet_id: str, step_num: int, tool_name: str, tool_input: Any, tool_output: str
    ) -> ContextPacket | None:
        """
        Intra-turn MMU hook. Tracks step count and token growth in Tier 3; triggers
        POST /v1/packets/{id}:fold when step_num % fold_threshold_steps == 0.
        """

    @abstractmethod
    async def observe(self, packet_id: str, native_result: Any) -> AgentOutcome:
        """Capture structured output, cited block IDs, and falsified hypotheses; POST to /v1/outcomes[cite: 31, 36]."""

    @abstractmethod
    async def cancel(self, task_id: str, reason: str) -> CancellationReceipt:
        """Handle task preemption or expiration cleanly[cite: 30, 31]."""