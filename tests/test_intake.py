"""P10 intake: validation, quarantine reason codes, and consent-gated aggregation.

Synthetic payloads only. The validator must never repair a record, the quarantine log must
carry no payload text, and the aggregator must refuse a paper table below three consented
developers.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import intake_aggregate as agg  # noqa: E402
import intake_validate as val  # noqa: E402

TEMPLATES = ROOT / ".github" / "ISSUE_TEMPLATE"


def _record(
    developer: str = "dev-01",
    rediscovery: float | None = 2.0,
    new_tokens: float | None = 12_000.0,
    sessions: int = 4,
    consent: bool = True,
) -> dict[str, Any]:
    return {
        "share": {
            "schema": "cf-share/2",
            "plugin_version": "0.1.1a1",
            "metric": "limit-weighted/1",
            "period_days": 7,
            "sessions": sessions,
            "turns": sessions * 30,
            "children": sessions,
            "children_by_model_family": {"opus": 1, "sonnet": sessions - 2, "haiku": 1},
            "new_token_equivalents": new_tokens,
            "cache_read_tokens": 67_890,
            "rediscovery_share_pct": rediscovery,
            "elisions": 3,
            "elided_bytes": 900,
            "consent_aggregate_publish": consent,
        },
        "feedback": {
            "developer": developer,
            "os_family": "windows",
            "task_type": "feature",
            "repo_type": "own",
            "brief_enabled": True,
            "brief_sessions": 2,
            "carry_enabled": False,
            "carry_sessions": 0,
            "friction": {"install": 0, "comprehension": 1, "trust": 0, "performance": 2},
            "consent_quote_free_text": False,
        },
    }


def _invalid_case(name: str) -> tuple[dict[str, Any], str]:
    record = _record()
    if name == "wrong schema":
        record["share"]["schema"] = "cf-share/3"
        return record, "schema"
    if name == "bad version":
        record["share"]["plugin_version"] = "my build /home/user"
        return record, "version"
    if name == "unknown metric":
        record["share"]["metric"] = "tokens/1"
        return record, "version"
    if name == "children above the plausible max":
        record["share"]["children"] = record["share"]["sessions"] * 10_000
        return record, "range"
    if name == "percentage above 100":
        record["share"]["rediscovery_share_pct"] = 150.0
        return record, "range"
    if name == "negative count":
        record["share"]["turns"] = -1
        return record, "range"
    if name == "brief sessions above sessions":
        record["feedback"]["brief_sessions"] = record["share"]["sessions"] + 1
        return record, "range"
    if name == "missing feedback field":
        del record["feedback"]["friction"]
        return record, "shape"
    if name == "unknown feedback field":
        record["feedback"]["note"] = "free text is not an intake field"
        return record, "shape"
    if name == "unknown share field":
        record["share"]["repo_name"] = "CANARY-REPO"
        return record, "shape"
    if name == "uncontrolled task type":
        record["feedback"]["task_type"] = "vibes"
        return record, "shape"
    raise AssertionError(f"unknown case {name}")


def _write(path: Path, record: Any) -> Path:
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_valid_payload_is_accepted_unchanged(tmp_path: Path) -> None:
    record = _record()
    out = tmp_path / "intake"
    assert val.main([str(_write(tmp_path / "one.json", record)), "--out", str(out)]) == 0
    accepted = _lines(out / "accepted.jsonl")
    assert accepted == [record]  # no coercion, no added fields
    assert not (out / "quarantine.jsonl").exists()


@pytest.mark.parametrize(
    "name",
    [
        "wrong schema",
        "bad version",
        "unknown metric",
        "children above the plausible max",
        "percentage above 100",
        "negative count",
        "brief sessions above sessions",
        "missing feedback field",
        "unknown feedback field",
        "unknown share field",
        "uncontrolled task type",
    ],
)
def test_each_defect_gets_its_reason_code(tmp_path: Path, name: str) -> None:
    record, expected = _invalid_case(name)
    out = tmp_path / "intake"
    assert val.main([str(_write(tmp_path / "bad.json", record)), "--out", str(out)]) == 0
    assert not (out / "accepted.jsonl").exists()
    [entry] = _lines(out / "quarantine.jsonl")
    assert entry["reason"] == expected


def test_truncated_json_is_quarantined(tmp_path: Path) -> None:
    out = tmp_path / "intake"
    bad = tmp_path / "cut.json"
    bad.write_text('{"share": {"schema": "cf-share/2"', encoding="utf-8")
    assert val.main([str(bad), "--out", str(out)]) == 0
    [entry] = _lines(out / "quarantine.jsonl")
    assert entry["reason"] == "json"


def test_duplicate_payload_is_quarantined_without_repair(tmp_path: Path) -> None:
    record = _record()
    first = _write(tmp_path / "a.json", record)
    second = _write(tmp_path / "b.json", copy.deepcopy(record))
    out = tmp_path / "intake"
    assert val.main([str(first), str(second), "--out", str(out)]) == 0
    assert len(_lines(out / "accepted.jsonl")) == 1
    [entry] = _lines(out / "quarantine.jsonl")
    assert entry["reason"] == "duplicate"
    # A later run appends the same payload again; the accepted log is the dedupe seed.
    assert val.main([str(first), "--out", str(out)]) == 0
    entries = _lines(out / "quarantine.jsonl")
    assert [e["reason"] for e in entries] == ["duplicate", "duplicate"]
    assert len(_lines(out / "accepted.jsonl")) == 1


def test_quarantine_log_carries_no_payload_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    record, _ = _invalid_case("unknown share field")
    record["feedback"]["developer"] = "CANARY-DEVELOPER"
    out = tmp_path / "intake"
    assert val.main([str(_write(tmp_path / "leak.json", record)), "--out", str(out)]) == 0
    captured = capsys.readouterr()
    written = (out / "quarantine.jsonl").read_text(encoding="utf-8")
    assert "CANARY" not in written
    assert "CANARY" not in captured.out
    entry = json.loads(written)
    assert set(entry) == {"reason", "payload_sha256"}
    assert len(entry["payload_sha256"]) == 64


def test_dry_run_ten_payloads_three_invalid(tmp_path: Path) -> None:
    out = tmp_path / "intake"
    files: list[str] = []
    for index in range(7):
        record = _record(developer=f"dev-{index + 1:02d}", rediscovery=float(index + 1))
        files.append(str(_write(tmp_path / f"good{index}.json", record)))
    for index, case in enumerate(
        ["wrong schema", "children above the plausible max", "missing feedback field"]
    ):
        record, _ = _invalid_case(case)
        files.append(str(_write(tmp_path / f"bad{index}.json", record)))
    assert val.main([*files, "--out", str(out)]) == 0
    assert len(_lines(out / "accepted.jsonl")) == 7
    reasons = sorted(entry["reason"] for entry in _lines(out / "quarantine.jsonl"))
    assert reasons == ["range", "schema", "shape"]


def _aggregate(records: list[dict[str, Any]]) -> str:
    return agg.render(agg.aggregate(records))


def test_below_three_developers_prints_qualitative_only() -> None:
    text = _aggregate([_record("dev-01"), _record("dev-02", rediscovery=4.0)])
    assert "qualitative round only" in text
    assert "| developer |" not in text
    assert "median" not in text
    assert "Consented developers: 2" in text


def test_consent_is_enforced_in_the_aggregator() -> None:
    records = [
        _record("dev-01"),
        _record("dev-02", rediscovery=4.0),
        _record("dev-03", rediscovery=10.0),
        _record("dev-04", consent=False, rediscovery=99.0),
    ]
    text = _aggregate(records)
    assert "Consented to aggregate-and-publish counts" in text
    row_lines = [line for line in text.splitlines() if line.startswith("| dev-")]
    assert {line.split("|")[1].strip() for line in row_lines} == {"dev-01", "dev-02", "dev-03"}
    assert "dev-04" not in text  # a non-consenting record is counted, never named
    assert "excluded from every table: 1" in text


def test_per_developer_median_and_range_never_a_pooled_mean() -> None:
    records = [
        _record("dev-01", rediscovery=4.0),
        _record("dev-02", rediscovery=1.0),
        _record("dev-03", rediscovery=10.0),
    ]
    text = _aggregate(records)
    assert "median 4.0%" in text
    assert "range 1.0% to 10.0%" in text
    assert "(3 developers)" in text
    assert "mean" not in text.lower()


def test_one_developer_submitting_twice_does_not_reach_the_floor() -> None:
    records = [
        _record("dev-01", sessions=4),
        _record("dev-01", sessions=6, rediscovery=5.0),
        _record("dev-02", rediscovery=4.0),
    ]
    text = _aggregate(records)
    assert "qualitative round only" in text
    assert "Consented developers: 2" in text
    assert "| developer |" not in text


def test_three_distinct_developers_clear_the_floor() -> None:
    records = [
        _record("dev-01"),
        _record("dev-01", sessions=6, rediscovery=5.0),
        _record("dev-02", rediscovery=4.0),
        _record("dev-03", rediscovery=10.0),
    ]
    text = _aggregate(records)
    assert "| developer |" in text
    assert "Consented developers (one row each): 3" in text
    assert "(3 developers)" in text  # the range counts distinct developers, not records


def test_developer_row_is_the_median_of_their_records() -> None:
    records = [
        _record("dev-01", rediscovery=1.0, sessions=4),
        _record("dev-01", rediscovery=7.0, sessions=7),
        _record("dev-02", rediscovery=2.0),
        _record("dev-03", rediscovery=6.0),
    ]
    text = _aggregate(records)
    dev01 = next(line for line in text.splitlines() if line.startswith("| dev-01 "))
    assert "4.0%" in dev01  # median of 1.0 and 7.0, not the pooled 8.0/2
    assert "| 6 |" in dev01  # median sessions 5.5 rounded to a whole count
    assert "| 2 |" in dev01  # submissions column
    assert "median 4.0%" in text
    assert "range 2.0% to 6.0%" in text


def test_rows_are_ordered_by_developer_whatever_the_input_order() -> None:
    records = [
        _record("dev-03"),
        _record("dev-01", sessions=6),
        _record("dev-02", rediscovery=4.0),
        _record("dev-01", rediscovery=3.0),
    ]
    developers = [
        line.split("|")[1].strip()
        for line in _aggregate(records).splitlines()
        if line.startswith("| dev-")
    ]
    assert developers == ["dev-01", "dev-02", "dev-03"]


def test_unconsented_records_of_a_consenting_developer_are_the_only_exclusion() -> None:
    records = [
        _record("dev-01", rediscovery=2.0),
        _record("dev-01", consent=False, rediscovery=50.0),
        _record("dev-02", rediscovery=4.0),
        _record("dev-03", rediscovery=6.0),
    ]
    text = _aggregate(records)
    dev01 = next(line for line in text.splitlines() if line.startswith("| dev-01 "))
    assert "2.0%" in dev01  # the unconsented record never reaches the row
    assert "| 1 |" in dev01  # one consented submission
    assert "excluded from every table: 1" in text
    assert "Consented developers (one row each): 3" in text


def test_limit_share_is_unavailable_and_tokens_lead() -> None:
    records = [_record(f"dev-0{i}") for i in range(1, 4)]
    text = _aggregate(records)
    assert "No limit share is available for any tier" in text
    assert text.index("## Limit metric") < text.index("## Per developer")
    assert "limit-weighted new tokens" in text
    assert "12,000.00" in text


def test_no_saving_claims() -> None:
    records = [_record(f"dev-0{i}") for i in range(1, 4)]
    text = _aggregate(records)
    for banned in ("saves", "saved", "reduces cost"):
        assert banned not in text.lower()
    assert "No saving is proven." in text
    assert "*observed*" in text


def test_unreadable_lines_are_counted_not_used(tmp_path: Path) -> None:
    source = tmp_path / "accepted.jsonl"
    source.write_text(
        json.dumps(_record("dev-01")) + "\nnot json\n" + json.dumps(_record("dev-02")) + "\n",
        encoding="utf-8",
    )
    records, unreadable = agg.load(source)
    assert len(records) == 2 and unreadable == 1
    assert "Unreadable lines skipped: 1" in agg.render(agg.aggregate(records, unreadable))


def test_form_carries_the_intake_vocabulary() -> None:
    text = (TEMPLATES / "tester-feedback.yml").read_text(encoding="utf-8")
    for option in (
        "Feature work",
        "Bug fixes",
        "Refactoring",
        "Exploration",
        "Own project",
        "Employer",
        "Open source",
        "Install",
        "Comprehension",
        "Trust",
        "Performance",
        "Brief sessions",
        "Carry sessions",
    ):
        assert option in text, f"tester-feedback.yml: missing {option}"
