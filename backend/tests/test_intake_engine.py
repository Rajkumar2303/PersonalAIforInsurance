"""Tests for the deterministic intake engine (Issue #5).

Covers product routing (section 27) and progressive intake (section 28):
ask-once, external discovery, invalid-answer retry, and safe unknown-field
handling. All hermetic with synthetic data.
"""

from __future__ import annotations

import pytest

from app.models.insurance.enums import InsuranceType
from app.models.insurance.paths import is_missing, resolve
from app.services.intake.engine import SessionNotFoundError

from intake_helpers import (
    SYNTHETIC_EXPIRY,
    SYNTHETIC_LEGAL_NAME,
    SYNTHETIC_LICENCE,
    SYNTHETIC_POSTAL,
    SYNTHETIC_VIN,
    make_engine,
    seed_profile,
)


def _complete_starter(engine, session_id: str) -> None:
    """Answer all starter fields (driver + vehicle units)."""
    seed_profile(engine, session_id)
    engine.submit_answer(session_id, "product_data.drivers[0].licence.name_on_licence", SYNTHETIC_LEGAL_NAME)
    engine.submit_answer(session_id, "product_data.drivers[0].licence.licence_number", SYNTHETIC_LICENCE)
    engine.submit_answer(session_id, "product_data.drivers[0].licence.expiry_date", SYNTHETIC_EXPIRY)
    engine.submit_answer(session_id, "product_data.vehicles[0].identity.vin", SYNTHETIC_VIN)
    engine.submit_answer(session_id, "product_data.vehicles[0].identity.model_year", 2022)
    engine.submit_answer(session_id, "product_data.vehicles[0].identity.make", "TestMake")
    engine.submit_answer(session_id, "product_data.vehicles[0].identity.model", "TestModel")


# --- product routing (section 27) -------------------------------------

@pytest.mark.parametrize(
    "insurance_type",
    [InsuranceType.HOME, InsuranceType.TENANT, InsuranceType.LIFE, InsuranceType.TRAVEL, InsuranceType.OTHER],
)
def test_unsupported_products_not_implemented(tmp_path, insurance_type) -> None:
    engine = make_engine(tmp_path)
    session, gate = engine.create_session(insurance_type)
    assert gate.is_supported is False
    assert gate.status == "product_not_implemented"
    assert session.status.value == "product_rejected"


def test_auto_creates_supported_intake(tmp_path) -> None:
    engine = make_engine(tmp_path)
    session, gate = engine.create_session(InsuranceType.AUTO)
    assert gate.is_supported is True
    assert gate.status == "started"
    assert session.insurance_type is InsuranceType.AUTO


def test_unsupported_products_do_not_ask_auto_fields(tmp_path) -> None:
    engine = make_engine(tmp_path)
    session, _ = engine.create_session(InsuranceType.HOME)
    result = engine.submit_answer(session.session_id, "applicant.identity.legal_name", "Someone")
    assert result.validation_success is False
    assert result.error_message == "product not implemented"


# --- progressive intake (section 28) ----------------------------------

def test_draft_profile_starts_incomplete(tmp_path) -> None:
    engine = make_engine(tmp_path)
    session, _ = engine.create_session(InsuranceType.AUTO)
    assert session.profile_id is None  # not yet materialized
    summary = engine.get_safe_profile_summary(session.session_id)
    assert summary.completed_field_count < summary.missing_field_count


def test_get_next_question_returns_catalog_field(tmp_path) -> None:
    engine = make_engine(tmp_path)
    session, _ = engine.create_session(InsuranceType.AUTO)
    _, question = engine.get_next_question(session.session_id)
    assert question is not None
    assert question.field_id == "legal_name"
    assert question.canonical_path == "applicant.identity.legal_name"


def test_valid_answer_updates_profile(tmp_path) -> None:
    engine = make_engine(tmp_path)
    session, _ = engine.create_session(InsuranceType.AUTO)
    result = engine.submit_answer(session.session_id, "applicant.identity.legal_name", SYNTHETIC_LEGAL_NAME)
    assert result.validation_success is True
    assert result.error_message is None


def test_next_question_changes(tmp_path) -> None:
    engine = make_engine(tmp_path)
    session, _ = engine.create_session(InsuranceType.AUTO)
    _, q1 = engine.get_next_question(session.session_id)
    engine.submit_answer(session.session_id, "applicant.identity.legal_name", SYNTHETIC_LEGAL_NAME)
    _, q2 = engine.get_next_question(session.session_id)
    assert q1.field_id == "legal_name"
    assert q2.field_id == "postal_code"


