"""Copy audit for tester guides, README non-evidence sections, and cfxd help strings (W2-03)."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from cfxd.cli import _build_parser

ROOT = Path(__file__).resolve().parents[1]
TESTERS_DIR = ROOT / "docs" / "testers"
README_PATH = ROOT / "README.md"
FEEDBACK_FORM = ROOT / ".github" / "ISSUE_TEMPLATE" / "tester-feedback.yml"

STUDY_DISCLOSURE = (
    "You are in a measurement study if you turn coach on. Half of testers see coach prompts "
    "and half do not; which half you are in is fixed by a hash of your install. You can leave "
    "the study any time with `cfxd set coach off`."
)

BANNED_RE = re.compile(
    r"(?:saves|saved|saving|reduces cost|cheaper|lower your bill)",
    re.IGNORECASE,
)
QUALIFIER_RE = re.compile(
    r"(?:no saving|not proven|does not claim)",
    re.IGNORECASE,
)
RETIRED_RE = re.compile(r"\b(?:fold|fold_read|bound|checkpoint)\b", re.IGNORECASE)
SMALLER_RE = re.compile(r"\bsmaller\b", re.IGNORECASE)
OBSERVED_DIET_LABEL = "observed (this machine, this project, N sessions)"

# Tiny allowlist for legacy argparse help strings in cfxd/cli.py (owned by W2-01, outside W2-03's
# file lock). Zero doc lines are allowlisted.
_CLI_HELP_ALLOWLIST: dict[str, str] = {
    # cfxd/cli.py:125 - legacy subcommand help on `cfxd stats`; cli.py is outside W2-03 scope.
    "cfxd stats": "report hook savings from the logs",
    # cfxd/cli.py:350 - legacy `--whatif` help on `cfxd routing`; cli.py is outside W2-03 scope.
    "cfxd routing --whatif": "cheaper sub-agent tier to re-price against (default: both)",
}


def _sentences(text: str) -> list[str]:
    """Split markdown into lines and sentences so qualifiers do not bleed across lines."""
    out: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        for part in re.split(r"(?<=[.!?])\s+", line):
            part = part.strip()
            if part:
                out.append(part)
    return out


def _violations(text: str) -> list[str]:
    """Return sentences containing a banned pattern without a qualifying negation."""
    bad: list[str] = []
    for sentence in _sentences(text):
        if BANNED_RE.search(sentence) and not QUALIFIER_RE.search(sentence):
            bad.append(sentence)
    return bad


def _readme_non_evidence(text: str) -> str:
    """Return all README text outside the `## Evidence so far` section."""
    evidence_header = "## Evidence so far"
    roadmap_header = "## Roadmap"
    assert evidence_header in text, "README.md missing '## Evidence so far' heading"
    assert roadmap_header in text, "README.md missing '## Roadmap' heading"
    before, rest = text.split(evidence_header, 1)
    _evidence, after = rest.split(roadmap_header, 1)
    return f"{before}\n{roadmap_header}{after}"


def _iter_help_entries(
    parser: argparse.ArgumentParser, prefix: str = "cfxd"
) -> list[tuple[str, str]]:
    """Collect (key, help_text) for every description, subcommand, and option in parser."""
    entries: list[tuple[str, str]] = []
    if parser.description:
        entries.append((f"{prefix} [description]", parser.description))
    for action in parser._actions:  # noqa: SLF001
        if action.help and action.help != argparse.SUPPRESS:
            opt = "/".join(action.option_strings) if action.option_strings else action.dest
            entries.append((f"{prefix} {opt}", action.help))
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            for choice_action in action._choices_actions:  # noqa: SLF001
                if choice_action.help and choice_action.help != argparse.SUPPRESS:
                    entries.append((f"{prefix} {choice_action.dest}", choice_action.help))
            for name, subparser in action.choices.items():
                entries.extend(_iter_help_entries(subparser, f"{prefix} {name}"))
    return entries


@pytest.mark.parametrize(
    "word",
    ["saves", "saved", "saving", "reduces cost", "cheaper", "lower your bill", "SAVES", "Cheaper"],
)
def test_sentence_audit_rejects_unqualified_claims_and_allows_qualified_ones(word: str) -> None:
    assert _violations(f"This feature {word} tokens on every turn.") != []
    assert _violations(f"This feature {word} tokens: no saving is proven.") == []
    assert _violations(f"Whether this {word} tokens is not proven yet.") == []
    assert _violations(f"The tool {word} nothing and does not claim a billing drop.") == []


def test_tester_guides_have_no_banned_copy() -> None:
    guides = sorted(TESTERS_DIR.glob("*.md"))
    assert guides, "no tester guides found in docs/testers/"
    failures: list[str] = []
    for path in guides:
        for bad in _violations(path.read_text(encoding="utf-8")):
            failures.append(f"{path.name}: {bad}")
    assert not failures, "banned copy in docs/testers/:\n" + "\n".join(failures)


def test_readme_non_evidence_has_no_banned_copy() -> None:
    non_evidence = _readme_non_evidence(README_PATH.read_text(encoding="utf-8"))
    failures = _violations(non_evidence)
    assert not failures, "banned copy in README.md non-evidence text:\n" + "\n".join(failures)


def test_cfxd_argparse_help_strings_have_no_unallowlisted_banned_copy() -> None:
    assert len(_CLI_HELP_ALLOWLIST) <= 2, "keep the CLI help allowlist tiny"
    failures: list[str] = []
    for key, help_text in _iter_help_entries(_build_parser()):
        if _CLI_HELP_ALLOWLIST.get(key) == help_text:
            continue
        for bad in _violations(help_text):
            failures.append(f"{key}: {bad!r}")
    assert not failures, "banned copy in cfxd argparse help:\n" + "\n".join(failures)


def test_tester_readme_carries_verbatim_coach_study_disclosure_on_first_screen() -> None:
    text = (TESTERS_DIR / "README.md").read_text(encoding="utf-8")
    first_screen = "\n".join(text.splitlines()[:10])
    assert STUDY_DISCLOSURE in first_screen, (
        "docs/testers/README.md must carry the verbatim coach study disclosure at the top"
    )
    # Verify `cfxd set coach off` is a valid command in `cfxd set`.
    parsed = _build_parser().parse_args(["set", "coach", "off"])
    assert parsed.command == "set"
    assert parsed.flag == "coach"
    assert parsed.value == "off"


def test_smaller_appears_only_for_diet_with_observed_label() -> None:
    targets: list[tuple[str, str]] = [
        ("README.md", _readme_non_evidence(README_PATH.read_text(encoding="utf-8"))),
        *((p.name, p.read_text(encoding="utf-8")) for p in sorted(TESTERS_DIR.glob("*.md"))),
    ]
    seen_in_diet = False
    for name, text in targets:
        for line in text.splitlines():
            if SMALLER_RE.search(line):
                assert name in ("diet.md", "README.md"), (
                    f"'smaller' is only allowed in diet copy, found in {name}: {line}"
                )
                assert OBSERVED_DIET_LABEL in line, (
                    f"{name}: 'smaller' must carry {OBSERVED_DIET_LABEL!r}: {line}"
                )
                if name == "diet.md":
                    seen_in_diet = True
    assert seen_in_diet, "docs/testers/diet.md should explain the observed 'smaller' label"


def test_retired_features_only_mentioned_as_retired() -> None:
    targets: list[tuple[str, str]] = [
        ("README.md", _readme_non_evidence(README_PATH.read_text(encoding="utf-8"))),
        *((p.name, p.read_text(encoding="utf-8")) for p in sorted(TESTERS_DIR.glob("*.md"))),
    ]
    for name, text in targets:
        for line in text.splitlines():
            if RETIRED_RE.search(line):
                assert "retired" in line.lower(), f"{name}: un-retired mention: {line}"


def test_guides_reflect_ground_truth_and_order() -> None:
    index_text = (TESTERS_DIR / "README.md").read_text(encoding="utf-8")
    assert (
        index_text.index("checkup.md")
        < index_text.index("diet.md")
        < index_text.index("brief-opt-in.md")
    )
    for term in ("prefix", "working set", "compaction"):
        assert term in index_text.lower(), f"docs/testers/README.md missing definition of {term}"

    checkup_text = (TESTERS_DIR / "checkup.md").read_text(encoding="utf-8")
    brief_text = (TESTERS_DIR / "brief-opt-in.md").read_text(encoding="utf-8")
    diet_text = (TESTERS_DIR / "diet.md").read_text(encoding="utf-8")

    for doc_name, doc_text in (("checkup.md", checkup_text), ("brief-opt-in.md", brief_text)):
        assert "limit-weighted new tokens" in doc_text.lower(), doc_name
        assert "limit-weighted/1" in doc_text, doc_name
        assert "--verbose" in doc_text, doc_name
        assert "D-3" in doc_text, doc_name
        assert "cf-share/2" in doc_text, doc_name
        assert "--include-name-digests" in doc_text, doc_name

    assert "--paste-safe" in brief_text
    assert "file-N" in brief_text

    # Every apply step in diet.md prints `cfxd diet --undo` beside it.
    diet_lines = diet_text.splitlines()
    apply_indices = [i for i, line in enumerate(diet_lines) if "diet --apply" in line]
    assert len(apply_indices) >= 2
    for idx in apply_indices:
        window = "\n".join(diet_lines[idx : idx + 3])
        assert "cfxd diet --undo" in window, f"diet.md line {idx + 1} missing cfxd diet --undo"

    form_text = FEEDBACK_FORM.read_text(encoding="utf-8")
    assert "Consent to be in the coach measurement study" in form_text
    assert "Consent to publish aggregated counts" in form_text
    assert (
        form_text.count("I pasted only `--share` output or counts, no prompts, paths or commands")
        == 1
    )
