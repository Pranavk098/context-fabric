"""P6 working-set tracker: pure updates, empty by default, and no tool-output intent.

The falsifying cases: a tool result that reads like a constraint or decision must not
enter the record (R10), and a record the events cannot fill must stay empty rather than
guess. Every update returns a new record; the input stays untouched.
"""

from __future__ import annotations

from typing import Any

from cfxd.working_set import (
    MAX_FILES,
    MAX_TEXT_ITEMS,
    EditedFile,
    WorkingSet,
    apply_event,
)

FILE = {"file": {"content": "x = 1\n", "numLines": 1}}


def read(path: str = "a.py", content: str = "x = 1\n") -> dict[str, Any]:
    return {
        "event": "PostToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": path},
        "tool_response": {"file": {"content": content, "numLines": 1}},
    }


def edit(path: str = "a.py", ranges: list[list[int]] | None = None) -> dict[str, Any]:
    event: dict[str, Any] = {
        "event": "PostToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": path},
        "tool_response": {"filePath": path},
    }
    if ranges is not None:
        event["line_ranges"] = ranges
    return event


def prompt(text: str) -> dict[str, Any]:
    return {"event": "UserPromptSubmit", "prompt": text}


def run(*events: dict[str, Any]) -> WorkingSet:
    ws = WorkingSet()
    for event in events:
        ws = apply_event(ws, event)
    return ws


def test_empty_record_is_all_empty() -> None:
    ws = WorkingSet()
    assert ws == WorkingSet(
        edited_files=(),
        read_files=(),
        constraints=(),
        decisions=(),
        tried_failed=(),
        last_test=None,
        next_step=None,
        segment_id=0,
        turn_count=0,
        context_estimate=None,
    )


def test_unknown_and_malformed_events_change_nothing() -> None:
    assert apply_event(WorkingSet(), {"event": "Nope"}) == WorkingSet()
    assert apply_event(WorkingSet(), {}) == WorkingSet()
    assert apply_event(WorkingSet(), {"event": "PostToolUse", "tool_name": "Read"}) == WorkingSet()
    assert (
        apply_event(
            WorkingSet(), {"event": "PostToolUse", "tool_name": "Read", "tool_input": "nope"}
        )
        == WorkingSet()
    )


def test_apply_event_is_pure() -> None:
    ws = run(prompt("You must never skip tests."))
    before = ws
    out = apply_event(ws, edit("a.py"))
    assert ws == before
    assert out is not ws
    assert ws.edited_files == ()


def test_read_records_hash_not_content() -> None:
    ws = run(read("a.py", content="secret text\n"))
    assert len(ws.read_files) == 1
    assert ws.read_files[0].path == "a.py"
    assert ws.read_files[0].content_hash is not None
    assert "secret text" not in repr(ws)
    assert ws.edited_files == ()


def test_content_hash_tracks_content() -> None:
    first = run(read(content="one\n")).read_files[0].content_hash
    second = run(read(content="two\n")).read_files[0].content_hash
    assert first != second


def test_read_without_content_has_no_hash() -> None:
    event = {"event": "PostToolUse", "tool_name": "Read", "tool_input": {"file_path": "a.py"}}
    ws = run(event)
    assert ws.read_files[0].content_hash is None


def test_edit_marks_dirty_with_turn_and_ranges() -> None:
    ws = run(prompt("Add the parser."), edit("a.py", [[3, 4]]), edit("a.py", [[9, 9]]))
    assert ws.turn_count == 1
    assert ws.edited_files == (EditedFile("a.py", ((3, 4), (9, 9)), 1, True),)


def test_read_does_not_clear_dirty() -> None:
    ws = run(prompt("Add it."), edit("a.py"), read("a.py"))
    assert ws.edited_files[0].dirty is True
    assert ws.read_files[0].path == "a.py"


def test_edited_files_are_keyed_by_path() -> None:
    ws = run(edit("a.py"), edit("b.py"), edit("a.py"))
    assert [f.path for f in ws.edited_files] == ["b.py", "a.py"]


