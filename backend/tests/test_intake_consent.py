"""Tests for consent receipts, route disclosure, and household-driver consent
(Issue #5, section 30)."""

from __future__ import annotations

import json

import pytest

from app.models.intake.consent import ConsentScope
from app.models.insurance.enums import InsuranceType

from intake_helpers import make_engine, seed_profile


@pytest.fixture()
def setup(tmp_path):
    engine = make_engine(tmp_path)
    session, _ = engine.create_session(InsuranceType.AUTO)
    seed_profile(engine, session.session_id)
    return engine, session.session_id


def test_collection_consent_recorded(setup) -> None:
    engine, sid = setup
    receipt = engine.record_collection_consent(sid)
    assert receipt.scope is ConsentScope.COLLECTION
    assert receipt.granted is True
    assert engine._consent.has_active(sid, ConsentScope.COLLECTION)


def test_granted_route_consent_produces_receipt(setup) -> None:
    engine, sid = setup
    decision = engine.grant_route_consent(sid, "td-insurance", ["applicant.identity.legal_name"], True)
    assert decision.granted is True
    assert decision.excluded is False
    assert decision.consent_id is not None


def test_denied_consent_blocks_and_excludes(setup) -> None:
    engine, sid = setup
    decision = engine.grant_route_consent(sid, "td-insurance", ["applicant.identity.legal_name"], False)
    assert decision.granted is False
    assert decision.excluded is True
    # denied -> no active disclosure consent for that route
    assert engine._consent.route_consent(sid, "td-insurance").granted is False


def test_route_disclosure_lists_paths_not_values(setup) -> None:
    engine, sid = setup
    disclosure = engine.create_route_disclosure(sid, "td-insurance")
    assert disclosure.registry_id == "td-insurance"
    assert disclosure.items  # populated fields
    dumped = json.dumps(disclosure.model_dump(mode="json"))
    for marker in ("Test Applicant", "M0A 0A0", "T0000-0000000-0000"):
        assert marker not in dumped
    # paths are present
    assert any(item.canonical_path == "applicant.identity.legal_name" for item in disclosure.items)


def test_route_consent_references_registry_id(setup) -> None:
    engine, sid = setup
    decision = engine.grant_route_consent(sid, "td-insurance", [], True)
    receipt = engine._consent.get(decision.consent_id)
    assert receipt.route_registry_id == "td-insurance"


def test_no_repeat_consent_ask_in_same_context(setup) -> None:
    engine, sid = setup
    engine.grant_route_consent(sid, "td-insurance", [], True)
    second = engine.grant_route_consent(sid, "td-insurance", [], False)
    assert second.already_decided is True
    assert second.granted is True  # first decision stands


def test_consent_receipt_contains_no_profile_values(setup) -> None:
    engine, sid = setup
    decision = engine.grant_route_consent(
        sid, "td-insurance", ["applicant.identity.legal_name", "applicant.address.postal_code"], True
    )
    receipt = engine._consent.get(decision.consent_id)
    dumped = json.dumps(receipt.model_dump(mode="json"))
    for marker in ("Test Applicant", "M0A 0A0", "T0000-0000000-0000"):
        assert marker not in dumped
    assert "applicant.identity.legal_name" in receipt.canonical_field_paths


def test_other_driver_requires_attestation_before_collection(tmp_path) -> None:
    engine = make_engine(tmp_path)
    session, _ = engine.create_session(InsuranceType.AUTO)
    seed_profile(engine, session.session_id)
    sid = session.session_id
    outcome = engine.request_fields(sid, ["product_data.drivers[0].other_drivers[0].name"], "broker")
    assert outcome[0].consent_required is True
    assert outcome[0].human_checkpoint_required is True
    assert outcome[0].checkpoint_kind == "consent_attestation"
    # direct submit is also blocked before attestation
    result = engine.submit_answer(sid, "product_data.drivers[0].other_drivers[0].name", "Other Person")
    assert result.validation_success is False
    assert "consent required" in result.error_message


def test_household_driver_attestation_unlocks_collection(tmp_path) -> None:
    engine = make_engine(tmp_path)
    session, _ = engine.create_session(InsuranceType.AUTO)
    seed_profile(engine, session.session_id)
    sid = session.session_id
    receipt = engine.record_household_driver_consent(sid, "driver_1")
    assert receipt.scope is ConsentScope.HOUSEHOLD_DRIVER
    assert receipt.subject_reference == "driver_1"
    outcome = engine.request_fields(sid, ["product_data.drivers[0].other_drivers[0].name"], "broker")
    assert outcome[0].consent_required is False
    assert outcome[0].state.value == "requested"


def test_consent_timestamp_serializes(setup) -> None:
    engine, sid = setup
    decision = engine.grant_route_consent(sid, "td-insurance", [], True)
    receipt = engine._consent.get(decision.consent_id)
    dumped = receipt.model_dump(mode="json")
    assert isinstance(dumped["timestamp"], str)  # ISO serialized


def test_revocation_marks_revoked(setup) -> None:
    engine, sid = setup
    decision = engine.grant_route_consent(sid, "td-insurance", [], True)
    revoked = engine._consent.revoke(decision.consent_id)
    assert revoked.revoked_at is not None
    assert engine._consent.has_active(sid, ConsentScope.ROUTE_DISCLOSURE, route_registry_id="td-insurance") is False
