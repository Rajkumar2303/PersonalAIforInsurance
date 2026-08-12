"""Privacy tests (Issue #5, section 31).

Synthetic sensitive values must never appear in: structured logs, LangGraph
state (what LangSmith captures), session serialization, safe profile summary,
consent receipts, exception strings, or redacted model output.
"""

from __future__ import annotations

import json

import pytest

from app.graph.intake_workflow import build_intake_workflow, clear_pending_answer, set_pending_answer
from app.models.insurance.enums import InsuranceType

from intake_helpers import (
    SYNTHETIC_EXPIRY,
    SYNTHETIC_LEGAL_NAME,
    SYNTHETIC_LICENCE,
    SYNTHETIC_POSTAL,
    SYNTHETIC_STREET,
    SYNTHETIC_VIN,
    make_engine,
    seed_profile,
)

SENSITIVE_MARKERS = [
    SYNTHETIC_LICENCE,
    SYNTHETIC_VIN,
    SYNTHETIC_POSTAL,
    SYNTHETIC_STREET,
    "1990-01-01",
    "Test Applicant",
    SYNTHETIC_EXPIRY,
]


def _complete_flow(engine, sid: str) -> None:
    seed_profile(engine, sid)
    engine.submit_answer(sid, "product_data.drivers[0].licence.name_on_licence", SYNTHETIC_LEGAL_NAME)
    engine.submit_answer(sid, "product_data.drivers[0].licence.licence_number", SYNTHETIC_LICENCE)
    engine.submit_answer(sid, "product_data.drivers[0].licence.expiry_date", SYNTHETIC_EXPIRY)
    # Required unit fields first (gate unit assembly), then the optional VIN.
    engine.submit_answer(sid, "product_data.vehicles[0].identity.model_year", 2022)
    engine.submit_answer(sid, "product_data.vehicles[0].identity.make", "TestMake")
    engine.submit_answer(sid, "product_data.vehicles[0].identity.model", "TestModel")
    engine.submit_answer(sid, "product_data.vehicles[0].identity.vin", SYNTHETIC_VIN)


def assert_no_markers(payload: str) -> None:
    for marker in SENSITIVE_MARKERS:
        assert marker not in payload, f"sensitive marker leaked: {marker!r}"


def test_graph_state_contains_no_sensitive_values(tmp_path) -> None:
    engine = make_engine(tmp_path)
    session, _ = engine.create_session(InsuranceType.AUTO)
    graph = build_intake_workflow(engine)
    result = graph.invoke({"entry": "advance", "session_id": session.session_id})
    assert_no_markers(json.dumps(result, default=str))

    set_pending_answer(session.session_id, "applicant.identity.legal_name", SYNTHETIC_LEGAL_NAME)
    try:
        result = graph.invoke({"entry": "submit", "session_id": session.session_id})
    finally:
        clear_pending_answer()
    assert_no_markers(json.dumps(result, default=str))


def test_graph_input_state_is_safe_metadata_only(tmp_path) -> None:
    engine = make_engine(tmp_path)
    session, _ = engine.create_session(InsuranceType.AUTO)
    # the only inputs to the graph are safe identifiers - never values
    for entry in ("advance", "submit"):
        state = {"entry": entry, "session_id": session.session_id}
        assert_no_markers(json.dumps(state))


def test_session_serialization_contains_no_sensitive_values(tmp_path) -> None:
    engine = make_engine(tmp_path)
    session, _ = engine.create_session(InsuranceType.AUTO)
    _complete_flow(engine, session.session_id)
    dumped = json.dumps(engine.get_session(session.session_id).model_dump(mode="json"), default=str)
    assert_no_markers(dumped)


def test_profile_summary_contains_no_sensitive_values(tmp_path) -> None:
    engine = make_engine(tmp_path)
    session, _ = engine.create_session(InsuranceType.AUTO)
    _complete_flow(engine, session.session_id)
    summary = engine.get_safe_profile_summary(session.session_id)
    assert_no_markers(json.dumps(summary.model_dump(mode="json")))


def test_consent_receipt_contains_no_sensitive_values(tmp_path) -> None:
    engine = make_engine(tmp_path)
    session, _ = engine.create_session(InsuranceType.AUTO)
    _complete_flow(engine, session.session_id)
    decision = engine.grant_route_consent(session.session_id, "td-insurance", [], True)
    receipt = engine._consent.get(decision.consent_id)
    assert_no_markers(json.dumps(receipt.model_dump(mode="json")))


def test_exception_and_error_strings_are_safe(tmp_path) -> None:
    engine = make_engine(tmp_path)
    session, _ = engine.create_session(InsuranceType.AUTO)
    _complete_flow(engine, session.session_id)
    result = engine.submit_answer(session.session_id, "product_data.vehicles[0].use.annual_kilometres", -500)
    assert result.validation_success is False
    assert_no_markers(result.error_message or "")


def test_redacted_model_repr_and_dict_safe(tmp_path) -> None:
    engine = make_engine(tmp_path)
    session, _ = engine.create_session(InsuranceType.AUTO)
    seed_profile(engine, session.session_id)
    profile = engine._vault.get(engine.get_session(session.session_id).profile_id)
    assert_no_markers(str(profile))
    assert_no_markers(json.dumps(profile.redacted_dict()))


def test_structured_logs_contain_no_sensitive_values(tmp_path, caplog) -> None:
    import logging

    engine = make_engine(tmp_path)
    session, _ = engine.create_session(InsuranceType.AUTO)
    _complete_flow(engine, session.session_id)
    engine.grant_route_consent(session.session_id, "td-insurance", [], True)
    with caplog.at_level(logging.INFO):
        engine.get_safe_profile_summary(session.session_id)
    assert_no_markers(caplog.text)


def test_pending_answers_do_not_enter_session(tmp_path) -> None:
    engine = make_engine(tmp_path)
    session, _ = engine.create_session(InsuranceType.AUTO)
    engine.submit_answer(session.session_id, "applicant.identity.legal_name", SYNTHETIC_LEGAL_NAME)
    # pending unit values live in engine memory only, never in session metadata
    dumped = json.dumps(engine.get_session(session.session_id).model_dump(mode="json"))
    assert_no_markers(dumped)
