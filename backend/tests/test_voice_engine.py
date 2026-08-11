"""Issue #9 Prompt 1 - VoiceEngine behaviour tests (hermetic).

Covers the full deterministic voice flow: prepare/disclose, JIT value
disclosure (never cached), pause-for-applicant + resume, consent gates and
revocation, human boundaries (identity/declaration/advice/applicant/manual),
quote vs estimate, terminal statements, callback, unknown questions, and
broker-unavailable - all against a REAL Issue #5 intake engine and Issue #8
recovery engine with synthetic data.
"""

from __future__ import annotations

from intake_helpers import SYNTHETIC_POSTAL

from app.models.voice import (
    BrokerQuestionKind,
    DisclosureStatus,
    VoiceLifecycleStatus,
    VoiceResponseAction,
)
from voice_helpers import (
    field_question,
    kind_question,
    make_handoff_context,
    make_voice_env,
    prepare_and_disclose,
    revoke_route_consent,
)


# -- prepare / disclosure ------------------------------------------------


def test_prepare_handoff_creates_prepared_session(tmp_path):
    env = make_voice_env(tmp_path)
    session = env.engine.prepare_handoff(make_handoff_context(env))
    assert session.lifecycle_status == VoiceLifecycleStatus.PREPARED
    assert session.disclosure_status == DisclosureStatus.NOT_DISCLOSED
    assert session.intake_session_id == env.session_id
    assert session.registry_id == env.registry_id
    assert session.provider_phone_route == "1-800-MOCK-PROVIDER"
    assert session.voice_session_id in env.transport.started


def test_disclose_automation_granted_then_active(tmp_path):
    env = make_voice_env(tmp_path)
    session = prepare_and_disclose(env)
    assert session.disclosure_status == DisclosureStatus.DISCLOSED
    assert session.lifecycle_status == VoiceLifecycleStatus.AWAITING_DISCLOSURE
    # First event flips to ACTIVE.
    decision = env.engine.receive_broker_event(
        session.voice_session_id,
        field_question("applicant.address.postal_code"),
    )
    assert decision.lifecycle_status == VoiceLifecycleStatus.ACTIVE
    assert decision.action == VoiceResponseAction.DISCLOSE_VALUE
    assert decision.value_present is True


def test_disclosure_gate_blocks_substantive_interaction(tmp_path):
    env = make_voice_env(tmp_path)
    session = env.engine.prepare_handoff(make_handoff_context(env))
    # No disclosure yet -> must speak disclosure, nothing substantive.
    decision = env.engine.receive_broker_event(
        session.voice_session_id, field_question("applicant.address.postal_code")
    )
    assert decision.action == VoiceResponseAction.SPEAK_DISCLOSURE
    assert decision.lifecycle_status == VoiceLifecycleStatus.AWAITING_DISCLOSURE
    assert env.transport.last_spoken is None  # no value spoken


def test_disclosure_refused_terminates(tmp_path):
    env = make_voice_env(tmp_path)
    session = env.engine.prepare_handoff(make_handoff_context(env))
    env.engine.disclose_automation(session.voice_session_id, granted=False)
    decision = env.engine.receive_broker_event(
        session.voice_session_id, field_question("applicant.address.postal_code")
    )
    assert decision.action == VoiceResponseAction.END_TERMINAL
    assert env.engine.get(session.voice_session_id).lifecycle_status == VoiceLifecycleStatus.TERMINATED


# -- JIT value disclosure -------------------------------------------------


def test_known_field_disclosed_jit_not_cached(tmp_path):
    # retain_transcript is TEST-ONLY so we can prove the JIT value flowed.
    env = make_voice_env(tmp_path, retain_transcript=True)
    session = prepare_and_disclose(env)
    decision = env.engine.receive_broker_event(
        session.voice_session_id,
        field_question("applicant.address.postal_code"),
    )
    assert decision.action == VoiceResponseAction.DISCLOSE_VALUE
    assert decision.value_present is True
    # The value WAS spoken through the transport boundary (transcript)...
    assert env.transport.spoken_count == 1
    assert env.transport.last_spoken_path == "applicant.address.postal_code"
    assert SYNTHETIC_POSTAL in "".join(env.transport.spoken)
    # ...but is DISCARDED after use (nothing lingers on the transport)...
    assert env.transport.last_spoken is None
    # ...and is NOT persisted in the session or decision.
    dumped = env.engine.get(session.voice_session_id).model_dump()
    assert SYNTHETIC_POSTAL not in str(dumped)
    assert SYNTHETIC_POSTAL not in str(decision.model_dump())


