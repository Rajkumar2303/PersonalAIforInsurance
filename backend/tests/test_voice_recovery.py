"""Issue #9 Prompt 1 - voice <-> Issue #8 recovery integration (hermetic).

Proves every voice outcome flows through the Issue #8 ``RecoveryEngine`` as an
``ExecutionObservation`` with ``source_channel=VOICE``, that the recovery
classification table maps the voice observation types, and that the system
NEVER assigns ``quoted_comparable`` / ``quoted_non_comparable`` (those belong
to Issues #11/#12).
"""

from __future__ import annotations

from app.models.recovery import (
    AttemptLifecycleStatus,
    RecoveryAction,
    RecoveryDecideRequest,
    RouteOutcomeStatus,
    SourceChannel,
)
from app.models.voice import BrokerQuestionKind, VoiceObservationType, VoiceResponseAction
from app.services.recovery.classification import _TABLE, classify_observation
from voice_helpers import field_question, kind_question, make_voice_env, prepare_and_disclose

COMPARABLE = {"quoted_comparable", "quoted_non_comparable"}


def _assert_never_comparable(terminal_status):
    assert terminal_status not in COMPARABLE


def test_voice_observation_types_have_recovery_rows():
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
        assert otype in _TABLE


def test_classify_phone_quote_never_comparable():
    from app.models.recovery import ExecutionObservation

    obs = ExecutionObservation(
        source_channel=SourceChannel.VOICE,
        observation_type="phone_quote_observed",
        safe_context={"voice_session_id": "v1", "quote_present": True, "is_firm_quote": True},
    )
    classified = classify_observation(obs)
    assert classified.quote_pending_normalization is True
    assert classified.terminal_status is None
    _assert_never_comparable(classified.terminal_status)


def test_quote_flow_recovery_decision_pending_normalization(tmp_path):
    env = make_voice_env(tmp_path)
    session = prepare_and_disclose(env)
    decision = env.engine.receive_broker_event(
        session.voice_session_id, kind_question(BrokerQuestionKind.QUOTE_DISCLOSURE)
    )
    assert decision.action == VoiceResponseAction.END_QUOTE
    # Find the recovery decision via the attempt store.
    attempts = env.recovery_store.list_all()
    assert attempts, "expected a recovery attempt"
    attempt = attempts[-1]
    assert attempt.channel is SourceChannel.VOICE
    assert attempt.quote_pending_normalization is True
    _assert_never_comparable(attempt.terminal_status)
    # Terminal lifecycle, no fabricated coverage status.
    assert attempt.lifecycle_status is AttemptLifecycleStatus.TERMINAL


def test_estimate_flow_terminal_estimate_only(tmp_path):
    env = make_voice_env(tmp_path)
    session = prepare_and_disclose(env)
    decision = env.engine.receive_broker_event(
        session.voice_session_id, kind_question(BrokerQuestionKind.ESTIMATE_DISCLOSURE)
    )
    assert decision.action == VoiceResponseAction.END_QUOTE
    attempt = env.recovery_store.list_all()[-1]
    assert attempt.terminal_status is RouteOutcomeStatus.ESTIMATE_ONLY
    _assert_never_comparable(attempt.terminal_status)


def test_callback_flow_prepare_voice_handoff(tmp_path):
    env = make_voice_env(tmp_path)
    session = prepare_and_disclose(env)
    decision = env.engine.receive_broker_event(
        session.voice_session_id, kind_question(BrokerQuestionKind.CALLBACK_REQUEST)
    )
    assert decision.action == VoiceResponseAction.CALLBACK_SCHEDULED
    # Issue #8 authority: callback -> callback_required -> prepare_voice_handoff.
    attempt = env.recovery_store.list_all()[-1]
    assert attempt.terminal_status is RouteOutcomeStatus.CALLBACK_REQUIRED
    assert attempt.recovery_action is RecoveryAction.PREPARE_VOICE_HANDOFF
    # The voice engine itself never assigned a comparable status.
    assert attempt.terminal_status not in COMPARABLE


def test_phone_unreachable_failover_eligible(tmp_path):
    env = make_voice_env(tmp_path, unreachable=True)
    session = prepare_and_disclose(env)
    decision = env.engine.receive_broker_event(
        session.voice_session_id, kind_question(BrokerQuestionKind.BROKER_UNAVAILABLE)
    )
    assert decision.action in (VoiceResponseAction.END_TERMINAL, VoiceResponseAction.MANUAL_HANDOFF)
    attempt = env.recovery_store.list_all()[-1]
    assert attempt.channel is SourceChannel.VOICE
    _assert_never_comparable(attempt.terminal_status)


def test_unknown_broker_question_pauses_no_comparable(tmp_path):
    env = make_voice_env(tmp_path)
    session = prepare_and_disclose(env)
    decision = env.engine.receive_broker_event(
        session.voice_session_id, kind_question(BrokerQuestionKind.UNKNOWN)
    )
    assert decision.action == VoiceResponseAction.MANUAL_HANDOFF
    attempt = env.recovery_store.list_all()[-1]
    _assert_never_comparable(attempt.terminal_status)


def test_emit_observation_uses_voice_source_channel(tmp_path):
    env = make_voice_env(tmp_path)
    session = prepare_and_disclose(env)
    recovery = env.engine.emit_observation(
        session.voice_session_id,
        VoiceObservationType.NOT_CURRENTLY_WRITING,
        reason="broker not writing new business",
    )
    assert recovery.terminal_status is RouteOutcomeStatus.NOT_CURRENTLY_WRITING
    attempt = env.recovery_store.list_all()[-1]
    assert attempt.channel is SourceChannel.VOICE
    assert attempt.recovery_action is RecoveryAction.STOP_TERMINAL


def test_voice_never_assigns_comparable_anywhere(tmp_path):
    """Full sweep: every voice-driven recovery attempt is never comparable."""
    env = make_voice_env(tmp_path)
    for _kind in (BrokerQuestionKind.QUOTE_DISCLOSURE, BrokerQuestionKind.ESTIMATE_DISCLOSURE,
                  BrokerQuestionKind.CALLBACK_REQUEST, BrokerQuestionKind.INELIGIBILITY,
                  BrokerQuestionKind.NOT_CURRENTLY_WRITING, BrokerQuestionKind.BROKER_UNAVAILABLE):
        session = prepare_and_disclose(env)
        env.engine.receive_broker_event(session.voice_session_id, kind_question(_kind))
    for attempt in env.recovery_store.list_all():
        _assert_never_comparable(attempt.terminal_status)
        assert attempt.quote_pending_normalization in (True, False)


def test_broker_requires_field_observation_pauses(tmp_path):
    env = make_voice_env(tmp_path)
    session = prepare_and_disclose(env)
    recovery = env.engine.emit_observation(
        session.voice_session_id,
        VoiceObservationType.BROKER_REQUIRES_FIELD,
        reason="broker needs another field",
        extra_safe_context={"canonical_path": "applicant.address.city"},
    )
    assert recovery.lifecycle_status is AttemptLifecycleStatus.PAUSED
    assert recovery.recommended_action is RecoveryAction.RESUME_AFTER_USER_INPUT
