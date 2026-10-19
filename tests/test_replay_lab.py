"""Offline tests for the trajectory replay lab ``evals/replay_lab.py`` (no model, no network)."""

from __future__ import annotations

from typing import Any

from evals.replay_lab import POLICIES, replay_messages


def _trajectory(n_steps: int, *, body_tokens: int = 60) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "you are a coding sub-agent"},
        {"role": "user", "content": "fix compute_cached in mod.py"},
    ]
    for i in range(n_steps):
        messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call_{i}",
                        "type": "function",
                        "function": {"name": "bash", "arguments": '{"command":"pytest"}'},
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": f"call_{i}",
                "name": "bash",
                "content": f"output {i}\n" + " ".join(f"w{i}_{j}" for j in range(body_tokens)),
            }
        )
    return messages


def test_replay_deterministic_and_reports_all_policies() -> None:
    messages = _trajectory(5)
    first = replay_messages(messages, name="synth")
    second = replay_messages(messages, name="synth")
    assert first == second
    assert set(first["policies"]) == set(POLICIES)
    assert first["n_steps"] == 5
    assert first["recorded_usage"] is None  # synthetic trajectory has no usage metadata
    share = first["tool_output_share"]
    assert 0.0 < share["share"] <= 1.0
    for policy in POLICIES:
        body = first["policies"][policy]
        assert len(body["steps"]) == 5
        assert body["peak_tokens"] > 0
        assert body["total_tokens"] > 0


def test_none_policy_is_verbatim_and_prefix_is_append_only() -> None:
    messages = _trajectory(6)
    report = replay_messages(messages)
    none = report["policies"]["none"]
    for step in none["steps"]:
        assert step["folded"] == 0  # verbatim: nothing folded
    # append-only: the shared prefix with the previous step is the previous step's length
    prefix_bytes = [step["prefix_bytes"] for step in none["steps"]]
    assert prefix_bytes == sorted(prefix_bytes)


def test_obsfold_policy_folds_older_observations() -> None:
    report = replay_messages(_trajectory(8))
    obsfold_steps = report["policies"]["obsfold"]["steps"]
    assert any(step["folded"] > 0 for step in obsfold_steps)
    # the last observation is never folded (n=3 mask): the final step folds < all tools
    assert obsfold_steps[-1]["folded"] < 8


def test_overflow_detection_at_window_caps() -> None:
    # 14 steps x ~2000 tokens each crosses 16,384 around step 8 and 24,576 near the end.
    report = replay_messages(_trajectory(14, body_tokens=2000))
    none = report["policies"]["none"]
    assert none["peak_tokens"] > 24576
    assert none["overflow_step_16384"] is not None
    assert none["overflow_step_24576"] is not None
    assert none["overflow_step_16384"] <= none["overflow_step_24576"]  # 16k overflows first