def test_collection_length_question_derives_count(tmp_path):
    env = make_voice_env(tmp_path, retain_transcript=True)
    session = prepare_and_disclose(env)
    decision = env.engine.receive_broker_event(
        session.voice_session_id,
        field_question("product_data.vehicles", kind=BrokerQuestionKind.COLLECTION_LENGTH),
    )
    assert decision.action == VoiceResponseAction.DISCLOSE_VALUE
    assert decision.value_present is True
    assert env.transport.spoken_count == 1
    assert env.transport.last_spoken_path == "product_data.vehicles"
    assert "count" in "".join(env.transport.spoken)


# -- pause for applicant / resume ----------------------------------------


def test_missing_field_pauses_for_applicant_then_resume_discloses(tmp_path):
    env = make_voice_env(tmp_path, retain_transcript=True)
    session = prepare_and_disclose(env)
    path = "product_data.vehicles[0].use.annual_kilometres"
    decision = env.engine.receive_broker_event(session.voice_session_id, field_question(path))
    assert decision.action == VoiceResponseAction.PAUSE_FOR_APPLICANT
    assert decision.canonical_path == path
    assert decision.lifecycle_status == VoiceLifecycleStatus.PAUSED_FOR_APPLICANT
    # Still missing -> resume stays paused (no infinite loop).
    still = env.engine.resume(session.voice_session_id)
    assert still.lifecycle_status == VoiceLifecycleStatus.PAUSED_FOR_APPLICANT
    # Applicant answers via Issue #5...
    env.intake.submit_answer(env.session_id, path, 15000)
    # ...then resume discloses JIT.
    resumed = env.engine.resume(session.voice_session_id)
    assert resumed.action == VoiceResponseAction.DISCLOSE_VALUE
    assert resumed.value_present is True
    assert env.transport.last_spoken_path == path
    assert "15000" in "".join(env.transport.spoken)


def test_field_question_goes_through_interpreter_to_issue5(tmp_path):
    env = make_voice_env(tmp_path)
    session = prepare_and_disclose(env)
    # Raw broker wording -> interpreter -> canonical path -> engine.
    question = env.interpreter.interpret("What is your postal code?")
    assert question.canonical_path == "applicant.address.postal_code"
    decision = env.engine.receive_broker_event(session.voice_session_id, question)
    assert decision.action == VoiceResponseAction.DISCLOSE_VALUE
    assert decision.canonical_path == "applicant.address.postal_code"


# -- consent gates --------------------------------------------------------


def test_route_disclosure_required_before_field(tmp_path):
    env = make_voice_env(tmp_path, grant_consent=False)
    session = prepare_and_disclose(env)
    path = "applicant.address.postal_code"
    decision = env.engine.receive_broker_event(session.voice_session_id, field_question(path))
    assert decision.action == VoiceResponseAction.REQUEST_CONSENT
    assert decision.requires_consent_expansion is True
    assert decision.lifecycle_status == VoiceLifecycleStatus.PAUSED_FOR_CONSENT
    # Applicant grants consent via Issue #5, then resume discloses.
    env.intake.grant_route_consent(env.session_id, env.registry_id, [], True)
    resumed = env.engine.resume(session.voice_session_id)
    assert resumed.action == VoiceResponseAction.DISCLOSE_VALUE


def test_consent_revocation_honoured_before_resume(tmp_path):
    env = make_voice_env(tmp_path, grant_consent=False)
    session = prepare_and_disclose(env)
    path = "applicant.address.postal_code"
    env.intake.grant_route_consent(env.session_id, env.registry_id, [path], True)
    decision = env.engine.receive_broker_event(session.voice_session_id, field_question(path))
    assert decision.action == VoiceResponseAction.DISCLOSE_VALUE
    # Now the applicant revokes route consent (via Issue #5 consent service).
    revoke_route_consent(env, env.registry_id)
    # Clear the transport boundary so we can prove nothing new was spoken.
    env.transport.clear_last_spoken()
    # Next question must NOT disclose.
    blocked = env.engine.receive_broker_event(session.voice_session_id, field_question(path))
    assert blocked.action == VoiceResponseAction.REQUEST_CONSENT
    assert env.transport.last_spoken is None


def test_consent_expansion_question_granted_acknowledges(tmp_path):
    env = make_voice_env(tmp_path)
    session = prepare_and_disclose(env)
    decision = env.engine.receive_broker_event(
        session.voice_session_id, kind_question(BrokerQuestionKind.CONSENT_EXPANSION)
    )
    assert decision.action == VoiceResponseAction.ACKNOWLEDGE


