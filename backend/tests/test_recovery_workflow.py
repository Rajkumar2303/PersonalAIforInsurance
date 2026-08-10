"""Issue #8 - LangGraph recovery workflow tests (safe metadata-only state)."""

from __future__ import annotations

from app.graph.recovery_workflow import WORKFLOW_NAME, build_recovery_workflow
from recovery_helpers import make_recovery_env

PII_MARKERS = ["T0000-0000000-0000", "1HGCM82633A000000", "1990-01-01", "123 Test Street"]


def _state(env, observation_type: str, **extra):
    base = {
        "entry": "decide",
        "plan_id": "p1",
        "planned_route_id": "route-a",
        "registry_id": "route-a",
        "distinct_rate_source_id": "RS-A",
        "intake_session_id": env.session_id,
        "observation_type": observation_type,
    }
    base.update(extra)
    return base


async def test_workflow_quote_pending_normalization(tmp_path):
    env = make_recovery_env(tmp_path)
    graph = build_recovery_workflow(env.engine)
    state = await graph.ainvoke(
        _state(env, "quote_detected", safe_context={"quote_present": True, "is_firm_quote": True})
    )
    assert state["workflow_stage"] == "decide"
    assert state["workflow_status"] == "decided"
    assert state["lifecycle_status"] == "terminal"
    assert state["recommended_action"] == "stop_terminal"
    assert state["terminal_status"] is None
    assert state["quote_pending_normalization"] is True
    assert state["reason_codes"] == ["quote_observed"]


async def test_workflow_pause_for_missing_field(tmp_path):
    env = make_recovery_env(tmp_path)
    graph = build_recovery_workflow(env.engine)
    state = await graph.ainvoke(_state(env, "needs_field", safe_context={"missing_field_paths": ["a"]}))
    assert state["lifecycle_status"] == "paused"
    assert state["recommended_action"] == "resume_after_user_input"
    assert state["terminal_status"] is None


async def test_workflow_retry_recoverable(tmp_path):
    env = make_recovery_env(tmp_path)
    graph = build_recovery_workflow(env.engine)
    state = await graph.ainvoke(
        _state(env, "technical_error", reason="nav",
               safe_context={"error_type": "navigation_timeout"})
    )
    assert state["lifecycle_status"] == "recoverable"
    assert state["recommended_action"] == "retry_same_route"
    assert state["retry_allowed"] is True
    assert state["attempts_used"] == 1
    assert state["attempts_remaining"] == 1


async def test_workflow_captcha_terminal_blocked(tmp_path):
    env = make_recovery_env(tmp_path)
    graph = build_recovery_workflow(env.engine)
    state = await graph.ainvoke(_state(env, "access_control_detected", safe_context={"error_type": "captcha"}))
    assert state["lifecycle_status"] == "terminal"
    assert state["terminal_status"] == "blocked"
    assert state["retry_allowed"] is False


async def test_workflow_invalid_request(tmp_path):
    env = make_recovery_env(tmp_path)
    graph = build_recovery_workflow(env.engine)
    state = await graph.ainvoke({"entry": "decide", "planned_route_id": "route-a"})
    assert state["workflow_status"] == "invalid_request"


async def test_workflow_state_never_contains_pii(tmp_path):
    env = make_recovery_env(tmp_path)
    graph = build_recovery_workflow(env.engine)
    state = await graph.ainvoke(
        _state(env, "quote_detected", safe_context={"quote_present": True, "is_firm_quote": True})
    )
    text = str(state)
    for marker in PII_MARKERS:
        assert marker not in text


def test_workflow_name():
    assert WORKFLOW_NAME == "recovery_workflow"
