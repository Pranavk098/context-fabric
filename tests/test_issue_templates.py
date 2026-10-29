"""Issue templates: required fields, the leak checkbox, and the private vulnerability link."""

from __future__ import annotations

from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[1] / ".github" / "ISSUE_TEMPLATE"
SAFE_OUTPUT = "I pasted only `--share` output or counts, no prompts, paths or commands"


def test_issue_forms_have_required_keys() -> None:
    for name in ("bug.yml", "tester-feedback.yml"):
        text = (TEMPLATES / name).read_text(encoding="utf-8")
        for key in ("name:", "description:", "body:", "type: input", "type: textarea"):
            assert key in text, f"{name}: missing {key}"


def test_feedback_form_covers_the_template_fields() -> None:
    text = (TEMPLATES / "tester-feedback.yml").read_text(encoding="utf-8")
    for field in (
        "OS and version",
        "Claude Code version",
        "cfxd version",
        "Which experiments did you run?",
        "cfxd week --share output",
        "What surprised you?",
        "What broke, errored or felt slower?",
        "Would you keep using it?",
        "May we quote you in public?",
    ):
        assert field in text, f"tester-feedback.yml: missing {field}"
    assert SAFE_OUTPUT in text
    assert text.count(SAFE_OUTPUT) == 1


def test_blank_issues_disabled_with_private_vulnerability_link() -> None:
    text = (TEMPLATES / "config.yml").read_text(encoding="utf-8")
    assert "blank_issues_enabled: false" in text
    assert "contact_links:" in text
    assert "SECURITY.md" in text
    assert "Do not open a public issue for a vulnerability" in text
