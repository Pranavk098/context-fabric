"""Shadow Cortex v0 — deterministic probe generation and keyword evaluation."""

from __future__ import annotations

from datetime import UTC, datetime

from kernel.router.compiler import compile_packet
from kernel.router.policy_engine import apply_policy
from kernel.router.shadow_cortex import (
    ACKNOWLEDGED,
    CONTRA_CUES,
    CONTRADICTED,
    FRAGMENT_WORDS,
    IGNORED,
    ProbeVerdict,
    evaluate_probes,
    generate_probes,
)
from protocol.canonical import sha256_hex
from protocol.models import (
    BlockKind,
    ContextBlock,
    Creator,
    CreatorType,
    Fidelity,
    LifecycleStatus,
    MemoryTier,
    Recipient,
    Scope,
)

AS_OF = datetime(2026, 1, 1, tzinfo=UTC)
SCOPE = Scope(tenant_id="t1", project_id="p1", workflow_id="w1", task_id="task1")
RECIPIENT = Recipient(agent_id="a1", role="implementer", stage="implement")


def mk(
    bid: str, kind: BlockKind, content: str, seq: int = 1, *, trust_domain: str = "t1"
) -> ContextBlock:
    return ContextBlock(
        block_id=bid,
        block_kind=kind,
        memory_tier=MemoryTier.ledger,
        content=content,
        content_hash="sha256:" + bid,
        token_estimate=1,
        source_event_seq=seq,
        creator=Creator(type=CreatorType.agent, id="a"),
        fidelity=Fidelity.extract,
        trust_domain=trust_domain,
    )


def _sources() -> list[ContextBlock]:
    return [
        mk("f1", BlockKind.fact, "The router is deterministic", 2),
        mk("d1", BlockKind.decision, "Use Postgres for canonical storage", 1),
        mk("nk1", BlockKind.negative_knowledge, "Do not retry naive masking of tokens", 3),
    ]


def _probes(limit: int = 3) -> tuple[ContextBlock, ...]:
    return generate_probes(_sources(), limit=limit, trust_domain="t1", event_seq=7, as_of=AS_OF)


def test_generation_priority_limit_and_candidate_filter() -> None:
    blocks = [*_sources(), mk("obs1", BlockKind.observation, "raw tool output", 4)]
    probes = generate_probes(blocks, trust_domain="t1", event_seq=7, as_of=AS_OF)
    assert [p.block_id for p in probes] == ["blk_probe_nk1", "blk_probe_d1", "blk_probe_f1"]
    limited = generate_probes(blocks, limit=2, trust_domain="t1", event_seq=7, as_of=AS_OF)
    assert [p.block_id for p in limited] == ["blk_probe_nk1", "blk_probe_d1"]
    assert generate_probes(blocks, limit=0, trust_domain="t1", event_seq=7, as_of=AS_OF) == ()
    assert (
        generate_probes(
            [mk("obs1", BlockKind.observation, "raw", 1)], trust_domain="t1", as_of=AS_OF
        )
        == ()
    )


def test_generation_is_deterministic_and_input_order_independent() -> None:
    first = _probes()
    second = _probes()
    assert first == second
    assert [p.model_dump() for p in first] == [p.model_dump() for p in second]  # byte-identical
    reversed_order = generate_probes(
        list(reversed(_sources())), trust_domain="t1", event_seq=7, as_of=AS_OF
    )
    assert reversed_order == first


def test_probe_blocks_validate_round_trip_and_carry_probe_shape() -> None:
    for probe in _probes():
        assert ContextBlock.model_validate(probe.model_dump()) == probe
        assert probe.block_kind is BlockKind.shadow_probe
        assert probe.memory_tier is MemoryTier.register
        assert probe.creator == Creator(type=CreatorType.deterministic_probe, id="shadow_cortex")
        assert probe.fidelity is Fidelity.extract
        assert probe.lifecycle_status is LifecycleStatus.routable
        assert probe.trust_domain == "t1"
        assert probe.source_event_seq == 7
        assert probe.observed_at == AS_OF
        assert probe.valid_from == AS_OF
        content = probe.content or ""
        assert probe.content_hash == "sha256:" + sha256_hex(content.encode("utf-8"))
        assert probe.token_estimate > 0


def test_probes_pass_policy_gates() -> None:
    probes = _probes()
    allowed, denials = apply_policy(probes, role="implementer", as_of=AS_OF, trust_domain="t1")
    assert allowed == list(probes)
    assert denials == []
    allowed, denials = apply_policy(probes, role="implementer", as_of=AS_OF, trust_domain="other")
    assert allowed == []
    assert {d.reason for d in denials} == {"trust_domain_mismatch"}


