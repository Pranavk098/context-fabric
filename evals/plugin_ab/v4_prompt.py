"""Parent prompt for the v4 delegation benchmark.

The parent (Sonnet by default) is told to plan and to delegate the implementation to
sub-agents (Haiku by default) through the ``Agent`` tool. The planted facts appear only in
this prompt: they are the "Constraints for this task" sentence of each task in
``tasks_v4.json``. A good handoff carries them to the child; a bad one drops them.

The ``control`` arm gets the task line only (everything before the constraints marker), so
no agent in that run can know the planted facts and the hidden planted-fact tests must fail.
"""

from __future__ import annotations

CONSTRAINTS_MARKER = "Constraints for this task:"
SUBAGENT_TYPE = "general-purpose"
# Fork mode is on by default in claude >= 2.1.302 (docs; "0" turns it off). A fork always runs on
# the parent's model, so the fork arm cannot be model-matched to the Haiku children. The pilot's
# ``child_parent_msg_overlap`` was 0 for fork runs, so whether the child really inherited the
# conversation is still unconfirmed.
FORK_ENV = {"CLAUDE_CODE_FORK_SUBAGENT": "1"}


def task_line(prompt: str) -> str:
    """The task text without the planted-fact constraints."""
    index = prompt.find(CONSTRAINTS_MARKER)
    return (prompt if index < 0 else prompt[:index]).strip()


def _delegation_rules(arm: str, child_model: str) -> str:
    if arm == "fork":
        spawn = (
            "Spawn each sub-agent as a fork: call the Agent tool without a subagent_type and "
            "without a model, so the sub-agent inherits this whole conversation."
        )
    else:
        spawn = (
            f'Call the Agent tool for each one with subagent_type "{SUBAGENT_TYPE}" and model '
            f'"{child_model}" (the sub-agent model must be {child_model}).'
        )
    return (
        "How to run this job:\n"
        "1. Read the files you need and write a short plan first.\n"
        "2. You must delegate the implementation to sub-agents; do not edit source files "
        "yourself. Split the work into one to three sub-tasks. " + spawn + "\n"
        "3. When the sub-agents report back, run the relevant tests with "
        "`python -m pytest -q`; if something is missing, delegate again.\n"
        "4. Finish with a two-line summary of what changed."
    )


def parent_prompt(prompt: str, arm: str, child_model: str) -> str:
    """The prompt handed to the parent session for one v4 run."""
    body = task_line(prompt) if arm == "control" else prompt.strip()
    return f"{body}\n\n{_delegation_rules(arm, child_model)}"
