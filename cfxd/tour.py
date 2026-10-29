"""``cfxd tour``: a guided first run over the user's own transcripts.

Five steps, each running a real analyzer and explaining the result in plain
words. Everything printed here is counts, sizes and flags: never a path, a name
from the user's setup, a command line or transcript text.
"""

from __future__ import annotations

import argparse
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from cfxd.audit import audit
from cfxd.console import ensure_utf8_output
from cfxd.diet import diet

HEADINGS = (
    "1. Doctor",
    "2. Where your tokens go",
    "3. Long sessions",
    "4. Your starting context",
    "5. What to try next",
)

_FIX_HINTS = (
    "Fix hints:",
    "  - No helper answering? Start a new Claude Code session, then run cfxd doctor.",
    "  - identity NOT verified? Run cfxd stop and restart Claude Code.",
    "  - On Windows? The plugin's start-up script needs Git for Windows installed.",
)


def _doctor_lines(state_dir: str | None) -> tuple[bool, list[str]]:
    """Run the real doctor command and return whether it passed and its output.

    On failure its text can contain the state directory path, so it is dropped
    and the caller prints generic fix hints instead.
    """
    from cfxd.cli import _cmd_doctor

    args = argparse.Namespace(state_dir=state_dir)
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        try:
            code = _cmd_doctor(args)
        except Exception:  # one broken step must not end the tour
            return False, []
    if code != 0:
        return False, []
    return True, [line for line in buffer.getvalue().splitlines() if line.strip()]


def _step_doctor(state_dir: str | None) -> list[str]:
    ok, lines = _doctor_lines(state_dir)
    if not ok:
        return ["The plugin or its local helper did not answer.", *_FIX_HINTS]
    return ["Plugin installed and helper reachable.", *lines, *_FIX_HINTS]


def _step_tokens(report: dict[str, Any]) -> list[str]:
    if report["sessions"] == 0:
        return ["No transcripts found, so this step has nothing to measure yet."]
    turns = report["turns"]
    tool_bytes = sum(st["bytes"] for st in report["tools"].values())
    over_10k = sum(report["share_over_10k"].values()) * 100
    return [
        f"In the last 30 days, across {report['sessions']:,} sessions, you ran "
        f"{turns['total']:,} assistant turns.",
        f"A typical session has {turns['median']:,.1f} turns; the longest 10% reach "
        f"{turns['p90']:,.1f}+.",
        f"Tool results alone were {tool_bytes:,} bytes, and {over_10k:,.1f}% of those bytes "
        "came from single results over 10 KB.",
        "Cost is roughly turns x context size (x means multiplied): every turn re-sends the "
        "context, so a long session keeps paying for the same history.",
        "Next: run cfxd audit for the full breakdown, or cfxd waste for loops and retries.",
    ]


def _step_long(report: dict[str, Any]) -> list[str]:
    if report["sessions"] == 0:
        return ["No transcripts found, so this step has nothing to measure yet."]
    turns = report["turns"]
    over = turns["sessions_over_30"]
    under = max(report["sessions"] - over, 0)
    return [
        f"Sessions by turn count: at most 30 turns: {under:,}; over 30 turns: {over:,}.",
        f"Median session: {turns['median']:,.1f} turns. Longest 10%: {turns['p90']:,.1f}+ turns.",
        "What the plugin does today: /context-fabric:checkpoint saves a local note and tells "
        "you to /clear, and the coach adds a short note in long sessions. Both are experimental "
        "and their token saving is unproven.",
        "Next: read step 5 for the switches, and how to turn each one off.",
    ]


def _step_context(report: dict[str, Any]) -> list[str]:
    if report["sessions"] == 0:
        return ["No transcripts found, so this step has nothing to measure yet."]
    unused_skills = sum(group["count"] for group in report["never_used"]["plugins"])
    unused_connectors = len(report["never_used"]["connectors"])
    return [
        f"Before you type anything, a typical session starts with about "
        f"{report['prefix_tokens_estimated_median']:,.0f} est. tokens of setup "
        f"(median over {report['sessions']:,} sessions).",
        f"Never used in the last 30 days: {unused_skills:,} skills and {unused_connectors:,} "
        f"connectors, about {report['effect']['per_turn_tokens']:,.1f} est. tokens per turn.",
        "Names stay on your machine; this tour only counted them.",
        "Next: run cfxd diet --apply for a dry run that lists the exact disable commands; "
        "add --yes to apply, then cfxd diet --undo to restore.",
    ]


def _step_next() -> list[str]:
    return [
        "Every switch below is experimental and its saving is unproven. Turn it off the same "
        "way you turned it on.",
        "  1. cfxd set bound on - keeps only the last 150 lines of test and log commands (pytest, "
        "npm test, git log, docker logs). Look for whether the agent still finds what it "
        "needs; turn off with cfxd set bound off.",
        "  2. cfxd set coach on - adds a short coaching note when a session gets long. Look for "
        "whether it helps you notice context growth; turn off with cfxd set coach off.",
        "  3. /context-fabric:checkpoint then /clear - saves a local note and restores it once "
        "in the next session. Look for a restored context-fabric-carry block that matches what "
        "you were doing; clear it with cfxd checkpoint clear.",
        "  4. cfxd week --share - a weekly summary of counts and sizes. Look for the trend it "
        "shows and paste it if useful; it is one-shot, so there is nothing to turn off.",
        'Feedback: section "6. Send results" in docs/TESTING-GUIDE.md says where to post it. '
        "Nothing leaves your machine.",
    ]


def _pause(enabled: bool) -> None:
    if not enabled:
        return
    # The prompt goes to stderr so redirected stdout stays one step per line.
    sys.stderr.write("Press Enter for the next step...\n")
    sys.stderr.flush()
    try:
        sys.stdin.readline()
    except OSError:
        pass


def run(root: Path, yes: bool, state_dir: str | None = None) -> int:
    ensure_utf8_output()
    interactive = not yes and sys.stdin.isatty()

    report = audit(root, "30d")
    diet_report = diet(root, "30d")

    steps = (
        _step_doctor(state_dir),
        _step_tokens(report),
        _step_long(report),
        _step_context(diet_report),
        _step_next(),
    )
    for index, (heading, lines) in enumerate(zip(HEADINGS, steps, strict=True)):
        print(heading)
        for line in lines:
            print(line)
        print()
        if index < len(steps) - 1:
            _pause(interactive)
    return 0