def test_answer_persists_in_canonical_profile(tmp_path) -> None:
    engine = make_engine(tmp_path)
    session, _ = engine.create_session(InsuranceType.AUTO)
    engine.submit_answer(session.session_id, "applicant.identity.legal_name", SYNTHETIC_LEGAL_NAME)
    engine.submit_answer(session.session_id, "applicant.address.postal_code", SYNTHETIC_POSTAL)
    profile = engine._vault.get(engine.get_session(session.session_id).profile_id)
    assert profile is not None
    assert profile.applicant.identity.legal_name == SYNTHETIC_LEGAL_NAME
    assert profile.applicant.address.postal_code == "M0A 0A0"


def test_external_request_for_missing_field(tmp_path) -> None:
    engine = make_engine(tmp_path)
    session, _ = engine.create_session(InsuranceType.AUTO)
    _complete_starter(engine, session.session_id)
    outcomes = engine.request_fields(
        session.session_id, ["product_data.vehicles[0].use.annual_kilometres"], "browser"
    )
    assert outcomes[0].state.value == "requested"
    assert outcomes[0].already_known is False


def test_requested_field_asked_only_once(tmp_path) -> None:
    engine = make_engine(tmp_path)
    session, _ = engine.create_session(InsuranceType.AUTO)
    _complete_starter(engine, session.session_id)
    sid = session.session_id
    engine.request_fields(sid, ["product_data.vehicles[0].use.annual_kilometres"])
    _, question = engine.get_next_question(sid)
    assert question.field_id == "vehicle_annual_km"
    # second request does not duplicate
    outcomes = engine.request_fields(sid, ["product_data.vehicles[0].use.annual_kilometres"])
    assert outcomes[0].already_known is False
    assert engine.get_session(sid).requested_fields.count("vehicle_annual_km") == 1


def test_browser_requested_field_can_be_fulfilled(tmp_path) -> None:
    engine = make_engine(tmp_path)
    session, _ = engine.create_session(InsuranceType.AUTO)
    _complete_starter(engine, session.session_id)
    sid = session.session_id
    engine.request_fields(sid, ["product_data.vehicles[0].use.annual_kilometres"], "browser")
    result = engine.submit_answer(sid, "product_data.vehicles[0].use.annual_kilometres", 12000)
    assert result.validation_success is True
    profile = engine._vault.get(engine.get_session(sid).profile_id)
    assert profile.product_data.vehicles[0].use.annual_kilometres == 12000


def test_voice_requested_field_uses_same_service(tmp_path) -> None:
    engine = make_engine(tmp_path)
    session, _ = engine.create_session(InsuranceType.AUTO)
    _complete_starter(engine, session.session_id)
    sid = session.session_id
    # voice agent requests the same field path - identical flow, no new logic
    outcomes = engine.request_fields(sid, ["product_data.vehicles[0].use.annual_kilometres"], "voice")
    assert outcomes[0].field_id == "vehicle_annual_km"
    assert outcomes[0].source_context == "voice"


def test_subsequent_route_sees_field_as_known(tmp_path) -> None:
    engine = make_engine(tmp_path)
    session, _ = engine.create_session(InsuranceType.AUTO)
    _complete_starter(engine, session.session_id)
    sid = session.session_id
    # TD asks
    engine.request_fields(sid, ["product_data.vehicles[0].use.annual_kilometres"], "td")
    engine.submit_answer(sid, "product_data.vehicles[0].use.annual_kilometres", 12000)
    # Aviva asks -> already known, no re-ask
    outcomes = engine.request_fields(sid, ["product_data.vehicles[0].use.annual_kilometres"], "aviva")
    assert outcomes[0].already_known is True
    assert outcomes[0].state.value == "answered"


def test_invalid_value_does_not_corrupt_profile(tmp_path) -> None:
    engine = make_engine(tmp_path)
    session, _ = engine.create_session(InsuranceType.AUTO)
    _complete_starter(engine, session.session_id)
    sid = session.session_id
    result = engine.submit_answer(sid, "product_data.vehicles[0].use.annual_kilometres", -500)
    assert result.validation_success is False
    assert result.retry_eligible is True
    assert "invalid value for canonical field path" in result.error_message
    # previous valid profile intact (annual_km never set)
    profile = engine._vault.get(engine.get_session(sid).profile_id)
    assert profile.product_data.vehicles[0].use.annual_kilometres is None


