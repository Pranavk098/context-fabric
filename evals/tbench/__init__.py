"""Terminal-Bench 2.1 adapter — parent -> small-window sub-agent, via Harbor + ``cfx serve``.

This package is the *next-benchmark* twin of the B8 live harness (``evals/b8_live.py``):
same frozen Parent-localization -> paired sub-agent arms (A naive / B current CF packet /
C BM25 skim + ``--obs-fold``), same ``cfx serve`` choke point (window cap + ledger +
observation folding), but the sub-agent runs inside a Terminal-Bench 2.1 container under
the Harbor harness instead of ``mini-swe-agent`` + ``DockerEnvironment`` against a
SWE-bench image.

Nothing here runs the harness or calls a model at import time; every public function is
pure and unit-tested offline (``tests/test_tbench.py`` mocks the harness by building its
command/config strings and mocks the model with an in-process HTTP upstream, exactly like
``tests/test_b8.py``). The live driver (spawn ``cfx serve``, then ``harbor run``) is
described in ``docs/engineering/TBENCH-DESIGN.md`` and is a post-B8 task.
"""

from evals.tbench.adapter import (
    AGENT,
    ARMS,
    DATASET,
    DEFAULT_N,
    DEFAULT_SEED,
    HANDOFF_BUDGET,
    WINDOW_CAP,
    build_handoff,
    build_task_header,
    harbor_command,
    sample_tasks,
    sidecar_command,
)

__all__ = [
    "AGENT",
    "ARMS",
    "DATASET",
    "DEFAULT_N",
    "DEFAULT_SEED",
    "HANDOFF_BUDGET",
    "WINDOW_CAP",
    "build_handoff",
    "build_task_header",
    "harbor_command",
    "sample_tasks",
    "sidecar_command",
]
