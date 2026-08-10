"""Issue #7 - browser observation tests (captcha / checkpoint / callback /
unknown / quote) against the local mock site."""

from __future__ import annotations

from app.browser.mock_site import MOCK_REGISTRY_ID
from app.models.browser.session import BrowserExecutionMode
from browser_helpers import make_browser_env


async def _run(env, session_id: str, max_steps: int = 6) -> dict:
    from app.graph.browser_workflow import build_browser_workflow

    return await build_browser_workflow(env.manager).ainvoke(
        {"entry": "run", "browser_session_id": session_id, "max_steps": max_steps}
    )


async def _stop(env, session_id: str) -> None:
    try:
        await env.manager.close(session_id)
    except Exception:
        pass
    try:
        await env.browser_manager.stop()
    except Exception:
        pass


async def test_captcha_stops(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site, scenario="captcha")
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        assert state["workflow_status"] == "stopped_access_control"
        assert state["observation_type"] == "access_control_detected"
        session = env.manager.get(bs.browser_session_id)
        assert session.status.value == "stopped_access_control"
    finally:
        await _stop(env, bs.browser_session_id)


async def test_human_checkpoint_pauses(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site, scenario="checkpoint")
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        assert state["workflow_status"] == "paused_human_checkpoint"
        assert state["checkpoint_type"] == "identity_lookup"
        assert state["observation_type"] == "human_checkpoint"
    finally:
        await _stop(env, bs.browser_session_id)


async def test_prohibited_action_stops(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site, scenario="buy")
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        assert state["workflow_status"] == "stopped_prohibited"
        assert state["checkpoint_type"] == "purchase"
        assert state["observation_type"] == "human_checkpoint"
    finally:
        await _stop(env, bs.browser_session_id)


async def test_callback_observation(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site, scenario="callback")
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        assert state["workflow_status"] == "succeeded"
        assert state["observation_type"] == "callback_detected"
        assert state["quote_present"] is False
    finally:
        await _stop(env, bs.browser_session_id)


async def test_unknown_field_pauses(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site, scenario="unknown")
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        assert state["workflow_status"] == "paused_unknown_field"
        assert state["observation_type"] == "unknown_external_field"
        assert state["unknown_field_count"] >= 1
        result = env.manager.last_result(bs.browser_session_id)
        assert "some-unknown-field" in result.observation.unknown_external_fields
    finally:
        await _stop(env, bs.browser_session_id)


async def test_quote_observation_raw_fields(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site, scenario="quote")
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        assert state["workflow_status"] == "succeeded"
        assert state["observation_type"] == "quote_detected"
        assert state["quote_present"] is True
        result = env.manager.last_result(bs.browser_session_id)
        raw = result.observation.quote.raw
        assert raw.annual_amount_parsed == 1234.56
        assert raw.monthly_amount_raw is None
        assert raw.currency == "CAD"
        assert len(raw.coverage_observations) >= 2
        assert len(raw.discount_observations) >= 1
        assert raw.reference_present is True
        assert raw.is_firm_quote is True
    finally:
        await _stop(env, bs.browser_session_id)
