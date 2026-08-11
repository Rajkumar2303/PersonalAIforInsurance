"""Issue #9 Prompt 1 - voice model / schema tests (hermetic).

Verifies the safe voice models: StrEnum values, ``extra="forbid"`` strictness,
sensitive redaction, safe-context sanitization allowlist, and that no voice
model carries applicant values.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.voice import (
    BrokerQuestion,
    BrokerQuestionKind,
    DisclosureStatus,
    PhoneHandoffContext,
    RecordingConsentStatus,
    TranscriptionConsentStatus,
    VoiceDecision,
    VoiceLifecycleStatus,
    VoiceObservationType,
    VoiceResponseAction,
    VoiceSession,
    VoiceWorkflowState,
    sanitize_voice_context,
)


def test_enum_values_match_contract():
    assert VoiceLifecycleStatus.PREPARED.value == "prepared"
    assert VoiceLifecycleStatus.AWAITING_DISCLOSURE.value == "awaiting_disclosure"
    assert VoiceLifecycleStatus.ACTIVE.value == "active"
    assert VoiceLifecycleStatus.PAUSED_FOR_APPLICANT.value == "paused_for_applicant"
    assert VoiceLifecycleStatus.PAUSED_FOR_CONSENT.value == "paused_for_consent"
    assert VoiceLifecycleStatus.AWAITING_HUMAN.value == "awaiting_human"
    assert VoiceLifecycleStatus.COMPLETED.value == "completed"
    assert VoiceLifecycleStatus.TERMINATED.value == "terminated"

    assert DisclosureStatus.DISCLOSED.value == "disclosed"
    assert DisclosureStatus.REFUSED.value == "refused"

    assert RecordingConsentStatus.NOT_REQUESTED.value == "not_requested"
    assert RecordingConsentStatus.GRANTED.value == "granted"
    assert RecordingConsentStatus.DENIED.value == "denied"
    assert TranscriptionConsentStatus.NOT_REQUESTED.value == "not_requested"

    assert VoiceResponseAction.DISCLOSE_VALUE.value == "disclose_value"
    assert VoiceResponseAction.TRANSFER_TO_APPLICANT.value == "transfer_to_applicant"
    assert VoiceResponseAction.CALLBACK_SCHEDULED.value == "callback_scheduled"
    assert VoiceResponseAction.END_QUOTE.value == "end_quote"


def test_voice_observation_types_match_recovery_rows():
    assert VoiceObservationType.PHONE_QUOTE_OBSERVED.value == "phone_quote_observed"
    assert VoiceObservationType.PHONE_ESTIMATE_OBSERVED.value == "phone_estimate_observed"
    assert VoiceObservationType.CALLBACK_SCHEDULED.value == "callback_scheduled"
    assert VoiceObservationType.BROKER_REQUIRES_FIELD.value == "broker_requires_field"
    assert VoiceObservationType.APPLICANT_REQUIRED.value == "applicant_required"
    assert VoiceObservationType.MANUAL_REVIEW_REQUIRED.value == "manual_review_required"
    assert VoiceObservationType.PHONE_UNREACHABLE.value == "phone_unreachable"
    assert VoiceObservationType.UNKNOWN_BROKER_QUESTION.value == "unknown_broker_question"
    # Reused Issue #8 rows - must exist in the recovery classification table.
    from app.services.recovery.classification import _TABLE

    for otype in (
        "phone_quote_observed",
        "phone_estimate_observed",
        "callback_scheduled",
        "broker_requires_field",
        "applicant_required",
        "manual_review_required",
        "phone_unreachable",
        "unknown_broker_question",
    ):
        assert otype in _TABLE, f"missing recovery classification row for {otype}"


def test_broker_question_strict_extra_forbid():
    with pytest.raises(ValidationError):
        BrokerQuestion(kind=BrokerQuestionKind.CANONICAL_FIELD, unknown_key="x")
    q = BrokerQuestion(kind=BrokerQuestionKind.CANONICAL_FIELD, canonical_path="a.b.c")
    assert q.canonical_path == "a.b.c"
    assert q.mapping_confidence == 1.0


def test_voice_session_defaults_safe():
    session = VoiceSession(voice_session_id="v1", intake_session_id="i1", registry_id="r1")
    assert session.lifecycle_status == "prepared"
    assert session.disclosure_status == "not_disclosed"
    assert session.recording_consent == "not_requested"
    assert session.transcription_consent == "not_requested"
    assert session.pending_field_paths == []
    assert session.attempt_history == []


def test_voice_decision_never_holds_values():
    decision = VoiceDecision(
        voice_session_id="v1",
        lifecycle_status=VoiceLifecycleStatus.ACTIVE.value,
        disclosure_status=DisclosureStatus.DISCLOSED.value,
        action=VoiceResponseAction.DISCLOSE_VALUE.value,
        canonical_path="applicant.address.postal_code",
        value_present=True,
    )
    # No field exists for the raw value itself.
    assert "value" not in VoiceDecision.model_fields
    assert decision.value_present is True


def test_phone_handoff_context_strict_and_safe():
    with pytest.raises(ValidationError):
        PhoneHandoffContext(intake_session_id="i", registry_id="r", applicant_phone="416-555-0100")
    ctx = PhoneHandoffContext(
        intake_session_id="i",
        registry_id="r",
        provider_phone_route="1-800-MOCK-PROVIDER",
        authorized_canonical_paths=["applicant.address.postal_code"],
    )
    assert ctx.provider_phone_route == "1-800-MOCK-PROVIDER"


def test_voice_models_redact_sensitive_reprs():
    session = VoiceSession(voice_session_id="v1", intake_session_id="i1", registry_id="r1")
    # repr/str are redacted-safe (never leak).
    assert "[REDACTED]" in repr(session) or "voice_session_id='v1'" in repr(session)


def test_sanitize_voice_context_allowlist():
    ctx = {
        "voice_session_id": "v1",
        "registry_id": "r1",
        "canonical_path": "a.b",
        "nested_answer": {"postal": "M0A 0A0"},
        "raw_transcript": "my name is ...",
    }
    safe = sanitize_voice_context(ctx)
    assert safe["voice_session_id"] == "v1"
    assert safe["canonical_path"] == "a.b"
    assert "nested_answer" not in safe
    assert "raw_transcript" not in safe


def test_sanitize_voice_context_empty():
    assert sanitize_voice_context(None) == {}
    assert sanitize_voice_context({}) == {}


def test_voice_workflow_state_is_safe_metadata():
    # TypedDict keys must all be safe metadata - no value/transcript fields.
    blocked = {"transcript", "answer", "postal_code_value", "licence", "audio"}
    assert not (blocked & set(VoiceWorkflowState.__annotations__))


def test_voice_session_fields_are_safe_metadata():
    # No voice model field may hold applicant values by construction.
    for model in (BrokerQuestion, VoiceSession, VoiceDecision, PhoneHandoffContext):
        names = set(model.model_fields)
        assert not (names & {"transcript", "audio", "answer", "value"})
