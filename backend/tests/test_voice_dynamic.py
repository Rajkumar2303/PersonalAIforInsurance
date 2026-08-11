"""Issue #9 Prompt 1 - dynamic-field / interpreter tests (hermetic).

Proves the voice layer is data-driven: adding a NEW canonical field to the
Issue #5 catalog plus one alias row requires NO voice-engine change. Renamed
broker wording maps to the same path; a removed catalog field maps to UNKNOWN
(never guessed).
"""

from __future__ import annotations

from intake_helpers import make_field, standard_fields

from app.models.voice import BrokerQuestionKind, VoiceResponseAction
from voice_helpers import make_voice_env, prepare_and_disclose

# A NEW canonical field not in the standard synthetic catalog, but backed by
# the canonical ``InsuranceProfile`` schema (``VehicleUse.carpool``).
CARPOOL_PATH = "product_data.vehicles[0].use.carpool"


def _fields_with_carpool():
    return standard_fields() + [
        make_field(
            "vehicle_carpool",
            CARPOOL_PATH,
            intake_phase="route_specific",
            priority=90,
            input_type="boolean",
            # NOT unit-required: keeps ``complete_starter`` vehicle
            # materialization unchanged (only required unit fields gate it).
            item_unit="vehicle",
            item_unit_required=False,
        )
    ]


def test_new_catalog_field_requires_no_engine_change(tmp_path):
    env = make_voice_env(
        tmp_path,
        catalog_fields=_fields_with_carpool(),
        extra_aliases={"does the customer carpool": CARPOOL_PATH},
        retain_transcript=True,
    )
    session = prepare_and_disclose(env)
    # Broker asks a brand-new question -> interpreter -> engine -> Issue #5.
    question = env.interpreter.interpret("Does the customer carpool to work?")
    assert question.canonical_path == CARPOOL_PATH
    decision = env.engine.receive_broker_event(session.voice_session_id, question)
    assert decision.action == VoiceResponseAction.PAUSE_FOR_APPLICANT
    assert decision.canonical_path == CARPOOL_PATH
    # Route-local: only this route pauses, with a batchable pending path.
    paused = env.engine.get(session.voice_session_id)
    assert paused.route_status == "paused_missing_information"
    assert CARPOOL_PATH in paused.pending_field_paths
    # Applicant answers via Issue #5, then resume discloses.
    env.intake.submit_answer(env.session_id, CARPOOL_PATH, True)
    resumed = env.engine.resume(session.voice_session_id)
    assert resumed.action == VoiceResponseAction.DISCLOSE_VALUE
    assert env.transport.last_spoken_path == CARPOOL_PATH
    assert "True" in "".join(env.transport.spoken)
    assert env.engine.get(session.voice_session_id).route_status == "running"


def test_renamed_wording_maps_to_same_path(tmp_path):
    env = make_voice_env(tmp_path)
    for wording in ("annual mileage", "kilometres per year", "How many kilometres do you drive?"):
        question = env.interpreter.interpret(wording)
        assert question.canonical_path == "product_data.vehicles[0].use.annual_kilometres"
        assert question.kind == BrokerQuestionKind.CANONICAL_FIELD


def test_removed_catalog_field_maps_to_unknown(tmp_path):
    # Standard catalog but WITHOUT the annual-kilometres field.
    fields = [f for f in standard_fields() if f["field_id"] != "vehicle_annual_km"]
    env = make_voice_env(tmp_path, catalog_fields=fields)
    question = env.interpreter.interpret("How many kilometres per year?")
    assert question.kind == BrokerQuestionKind.UNKNOWN
    assert question.canonical_path is None
    # Engine never guesses: manual handoff, nothing spoken.
    session = prepare_and_disclose(env)
    decision = env.engine.receive_broker_event(session.voice_session_id, question)
    assert decision.action == VoiceResponseAction.MANUAL_HANDOFF
    assert env.transport.last_spoken is None


def test_unknown_wording_maps_to_unknown(tmp_path):
    env = make_voice_env(tmp_path)
    question = env.interpreter.interpret("Can you kindly explain the orbital mechanics here?")
    assert question.kind == BrokerQuestionKind.UNKNOWN
    assert question.canonical_path is None


def test_interpreter_collection_alias(tmp_path):
    env = make_voice_env(tmp_path)
    question = env.interpreter.interpret("How many vehicles do you have?")
    assert question.canonical_path == "product_data.vehicles"
    assert question.kind == BrokerQuestionKind.COLLECTION_LENGTH


def test_interpreter_identity_phrase_never_answers(tmp_path):
    env = make_voice_env(tmp_path)
    question = env.interpreter.interpret("Please confirm your identity with your licence number.")
    assert question.kind == BrokerQuestionKind.IDENTITY_CHECKPOINT
    assert question.is_identity_checkpoint is True
    session = prepare_and_disclose(env)
    decision = env.engine.receive_broker_event(session.voice_session_id, question)
    assert decision.action == VoiceResponseAction.TRANSFER_TO_APPLICANT
    assert env.transport.last_spoken is None