def test_probe_fragments_trace_to_their_source_blocks() -> None:
    sources = {b.block_id: b for b in _sources()}
    for probe in _probes():
        source_id = probe.block_id.removeprefix("blk_probe_")
        source = sources[source_id]
        content = probe.content or ""
        assert f"[probe-of:{source_id}]" in content
        fragment = " ".join((source.content or "").split()[:FRAGMENT_WORDS])
        assert fragment in content
        if source.block_kind is BlockKind.negative_knowledge:
            assert content.startswith("Do not repeat: ")
        elif source.block_kind is BlockKind.decision:
            assert content.startswith("Still true: ")


def test_evaluate_acknowledged_by_tag() -> None:
    probes = _probes()
    response = (
        "Yes, still true: use Postgres for canonical storage. [probe-of:d1]\n"
        "Noted, we will avoid naive masking of tokens. [probe-of:nk1]"
    )
    verdicts = evaluate_probes(response, probes)
    assert [v.block_id for v in verdicts] == [p.block_id for p in probes]
    assert [v.verdict for v in verdicts] == [ACKNOWLEDGED, ACKNOWLEDGED, IGNORED]
    assert verdicts[2] == ProbeVerdict(probes[2].block_id, IGNORED, "")


def test_evaluate_acknowledged_without_tags_uses_fragment() -> None:
    probes = _probes()
    response = "Yes, we still use Postgres for canonical storage."
    verdicts = evaluate_probes(response, probes)
    assert [v.verdict for v in verdicts] == [IGNORED, ACKNOWLEDGED, IGNORED]


def test_evaluate_contradicted_echoed_question_does_not_count_as_answer() -> None:
    probes = _probes()
    response = (
        "Still true: Use Postgres for canonical storage? Answer yes/no.\n"
        "[probe-of:d1]\n"
        "No, that decision is no longer true \u2014 it was reverted."
    )
    verdicts = evaluate_probes(response, probes)
    assert verdicts[1].verdict == CONTRADICTED
    assert verdicts[1].cue in CONTRA_CUES


def test_evaluate_ignored_cases_and_empty_response() -> None:
    probes = _probes()
    assert evaluate_probes("", probes) == tuple(ProbeVerdict(p.block_id, IGNORED) for p in probes)
    unrelated = "Here is the implementation plan for the migration sprint."
    assert all(v.verdict == IGNORED for v in evaluate_probes(unrelated, probes))
    # v0 ceiling: a bare cue without tag or fragment is unattributable.
    assert all(v.verdict == IGNORED for v in evaluate_probes("Yes.", probes))
    assert evaluate_probes("anything", ()) == ()


def test_evaluate_is_case_insensitive() -> None:
    probes = _probes()
    yes = evaluate_probes("YES \u2014 WE STILL USE POSTGRES FOR CANONICAL STORAGE.", probes)
    assert yes[1].verdict == ACKNOWLEDGED
    no = evaluate_probes("NO, USE POSTGRES FOR CANONICAL STORAGE IS SUPERSEDED.", probes)
    assert no[1].verdict == CONTRADICTED


def test_evaluate_unicode_fragment() -> None:
    source = mk("u1", BlockKind.decision, "Планируй этапы миграции заранее")
    probe = generate_probes([source], trust_domain="t1", event_seq=1, as_of=AS_OF)[0]
    response = "Yes — планируй этапы миграции без спешки."
    assert evaluate_probes(response, [probe])[0].verdict == ACKNOWLEDGED
    assert evaluate_probes("Посторонний ответ.", [probe])[0].verdict == IGNORED


def test_compiled_packet_carries_probes_within_budget() -> None:
    blocks = _sources()
    probes = generate_probes(blocks, trust_domain="t1", event_seq=7, as_of=AS_OF)
    kwargs = dict(
        scope=SCOPE,
        recipient=RECIPIENT,
        objective="implement canonical storage",
        stage="implement",
        blocks=[*blocks, *probes],
        as_of=AS_OF,
    )
    first = compile_packet(**kwargs)
    second = compile_packet(**kwargs)
    assert first.packet.hashes.content_hash == second.packet.hashes.content_hash
    probe_ids = [b.block_id for b in first.packet.tiers.tier3.shadow_probes]
    assert probe_ids == sorted(p.block_id for p in probes)  # packer orders probes by block_id
    observed_ids = {b.block_id for b in first.packet.tiers.tier3.observations}
    assert not observed_ids & set(probe_ids)
    assert first.packet.total_tokens <= first.packet.budget.hard_total_tokens