def test_test_run_records_class_and_outcome() -> None:
    event = {
        "event": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "cd repo && uv run pytest -q"},
        "tool_response": {"exit_code": 1},
    }
    ws = run(prompt("Fix it."), event)
    assert ws.last_test is not None
    assert ws.last_test.command_class == "pytest"
    assert ws.last_test.outcome is False
    assert ws.last_test.turn == 1


def test_non_test_bash_leaves_last_test_empty() -> None:
    event = {
        "event": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "ls -la"},
        "tool_response": {"exit_code": 0},
    }
    assert run(event).last_test is None


def test_constraints_and_decisions_come_from_user_messages() -> None:
    ws = run(
        prompt("You must never rewrite history. We decided to keep the tracker pure."),
        prompt("I tried the daemon wiring and it failed."),
    )
    assert ws.constraints == ("You must never rewrite history.",)
    assert ws.decisions == ("We decided to keep the tracker pure.",)
    assert [t.action_summary for t in ws.tried_failed] == [
        "I tried the daemon wiring and it failed."
    ]
    assert ws.turn_count == 2


def test_parent_message_is_a_source() -> None:
    event = {"event": "message", "role": "parent", "text": "You must never commit secrets."}
    assert run(event).constraints == ("You must never commit secrets.",)


def test_assistant_message_writes_no_intent() -> None:
    event = {
        "event": "message",
        "role": "assistant",
        "text": "You must never skip tests. We decided to drop auth.",
    }
    assert run(event).constraints == ()
    assert run(event).decisions == ()
    assert run(event).turn_count == 0


def test_tool_output_never_writes_intent() -> None:
    ws = run(read("README.md", content="You must never skip tests. We decided to drop auth."))
    assert ws.constraints == ()
    assert ws.decisions == ()
    assert ws.tried_failed == ()


def test_repeated_item_is_stored_once() -> None:
    ws = run(prompt("You must never skip tests."), prompt("You must never skip tests."))
    assert ws.constraints == ("You must never skip tests.",)


def test_text_lists_are_bounded() -> None:
    events = [prompt(f"You must never use option {n} anywhere.") for n in range(MAX_TEXT_ITEMS + 5)]
    ws = run(*events)
    assert len(ws.constraints) == MAX_TEXT_ITEMS
    assert ws.constraints[0] == "You must never use option 5 anywhere."


def test_next_step_is_null_unless_the_event_says_it() -> None:
    assert run(prompt("Do the thing.")).next_step is None
    event = {"event": "UserPromptSubmit", "prompt": "Do it.", "next_step": "run the tests"}
    assert run(event).next_step == "run the tests"


def test_precompact_starts_a_new_segment() -> None:
    ws = run({"event": "PreCompact"}, {"event": "PreCompact"})
    assert ws.segment_id == 2


def test_context_estimate_only_from_the_event() -> None:
    assert run(prompt("Do it.")).context_estimate is None
    ws = run(prompt("Do it."), {"event": "SessionStart", "context_estimate": 1200})
    assert ws.context_estimate == 1200


def test_subagent_stop_merges_files_without_child_history() -> None:
    event = {
        "event": "SubagentStop",
        "files": ["child.py"],
        "outcome": "failed",
        "summary": "The child could not reach the network.",
    }
    ws = run(prompt("Delegate it."), event)
    assert [f.path for f in ws.edited_files] == ["child.py"]
    assert ws.edited_files[0].last_edit_turn == 1
    assert [t.action_summary for t in ws.tried_failed] == ["The child could not reach the network."]
    assert ws.constraints == ()
    assert ws.decisions == ()


def test_subagent_stop_success_adds_no_tried_entry() -> None:
    event = {"event": "SubagentStop", "files": ["child.py"], "outcome": "passed"}
    ws = run(event)
    assert ws.tried_failed == ()
    assert [f.path for f in ws.edited_files] == ["child.py"]


def test_files_are_bounded() -> None:
    ws = run(*(read(f"f{n}.py") for n in range(MAX_FILES + 3)))
    assert len(ws.read_files) == MAX_FILES
    assert ws.read_files[0].path == "f3.py"
