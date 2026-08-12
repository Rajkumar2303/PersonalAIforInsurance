"""Preserved evidence-backed live blocker (Square One / callback_required).

Captured from the real controlled LIVE run (no quote returned):

    run_id      = 9be37dc55e8747dd8a922131efddcb3c
    session_id  = fa950e310cb6405ba538288bd5cc6598
    attempt_id  = 606d629ee9954503a5e6444cc97611d3
    outcome     = callback_required   (browser opened then closed; callback
                                       barrier detected; NO quote returned)

The evidence was exported from the running backend, verified to contain no
applicant values, and preserved here as hermetic regression fixtures so the
genuine evidence-backed live blocker survives server restarts and is
verifiable in CI. The outcome is NEVER changed and no premium is fabricated.

Hermetic: reads ONLY the redacted fixture files - no real browser, no LLM, no
LangSmith, no network.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

from app.core.redaction import ONTARIO_LICENCE_PATTERN
from app.models.evidence import EvidenceRecordView

from evidence_helpers import SENSITIVE_MARKERS, assert_evidence_privacy_safe

FIXTURES = Path(__file__).resolve().parent / "fixtures"
EVIDENCE_FIXTURE = FIXTURES / "live_square_one_callback_required.json"
ROUTE_SUMMARY_FIXTURE = FIXTURES / "live_square_one_route_summary.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _by_event_type(export: dict) -> dict[str, dict]:
    return {record["event_type"]: record for record in export["evidence"]}


# ---------------------------------------------------------------------------
# Task 1 - exact terminal reason + evidence timestamp
# ---------------------------------------------------------------------------


def test_captured_evidence_chain_is_genuine_callback_required() -> None:
    """The preserved chain contains the full lifecycle for Square One ending in
    callback_required - nothing was edited, no outcome was changed."""
    data = _load(EVIDENCE_FIXTURE)
    export = data["evidence_export"]
    by_type = _by_event_type(export)

    # The exact lifecycle observed in the real run, in order.
    assert set(by_type) == {
        "consent_event",
        "route_planned",
        "attempt_started",
        "callback_observed",
        "recovery_decision",
        "attempt_completed",
    }

    # Square One / Zurich rate source is threaded through the attempt evidence.
    for event in ("attempt_started", "callback_observed", "recovery_decision", "attempt_completed"):
        assert by_type[event]["registry_id"] == "square-one"
        assert by_type[event]["distinct_rate_source_id"] == "RS-ZURICH-AUTO"

    # The callback barrier is evidence-backed, not inferred.
    cb = by_type["callback_observed"]
    assert cb["page_signature"] == "square-one_landing"
    assert cb["safe_url"] == "www.squareone.ca/auto-insurance/"
    assert cb["observation_type"] == "callback_detected"
    assert cb["source_channel"] == "browser"
    assert cb["payload"]["checkpoint_type"] == "callback"
    assert cb["payload"]["automation_decision"] == "escalate"
    assert cb["payload"]["requires_human"] is True
    assert cb["payload"]["must_not_automate"] is False

    # The terminal decision is exactly callback_required.
    recovery = by_type["recovery_decision"]["payload"]
    assert recovery["lifecycle_status"] == "terminal"
    assert recovery["terminal_status"] == "callback_required"
    assert recovery["reason_codes"] == ["callback_required"]
    assert recovery["recommended_action"] == "prepare_voice_handoff"
    assert recovery["retry_allowed"] is False
    assert recovery["quote_pending_normalization"] is False


def test_captured_evidence_timestamps_and_sequence() -> None:
    data = _load(EVIDENCE_FIXTURE)
    export = data["evidence_export"]
    by_type = _by_event_type(export)

    def ts(record: dict) -> dt.datetime:
        return dt.datetime.fromisoformat(record["observed_at"].replace("Z", "+00:00"))

    # Consent -> plan -> attempt start -> callback observed -> terminal decision.
    assert ts(by_type["consent_event"]) <= ts(by_type["route_planned"])
    assert ts(by_type["route_planned"]) <= ts(by_type["attempt_started"])
    assert ts(by_type["attempt_started"]) <= ts(by_type["callback_observed"])
    assert ts(by_type["callback_observed"]) <= ts(by_type["recovery_decision"])
    assert ts(by_type["recovery_decision"]) == ts(by_type["attempt_completed"])

    # The callback barrier timestamp (the moment the live page demanded a
    # callback) is preserved exactly.
    assert ts(by_type["callback_observed"]).isoformat() == "2026-08-12T21:21:45.197592+00:00"


# ---------------------------------------------------------------------------
# Task 6 - no premium fabricated, outcome unchanged
# ---------------------------------------------------------------------------


def test_captured_evidence_contains_no_quote() -> None:
    """The genuine live run returned NO quote - preserved exactly."""
    data = _load(EVIDENCE_FIXTURE)
    export = data["evidence_export"]
    assert export["quote_count"] == 0
    assert export["evidence_count"] == 6
    assert export["audit_event_count"] == 0
    # No quote observation was recorded anywhere in the evidence chain.
    assert not any(r["event_type"].startswith("quote") for r in export["evidence"])


def test_route_summary_preserves_callback_outcome_no_premium() -> None:
    """The comparison-run route summary keeps Square One as callback_required
    with NO annual premium - the UI must render exactly this, never a number."""
    summary = _load(ROUTE_SUMMARY_FIXTURE)
    assert summary["registry_id"] == "square-one"
    assert summary["display_name"] == "Square One"
    assert summary["channel"] == "browser"
    assert summary["status"] == "callback_required"
    assert summary["route_outcome_semantics"] == "callback_required"
    assert summary["terminal_status"] == "callback_required"
    assert summary["reason_codes"] == ["callback_required"]
    assert summary["annual_premium"] is None
    assert summary["quote_observation_id"] is None
    assert summary["normalized_quote_id"] is None
    assert summary["distinct_rate_source_id"] == "RS-ZURICH-AUTO"


# ---------------------------------------------------------------------------
# Task 2 - redacted evidence contains no applicant values
# ---------------------------------------------------------------------------


def test_captured_evidence_is_redacted_no_applicant_values() -> None:
    """The preserved evidence carries safe metadata only - no licence, VIN,
    DOB, address, name, email, phone, or claims content."""
    data = _load(EVIDENCE_FIXTURE)
    export = data["evidence_export"]

    # Canonical allowlist scan of PII-capable fields (payload/safe_url/
    # page_signature), skipping opaque generated ids - the same check that
    # guards all evidence tests.
    records = [EvidenceRecordView.model_validate(record) for record in export["evidence"]]
    assert_evidence_privacy_safe(records)

    # Defense-in-depth: the raw JSON and the fixture wrapper must not contain
    # any known applicant marker or any Ontario licence number.
    raw = json.dumps(data)
    for marker in SENSITIVE_MARKERS:
        assert marker not in raw, f"sensitive marker {marker!r} leaked into preserved evidence"
    assert not ONTARIO_LICENCE_PATTERN.search(raw)
    assert "T0000-0000000-0000" not in raw  # old-format licence never leaks either


def test_route_summary_contains_no_applicant_values() -> None:
    raw = json.dumps(_load(ROUTE_SUMMARY_FIXTURE))
    for marker in SENSITIVE_MARKERS:
        assert marker not in raw, f"sensitive marker {marker!r} leaked into route summary"
    assert not ONTARIO_LICENCE_PATTERN.search(raw)


# ---------------------------------------------------------------------------
# Task 3 - the fixtures are valid against the current schemas
# ---------------------------------------------------------------------------


def test_preserved_fixtures_are_schema_valid() -> None:
    """If the evidence schema evolves, these fixtures must still validate -
    they are a living regression for the evidence-backed blocker."""
    data = _load(EVIDENCE_FIXTURE)
    export = data["evidence_export"]
    for record in export["evidence"]:
        EvidenceRecordView.model_validate(record)  # raises if schema drifts
    summary = _load(ROUTE_SUMMARY_FIXTURE)
    assert re.fullmatch(r"[a-z0-9-]+", summary["registry_id"])


def test_fixture_files_are_git_clean_of_applicant_markers() -> None:
    """Every fixture file under tests/fixtures stays applicant-free."""
    for path in sorted(FIXTURES.glob("*.json")):
        raw = path.read_text(encoding="utf-8")
        for marker in SENSITIVE_MARKERS:
            assert marker not in raw, f"{path.name} contains {marker!r}"
        assert not ONTARIO_LICENCE_PATTERN.search(raw)
