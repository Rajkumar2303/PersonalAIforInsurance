"""Dynamic field-change tests (Issue #5, section 29 - REQUIRED).

Scenarios A-G prove the intake workflow does NOT need redesign when question
text, ordering, enabled state, new field definitions, or existing values
change. All changes are DATA changes in the field catalog.
"""

from __future__ import annotations

from app.models.insurance.enums import InsuranceType
from app.services.intake.vault import InMemoryProfileVault

from intake_helpers import (
    SYNTHETIC_LEGAL_NAME,
    SYNTHETIC_LICENCE,
    SYNTHETIC_POSTAL,
    SYNTHETIC_VIN,
    make_engine,
    make_field,
    seed_profile,
    standard_fields,
    write_catalog,
)

EXPIRY = "2030-12-31"


def _starter(engine, sid: str) -> None:
    seed_profile(engine, sid)
    engine.submit_answer(sid, "product_data.drivers[0].licence.name_on_licence", SYNTHETIC_LEGAL_NAME)
    engine.submit_answer(sid, "product_data.drivers[0].licence.licence_number", SYNTHETIC_LICENCE)
    engine.submit_answer(sid, "product_data.drivers[0].licence.expiry_date", EXPIRY)
    engine.submit_answer(sid, "product_data.vehicles[0].identity.vin", SYNTHETIC_VIN)
    engine.submit_answer(sid, "product_data.vehicles[0].identity.model_year", 2022)
    engine.submit_answer(sid, "product_data.vehicles[0].identity.make", "TestMake")
    engine.submit_answer(sid, "product_data.vehicles[0].identity.model", "TestModel")


def test_scenario_a_question_text_changes(tmp_path) -> None:
    fields = standard_fields()
    for f in fields:
        if f["field_id"] == "vehicle_annual_km":
            f["question"] = "How many kilometres in a typical year?"
    engine = make_engine(tmp_path, fields)
    session, _ = engine.create_session(InsuranceType.AUTO)
    _starter(engine, session.session_id)
    engine.request_fields(session.session_id, ["product_data.vehicles[0].use.annual_kilometres"])
    _, question = engine.get_next_question(session.session_id)
    assert question.question == "How many kilometres in a typical year?"
    assert question.field_id == "vehicle_annual_km"
    # behavior unchanged: answer still updates
    result = engine.submit_answer(
        session.session_id, "product_data.vehicles[0].use.annual_kilometres", 9000
    )
    assert result.validation_success is True


def test_scenario_b_new_route_specific_field_definition(tmp_path) -> None:
    fields = standard_fields()
    fields.append(
        make_field(
            "synthetic_gender",
            "applicant.identity.gender",
            intake_phase="route_specific",
            input_type="single_select",
            choices=["male", "female", "other", "prefer_not_to_say"],
            priority=5,
        )
    )
    engine = make_engine(tmp_path, fields)
    session, _ = engine.create_session(InsuranceType.AUTO)
    _starter(engine, session.session_id)
    sid = session.session_id
    outcome = engine.request_fields(sid, ["applicant.identity.gender"], "broker")
    assert outcome[0].state.value == "requested"
    _, question = engine.get_next_question(sid)
    assert question.field_id == "synthetic_gender"
    result = engine.submit_answer(sid, "applicant.identity.gender", "female")
    assert result.validation_success is True
    profile = engine._vault.get(engine.get_session(sid).profile_id)
    assert profile.applicant.identity.gender.value == "female"


def test_scenario_c_disable_field(tmp_path) -> None:
    email_field = make_field(
        "applicant_email",
        "applicant.contact.email",
        intake_phase="starter",
        priority=5,
        input_type="email",
    )
    # ENABLED: email is the first starter question after the seed fields
    engine_enabled = make_engine(tmp_path, [*standard_fields(), email_field])
    session, _ = engine_enabled.create_session(InsuranceType.AUTO)
    seed_profile(engine_enabled, session.session_id)
    _, question = engine_enabled.get_next_question(session.session_id)
    assert question.field_id == "applicant_email"
    # DISABLED: the same field disappears from starter questions (data only)
    disabled_email = {**email_field, "enabled": False}
    engine_disabled = make_engine(tmp_path, [*standard_fields(), disabled_email])
    session2, _ = engine_disabled.create_session(InsuranceType.AUTO)
    seed_profile(engine_disabled, session2.session_id)
    _, question2 = engine_disabled.get_next_question(session2.session_id)
    assert question2 is not None
    assert question2.field_id != "applicant_email"
    assert question2.field_id == "driver_name_on_licence"


