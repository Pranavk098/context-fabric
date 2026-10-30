"""Offline check: do facts stated in an early user turn survive in the Brief v1 packet?

Builds a synthetic transcript (facts in turn 1, then N later turns whose text contains
constraint-shaped sentences, as a working session does), renders the packet for each child
model, and reports which planted facts are still present. No model calls, no cost.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from cfxd.brief_intent import extract_intent
from cfxd.brief_render import render
from cfxd.brief_select import resolve_target, select
from cfxd.session_state import SessionFacts

NOW = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)
PROMPT = "Implement the change in src/app/core.py"
FACTS = (
    "Put the lookup helper in a new module and call it find_thing.",
    "Use the constant THING_SEPARATOR = ' / ' between items.",
    "An empty item must raise ValueError with the exact message items must be non-empty.",
)
CHATTER = (
    "I must not edit the tests directory while exploring.",
    "We should always run the full test suite before delegating.",
    "Do not touch the vendored files under third_party.",
    "Never commit directly to main in this repo.",
    "Only change files that the task names.",
    "Avoid renaming public functions unless required.",
)


def _rec(kind: str, text: str) -> str:
    content: object = text if kind == "user" else [{"type": "text", "text": text}]
    return json.dumps({"type": kind, "message": {"role": kind, "content": content}})


def survivors(model: str, later_turns: int, per_turn: int, role: str = "assistant") -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        lines = [_rec("user", "Task context. " + " ".join(FACTS))]
        for n in range(later_turns):
            note = " ".join(
                f"{CHATTER[(n + k) % len(CHATTER)].rstrip('.')} in step {n}-{k}."
                for k in range(per_turn)
            )
            lines.append(_rec(role, note))
        path = root / "t.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        facts = SessionFacts("s", (), (), (), ())
        sel = select(facts, PROMPT, resolve_target({"model": model}))
        intent = extract_intent(str(path), PROMPT, facts)
        out = render(sel, intent, cwd=str(root), session_id="s", now=NOW).text
    return sum(1 for f in FACTS if f in out)


def main() -> int:
    turns = (0, 2, 5, 10, 20)
    for role in ("assistant", "user"):
        print(
            f"{len(FACTS)} early facts; columns = later {role} turns (3 constraint sentences each)"
        )
        print("model  " + "  ".join(f"{t:>3}" for t in turns))
        for model in ("haiku", "sonnet", "opus"):
            row = "  ".join(f"{survivors(model, t, 3, role):>3}" for t in turns)
            print(f"{model:<6} {row}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
