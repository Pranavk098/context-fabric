"""Position-probe instrument tests — placement, scorer, determinism (no GPU)."""

from __future__ import annotations

from evals.harness import count_tokens
from evals.position_probe import POSITIONS, PROBES, RULE, contexts, score_response


def test_rule_sits_at_head_middle_tail_with_equal_token_counts() -> None:
    ctx = contexts()
    assert set(ctx) == set(POSITIONS)
    tokens = {position: count_tokens(text) for position, text in ctx.items()}
    assert len(set(tokens.values())) == 1  # position changes, token count does not
    assert 2000 <= tokens["head"] <= 4500  # ~3k-token context
    assert ctx["head"].splitlines()[0] == RULE
    assert ctx["tail"].splitlines()[-1] == RULE
    prefix = ctx["middle"].split(RULE)[0]
    fraction = count_tokens(prefix) / tokens["middle"]
    assert 0.35 <= fraction <= 0.65


def test_contexts_are_deterministic() -> None:
    assert contexts() == contexts()


def test_eight_distinct_probes() -> None:
    assert len(PROBES) == 8
    assert len(set(PROBES)) == 8


def test_scorer_success_when_only_7443_is_claimed() -> None:
    ok, other = score_response("Use port 7443 for the gateway.")
    assert ok is True
    assert other == []


def test_scorer_fails_on_missing_or_different_port() -> None:
    assert score_response("I could not find the port.") == (False, [])
    assert score_response("Use port 8443.") == (False, ["8443"])
    # Crude by design: any other 2-5 digit number is a failed "other port claim".
    assert score_response("The gateway is port 7443; TTL 120 s.") == (False, ["120"])
    assert score_response("Use 7443, not 8443.") == (False, ["8443"])
