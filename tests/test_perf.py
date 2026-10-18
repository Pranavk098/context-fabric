"""Performance budget gate for the deterministic compiler (no GPU, no network).

The bounds are deliberately generous (roughly 4x the measured dev-box mean): CI
machines are slower and noisier than the dev box, and the job of this test is to catch
order-of-magnitude regressions, not to set speed records. No wall-clock assumptions are
made beyond "compiling 1000 blocks must stay within interactive latency".
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from kernel.router.compiler import CompileResult, compile_packet
from protocol.canonical import sha256_hex
from protocol.models import (
    BlockKind,
    ContextBlock,
    Creator,
    CreatorType,
    Fidelity,
    MemoryTier,
    Recipient,
    Scope,
)
from sidecar.render import render_packet

_AS_OF = datetime(2026, 1, 1, tzinfo=UTC)
_SCOPE = Scope(tenant_id="t1", project_id="p1", workflow_id="w1", task_id="bench")
_RECIPIENT = Recipient(agent_id="a1", role="implementer", stage="implement")
_OBJECTIVE = "compile the deterministic context router"
_CONTENT = "The deterministic router compiles a context packet for the downstream agent. " * 4
_CONTENT_HASH = "sha256:" + sha256_hex(_CONTENT.encode("utf-8"))
_TOKEN_ESTIMATE = 60  # precomputed once; count_tokens is not what we are measuring


def _blocks(n: int) -> list[ContextBlock]:
    return [
        ContextBlock(
            block_id=f"blk_{i:05d}",
            block_kind=BlockKind.fact,
            memory_tier=MemoryTier.ledger,
            content=_CONTENT,
            content_hash=_CONTENT_HASH,
            token_estimate=_TOKEN_ESTIMATE,
            source_event_seq=i,
            creator=Creator(type=CreatorType.agent, id="bench"),
            fidelity=Fidelity.extract,
            trust_domain="t1",
            observed_at=_AS_OF,
            valid_from=_AS_OF,
        )
        for i in range(n)
    ]


def _compile(blocks: list[ContextBlock]) -> CompileResult:
    return compile_packet(
        scope=_SCOPE,
        recipient=_RECIPIENT,
        objective=_OBJECTIVE,
        stage="implement",
        blocks=blocks,
        as_of=_AS_OF,
    )


def _mean_compile_ms(blocks: list[ContextBlock], runs: int) -> float:
    start = time.perf_counter()
    for _ in range(runs):
        _compile(blocks)
    return (time.perf_counter() - start) * 1000.0 / runs


def test_compile_packet_performance_budget() -> None:
    blocks_1000 = _blocks(1000)
    _compile(blocks_1000)  # warm-up: imports, tiktoken caches, pydantic schemas

    runs = 30
    mean_1000 = _mean_compile_ms(blocks_1000, runs)

    # Measure-and-print only, for the record (docs/engineering/PERFORMANCE.md).
    for n in (50, 200):
        mean_n = _mean_compile_ms(_blocks(n), 10)
        print(f"compile_packet @ {n:4d} blocks: {mean_n:7.2f} ms (mean of 10)")
    print(f"compile_packet @ 1000 blocks: {mean_1000:7.2f} ms (mean of {runs})")

    result = _compile(blocks_1000)
    start = time.perf_counter()
    for _ in range(5):
        render_packet(result.packet)
    mean_render = (time.perf_counter() - start) * 1000.0 / 5
    print(f"render_packet @ 1000-block packet: {mean_render:7.2f} ms (mean of 5)")

    assert result.trace["included"], "bench packet must include blocks"
    assert mean_1000 < 250.0, f"compile_packet mean {mean_1000:.1f} ms exceeds 250 ms budget"
    assert mean_render < 50.0, f"render_packet mean {mean_render:.1f} ms exceeds 50 ms budget"