def test_corrected_value_succeeds(tmp_path) -> None:
    engine = make_engine(tmp_path)
    session, _ = engine.create_session(InsuranceType.AUTO)
    _complete_starter(engine, session.session_id)
    sid = session.session_id
    engine.submit_answer(sid, "product_data.vehicles[0].use.annual_kilometres", -500)
    result = engine.submit_answer(sid, "product_data.vehicles[0].use.annual_kilometres", 12000)
    assert result.validation_success is True
    profile = engine._vault.get(engine.get_session(sid).profile_id)
    assert profile.product_data.vehicles[0].use.annual_kilometres == 12000


def test_unknown_field_fails_safely(tmp_path) -> None:
    engine = make_engine(tmp_path)
    session, _ = engine.create_session(InsuranceType.AUTO)
    result = engine.submit_answer(session.session_id, "applicant.address.not_a_real_field", "x")
    assert result.validation_success is False
    assert "unsupported field path" in result.error_message
    # external request for unknown path
    outcomes = engine.request_fields(session.session_id, ["product_data.vehicles[0].use.mystery"])
    assert outcomes[0].state.value == "unsupported"
    assert outcomes[0].unsupported_reason


def test_disabled_field_not_automatically_asked(tmp_path) -> None:
    from intake_helpers import make_field, standard_fields, write_catalog
    from app.services.intake.catalog import IntakeFieldCatalog
    from app.services.intake.engine import IntakeEngine
    from app.services.intake.session_store import InMemorySessionStore
    from app.services.intake.vault import InMemoryProfileVault
    from app.services.intake.consent import ConsentService

    fields = standard_fields()
    fields.append(make_field("disabled_email", "applicant.contact.email", enabled=False))
    catalog = IntakeFieldCatalog(catalog_dir=write_catalog(tmp_path, fields))
    engine = IntakeEngine(
        catalog=catalog,
        vault=InMemoryProfileVault(),
        sessions=InMemorySessionStore(),
        consent=ConsentService(),
    )
    session, _ = engine.create_session(InsuranceType.AUTO)
    _complete_starter(engine, session.session_id)
    _, question = engine.get_next_question(session.session_id)
    # disabled field never surfaces; no starter fields left -> starter complete
    assert question is None
    assert engine.get_session(session.session_id).status.value == "starter_complete"


def test_unknown_session_raises(tmp_path) -> None:
    engine = make_engine(tmp_path)
    with pytest.raises(SessionNotFoundError):
        engine.get_session("nope")


def test_already_populated_field_skipped_in_progression(tmp_path) -> None:
    engine = make_engine(tmp_path)
    session, _ = engine.create_session(InsuranceType.AUTO)
    _complete_starter(engine, session.session_id)
    _, question = engine.get_next_question(session.session_id)
    # starter complete -> no starter field re-asked
    assert question is None
    assert engine.get_session(session.session_id).status.value == "starter_complete"


def test_profile_summary_contains_no_values(tmp_path) -> None:
    engine = make_engine(tmp_path)
    session, _ = engine.create_session(InsuranceType.AUTO)
    _complete_starter(engine, session.session_id)
    summary = engine.get_safe_profile_summary(session.session_id)
    dumped = summary.model_dump(mode="json")
    text = str(dumped)
    for marker in (SYNTHETIC_LICENCE, SYNTHETIC_VIN, SYNTHETIC_POSTAL, "TestMake"):
        assert marker not in text


def test_get_missing_requested_fields(tmp_path) -> None:
    engine = make_engine(tmp_path)
    session, _ = engine.create_session(InsuranceType.AUTO)
    _complete_starter(engine, session.session_id)
    sid = session.session_id
    engine.request_fields(sid, ["product_data.vehicles[0].use.annual_kilometres"])
    assert engine.get_missing_requested_fields(sid) == ["vehicle_annual_km"]
    engine.submit_answer(sid, "product_data.vehicles[0].use.annual_kilometres", 12000)
    assert engine.get_missing_requested_fields(sid) == []


def test_delete_session_removes_vault_profile(tmp_path) -> None:
    engine = make_engine(tmp_path)
    session, _ = engine.create_session(InsuranceType.AUTO)
    seed_profile(engine, session.session_id)
    profile_id = engine.get_session(session.session_id).profile_id
    assert engine._vault.exists(profile_id)
    engine.delete_session(session.session_id)
    with pytest.raises(SessionNotFoundError):
        engine.get_session(session.session_id)
    assert engine._vault.exists(profile_id) is False
