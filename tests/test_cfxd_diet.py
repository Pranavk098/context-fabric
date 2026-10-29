"""cfxd diet tests: starting-prefix dead-weight analysis of Claude Code transcripts."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cfxd.diet import _anonymize, _norm, _skill_entry_bytes, diet, format_human

ROOT = Path(__file__).resolve().parents[1]


def _usage(input_tokens: int = 0) -> dict:
    return {"input_tokens": input_tokens, "output_tokens": 0, "cache_creation_input_tokens": 0}


def _assistant(*blocks: dict, usage: dict | None = None) -> dict:
    return {"type": "assistant", "message": {"content": list(blocks), "usage": usage or _usage()}}


def _user(*blocks: dict) -> dict:
    return {"type": "user", "message": {"content": list(blocks)}}


def _tool_use(tool_id: str, name: str, **inp: object) -> dict:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": inp}


def _attachment(att: dict) -> dict:
    return {"type": "attachment", "attachment": att}


def _skill_listing(names: list[str], content: str) -> dict:
    return _attachment(
        {"type": "skill_listing", "names": names, "content": content, "skillCount": len(names)}
    )


def _deferred_tools(names: list[str]) -> dict:
    return _attachment({"type": "deferred_tools_delta", "addedNames": names})


def _mcp_instructions(names: list[str], blocks: list[str]) -> dict:
    return _attachment(
        {"type": "mcp_instructions_delta", "addedNames": names, "addedBlocks": blocks}
    )


def _instructions(files: list[dict]) -> dict:
    return _attachment({"type": "instructions", "files": files})


def _hook(name: str, content: str) -> dict:
    return _attachment({"type": "hook_additional_context", "hookName": name, "content": [content]})


def _session(root: Path, project: str, session_id: str, lines: list[dict]) -> Path:
    path = root / project / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def _bytes(s: str) -> int:
    return len(s.encode("utf-8"))


def test_skill_entry_bytes_order_and_fallback() -> None:
    content = "- alpha:one: desc one\n- alpha:two: desc two\n- beta:three: desc three\n"
    assert _skill_entry_bytes(content, ["alpha:one", "alpha:two", "beta:three"]) == {
        "alpha:one": _bytes("- alpha:one: desc one"),
        "alpha:two": _bytes("- alpha:two: desc two"),
        "beta:three": _bytes("- beta:three: desc three"),
    }
    # Fallback when names is missing: parse the bullet's leading name.
    assert _skill_entry_bytes(content, None) == {
        "alpha:one": _bytes("- alpha:one: desc one"),
        "alpha:two": _bytes("- alpha:two: desc two"),
        "beta:three": _bytes("- beta:three: desc three"),
    }


def test_skill_entry_bytes_wrapped_lines() -> None:
    content = "- alpha:one: first line\n  continued here\n- alpha:two: second\n"
    assert _skill_entry_bytes(content, ["alpha:one", "alpha:two"]) == {
        "alpha:one": _bytes("- alpha:one: first line") + _bytes("  continued here"),
        "alpha:two": _bytes("- alpha:two: second"),
    }


def test_skills_used_vs_unused(tmp_path: Path) -> None:
    lines = [
        _skill_listing(
            ["alpha:used", "alpha:unused", "beta:other"],
            "- alpha:used: A\n- alpha:unused: B\n- beta:other: C\n",
        ),
        _assistant(_tool_use("s1", "Skill", skill="alpha:used")),
    ]
    _session(tmp_path, "proj", "s1", lines)
    report = diet(tmp_path, "all")

    skills = {it["name"]: it for it in report["items"] if it["kind"] == "skill"}
    assert skills["alpha:used"]["used"] is True
    assert skills["alpha:unused"]["used"] is False
    assert skills["beta:other"]["used"] is False
    assert skills["alpha:used"]["group"] == "alpha"
    assert skills["alpha:unused"]["group"] == "alpha"
    assert skills["beta:other"]["group"] == "beta"

    never = {g["plugin"]: g for g in report["never_used"]["plugins"]}
    assert set(never["alpha"]["skills"]) == {"alpha:unused"}
    assert set(never["beta"]["skills"]) == {"beta:other"}
    used = {g["plugin"]: g for g in report["used"]["plugins"]}
    assert set(used["alpha"]["skills"]) == {"alpha:used"}


def test_plugin_grouping_by_prefix(tmp_path: Path) -> None:
    lines = [
        _skill_listing(
            ["acme:x", "acme:y", "acme:z"],
            "- acme:x: 1\n- acme:y: 2\n- acme:z: 3\n",
        ),
        _assistant(_tool_use("s1", "Skill", skill="acme:x")),
    ]
    _session(tmp_path, "proj", "s1", lines)
    report = diet(tmp_path, "all")

    never = {g["plugin"]: g for g in report["never_used"]["plugins"]}
    assert never["acme"]["count"] == 2
    assert set(never["acme"]["skills"]) == {"acme:y", "acme:z"}
    used = {g["plugin"]: g for g in report["used"]["plugins"]}
    assert used["acme"]["count"] == 1
    assert used["acme"]["skills"] == ["acme:x"]


def test_connector_server_tools_and_instructions(tmp_path: Path) -> None:
    tools = ["mcp__srv__t1", "mcp__srv__t2", "mcp__srv__t3", "Bash", "Read"]
    block = "## srv\ninstructions here"
    lines = [
        _deferred_tools(tools),
        _mcp_instructions(["srv"], [block]),
        _assistant(),
    ]
    _session(tmp_path, "proj", "s1", lines)
    report = diet(tmp_path, "all")

    conns = [it for it in report["items"] if it["kind"] == "connector"]
    assert len(conns) == 1
    conn = conns[0]
    assert conn["name"] == "srv"
    assert conn["tools"] == 3
    assert conn["tool_names_bytes"] == sum(_bytes(t) for t in tools[:3])
    assert conn["instructions_bytes"] == _bytes(block)
    assert conn["median_bytes"] == conn["tool_names_bytes"] + conn["instructions_bytes"]
    assert conn["used"] is False
    assert report["never_used"]["connectors"][0]["name"] == "srv"


def test_connector_server_used(tmp_path: Path) -> None:
    lines = [
        _deferred_tools(["mcp__srv__t1"]),
        _assistant(_tool_use("c1", "mcp__srv__t1", arg=1)),
    ]
    _session(tmp_path, "proj", "s1", lines)
    report = diet(tmp_path, "all")

    conns = [it for it in report["items"] if it["kind"] == "connector"]
    assert conns[0]["used"] is True
    assert report["used"]["connectors"][0]["name"] == "srv"
    assert report["never_used"]["connectors"] == []


def test_memory_attachment_bytes(tmp_path: Path) -> None:
    content = "memory text here"
    lines = [
        _instructions([{"path": "/home/u/CLAUDE.md", "content": content, "type": "user"}]),
        _assistant(),
    ]
    _session(tmp_path, "proj", "s1", lines)
    report = diet(tmp_path, "all")

    mem = [it for it in report["items"] if it["kind"] == "memory"]
    assert len(mem) == 1
    assert mem[0]["name"] == "/home/u/CLAUDE.md"
    assert mem[0]["median_bytes"] == _bytes(content)


def test_hook_context_bytes(tmp_path: Path) -> None:
    lines = [
        _hook("PreToolUse:Bash", "injected context"),
        _assistant(),
    ]
    _session(tmp_path, "proj", "s1", lines)
    report = diet(tmp_path, "all")

    hooks = [it for it in report["items"] if it["kind"] == "hook"]
    assert hooks[0]["name"] == "PreToolUse:Bash"
    assert hooks[0]["median_bytes"] == _bytes("injected context")


def test_session_lacking_listing(tmp_path: Path) -> None:
    _session(tmp_path, "proj", "s1", [_assistant(_tool_use("b1", "Bash"))])
    _session(
        tmp_path,
        "proj",
        "s2",
        [_skill_listing(["p:a"], "- p:a: desc\n"), _assistant()],
    )
    report = diet(tmp_path, "all")
    assert report["sessions"] == 2
    assert report["listing_not_found"] == 1


def test_effect_arithmetic(tmp_path: Path) -> None:
    unused_line = "- p:unused: never\n"
    used_line = "- p:used: yes\n"
    lines = [
        _skill_listing(["p:unused", "p:used"], unused_line + used_line),
        _assistant(_tool_use("s1", "Skill", skill="p:used")),
        _assistant(),
        _assistant(),
        _assistant(),
    ]
    _session(tmp_path, "proj", "s1", lines)
    report = diet(tmp_path, "all")

    assert report["turns"] == 4
    per_turn = _bytes(unused_line.rstrip("\n")) / 4.0
    assert report["effect"]["per_turn_tokens"] == pytest.approx(round(per_turn, 1))
    assert report["effect"]["turns"] == 4
    assert report["effect"]["total_tokens"] == pytest.approx(round(per_turn * 4, 1))


def test_do_not_paste_line(tmp_path: Path) -> None:
    _session(tmp_path, "proj", "s1", [_assistant()])
    out = format_human(diet(tmp_path, "all"), share=False)
    assert out.splitlines()[0] == "contains names from your setup; do not paste"
    shared = format_human(diet(tmp_path, "all"), share=True)
    assert "do not paste" not in shared.splitlines()[0]


def test_default_output_shows_names(tmp_path: Path) -> None:
    _session(
        tmp_path,
        "proj",
        "s1",
        [_skill_listing(["acme:mytool"], "- acme:mytool: desc\n"), _assistant()],
    )
    out = format_human(diet(tmp_path, "all"), share=False)
    assert "acme" in out
    assert "mytool" in out


def test_share_hides_canaries_keeps_counts(tmp_path: Path) -> None:
    tools = ["mcp__canaryconn__t1", "mcp__canaryconn__t2"]
    lines = [
        _skill_listing(
            ["canaryplugin:canaryskill"],
            "- canaryplugin:canaryskill: secret desc\n",
        ),
        _deferred_tools(tools),
        _instructions([{"path": "/home/u/canaryfile.md", "content": "c", "type": "user"}]),
        _assistant(),
    ]
    _session(tmp_path, "proj", "s1", lines)

    for extra in ([], ["--json"]):
        result = subprocess.run(
            [sys.executable, "-m", "cfxd", "diet", "--root", str(tmp_path), "--share", *extra],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        for canary in ("canaryplugin", "canaryskill", "canaryconn", "canaryfile"):
            assert canary not in result.stdout
            assert canary not in result.stderr

    # Correct counts survive anonymisation.
    data = json.loads(
        subprocess.run(
            [sys.executable, "-m", "cfxd", "diet", "--root", str(tmp_path), "--share", "--json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout
    )
    conn = next(it for it in data["items"] if it["kind"] == "connector")
    assert conn["tools"] == 2
    assert conn["tool_names_bytes"] == sum(_bytes(t) for t in tools)
    skill = next(it for it in data["items"] if it["kind"] == "skill")
    assert skill["name"].startswith("skill-")
    assert skill["group"].startswith("plugin-")
    memory = next(it for it in data["items"] if it["kind"] == "memory")
    assert memory["name"].startswith("file-")


def _share_report(names: list[str]) -> dict:
    return {
        "items": [{"kind": "skill", "name": n, "group": "acme"} for n in names]
        + [{"kind": "connector", "name": n, "group": ""} for n in names],
        "subagent_types": ["explore"],
        "never_used": {
            "plugins": [{"plugin": "acme", "skills": names[:1]}],
            "connectors": [{"name": names[0]}],
        },
        "used": {"plugins": [], "connectors": []},
    }


def test_share_labels_are_ordinal_and_unique() -> None:
    for names in (["some-skill", "other-skill"], ["private-a", "private-b", "private-c"]):
        out = _anonymize(_share_report(names))
        skills = [it["name"] for it in out["items"] if it["kind"] == "skill"]
        conns = [it["name"] for it in out["items"] if it["kind"] == "connector"]
        assert skills == [f"skill-{i}" for i in range(1, len(names) + 1)]
        assert conns == [f"connector-{i}" for i in range(1, len(names) + 1)]
        assert len(set(skills)) == len(skills)
        assert len(set(conns)) == len(conns)
        for name in names:
            digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:4]
            assert f"skill-{digest}" not in skills
            assert f"connector-{digest}" not in conns
    # Same name -> same label inside one report.
    dup = _anonymize(
        {
            "items": [
                {"kind": "skill", "name": "x", "group": "g"},
                {"kind": "skill", "name": "x", "group": "g"},
            ],
            "subagent_types": [],
            "never_used": {"plugins": [], "connectors": []},
            "used": {"plugins": [], "connectors": []},
        }
    )
    assert [it["name"] for it in dup["items"]] == ["skill-1", "skill-1"]
    out = _anonymize(_share_report(["some-skill", "other-skill"]))
    assert out["items"][0]["group"] == "plugin-1"
    assert out["subagent_types"] == ["subagent-1"]
    assert out["never_used"]["plugins"][0]["skills"] == ["skill-1"]
    assert out["never_used"]["connectors"][0]["name"] == "connector-1"
    assert _norm("claude.ai Vercel") == _norm("claude_ai_Vercel") == "claudeaivercel"


def test_since_filter_by_mtime(tmp_path: Path) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    new = _session(tmp_path, "proj", "new", [_assistant()])
    old = _session(tmp_path, "proj", "old", [_assistant()])
    os.utime(new, ((now - timedelta(days=2)).timestamp(),) * 2)
    os.utime(old, ((now - timedelta(days=40)).timestamp(),) * 2)

    assert diet(tmp_path, "7d", now=now)["sessions"] == 1
    assert diet(tmp_path, "30d", now=now)["sessions"] == 1
    assert diet(tmp_path, "all", now=now)["sessions"] == 2


def test_empty_root(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    report = diet(empty, "all")
    assert report["sessions"] == 0
    assert format_human(report, dir_name="empty") == "No transcripts found under empty."


def test_cli_human_output(tmp_path: Path) -> None:
    _session(
        tmp_path,
        "proj",
        "s1",
        [_skill_listing(["acme:mytool"], "- acme:mytool: desc\n"), _assistant()],
    )
    result = subprocess.run(
        [sys.executable, "-m", "cfxd", "diet", "--root", str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "contains names from your setup; do not paste" in result.stdout
    assert "Never used in this window" in result.stdout
    assert "acme" in result.stdout


def test_cli_json_output(tmp_path: Path) -> None:
    _session(
        tmp_path,
        "proj",
        "s1",
        [_deferred_tools(["mcp__srv__t1"]), _assistant(_tool_use("c1", "mcp__srv__t1"))],
    )
    result = subprocess.run(
        [sys.executable, "-m", "cfxd", "diet", "--root", str(tmp_path), "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["sessions"] == 1
    assert data["used"]["connectors"][0]["name"] == "srv"


def test_subagent_types_recorded(tmp_path: Path) -> None:
    lines = [
        _assistant(_tool_use("a1", "Agent", subagent_type="general-purpose", prompt="go")),
    ]
    _session(tmp_path, "proj", "s1", lines)
    report = diet(tmp_path, "all")
    assert report["subagent_types"] == ["general-purpose"]


def test_first_turn_context_from_usage_is_reported(tmp_path: Path) -> None:
    # The itemized setup prefix is only part of the first-turn context; show the real figure too.
    lines = [
        _skill_listing(["alpha:one"], "- alpha:one: A\n"),
        _assistant(usage={"input_tokens": 100, "cache_creation_input_tokens": 900}),
    ]
    _session(tmp_path, "proj", "s1", lines)
    report = diet(tmp_path, "all")
    assert report["first_turn_tokens_median"] == 1000
    text = format_human(report)
    assert "First-turn context" in text
    assert "system prompt and tool definitions" in text


def test_effect_is_computed_per_session_not_from_summed_medians(tmp_path: Path) -> None:
    # Session A has a never-used connector (400 bytes) and 3 turns; session B lacks it, 2 turns.
    # Effect = 400/4 tokens x 3 turns = 300 tokens, over 5 turns = 60 tokens per turn (not 100).
    block = "x" * 387
    listing = _skill_listing(["alpha:used"], "- alpha:used: A\n")
    a = [
        listing,
        _deferred_tools(["mcp__gone__t1"]),
        _mcp_instructions(["gone"], [block]),
        _assistant(_tool_use("s1", "Skill", skill="alpha:used")),
        _assistant(),
        _assistant(),
    ]
    b = [listing, _assistant(), _assistant()]
    _session(tmp_path, "proj", "a", a)
    _session(tmp_path, "proj", "b", b)
    report = diet(tmp_path, "all")

    assert report["turns"] == 5
    assert report["effect"]["total_tokens"] == 300
    assert report["effect"]["per_turn_tokens"] == 60
    (conn,) = report["never_used"]["connectors"]
    assert (
        conn["per_turn_tokens"] == 50.0
    )  # expected cost per session: 400 bytes in 1 of 2 sessions
    assert conn["share"] == round(200 / 215, 4)  # mean prefix per session: (415 + 15) / 2


def test_cli_survives_a_legacy_windows_console_encoding(tmp_path: Path) -> None:
    # A CJK skill name must not crash `cfxd diet` on a legacy cp1252 console.
    lines = [_skill_listing(["技能:héllo"], "- 技能:héllo: A\n"), _assistant()]
    _session(tmp_path, "proj", "s1", lines)
    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}
    proc = subprocess.run(
        [sys.executable, "-m", "cfxd", "diet", "--root", str(tmp_path), "--since", "all"],
        capture_output=True,
        env=env,
        cwd=ROOT,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert b"Traceback" not in proc.stderr
    assert b"llo" in proc.stdout  # the skill name was printed, not silently skipped
