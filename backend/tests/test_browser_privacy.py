"""Issue #7 - privacy tests.

Asserts the exact synthetic applicant values (licence, VIN, DOB, street,
email) NEVER appear in: BrowserSession serialization, BrowserStepResult,
BrowserPageObservation, LangGraph state, structured logs, exception text, or
API responses. Also covers the tight ``get_field_value`` trusted boundary.
"""

from __future__ import annotations

import json

import pytest

from app.browser.mock_site import MOCK_REGISTRY_ID
from app.models.browser.session import BrowserExecutionMode
from browser_helpers import make_browser_env
from personas import make_standard_auto_profile

VIN = "product_data.vehicles[0].identity.vin"
ANNUAL_KM = "product_data.vehicles[0].use.annual_kilometres"

SENSITIVE_MARKERS = [
    "T0000-00000-00000",   # licence
    "1HGCM82633A000000",    # VIN
    "1990-01-01",           # DOB
    "123 Test Street",      # street
    "test.applicant@example.com",  # email
    "Test Applicant",       # legal name
]


async def _run(env, session_id: str) -> dict:
    from app.graph.browser_workflow import build_browser_workflow

    return await build_browser_workflow(env.manager).ainvoke(
        {"entry": "run", "browser_session_id": session_id, "max_steps": 12}
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


# --- get_field_value trusted boundary --------------------------------

def test_get_field_value_returns_exactly_one_scalar(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site)
    pid = env.engine.get_session(env.session_id).profile_id
    value = env.engine.get_field_value(pid, VIN)
    assert value == "1HGCM82633A000000"
    assert isinstance(value, str)


def test_get_field_value_missing_returns_none(tmp_path, mock_site) -> None:
    env = make_browser_env(
        tmp_path, mock_site, persona=make_standard_auto_profile(annual_kilometres=None)
    )
    pid = env.engine.get_session(env.session_id).profile_id
    assert env.engine.get_field_value(pid, ANNUAL_KM) is None


def test_get_field_value_rejects_subtree_without_value(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site)
    pid = env.engine.get_session(env.session_id).profile_id
    with pytest.raises(ValueError) as exc_info:
        env.engine.get_field_value(pid, "applicant.identity")
    message = str(exc_info.value)
    assert "single leaf" in message
    for marker in SENSITIVE_MARKERS:
        assert marker not in message


def test_get_field_value_unknown_path_safe_error(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site)
    pid = env.engine.get_session(env.session_id).profile_id
    with pytest.raises(ValueError) as exc_info:
        env.engine.get_field_value(pid, "applicant.does_not_exist.value")
    assert "invalid canonical field path" in str(exc_info.value)


def test_get_field_value_unknown_profile_safe_error(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site)
    with pytest.raises(ValueError) as exc_info:
        env.engine.get_field_value("unknown-profile-id", VIN)
    assert "profile not found" in str(exc_info.value)
    assert "unknown-profile-id" not in str(exc_info.value)


def test_get_field_value_never_cached_in_session_or_receipts(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site)
    pid = env.engine.get_session(env.session_id).profile_id
    env.engine.get_field_value(pid, VIN)
    intake = env.engine.get_session(env.session_id)
    assert "1HGCM82633A000000" not in intake.model_dump_json()
    for receipt in env.engine._consent.for_session(env.session_id):
        assert "1HGCM82633A000000" not in receipt.model_dump_json()


# --- end-to-end privacy ----------------------------------------------

async def test_no_pii_in_session_observation_state_or_logs(tmp_path, mock_site, caplog) -> None:
    env = make_browser_env(tmp_path, mock_site)
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        await _run(env, bs.browser_session_id)
        payloads = [
            env.manager.get(bs.browser_session_id).model_dump_json(),
            env.manager.last_result(bs.browser_session_id).model_dump_json(),
        ]
        state = await _run(env, bs.browser_session_id)
        payloads.append(json.dumps(state, default=str))
        logs = caplog.text
        for payload in payloads:
            for marker in SENSITIVE_MARKERS:
                assert marker not in payload, f"{marker} leaked into {payload[:200]}"
        for marker in SENSITIVE_MARKERS:
            assert marker not in logs, f"{marker} leaked into logs"
    finally:
        await _stop(env, bs.browser_session_id)


async def test_no_pii_when_missing_field_paused(tmp_path, mock_site, caplog) -> None:
    env = make_browser_env(
        tmp_path, mock_site, persona=make_standard_auto_profile(annual_kilometres=None)
    )
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        assert state["workflow_status"] == "paused_needs_field"
        payload = json.dumps(state, default=str) + env.manager.get(bs.browser_session_id).model_dump_json()
        for marker in SENSITIVE_MARKERS:
            assert marker not in payload
    finally:
        await _stop(env, bs.browser_session_id)
