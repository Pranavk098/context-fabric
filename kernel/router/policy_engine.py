"""Hard policy engine — deny-by-default RBAC / lifecycle / freshness / purpose gates.

Runs BEFORE scoring (invariant 2: policy-before-ranking). Role blindness (e.g. an auditor
must not see an implementer's persuasive observations) is enforced via a block's
``denied_roles`` field. Purpose compartments are enforced via ``allowed_purposes``.
Returns allowed blocks plus a full denial trace.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from protocol.models import ContextBlock, LifecycleStatus


@dataclass(frozen=True)
class Denial:
    block_id: str
    reason: str


def apply_policy(
    blocks: Sequence[ContextBlock],
    *,
    role: str,
    as_of: datetime | None = None,
    trust_domain: str | None = None,
    purpose: str | None = None,
) -> tuple[list[ContextBlock], list[Denial]]:
    allowed: list[ContextBlock] = []
    denials: list[Denial] = []
    for b in blocks:
        if trust_domain is not None and b.trust_domain != trust_domain:
            denials.append(Denial(b.block_id, "trust_domain_mismatch"))
            continue
        if purpose is not None and b.allowed_purposes and purpose not in b.allowed_purposes:
            denials.append(Denial(b.block_id, "purpose_not_allowed"))
            continue
        if b.lifecycle_status is not LifecycleStatus.routable:
            denials.append(Denial(b.block_id, f"lifecycle:{b.lifecycle_status.value}"))
            continue
        if role in b.denied_roles:
            denials.append(Denial(b.block_id, "role_denied"))
            continue
        if b.allowed_roles and role not in b.allowed_roles:
            denials.append(Denial(b.block_id, "role_not_allowed"))
            continue
        if b.valid_to is not None and as_of is not None and b.valid_to <= as_of:
            denials.append(Denial(b.block_id, "expired"))
            continue
        allowed.append(b)
    return allowed, denials


if __name__ == "__main__":
    from protocol.models import BlockKind, Creator, CreatorType, Fidelity, MemoryTier

    def mk(bid: str, kind: BlockKind, denied: tuple[str, ...] = ()) -> ContextBlock:
        return ContextBlock(
            block_id=bid,
            block_kind=kind,
            memory_tier=MemoryTier.register,
            content="x",
            content_hash="sha256:x",
            token_estimate=1,
            source_event_seq=1,
            creator=Creator(type=CreatorType.agent, id="a"),
            fidelity=Fidelity.extract,
            trust_domain="t",
            denied_roles=denied,
        )

    obs = mk("obs", BlockKind.observation, denied=("auditor",))
    dec = mk("dec", BlockKind.decision)
    allowed, denials = apply_policy([obs, dec], role="auditor")
    assert [b.block_id for b in allowed] == ["dec"]
    assert denials[0].block_id == "obs" and denials[0].reason == "role_denied"
    print("policy_engine.py self-check: OK")
