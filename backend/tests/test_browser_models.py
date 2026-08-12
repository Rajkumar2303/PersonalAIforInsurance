"""Issue #7 - browser model tests: validation + safe serialization."""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from app.models.browser.observation import (
    BrowserFieldObservation,
    BrowserObservation,
    BrowserObservationType,
    RawQuoteObservation,
)
from app.models.browser.session import (
    BrowserExecutionMode,
    BrowserSession,
    BrowserSessionStatus,
    LiveExecutionGate,
)

SENSITIVE_MARKERS = ["T0000-00000-00000", "1HGCM82633A000000", "1990-01-01", "123 Test Street"]


def _session() -> BrowserSession:
    now = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    return BrowserSession(
        browser_session_id="bs1",
        plan_id="plan-1",
        planned_route_id="mock-insurer",
        registry_id="mock-insurer",
        profile_id="opaque-profile-key",
        intake_session_id="intake-1",
        execution_mode=BrowserExecutionMode.SANDBOX,
        status=BrowserSessionStatus.CREATED,
        started_at=now,
        updated_at=now,
    )


def test_browser_session_serialization_contains_no_applicant_values() -> None:
    payload = _session().model_dump_json()
    for marker in SENSITIVE_MARKERS:
        assert marker not in payload
    assert '"planned_route_id":"mock-insurer"' in payload
    assert '"profile_id":"opaque-profile-key"' in payload


def test_browser_field_observation_never_holds_input_values() -> None:
    field = BrowserFieldObservation(
        external_field_id="legal-name", control_type="input", label="Legal name",
        name="legal_name", input_type="text", required=True,
    )
    payload = field.model_dump_json()
    assert "T0000" not in payload and "Test Applicant" not in payload


def test_browser_observation_defaults_are_safe() -> None:
    obs = BrowserObservation(observation_type=BrowserObservationType.PAGE_LOADED)
    assert obs.filled_field_count == 0
    assert obs.missing_field_paths == []
    assert obs.pending_field_paths == []
    assert obs.quote is None and obs.checkpoint is None


def test_raw_quote_observation_keeps_reference_private() -> None:
    quote = RawQuoteObservation(
        registry_id="mock-insurer",
        observed_at=dt.datetime.now(dt.timezone.utc),
        annual_amount_raw="$1,234.56",
        annual_amount_parsed=1234.56,
        currency="CAD",
        reference_present=True,
        private_reference_handle="a1b2c3d4e5f6a7b8",
        is_firm_quote=True,
    )
    payload = quote.model_dump_json()
    assert "MOCK-8F3K-2026" not in payload  # raw reference never serialized
    assert '"reference_present":true' in payload
    assert '"private_reference_handle":"a1b2c3d4e5f6a7b8"' in payload


def test_live_execution_gate_satisfied() -> None:
    assert LiveExecutionGate().satisfied is False
    gate = LiveExecutionGate(personal_use_confirmed=True, accurate_information_attested=True)
    assert gate.satisfied is True


def test_browser_session_extra_forbidden() -> None:
    with pytest.raises(ValidationError):
        BrowserSession.model_validate({**_session().model_dump(), "extra_key": 1})


def test_browser_session_requires_required_fields() -> None:
    with pytest.raises(ValidationError):
        BrowserSession.model_validate({})