def test_scenario_d_reorder_fields(tmp_path) -> None:
    fields = standard_fields()
    for f in fields:
        if f["field_id"] == "legal_name":
            f["priority"] = 20
        if f["field_id"] == "postal_code":
            f["priority"] = 10
    engine = make_engine(tmp_path, fields)
    session, _ = engine.create_session(InsuranceType.AUTO)
    _, question = engine.get_next_question(session.session_id)
    assert question.field_id == "postal_code"  # order changed from data only


def test_scenario_e_existing_value_not_asked(tmp_path) -> None:
    fields = [f for f in standard_fields() if f["field_id"] != "date_of_birth"]
    vault = InMemoryProfileVault()
    engine = make_engine(tmp_path, fields, vault=vault)
    session, _ = engine.create_session(InsuranceType.AUTO)
    seed_profile(engine, session.session_id)
    # populate the field directly (simulating prior collection)
    profile_id = engine.get_session(session.session_id).profile_id
    profile = vault.get(profile_id)
    vault.update(profile_id, profile.updated("applicant.identity.date_of_birth", "1990-01-01"))
    # add a catalog definition for the already-populated field
    engine2 = make_engine(tmp_path, standard_fields(), vault=vault, sessions=engine._sessions)
    outcome = engine2.request_fields(session.session_id, ["applicant.identity.date_of_birth"])
    assert outcome[0].already_known is True
    assert outcome[0].state.value == "answered"
    _, question = engine2.get_next_question(session.session_id)
    assert question is None or question.field_id != "date_of_birth"


def test_scenario_f_unknown_future_field_structured_result(tmp_path) -> None:
    engine = make_engine(tmp_path)
    session, _ = engine.create_session(InsuranceType.AUTO)
    outcome = engine.request_fields(session.session_id, ["applicant.address.years_lived_in_province"], "browser")
    assert outcome[0].state.value == "unsupported"
    assert outcome[0].unsupported_reason
    assert outcome[0].canonical_path == "applicant.address.years_lived_in_province"
    # no crash, no guessing
    assert session.session_id in engine._sessions._sessions


def test_scenario_g_years_at_current_address_is_localized(tmp_path) -> None:
    """Newly discovered question: schema field + catalog + validation only.

    Proves support came from one optional schema field, one catalog definition,
    and validation/tests - NOT a new graph node, an insurer branch, or a
    field-specific service method.
    """
    from app.models.insurance.common import AddressInformation

    # 1. one localized optional schema field
    assert "years_at_current_address" in AddressInformation.model_fields

    # 2. the engine has NO special case for it (no field-specific branch)
    import inspect

    import app.services.intake.engine as engine_module

    source = inspect.getsource(engine_module)
    assert "years_at_current_address" not in source

    # 3. end-to-end: catalog resolves it -> missing -> asked -> validated ->
    #    stored -> reused
    engine = make_engine(tmp_path)
    session, _ = engine.create_session(InsuranceType.AUTO)
    _starter(engine, session.session_id)
    sid = session.session_id
    outcome = engine.request_fields(sid, ["applicant.address.years_at_current_address"], "browser")
    assert outcome[0].state.value == "requested"
    _, question = engine.get_next_question(sid)
    assert question.field_id == "years_at_current_address"
    # invalid value rejected
    bad = engine.submit_answer(sid, "applicant.address.years_at_current_address", -3)
    assert bad.validation_success is False
    # valid value stored
    good = engine.submit_answer(sid, "applicant.address.years_at_current_address", 7)
    assert good.validation_success is True
    profile = engine._vault.get(engine.get_session(sid).profile_id)
    assert profile.applicant.address.years_at_current_address == 7
    # reused without asking again
    reuse = engine.request_fields(sid, ["applicant.address.years_at_current_address"], "aviva")
    assert reuse[0].already_known is True
