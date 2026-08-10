"""Issue #7 - browser executor integration tests (local mock quote site).

Hermetic: local mock site only, no external network, no LLM, no LangSmith
uploads. Uses the real Playwright Chromium (headless) against the mock site.
"""

from __future__ import annotations

import pytest

from app.browser.mock_site import MOCK_REGISTRY_ID, build_mock_route_config
from app.models.browser.config import BrowserFieldBinding, MatchPattern, MatchStrategy
from app.models.browser.session import BrowserExecutionMode
from browser_helpers import make_browser_env
from personas import make_standard_auto_profile

ANNUAL_KM = "product_data.vehicles[0].use.annual_kilometres"


async def _run_workflow(env, session_id: str, entry: str = "run", max_steps: int = 15) -> dict:
    from app.graph.browser_workflow import build_browser_workflow

    return await build_browser_workflow(env.manager).ainvoke(
        {"entry": entry, "browser_session_id": session_id, "max_steps": max_steps}
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


async def test_full_journey_standard_reaches_quote(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site)
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run_workflow(env, bs.browser_session_id)
        assert state["workflow_status"] == "succeeded"
        session = env.manager.get(bs.browser_session_id)
        assert session.quote_present is True
        assert session.reference_present is True
        result = env.manager.last_result(bs.browser_session_id)
        assert result.observation_type.value == "quote_detected"
        raw = result.observation.quote.raw
        assert raw.annual_amount_parsed == 1234.56
        assert raw.currency == "CAD"
        assert raw.is_firm_quote is True
    finally:
        await _stop(env, bs.browser_session_id)


async def test_all_fill_strategies_encountered(tmp_path, mock_site) -> None:
    """text / select / date / integer / checkbox / radio all handled."""
    env = make_browser_env(tmp_path, mock_site)
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        await _run_workflow(env, bs.browser_session_id)
        session = env.manager.get(bs.browser_session_id)
        expected = {
            "legal-name", "preferred-language", "postal-code", "date-of-birth", "street",
            "vin", "model-year", "annual-km", "winter-tires", "carpool-yes",
        }
        assert expected <= set(session.observed_field_ids), (
            f"missing: {sorted(expected - set(session.observed_field_ids))}"
        )
        assert session.quote_present is True
    finally:
        await _stop(env, bs.browser_session_id)


async def test_missing_field_pauses_then_resumes(tmp_path, mock_site) -> None:
    env = make_browser_env(
        tmp_path, mock_site, persona=make_standard_auto_profile(annual_kilometres=None)
    )
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run_workflow(env, bs.browser_session_id)
        assert state["workflow_status"] == "paused_needs_field"
        session = env.manager.get(bs.browser_session_id)
        assert ANNUAL_KM in session.pending_field_paths

        # Applicant answers through Issue #5 (same intake session).
        result = env.engine.submit_answer(env.session_id, ANNUAL_KM, 12000)
        assert result.validation_success is True

        # Resume the SAME browser session (no restart).
        state2 = await _run_workflow(env, bs.browser_session_id, entry="resume")
        assert state2["workflow_status"] == "succeeded"
        assert env.manager.get(bs.browser_session_id).quote_present is True
    finally:
        await _stop(env, bs.browser_session_id)


async def test_known_field_not_reasked_after_resume(tmp_path, mock_site) -> None:
    env = make_browser_env(
        tmp_path, mock_site, persona=make_standard_auto_profile(annual_kilometres=None)
    )
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        await _run_workflow(env, bs.browser_session_id)
        env.engine.submit_answer(env.session_id, ANNUAL_KM, 12000)
        await _run_workflow(env, bs.browser_session_id, entry="resume")
        # Ask-once preserved: the field was requested once, then known.
        assert env.engine.get_missing_requested_fields(env.session_id) == []
        intake = env.engine.get_session(env.session_id)
        assert "vehicle_annual_km" in intake.requested_fields
    finally:
        await _stop(env, bs.browser_session_id)


async def test_conditional_field_revealed_and_filled(tmp_path, mock_site) -> None:
    persona = make_standard_auto_profile(**{"product_data.vehicles[0].use.carpool": True})
    env = make_browser_env(tmp_path, mock_site, persona=persona, scenario="commute")
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        await _run_workflow(env, bs.browser_session_id)
        session = env.manager.get(bs.browser_session_id)
        assert session.quote_present is True
        assert "one-way-commute" in session.observed_field_ids
    finally:
        await _stop(env, bs.browser_session_id)


async def test_dynamic_selector_change_no_executor_change(tmp_path, mock_site) -> None:
    """Change id="annual-km" -> id="distance-driven"; label-based mapping still
    works - no BrowserExecutor code changes needed."""
    env = make_browser_env(tmp_path, mock_site, scenario="change")
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        await _run_workflow(env, bs.browser_session_id)
        session = env.manager.get(bs.browser_session_id)
        assert session.quote_present is True
        assert "annual-km" in session.observed_field_ids or "distance-driven" in session.observed_field_ids
    finally:
        await _stop(env, bs.browser_session_id)


async def test_new_known_field_no_executor_change(tmp_path, mock_site) -> None:
    """A new known optional field (email) is handled via config/binding only."""
    config = build_mock_route_config(MOCK_REGISTRY_ID, start_url=mock_site.url("/newfield"))
    email_binding = BrowserFieldBinding(
        external_field_id="email",
        match_patterns=[MatchPattern(strategy=MatchStrategy.LABEL_TEXT, value="Email address")],
        canonical_path="applicant.contact.email",
        control_type="input",
    )
    config = config.model_copy(update={"field_bindings": [*config.field_bindings, email_binding]})
    env = make_browser_env(tmp_path, mock_site, route_config=config)
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        await _run_workflow(env, bs.browser_session_id)
        session = env.manager.get(bs.browser_session_id)
        assert session.quote_present is True
        assert "email" in session.observed_field_ids
    finally:
        await _stop(env, bs.browser_session_id)


async def test_browser_closes_cleanly(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site)
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        await _run_workflow(env, bs.browser_session_id)
        closed = await env.manager.close(bs.browser_session_id)
        assert closed.status.value == "closed"
        assert bs.browser_session_id not in env.manager._pages
        # idempotent close
        closed2 = await env.manager.close(bs.browser_session_id)
        assert closed2.status.value == "closed"
    finally:
        await env.browser_manager.stop()
