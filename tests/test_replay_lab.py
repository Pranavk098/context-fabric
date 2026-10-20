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


def test_info_retention_proxy_reports_per_policy() -> None:
    # A traceback + failing-test line in the first observation: none keeps everything,
    # obsfold stubs it away, obsfold_v2 keeps the content-aware lines (and so more survives).
    traceback = (
        "Traceback (most recent call last):\n"
        '  File "mod.py", line 42, in compute_cached\n'
        "    return self.cache[x]\n"
        "AssertionError: cache miss\n"
        "FAILED tests/test_mod.py::test_cached - AssertionError: cache miss\n"
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "you are a coding sub-agent"},
        {"role": "user", "content": "fix compute_cached in mod.py"},
    ]
    for i in range(8):
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
        content = traceback if i == 0 else f"output {i} mentions compute_cached"
        messages.append(
            {"role": "tool", "tool_call_id": f"call_{i}", "name": "bash", "content": content}
        )

    report = replay_messages(messages, task_identifiers=("mod.py", "compute_cached"), name="synth")
    none = report["policies"]["none"]["info_retention"]
    obsfold = report["policies"]["obsfold"]["info_retention"]
    v2 = report["policies"]["obsfold_v2"]["info_retention"]

    assert none["test_names"]["ratio"] == 1.0
    assert none["traceback_frames"]["ratio"] == 1.0
    assert none["identifiers"]["ratio"] == 1.0
    assert v2["test_names"]["ratio"] == 1.0  # content-aware fold keeps the failure line
    assert v2["traceback_frames"]["ratio"] == 1.0  # test fold keeps the traceback tail
    assert obsfold["test_names"]["ratio"] < 1.0  # stub loses the failure name
    assert obsfold["traceback_frames"]["ratio"] < 1.0  # stub loses the traceback frames
    assert v2["identifiers"]["ratio"] >= obsfold["identifiers"]["ratio"]