def test_household_driver_question_pauses_for_consent_without_consent(tmp_path):
    env = make_voice_env(tmp_path, grant_consent=False)
    session = prepare_and_disclose(env)
    decision = env.engine.receive_broker_event(
        session.voice_session_id, kind_question(BrokerQuestionKind.HOUSEHOLD_DRIVER)
    )
    assert decision.action == VoiceResponseAction.REQUEST_CONSENT
    assert decision.lifecycle_status == VoiceLifecycleStatus.PAUSED_FOR_CONSENT


# -- human boundaries -----------------------------------------------------


def test_identity_checkpoint_transfers_to_applicant(tmp_path):
    env = make_voice_env(tmp_path)
    session = prepare_and_disclose(env)
    decision = env.engine.receive_broker_event(
        session.voice_session_id, kind_question(BrokerQuestionKind.IDENTITY_CHECKPOINT)
    )
    assert decision.action == VoiceResponseAction.TRANSFER_TO_APPLICANT
    assert decision.checkpoint_kind == BrokerQuestionKind.IDENTITY_CHECKPOINT.value
    assert env.engine.get(session.voice_session_id).lifecycle_status == VoiceLifecycleStatus.AWAITING_HUMAN
    assert env.transport.last_spoken is None  # never answered


def test_declaration_transfers_to_applicant(tmp_path):
    env = make_voice_env(tmp_path)
    session = prepare_and_disclose(env)
    decision = env.engine.receive_broker_event(
        session.voice_session_id, kind_question(BrokerQuestionKind.DECLARATION)
    )
    assert decision.action == VoiceResponseAction.TRANSFER_TO_APPLICANT


def test_advice_request_transfers_to_applicant(tmp_path):
    env = make_voice_env(tmp_path)
    session = prepare_and_disclose(env)
    decision = env.engine.receive_broker_event(
        session.voice_session_id, kind_question(BrokerQuestionKind.ADVICE_REQUEST)
    )
    assert decision.action == VoiceResponseAction.TRANSFER_TO_APPLICANT
    assert env.transport.last_spoken is None


def test_applicant_required_transfers_to_applicant(tmp_path):
    env = make_voice_env(tmp_path)
    session = prepare_and_disclose(env)
    decision = env.engine.receive_broker_event(
        session.voice_session_id, kind_question(BrokerQuestionKind.APPLICANT_REQUIRED)
    )
    assert decision.action == VoiceResponseAction.TRANSFER_TO_APPLICANT


def test_manual_review_transfers_to_human(tmp_path):
    env = make_voice_env(tmp_path)
    session = prepare_and_disclose(env)
    decision = env.engine.receive_broker_event(
        session.voice_session_id, kind_question(BrokerQuestionKind.MANUAL_REVIEW)
    )
    assert decision.action == VoiceResponseAction.TRANSFER_TO_HUMAN
    assert env.transport.transferred


def test_unknown_question_manual_handoff_never_guessed(tmp_path):
    env = make_voice_env(tmp_path)
    session = prepare_and_disclose(env)
    decision = env.engine.receive_broker_event(
        session.voice_session_id, kind_question(BrokerQuestionKind.UNKNOWN)
    )
    assert decision.action == VoiceResponseAction.MANUAL_HANDOFF
    assert env.engine.get(session.voice_session_id).lifecycle_status == VoiceLifecycleStatus.AWAITING_HUMAN
    assert env.transport.last_spoken is None


# -- callback / quote / estimate ------------------------------------------


def test_callback_scheduled_terminates_callback_required(tmp_path):
    env = make_voice_env(tmp_path)
    session = prepare_and_disclose(env)
    decision = env.engine.receive_broker_event(
        session.voice_session_id, kind_question(BrokerQuestionKind.CALLBACK_REQUEST)
    )
    assert decision.action == VoiceResponseAction.CALLBACK_SCHEDULED
    voice = env.engine.get(session.voice_session_id)
    assert voice.lifecycle_status == VoiceLifecycleStatus.COMPLETED
    assert voice.terminal_status == "callback_required"


def test_quote_disclosure_pending_normalization_never_comparable(tmp_path):
    env = make_voice_env(tmp_path)
    session = prepare_and_disclose(env)
    decision = env.engine.receive_broker_event(
        session.voice_session_id, kind_question(BrokerQuestionKind.QUOTE_DISCLOSURE)
    )
    assert decision.action == VoiceResponseAction.END_QUOTE
    voice = env.engine.get(session.voice_session_id)
    assert voice.lifecycle_status == VoiceLifecycleStatus.COMPLETED
    # Issue #8 leaves terminal_status None pending normalization (Issues #11/#12).
    assert voice.terminal_status is None


