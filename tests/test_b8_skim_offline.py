"""Offline tests for the skim eval ``evals/b8_skim_offline.py`` (no model, no network).

Asserts the R/SKIM/C arms build on a synthetic fixture and that the dry-run is
deterministic and reports a per-arm/budget recall diagnostic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from evals.b4_v3 import AgentSpec, package_files
from evals.b8_skim_offline import ARMS, BUDGETS, dry_run_payload

AS_OF = datetime(2026, 1, 1, tzinfo=UTC)
MODEL = "Qwen/Qwen2.5-1.5B-Instruct"


def _synthetic_httpx(root: Path) -> None:
    pkg = root / "httpx"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        '"""httpx package."""\nfrom ._api import get\n', encoding="utf-8"
    )
    (pkg / "_config.py").write_text(
        "DEFAULT_MAX_REDIRECTS = 20\n\n"
        "def get_max_redirects() -> int:\n"
        '    """Return the default max redirects."""\n'
        "    return DEFAULT_MAX_REDIRECTS\n",
        encoding="utf-8",
    )
    (pkg / "_api.py").write_text(
        'def request(method, url):\n    """Send a request."""\n    return None\n',
        encoding="utf-8",
    )


_SYNTH_OBJECTIVES = (
    AgentSpec("p1", "planner", "What is the default max redirects?", ("20",), ("5.0",)),
    AgentSpec("i1", "implementer", "Which helper sends a request?", ("request",), ("get",)),
)


def test_dry_run_deterministic_and_reports_recall(tmp_path: Path) -> None:
    _synthetic_httpx(tmp_path)
    files = package_files(tmp_path)
    assert files  # the package is discovered
    first = dry_run_payload(tmp_path, _SYNTH_OBJECTIVES, model=MODEL, as_of=AS_OF)
    second = dry_run_payload(tmp_path, _SYNTH_OBJECTIVES, model=MODEL, as_of=AS_OF)
    assert first == second
    assert set(first["arms"]) == {f"{arm}:{budget}" for budget in BUDGETS for arm in ARMS}
    assert first["tokenizer"] in {"qwen", "tiktoken"}
    for summary in first["arms"].values():
        assert summary["prompt_tokens_total"] > 0
        assert summary["max_objective_prompt_tokens"] > 0
    recall = first["recall"]
    assert recall
    assert {row["arm"] for row in recall} == set(first["arms"])
    for row in recall:
        assert set(row["facts_in_packet"]) == set(row["required"])
        assert isinstance(row["all_in_packet"], bool)
