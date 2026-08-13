"""Focused tests for the deterministic submission demo artifacts (wrap-up).

Proves the demo/safety claims WITHOUT any live provider, LLM, or applicant data.
The generator is run in-process against an in-memory evidence + normalization
stack and writes artifacts to a tmp dir.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone

from demos import submission_demo


def _run(tmp_path):
    now = datetime(2026, 8, 12, 12, 0, 0, tzinfo=timezone.utc)
    counter = {"n": 0}

    def uuidf() -> str:
        counter["n"] += 1
        return f"fixed-{counter['n']:04d}"

    return submission_demo.build_submission_demo(now=now, uuidf=uuidf, write_dir=tmp_path)


SENSITIVE = ["Test Applicant", "T0000-00000-00000", "1HGCM82633A000000",
             "1990-01-01", "M0A 0A0", "416-555", "@example.com"]


def test_sandbox_outcomes_labelled_estimate_only_not_live(tmp_path) -> None:
    report = _run(tmp_path)
    assert len(report["sandbox_outcomes"]) == 2
    for outcome in report["sandbox_outcomes"]:
        assert outcome["status"] == "estimate_only"
        assert outcome["source_environment"] == "local_sandbox"
        assert outcome["not_a_live_quote"] is True
        assert outcome["quote_vs_estimate"] == "estimate"
        assert "quoted" not in outcome["status"]


def test_sandbox_outcomes_cannot_be_confused_with_live_quotes(tmp_path) -> None:
    report = _run(tmp_path)
    for outcome in report["sandbox_outcomes"]:
        assert outcome["safe_source"].startswith("local sandbox")
        assert "Sonnet" not in outcome["source_identity"]
        assert "Square One" not in outcome["source_identity"]


def test_comparison_detects_coverage_differences_and_no_best(tmp_path) -> None:
    report = _run(tmp_path)
    fields = {v["field"] for v in report["coverage_ledger"]["variances"]}
    assert {"collision_deductible", "accident_forgiveness"} <= fields
    assert report["comparisons"]["lower_premium_is_not_labeled_best"] is True
    assert "best" not in report["comparisons"]["note"].lower().replace("best'", "")


def test_evidence_timestamps_and_ids_exist(tmp_path) -> None:
    report = _run(tmp_path)
    for outcome in report["sandbox_outcomes"]:
        assert outcome["evidence_identifier"]
        assert outcome["timestamp"]
        assert outcome["normalized_quote_id"]
    assert report["generated_at"]


def test_sonnet_remains_unresolved_and_quote_returned_false(tmp_path) -> None:
    report = _run(tmp_path)
    s = report["sonnet_outcome"]
    assert s["status"] == "unresolved"
    assert s["quote_returned"] is False
    assert s["access_control_confirmed"] is False
    assert s["last_confirmed_stage"] == "province_page"
    # The outcome must NOT be labeled with any of these; not_labeled documents
    # the labels we explicitly refuse to claim.
    not_labeled = {x.lower() for x in s["not_labeled"]}
    for forbidden in ("quoted", "blocked", "captcha", "access_denied", "successfully_autofilled"):
        assert forbidden not in (s["status"], s["last_confirmed_stage"])
        assert forbidden in not_labeled


def test_manual_handoff_remains_not_executed(tmp_path) -> None:
    report = _run(tmp_path)
    h = report["manual_handoff"]
    assert h["status"] == "manual_handoff"
    assert h["handoff_executed"] is False
    assert h["recording_consent"] == "not_requested"
    assert h["registry_id"]
    assert h["canonical_field_names"]  # field names only, no values


def test_no_sensitive_values_in_generated_artifacts(tmp_path) -> None:
    _run(tmp_path)
    for path in (tmp_path / "demo_run_report.json", tmp_path / "demo_run_report.md",
                 tmp_path / "market_registry.json"):
        blob = path.read_text(encoding="utf-8")
        for marker in SENSITIVE:
            assert marker not in blob, (path.name, marker)


def test_registry_export_valid_json_and_csv(tmp_path) -> None:
    report = _run(tmp_path)
    assert isinstance(report["market_registry_export"]["duplicate_suppression_count"], int)
    data = json.loads((tmp_path / "market_registry.json").read_text(encoding="utf-8"))
    assert data["records"]
    assert "demo_mode_disclosure" in data
    with (tmp_path / "market_registry.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows
    assert "distinct_rate_source_id" in rows[0]


def test_artifacts_declare_demo_data_not_live(tmp_path) -> None:
    report = _run(tmp_path)
    assert "DEMO DATA" in report["demo_mode_disclosure"]
    assert report["not_a_live_quote"] is True
    md = (tmp_path / "demo_run_report.md").read_text(encoding="utf-8")
    assert "DEMO DATA - LOCAL ESTIMATES, NOT LIVE INSURANCE QUOTES" in md
