"""Issue #10 - ingestion adapter tests: mapping of existing Issue #7/#8/#9
domain objects onto typed evidence drafts and quote observations."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app.models.browser.observation import BrowserObservation, BrowserObservationType
from app.models.evidence import EvidenceEventType, QuoteObservation
from app.models.recovery import (
    RecoveryAction,
    RecoveryDecision,
    AttemptLifecycleStatus,
)
from app.services.evidence.ingest import (
    browser_event_type,
    quote_from_browser_observation,
    recovery_draft_from_decision,
    voice_draft,
    voice_event_type,
    voice_quote,
)
from app.models.voice import VoiceObservationType

from evidence_helpers import EvidenceEnv, make_evidence_env, quote_observation


def test_browser_event_type_mapping_is_exhaustive() -> None:
    expected = {
        BrowserObservationType.PAGE_LOADED: EvidenceEventType.PAGE_OBSERVED,
        BrowserObservationType.FIELDS_FILLED: EvidenceEventType.FIELD_INTERACTION_OBSERVED,
        BrowserObservationType.NEEDS_FIELD: EvidenceEventType.FIELD_REQUIREMENT_OBSERVED,
        BrowserObservationType.NEEDS_CONSENT: EvidenceEventType.CHECKPOINT_OBSERVED,
        BrowserObservationType.HUMAN_CHECKPOINT: EvidenceEventType.CHECKPOINT_OBSERVED,
        BrowserObservationType.ACCESS_CONTROL_DETECTED: EvidenceEventType.BLOCKING_ACCESS_CONTROL_OBSERVED,
        BrowserObservationType.UNKNOWN_EXTERNAL_FIELD: EvidenceEventType.FIELD_REQUIREMENT_OBSERVED,
        BrowserObservationType.CALLBACK_DETECTED: EvidenceEventType.CALLBACK_OBSERVED,
        BrowserObservationType.MANUAL_CONTACT_DETECTED: EvidenceEventType.HUMAN_HANDOFF_REQUIRED,
        BrowserObservationType.TECHNICAL_ERROR: EvidenceEventType.UNAVAILABLE_OBSERVED,
        BrowserObservationType.ROUTE_CHANGED: EvidenceEventType.PAGE_OBSERVED,
        BrowserObservationType.COMPLETE_WITHOUT_QUOTE: EvidenceEventType.UNAVAILABLE_OBSERVED,
        BrowserObservationType.UNSUPPORTED_PAGE: EvidenceEventType.UNAVAILABLE_OBSERVED,
        BrowserObservationType.VALUE_NOT_SUPPORTED: EvidenceEventType.FIELD_INTERACTION_OBSERVED,
        BrowserObservationType.VALIDATION_ERROR: EvidenceEventType.VALIDATION_OBSERVED,
        BrowserObservationType.AMBIGUOUS_FIELD: EvidenceEventType.VALIDATION_OBSERVED,
        BrowserObservationType.AMBIGUOUS_ACTION: EvidenceEventType.VALIDATION_OBSERVED,
    }
    for ot, ev in expected.items():
        assert browser_event_type(ot) is ev, f"{ot} -> {browser_event_type(ot)} != {ev}"


def test_voice_event_type_mapping() -> None:
    cases = {
        VoiceObservationType.PHONE_QUOTE_OBSERVED: EvidenceEventType.VOICE_QUOTE_OBSERVED,
        VoiceObservationType.PHONE_ESTIMATE_OBSERVED: EvidenceEventType.VOICE_ESTIMATE_OBSERVED,
        VoiceObservationType.CALLBACK_SCHEDULED: EvidenceEventType.CALLBACK_OBSERVED,
        VoiceObservationType.APPLICANT_REQUIRED: EvidenceEventType.CHECKPOINT_OBSERVED,
        VoiceObservationType.MANUAL_REVIEW_REQUIRED: EvidenceEventType.HUMAN_HANDOFF_REQUIRED,
        VoiceObservationType.EXPLICIT_INELIGIBLE: EvidenceEventType.EXPLICIT_INELIGIBILITY_OBSERVED,
        VoiceObservationType.AFFINITY_RESTRICTED: EvidenceEventType.AFFINITY_RESTRICTED_OBSERVED,
        VoiceObservationType.SPECIALTY_ONLY: EvidenceEventType.SPECIALTY_ONLY_OBSERVED,
        VoiceObservationType.NOT_CURRENTLY_WRITING: EvidenceEventType.NOT_CURRENTLY_WRITING_OBSERVED,
        VoiceObservationType.PHONE_UNREACHABLE: EvidenceEventType.UNAVAILABLE_OBSERVED,
    }
    for vt, ev in cases.items():
        assert voice_event_type(vt.value) is ev, f"{vt} -> {voice_event_type(vt.value)}"


def test_voice_draft_is_safe_and_typed() -> None:
    draft = voice_draft(
        "intake-1",
        voice_session_id="vs-1",
        observation_type="broker_requires_field",
        plan_id="plan-1",
        planned_route_id="mock-insurer",
        registry_id="mock-insurer",
        distinct_rate_source_id="RS-MOCK-INSURER",
        attempt_id="att-1",
        parent_attempt_id="att-0",
        canonical_path="applicant.vehicle.0.use.carpool",
        route_status="running",
    )
    assert draft.event_type is EvidenceEventType.FIELD_REQUIREMENT_OBSERVED
    assert draft.payload.kind == "voice_observation"
    assert draft.payload.canonical_path == "applicant.vehicle.0.use.carpool"
    assert draft.payload.voice_observation_type == "broker_requires_field"


def test_quote_from_browser_observation_converts_float_to_decimal() -> None:
    obs = quote_observation(annual=1234.56, monthly=102.88, firm=True)
    quote = quote_from_browser_observation(
        "intake-1",
        obs,
        plan_id="plan-1",
        planned_route_id="mock-insurer",
        registry_id="mock-insurer",
        distinct_rate_source_id="RS-MOCK-INSURER",
        attempt_id="att-1",
    )
    assert quote is not None
    assert quote.annual_premium == Decimal("1234.56")
    assert quote.monthly_premium == Decimal("102.88")
    assert quote.firm_vs_estimate == "firm"
    assert quote.quote_pending_normalization is True
    assert isinstance(quote, QuoteObservation)


def test_quote_from_browser_observation_none_when_no_quote() -> None:
    obs = BrowserObservation(observation_type=BrowserObservationType.PAGE_LOADED)
    quote = quote_from_browser_observation(
        "intake-1",
        obs,
        plan_id="plan-1",
        planned_route_id="mock-insurer",
        registry_id="mock-insurer",
        distinct_rate_source_id="RS-MOCK-INSURER",
        attempt_id="att-1",
    )
    assert quote is None


def test_recovery_draft_records_decision_fields() -> None:
    decision = RecoveryDecision(
        decision_id="dec-1",
        attempt_id="att-1",
        plan_id="plan-1",
        planned_route_id="mock-insurer",
        registry_id="mock-insurer",
        distinct_rate_source_id="RS-MOCK-INSURER",
        lifecycle_status=AttemptLifecycleStatus.TERMINAL,
        recommended_action=RecoveryAction.STOP_TERMINAL,
        reason_codes=["captcha_or_bot_control"],
        retry_allowed=False,
        terminal_status="blocked",
        quote_pending_normalization=False,
        policy_version="v1",
        plan_version="v1",
        safe_context={"reason_code": "blocked"},
        decided_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
    )
    draft = recovery_draft_from_decision("intake-1", decision)
    assert draft.event_type is EvidenceEventType.RECOVERY_DECISION
    assert draft.payload.reason_codes == ["captcha_or_bot_control"]
    assert draft.payload.terminal_status == "blocked"
    assert draft.payload.policy_version == "v1"
    assert draft.attempt_id == "att-1"


def test_voice_quote_preserves_firm_vs_estimate() -> None:
    firm = voice_quote(
        "intake-1",
        voice_session_id="vs-1",
        plan_id="plan-1",
        planned_route_id="mock-insurer",
        registry_id="mock-insurer",
        distinct_rate_source_id="RS-MOCK-INSURER",
        attempt_id="att-1",
        annual_premium=Decimal("1500.00"),
        firm_vs_estimate="firm",
    )
    estimate = voice_quote(
        "intake-1",
        voice_session_id="vs-1",
        plan_id="plan-1",
        planned_route_id="mock-insurer",
        registry_id="mock-insurer",
        distinct_rate_source_id="RS-MOCK-INSURER",
        attempt_id="att-2",
        annual_premium=Decimal("1400.00"),
        firm_vs_estimate="estimate",
    )
    assert firm.firm_vs_estimate == "firm"
    assert estimate.firm_vs_estimate == "estimate"
    assert firm.quote_pending_normalization is True
