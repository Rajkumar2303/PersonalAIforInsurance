"""Issue #7 Prompt 2 - browser-flow hardening tests (local mock site).

Covers: multiple missing fields, same-field reuse, conditional chains,
dynamic field changes A-G, form-control handling, optional-unknown policy,
unsupported option, validation errors, ambiguity, consent expansion/revocation,
household-driver gate, human checkpoints, CAPTCHA variants, host safety,
browser crash, timeout, session lifecycle, concurrent sessions, quote
variants, estimate-vs-firm, callback context, private references, live-mode
safety, config-driven site change, and a second synthetic route.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.browser.executor import BrowserExecutor
from app.browser.mock_site import MOCK_REGISTRY_ID, build_mock_route_config
from app.browser.session import BrowserExecutionMode, BrowserSessionManager, BrowserSessionNotFoundError
from app.browser.value_provider import IntakeValueSource
from app.graph.browser_workflow import build_browser_workflow
from app.models.browser.config import (
    BrowserFieldBinding,
    FillStrategy,
    MatchPattern,
    MatchStrategy,
    TransformKind,
)
from app.models.browser.session import LiveExecutionGate
from app.models.insurance.enums import InsuranceType
from browser_helpers import make_browser_env
from personas import make_edge_case_auto_profile, make_standard_auto_profile

ANNUAL_KM = "product_data.vehicles[0].use.annual_kilometres"
YEARS_ADDR = "applicant.address.years_at_current_address"
TPL = "product_data.coverage.third_party_liability.selected_limit"
VIN = "product_data.vehicles[0].identity.vin"
MODEL_YEAR = "product_data.vehicles[0].identity.model_year"
CARPOOL = "product_data.vehicles[0].use.carpool"
WINTER = "product_data.vehicles[0].risk.winter_tires"
SENSITIVE = ["T0000-00000-00000", "1HGCM82633A000000", "1990-01-01", "123 Test Street",
             "MOCK-8F3K-2026", "test.applicant@example.com"]


async def _run(env, session_id: str, entry: str = "run", max_steps: int = 20) -> dict:
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


# --- 2. multiple missing fields ----------------------------------------

async def test_multiple_missing_fields_batched(tmp_path, mock_site) -> None:
    persona = make_standard_auto_profile(annual_kilometres=None, years_at_current_address=None, tpl_selected_limit=None)
    env = make_browser_env(tmp_path, mock_site, persona=persona, scenario="multi-missing")
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        assert state["workflow_status"] == "paused_needs_field"
        session = env.manager.get(bs.browser_session_id)
        # deterministic order, no duplicates, all three present
        assert session.pending_field_paths == sorted({ANNUAL_KM, YEARS_ADDR, TPL})
        assert len(session.pending_field_paths) == 3
        # supply all three -> resume -> quote
        env.engine.submit_answer(env.session_id, ANNUAL_KM, 12000)
        env.engine.submit_answer(env.session_id, YEARS_ADDR, 4)
        env.engine.submit_answer(env.session_id, TPL, 2_000_000)
        state2 = await _run(env, bs.browser_session_id, entry="resume")
        assert state2["workflow_status"] == "succeeded"
    finally:
        await _stop(env, bs.browser_session_id)


async def test_same_field_appears_twice_reused(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site, scenario="annual-twice")
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        assert state["workflow_status"] == "succeeded"
        assert state["quote_present"] is True
        # annual-kilometres was never re-asked (no pause)
        assert "paused_needs_field" not in state["workflow_status"]
        # canonical profile remained the single source of truth
        assert env.engine.get_session(env.session_id).requested_fields.count("vehicle_annual_km") <= 1
    finally:
        await _stop(env, bs.browser_session_id)


# --- 4. conditional field chain ----------------------------------------

async def test_conditional_field_chain(tmp_path, mock_site) -> None:
    persona = make_standard_auto_profile(**{
        "product_data.vehicles[0].use.commuting": True,
        "product_data.vehicles[0].special_use.rideshare": True,
        "product_data.vehicles[0].use.rideshare_hours_per_week": 5.0,
        "product_data.vehicles[0].use.one_way_commute_distance_km": 18,
    })
    env = make_browser_env(tmp_path, mock_site, persona=persona, scenario="chain")
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        assert state["workflow_status"] == "succeeded"
        session = env.manager.get(bs.browser_session_id)
        # newly visible chain fields were detected + filled (no executor branch)
        assert "one-way-commute" in session.observed_field_ids
        assert "rideshare-hours" in session.observed_field_ids
    finally:
        await _stop(env, bs.browser_session_id)


# --- 5. dynamic field changes A-G --------------------------------------

async def test_dynamic_label_wording_change_via_config(tmp_path, mock_site) -> None:
    # Old config (label "annual distance") on the re-worded page -> unknown pause.
    env_old = make_browser_env(tmp_path, mock_site, scenario="label-changed")
    bs_old = env_old.manager.create(env_old.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env_old, bs_old.browser_session_id)
        assert state["workflow_status"] == "paused_unknown_field"
    finally:
        await _stop(env_old, bs_old.browser_session_id)

    # New config (binding updated to the new wording) -> works with NO executor change.
    config = build_mock_route_config(MOCK_REGISTRY_ID, start_url=mock_site.url("/page-b?label=1"))
    new_binding = BrowserFieldBinding(
        external_field_id="annual-km",
        match_patterns=[MatchPattern(strategy=MatchStrategy.LABEL_CONTAINS, value="how far do you drive")],
        canonical_path=ANNUAL_KM,
        fill_strategy=FillStrategy.INTEGER, transform=TransformKind.INTEGER_TO_STRING,
    )
    config = config.model_copy(update={"field_bindings": [b for b in config.field_bindings if b.external_field_id != "annual-km"] + [new_binding]})
    env_new = make_browser_env(tmp_path, mock_site, scenario="label-changed", route_config=config)
    bs_new = env_new.manager.create(env_new.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env_new, bs_new.browser_session_id)
        assert state["workflow_status"] == "succeeded"
    finally:
        await _stop(env_new, bs_new.browser_session_id)


async def test_dynamic_question_order_change(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site, scenario="order")
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        assert state["workflow_status"] == "succeeded"
    finally:
        await _stop(env, bs.browser_session_id)


async def test_optional_field_becomes_required(tmp_path, mock_site) -> None:
    persona = make_standard_auto_profile(**{"product_data.vehicles[0].risk.winter_tires": None})
    env = make_browser_env(tmp_path, mock_site, persona=persona, scenario="winter-required")
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        assert state["workflow_status"] == "paused_needs_field"
        assert WINTER in env.manager.get(bs.browser_session_id).pending_field_paths
    finally:
        await _stop(env, bs.browser_session_id)


async def test_field_removed_still_works(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site, scenario="nomodel")
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        assert state["workflow_status"] == "succeeded"
    finally:
        await _stop(env, bs.browser_session_id)


async def test_field_type_change_to_select_via_config(tmp_path, mock_site) -> None:
    # Without the config change, filling a select with INTEGER fill fails safely.
    env_bad = make_browser_env(tmp_path, mock_site, scenario="annual-select")
    bs_bad = env_bad.manager.create(env_bad.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env_bad, bs_bad.browser_session_id)
        assert state["workflow_status"] == "failed"
        result = env_bad.manager.last_result(bs_bad.browser_session_id)
        assert ANNUAL_KM in (result.observation.error_paths or [])
    finally:
        await _stop(env_bad, bs_bad.browser_session_id)

    # Config change (fill strategy SELECT + option_map) -> works, no executor change.
    config = build_mock_route_config(MOCK_REGISTRY_ID, start_url=mock_site.url("/page-b?annual=select"))
    select_binding = BrowserFieldBinding(
        external_field_id="annual-km",
        match_patterns=[MatchPattern(strategy=MatchStrategy.LABEL_CONTAINS, value="annual distance")],
        canonical_path=ANNUAL_KM,
        control_type="select", fill_strategy=FillStrategy.SELECT,
        transform=TransformKind.ENUM_TO_LABEL,
        option_map={"12000": "10,000 - 15,000"},
    )
    config = config.model_copy(update={"field_bindings": [b for b in config.field_bindings if b.external_field_id != "annual-km"] + [select_binding]})
    env_ok = make_browser_env(tmp_path, mock_site, scenario="annual-select", route_config=config)
    bs_ok = env_ok.manager.create(env_ok.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env_ok, bs_ok.browser_session_id)
        assert state["workflow_status"] == "succeeded"
    finally:
        await _stop(env_ok, bs_ok.browser_session_id)


# --- 6. form control handling ------------------------------------------

async def test_form_controls_all_strategies(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site, scenario="controls-ok")
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        assert state["workflow_status"] == "succeeded"
        ids = set(env.manager.get(bs.browser_session_id).observed_field_ids)
        for expected in ("c-text", "c-int", "c-dec", "c-select", "c-radio-yes", "c-check", "c-date", "c-postal"):
            assert expected in ids
    finally:
        await _stop(env, bs.browser_session_id)


async def test_hidden_disabled_readonly_ignored(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site, scenario="controls-skip")
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        assert state["workflow_status"] == "succeeded"
        ids = set(env.manager.get(bs.browser_session_id).observed_field_ids)
        assert "d-disabled" not in ids and "d-readonly" not in ids and "hidden_field" not in ids
    finally:
        await _stop(env, bs.browser_session_id)


async def test_aria_and_placeholder_controls(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site, scenario="controls-aria")
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        assert state["workflow_status"] == "succeeded"
    finally:
        await _stop(env, bs.browser_session_id)


async def test_duplicate_label_ambiguity_pauses(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site, scenario="controls-dup")
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        assert state["workflow_status"] == "paused_ambiguous"
        assert state["observation_type"] == "ambiguous_field"
        result = env.manager.last_result(bs.browser_session_id)
        assert "legal-name" in result.observation.ambiguous_field_ids
    finally:
        await _stop(env, bs.browser_session_id)


async def test_required_no_label_unknown_pauses(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site, scenario="controls-nolabel")
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        assert state["workflow_status"] == "paused_unknown_field"
    finally:
        await _stop(env, bs.browser_session_id)


async def test_optional_unknown_field_left_blank(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site, scenario="controls-optional-unknown")
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        assert state["workflow_status"] == "succeeded"
    finally:
        await _stop(env, bs.browser_session_id)


# --- 8. option not available -------------------------------------------

async def test_unsupported_dropdown_value_pauses(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site, scenario="select-unsupported")
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        assert state["workflow_status"] == "paused_value_not_supported"
        assert state["observation_type"] == "value_not_supported"
        result = env.manager.last_result(bs.browser_session_id)
        assert "applicant.identity.preferred_language" in result.observation.unsupported_value_paths
    finally:
        await _stop(env, bs.browser_session_id)


# --- 9. website validation error ---------------------------------------

async def test_website_validation_error_pauses_no_loop(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site, scenario="validate")
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        assert state["workflow_status"] == "paused_validation_error"
        assert state["observation_type"] == "validation_error"
        # bounded: it never loops indefinitely
        assert state["current_step"] <= 6
    finally:
        await _stop(env, bs.browser_session_id)


# --- 12. consent expansion mid-session ---------------------------------

async def test_consent_expansion_mid_session(tmp_path, mock_site) -> None:
    covered = [VIN, MODEL_YEAR, CARPOOL, WINTER]
    env = make_browser_env(tmp_path, mock_site, scenario="vehicle", consent_paths=covered)
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        # annual_km is NOT in the disclosure scope -> pause before any fill
        state = await _run(env, bs.browser_session_id)
        assert state["workflow_status"] == "paused_needs_consent"
        assert ANNUAL_KM in env.manager.get(bs.browser_session_id).pending_field_paths
        # applicant expands disclosure scope (revoke + re-grant with the path)
        existing = env.engine._consent.route_consent(env.session_id, MOCK_REGISTRY_ID)
        env.engine._consent.revoke(existing.consent_id)
        env.engine.grant_route_consent(env.session_id, MOCK_REGISTRY_ID, [*covered, ANNUAL_KM], True)
        state2 = await _run(env, bs.browser_session_id, entry="resume")
        assert state2["workflow_status"] == "succeeded"
    finally:
        await _stop(env, bs.browser_session_id)


# --- 13. consent revoked while paused ----------------------------------

async def test_consent_revoked_while_paused(tmp_path, mock_site) -> None:
    env = make_browser_env(
        tmp_path, mock_site, scenario="vehicle",
        persona=make_standard_auto_profile(annual_kilometres=None),
    )
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        assert state["workflow_status"] == "paused_needs_field"
        # revoke route consent before resume
        decision = env.engine._consent.route_consent(env.session_id, MOCK_REGISTRY_ID)
        env.engine._consent.revoke(decision.consent_id)
        state2 = await _run(env, bs.browser_session_id, entry="resume")
        assert state2["workflow_status"] == "paused_needs_consent"
    finally:
        await _stop(env, bs.browser_session_id)


# --- 14. household driver data -----------------------------------------

async def test_household_driver_consent_gate(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site, scenario="household", persona=make_edge_case_auto_profile())
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        assert state["workflow_status"] == "paused_needs_consent"
        # no household-driver value filled before consent
        env.engine.record_household_driver_consent(env.session_id, "household")
        state2 = await _run(env, bs.browser_session_id, entry="resume")
        assert state2["workflow_status"] == "succeeded"
    finally:
        await _stop(env, bs.browser_session_id)


# --- 15. human checkpoint hardening ------------------------------------

@pytest.mark.parametrize("scenario,expected_status", [
    ("checkpoint", "paused_human_checkpoint"),       # identity lookup (approvable)
    ("declaration", "stopped_prohibited"),           # application declaration: terminal, never automated/approved
    ("consent-attestation", "paused_human_checkpoint"),
    ("signature", "stopped_prohibited"),
    ("payment", "stopped_prohibited"),
    ("bind", "stopped_prohibited"),
])
async def test_human_checkpoints(scenario: str, expected_status: str, tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site, scenario=scenario)
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        assert state["workflow_status"] == expected_status
    finally:
        await _stop(env, bs.browser_session_id)


# --- 16. captcha / access-control variants -----------------------------

@pytest.mark.parametrize("scenario", [
    "captcha", "captcha-hcaptcha", "access-denied", "rate-limit", "bot", "login",
])
async def test_access_control_variants_stop(scenario: str, tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site, scenario=scenario)
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        assert state["workflow_status"] == "stopped_access_control"
        assert state["observation_type"] == "access_control_detected"
    finally:
        await _stop(env, bs.browser_session_id)


# --- 17. host safety ---------------------------------------------------

async def test_unexpected_host_start_stops(tmp_path, mock_site) -> None:
    config = build_mock_route_config(MOCK_REGISTRY_ID, start_url="https://evil.example/quote")
    env = make_browser_env(tmp_path, mock_site, route_config=config)
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        assert state["workflow_status"] == "stopped_unexpected_host"
        assert state["observation_type"] == "route_changed"
    finally:
        await _stop(env, bs.browser_session_id)


async def test_session_url_has_no_query_string(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site, scenario="quote")
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        await _run(env, bs.browser_session_id)
        url = env.manager.get(bs.browser_session_id).current_url or ""
        assert "?" not in url
    finally:
        await _stop(env, bs.browser_session_id)


# --- 21. browser crash / context closed --------------------------------

async def test_context_closed_mid_session_returns_technical_error(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site, scenario="applicant")
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        await _run(env, bs.browser_session_id, max_steps=1)  # open + first step
        page = env.manager._pages[bs.browser_session_id]
        await page.close()  # simulate unexpected context/page close
        state = await _run(env, bs.browser_session_id, entry="resume")
        assert state["workflow_status"] == "failed"
        assert state["observation_type"] == "technical_error"
        payload = str(state)
        for marker in SENSITIVE:
            assert marker not in payload
    finally:
        await _stop(env, bs.browser_session_id)


# --- 22. timeout hardening ---------------------------------------------

async def test_page_load_timeout_returns_technical_error(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site, scenario="slow")
    from app.browser.session import BrowserSessionManager

    fast_executor = BrowserExecutor(IntakeValueSource(env.engine), goto_timeout_ms=200)
    env.manager = BrowserSessionManager(
        engine=env.engine, planner=env.planner, registry=env.registry,
        config_loader=env.config_loader, browser=env.browser_manager,
        executor=fast_executor, headless=True,
    )
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        assert state["workflow_status"] == "failed"
        assert state["observation_type"] == "technical_error"
        assert "page load failed" in state.get("message", "")
    finally:
        await _stop(env, bs.browser_session_id)


# --- 23. session lifecycle ---------------------------------------------

async def test_multiple_pause_resume_cycles(tmp_path, mock_site) -> None:
    persona = make_standard_auto_profile(annual_kilometres=None, years_at_current_address=None, tpl_selected_limit=None)
    env = make_browser_env(tmp_path, mock_site, persona=persona, scenario="multi-missing")
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        assert state["workflow_status"] == "paused_needs_field"
        env.engine.submit_answer(env.session_id, ANNUAL_KM, 12000)
        state = await _run(env, bs.browser_session_id, entry="resume")
        assert state["workflow_status"] == "paused_needs_field"
        env.engine.submit_answer(env.session_id, YEARS_ADDR, 4)
        state = await _run(env, bs.browser_session_id, entry="resume")
        assert state["workflow_status"] == "paused_needs_field"
        env.engine.submit_answer(env.session_id, TPL, 2_000_000)
        state = await _run(env, bs.browser_session_id, entry="resume")
        assert state["workflow_status"] == "succeeded"
    finally:
        await _stop(env, bs.browser_session_id)


async def test_resume_after_close_fails_safely(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site)
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        await _run(env, bs.browser_session_id, max_steps=1)
        await env.manager.close(bs.browser_session_id)
        with pytest.raises(BrowserSessionNotFoundError):
            await env.manager.step_session(bs.browser_session_id)
    finally:
        await env.browser_manager.stop()


async def test_abandoned_session_cleanup(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site)
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=10000)
        env.manager._store.save(env.manager.get(bs.browser_session_id).model_copy(update={"updated_at": old}))
        closed = await env.manager.cleanup_abandoned(now=dt.datetime.now(dt.timezone.utc))
        assert closed >= 1
        assert env.manager.get(bs.browser_session_id).status.value == "closed"
    finally:
        await env.browser_manager.stop()


# --- 24. concurrent sessions -------------------------------------------

async def test_two_sessions_isolated(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site)
    # second intake session with a different profile
    session2, _ = env.engine.create_session(InsuranceType.AUTO)
    from route_planner_helpers import complete_starter

    complete_starter(env.engine, session2.session_id)
    env.vault.update(env.engine.get_session(session2.session_id).profile_id,
                     make_standard_auto_profile(annual_kilometres=None))
    env.engine.grant_route_consent(session2.session_id, MOCK_REGISTRY_ID, [], True)

    bs1 = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    bs2 = env.manager.create(session2.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state1 = await _run(env, bs1.browser_session_id)
        state2 = await _run(env, bs2.browser_session_id)
        assert state1["workflow_status"] == "succeeded"
        assert state2["workflow_status"] == "paused_needs_field"  # profile B missing annual_km
        assert bs1.browser_session_id != bs2.browser_session_id
        assert len(env.manager._contexts) == 2  # separate browser contexts
    finally:
        await _stop(env, bs1.browser_session_id)
        await _stop(env, bs2.browser_session_id)


# --- 28/30. quote variants + estimate vs firm --------------------------

@pytest.mark.parametrize("scenario,annual,monthly,firm,ref", [
    ("quote-annual", 1200.0, None, False, True),
    ("quote-monthly", None, 100.0, False, True),
    ("quote-both", 1200.0, 100.0, False, True),
    ("quote-estimate", 900.0, None, False, False),
    ("quote-noref", 1100.0, None, False, False),
])
async def test_quote_variants(scenario: str, annual, monthly, firm: bool, ref: bool, tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site, scenario=scenario)
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        assert state["workflow_status"] == "succeeded"
        assert state["observation_type"] == "quote_detected"
        raw = env.manager.last_result(bs.browser_session_id).observation.quote.raw
        assert raw.annual_amount_parsed == annual
        assert raw.monthly_amount_parsed == monthly
        assert raw.is_firm_quote is firm
        assert raw.reference_present is ref
    finally:
        await _stop(env, bs.browser_session_id)


async def test_estimate_wording_not_firm(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site, scenario="quote-estimate")
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        await _run(env, bs.browser_session_id)
        raw = env.manager.last_result(bs.browser_session_id).observation.quote.raw
        assert raw.is_firm_quote is False
        assert "Estimated premium" in (raw.annual_amount_raw or "")
    finally:
        await _stop(env, bs.browser_session_id)


# --- 31. callback handoff context --------------------------------------

async def test_callback_observation_safe_context(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site, scenario="callback")
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        assert state["observation_type"] == "callback_detected"
        result = env.manager.last_result(bs.browser_session_id)
        assert MOCK_REGISTRY_ID in (result.message or "")
        assert result.observation.url  # source URL/host present
        payload = result.model_dump_json() + str(state)
        for marker in SENSITIVE:
            assert marker not in payload
    finally:
        await _stop(env, bs.browser_session_id)


# --- 32. private quote reference ---------------------------------------

async def test_private_reference_never_exposed(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site, scenario="quote")
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        payload = (
            env.manager.get(bs.browser_session_id).model_dump_json()
            + env.manager.last_result(bs.browser_session_id).model_dump_json()
            + str(state)
        )
        assert "MOCK-8F3K-2026" not in payload
        assert '"reference_present":true' in env.manager.last_result(bs.browser_session_id).model_dump_json()
    finally:
        await _stop(env, bs.browser_session_id)


# --- 34. config-driven site change (mandatory) -------------------------

async def test_config_driven_site_change_no_executor_change(tmp_path, mock_site) -> None:
    # Original config over the standard mock journey.
    env_a = make_browser_env(tmp_path, mock_site, scenario="applicant")
    bs_a = env_a.manager.create(env_a.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        assert (await _run(env_a, bs_a.browser_session_id))["workflow_status"] == "succeeded"
    finally:
        await _stop(env_a, bs_a.browser_session_id)
    # Altered config: new label wording + updated page signature + page-b selector.
    config = build_mock_route_config(MOCK_REGISTRY_ID, start_url=mock_site.url("/page-b?label=1"))
    from app.models.browser.config import PageSignatureSpec

    new_binding = BrowserFieldBinding(
        external_field_id="annual-km",
        match_patterns=[MatchPattern(strategy=MatchStrategy.LABEL_CONTAINS, value="how far do you drive")],
        canonical_path=ANNUAL_KM, fill_strategy=FillStrategy.INTEGER,
        transform=TransformKind.INTEGER_TO_STRING,
    )
    config = config.model_copy(update={
        "field_bindings": [b for b in config.field_bindings if b.external_field_id != "annual-km"] + [new_binding],
        "page_signatures": [s for s in config.page_signatures if s.signature_id != "vehicle"]
                           + [PageSignatureSpec(signature_id="vehicle-2026", url_pattern=r"/page-b",
                                                heading_patterns=["Vehicle Information - Updated"], field_ids=["vin"])],
    })
    env_b = make_browser_env(tmp_path, mock_site, scenario="label-changed", route_config=config)
    bs_b = env_b.manager.create(env_b.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        assert (await _run(env_b, bs_b.browser_session_id))["workflow_status"] == "succeeded"
    finally:
        await _stop(env_b, bs_b.browser_session_id)


# --- 35. second synthetic route ----------------------------------------

async def test_second_synthetic_route_via_config(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site, registry_id="mock-insurer-2", scenario="applicant")
    bs = env.manager.create(env.session_id, "mock-insurer-2", BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        assert state["workflow_status"] == "succeeded"
        assert env.manager.get(bs.browser_session_id).registry_id == "mock-insurer-2"
    finally:
        await _stop(env, bs.browser_session_id)


# --- 37. live-mode safety ----------------------------------------------

async def test_live_accurate_attested_missing_refused(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site)
    gate = LiveExecutionGate(personal_use_confirmed=True, accurate_information_attested=False)
    result = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.LIVE, live_gate=gate)
    assert result.reason.value == "live_gate_required"


async def test_live_automation_prohibited_refused(tmp_path, mock_site) -> None:
    entry_ov = {
        "status": "verified",
        "last_verified_at": "2026-01-01T00:00:00Z",
        "quote_url": "https://example.example/quote",
        "automation_notes": "No automation permitted",
    }
    env = make_browser_env(tmp_path, mock_site, entry_overrides=entry_ov)
    gate = LiveExecutionGate(personal_use_confirmed=True, accurate_information_attested=True)
    result = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.LIVE, live_gate=gate)
    assert result.reason.value == "automation_not_permitted"
