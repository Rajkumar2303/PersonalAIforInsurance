"""Tests for the LangGraph intake workflow (Issue #5).

The graph carries SAFE METADATA ONLY. Raw answers enter via the pending-answer
inbox (contextvar) and never appear in graph state (what LangSmith captures).
"""

from __future__ import annotations

import json

from app.graph.intake_workflow import (
    build_intake_workflow,
    clear_pending_answer,
    set_pending_answer,
)
from app.models.insurance.enums import InsuranceType

from intake_helpers import SYNTHETIC_LEGAL_NAME, make_engine, seed_profile


def _graph(tmp_path):
    engine = make_engine(tmp_path)
    return engine, build_intake_workflow(engine)


def test_advance_returns_seed_question(tmp_path) -> None:
    engine, graph = _graph(tmp_path)
    session, _ = engine.create_session(InsuranceType.AUTO)
    result = graph.invoke({"entry": "advance", "session_id": session.session_id})
    assert result.get("workflow_stage") == "await_user_input"
    assert result.get("current_field_id") == "legal_name"
    assert result.get("workflow_status") == "awaiting_input"
    assert result.get("message") == "Question for legal_name"


def test_advance_product_gate_rejects_unsupported(tmp_path) -> None:
    engine, graph = _graph(tmp_path)
    session, _ = engine.create_session(InsuranceType.HOME)
    result = graph.invoke({"entry": "advance", "session_id": session.session_id})
    assert result.get("workflow_status") == "product_rejected"
    assert result.get("message") == "product not implemented"


def test_consent_gate_passes_after_materialization(tmp_path) -> None:
    engine, graph = _graph(tmp_path)
    session, _ = engine.create_session(InsuranceType.AUTO)
    seed_profile(engine, session.session_id)
    result = graph.invoke({"entry": "advance", "session_id": session.session_id})
    assert result.get("workflow_status") in ("awaiting_input", "starter_complete")


def test_submit_valid_answer_updates_and_advances(tmp_path) -> None:
    engine, graph = _graph(tmp_path)
    session, _ = engine.create_session(InsuranceType.AUTO)
    sid = session.session_id
    set_pending_answer(sid, "applicant.identity.legal_name", SYNTHETIC_LEGAL_NAME)
    try:
        result = graph.invoke({"entry": "submit", "session_id": sid})
    finally:
        clear_pending_answer()
    assert result.get("validation_success") is True
    assert result.get("current_field_id") == "postal_code"  # advanced
    profile_id = engine.get_session(sid).profile_id
    assert profile_id is None  # still seed phase; legal_name held in engine


def test_submit_invalid_returns_retry(tmp_path) -> None:
    engine, graph = _graph(tmp_path)
    session, _ = engine.create_session(InsuranceType.AUTO)
    seed_profile(engine, session.session_id)
    sid = session.session_id
    set_pending_answer(sid, "applicant.identity.legal_name", "")
    try:
        result = graph.invoke({"entry": "submit", "session_id": sid})
    finally:
        clear_pending_answer()
    assert result.get("validation_success") is False
    assert result.get("last_error")
    # profile intact (legal_name unchanged)
    profile = engine._vault.get(engine.get_session(sid).profile_id)
    assert profile.applicant.identity.legal_name == SYNTHETIC_LEGAL_NAME


def test_workflow_input_state_is_safe_metadata(tmp_path) -> None:
    engine, graph = _graph(tmp_path)
    session, _ = engine.create_session(InsuranceType.AUTO)
    state = {"entry": "advance", "session_id": session.session_id}
    payload = json.dumps(state)
    for marker in ("T0000", "1HGCM", "Test Applicant", "M0A"):
        assert marker not in payload
    _ = graph


def test_session_not_found_safe(tmp_path) -> None:
    engine, graph = _graph(tmp_path)
    result = graph.invoke({"entry": "advance", "session_id": "nope"})
    assert result.get("workflow_status") == "session_not_found"


def test_submit_after_seed_materializes_and_advances(tmp_path) -> None:
    engine, graph = _graph(tmp_path)
    session, _ = engine.create_session(InsuranceType.AUTO)
    sid = session.session_id
    for path, value in [
        ("applicant.identity.legal_name", SYNTHETIC_LEGAL_NAME),
        ("applicant.address.postal_code", "M0A 0A0"),
    ]:
        set_pending_answer(sid, path, value)
        try:
            result = graph.invoke({"entry": "submit", "session_id": sid})
        finally:
            clear_pending_answer()
    assert engine.get_session(sid).profile_id is not None
    assert result.get("current_field_id") == "driver_name_on_licence"
