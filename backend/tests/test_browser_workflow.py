"""Issue #7 - LangGraph browser workflow: safe state + bounded loop."""

from __future__ import annotations

import json

from app.browser.mock_site import MOCK_REGISTRY_ID
from app.graph.browser_workflow import WORKFLOW_NAME, build_browser_workflow
from app.models.browser.session import BrowserExecutionMode
from browser_helpers import make_browser_env
from personas import make_standard_auto_profile

MARKERS = ["T0000-00000-00000", "1HGCM82633A000000", "1990-01-01", "123 Test Street"]


async def _stop(env, session_id: str) -> None:
    try:
        await env.manager.close(session_id)
    except Exception:
        pass
    try:
        await env.browser_manager.stop()
    except Exception:
        pass


async def test_workflow_state_is_safe_metadata_only(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site)
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await build_browser_workflow(env.manager).ainvoke(
            {"entry": "run", "browser_session_id": bs.browser_session_id, "max_steps": 12}
        )
        payload = json.dumps(state, default=str)
        for marker in MARKERS:
            assert marker not in payload
        assert state["workflow_status"] == "succeeded"
        assert state["observation_type"] == "quote_detected"
        assert state["registry_id"] == MOCK_REGISTRY_ID
        assert state["quote_present"] is True
        assert state["reference_present"] is True
        assert state["current_step"] >= 1
    finally:
        await _stop(env, bs.browser_session_id)


async def test_workflow_max_steps_bounds_the_loop(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site)
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await build_browser_workflow(env.manager).ainvoke(
            {"entry": "run", "browser_session_id": bs.browser_session_id, "max_steps": 1}
        )
        assert state["current_step"] <= 1
        # loop terminates (does not hang) even though the journey is not done
        assert state.get("workflow_status") in ("running", "paused_needs_field", "failed")
    finally:
        await _stop(env, bs.browser_session_id)


async def test_workflow_resume_continues_same_session(tmp_path, mock_site) -> None:
    env = make_browser_env(
        tmp_path, mock_site, persona=make_standard_auto_profile(annual_kilometres=None)
    )
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await build_browser_workflow(env.manager).ainvoke(
            {"entry": "run", "browser_session_id": bs.browser_session_id, "max_steps": 12}
        )
        assert state["workflow_status"] == "paused_needs_field"
        env.engine.submit_answer(env.session_id, "product_data.vehicles[0].use.annual_kilometres", 12000)
        resumed = await build_browser_workflow(env.manager).ainvoke(
            {"entry": "resume", "browser_session_id": bs.browser_session_id, "max_steps": 12}
        )
        assert resumed["workflow_status"] == "succeeded"
        assert resumed["quote_present"] is True
    finally:
        await _stop(env, bs.browser_session_id)