def test_estimate_disclosure_terminal_estimate_only(tmp_path):
    env = make_voice_env(tmp_path)
    session = prepare_and_disclose(env)
    decision = env.engine.receive_broker_event(
        session.voice_session_id, kind_question(BrokerQuestionKind.ESTIMATE_DISCLOSURE)
    )
    assert decision.action == VoiceResponseAction.END_QUOTE
    voice = env.engine.get(session.voice_session_id)
    assert voice.terminal_status == "estimate_only"


# -- terminal statements ---------------------------------------------------


def test_explicit_ineligible_terminal_ineligible(tmp_path):
    env = make_voice_env(tmp_path)
    session = prepare_and_disclose(env)
    decision = env.engine.receive_broker_event(
        session.voice_session_id, kind_question(BrokerQuestionKind.INELIGIBILITY)
    )
    assert decision.action == VoiceResponseAction.END_TERMINAL
    assert env.engine.get(session.voice_session_id).terminal_status == "ineligible"


def test_affinity_restricted_terminal(tmp_path):
    env = make_voice_env(tmp_path)
    session = prepare_and_disclose(env)
    decision = env.engine.receive_broker_event(
        session.voice_session_id, kind_question(BrokerQuestionKind.AFFINITY_RESTRICTION)
    )
    assert decision.action == VoiceResponseAction.END_TERMINAL
    assert env.engine.get(session.voice_session_id).terminal_status == "affinity_restricted"


def test_specialty_only_terminal(tmp_path):
    env = make_voice_env(tmp_path)
    session = prepare_and_disclose(env)
    decision = env.engine.receive_broker_event(
        session.voice_session_id, kind_question(BrokerQuestionKind.SPECIALTY_ONLY)
    )
    assert decision.action == VoiceResponseAction.END_TERMINAL
    assert env.engine.get(session.voice_session_id).terminal_status == "specialty_only"


def test_not_currently_writing_terminal(tmp_path):
    env = make_voice_env(tmp_path)
    session = prepare_and_disclose(env)
    decision = env.engine.receive_broker_event(
        session.voice_session_id, kind_question(BrokerQuestionKind.NOT_CURRENTLY_WRITING)
    )
    assert decision.action == VoiceResponseAction.END_TERMINAL
    assert env.engine.get(session.voice_session_id).terminal_status == "not_currently_writing"


def test_broker_unavailable_terminated_not_comparable(tmp_path):
    env = make_voice_env(tmp_path)
    session = prepare_and_disclose(env)
    decision = env.engine.receive_broker_event(
        session.voice_session_id, kind_question(BrokerQuestionKind.BROKER_UNAVAILABLE)
    )
    voice = env.engine.get(session.voice_session_id)
    assert voice.lifecycle_status == VoiceLifecycleStatus.TERMINATED
    assert decision.action in (VoiceResponseAction.END_TERMINAL, VoiceResponseAction.MANUAL_HANDOFF)


def test_completed_without_quote_terminal(tmp_path):
    env = make_voice_env(tmp_path)
    session = prepare_and_disclose(env)
    decision = env.engine.receive_broker_event(
        session.voice_session_id, kind_question(BrokerQuestionKind.COMPLETED_WITHOUT_QUOTE)
    )
    assert decision.action == VoiceResponseAction.END_TERMINAL
    assert env.engine.get(session.voice_session_id).lifecycle_status == VoiceLifecycleStatus.COMPLETED


# -- explicit human handoff / end -----------------------------------------


def test_explicit_transfer_to_human(tmp_path):
    env = make_voice_env(tmp_path)
    session = prepare_and_disclose(env)
    updated = env.engine.transfer_to_human(
        session.voice_session_id, reason="applicant requested a person", checkpoint_kind="advice_request"
    )
    assert updated.lifecycle_status == VoiceLifecycleStatus.AWAITING_HUMAN
    assert updated.pending_checkpoint == "advice_request"
    assert env.transport.transferred


def test_end_session_terminated(tmp_path):
    env = make_voice_env(tmp_path)
    session = prepare_and_disclose(env)
    updated = env.engine.end_session(
        session.voice_session_id, status=VoiceLifecycleStatus.TERMINATED, reason="operator ended"
    )
    assert updated.lifecycle_status == VoiceLifecycleStatus.TERMINATED
    assert env.transport.ended
