"""Policy engine tests — purpose compartments and existing RBAC/lifecycle gates."""

from __future__ import annotations

from kernel.router.policy_engine import apply_policy
from protocol.models import BlockKind, ContextBlock, Creator, CreatorType, Fidelity, MemoryTier


def _block(
    bid: str,
    *,
    kind: BlockKind = BlockKind.decision,
    trust_domain: str = "t1",
    allowed_purposes: tuple[str, ...] = (),
    denied_roles: tuple[str, ...] = (),
    lifecycle: str = "routable",
) -> ContextBlock:
    return ContextBlock(
        block_id=bid,
        block_kind=kind,
        memory_tier=MemoryTier.ledger,
        content="x",
        content_hash="sha256:x",
        token_estimate=1,
        source_event_seq=1,
        creator=Creator(type=CreatorType.agent, id="a"),
        fidelity=Fidelity.extract,
        trust_domain=trust_domain,
        allowed_purposes=allowed_purposes,
        denied_roles=denied_roles,
        lifecycle_status=lifecycle,  # type: ignore[arg-type]
    )


def test_purpose_none_skips_purpose_check() -> None:
    block = _block("b1", allowed_purposes=("audit",))
    allowed, denials = apply_policy([block], role="user")
    assert allowed == [block]
    assert not denials


def test_allowed_purposes_empty_allows_any_purpose() -> None:
    block = _block("b1", allowed_purposes=())
    allowed, denials = apply_policy([block], role="user", purpose="anything")
    assert allowed == [block]
    assert not denials


def test_matching_purpose_is_allowed() -> None:
    block = _block("b1", allowed_purposes=("plan", "review"))
    allowed, denials = apply_policy([block], role="user", purpose="review")
    assert allowed == [block]
    assert not denials


def test_mismatching_purpose_is_denied() -> None:
    block = _block("b1", allowed_purposes=("plan",))
    allowed, denials = apply_policy([block], role="user", purpose="audit")
    assert not allowed
    assert denials[0].block_id == "b1"
    assert denials[0].reason == "purpose_not_allowed"


def test_trust_domain_exact_match_still_enforced() -> None:
    block = _block("b1", trust_domain="t1")
    allowed, denials = apply_policy([block], role="user", trust_domain="t2")
    assert not allowed
    assert denials[0].reason == "trust_domain_mismatch"
