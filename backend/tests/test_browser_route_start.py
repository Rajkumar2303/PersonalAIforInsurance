"""Issue #7 - route-start validation & structured refusals."""

from __future__ import annotations

import pytest

from app.browser.mock_site import MOCK_REGISTRY_ID
from app.browser.session import BrowserSessionManager
from app.models.browser.session import (
    BrowserExecutionMode,
    BrowserRefusalReason,
    BrowserSession,
    BrowserStartRefusal,
    LiveExecutionGate,
)
from app.models.insurance.enums import InsuranceType
from browser_helpers import make_browser_env

ANNUAL_KM = "product_data.vehicles[0].use.annual_kilometres"


def _live_gate() -> LiveExecutionGate:
    import datetime as dt

    return LiveExecutionGate(personal_use_confirmed=True, accurate_information_attested=True,
                             attested_at=dt.datetime.now(dt.timezone.utc))


def test_non_auto_session_refused(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site)
    home, _ = env.engine.create_session(InsuranceType.HOME)
    result = env.manager.create(home.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    assert isinstance(result, BrowserStartRefusal)
    assert result.reason is BrowserRefusalReason.NOT_AUTO


def test_non_web_channel_refused(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site, entry_overrides={"quote_url": None})
    result = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    assert isinstance(result, BrowserStartRefusal)
    assert result.reason is BrowserRefusalReason.NON_WEB_CHANNEL


def test_route_not_found_refused(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site)
    result = env.manager.create(env.session_id, "does-not-exist", BrowserExecutionMode.SANDBOX)
    assert isinstance(result, BrowserStartRefusal)
    assert result.reason is BrowserRefusalReason.ROUTE_NOT_FOUND


def test_route_not_ready_refused(tmp_path, mock_site) -> None:
    from personas import make_standard_auto_profile

    env = make_browser_env(
        tmp_path,
        mock_site,
        per_route_reqs={MOCK_REGISTRY_ID: [ANNUAL_KM]},
        persona=make_standard_auto_profile(annual_kilometres=None),
    )
    result = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    assert isinstance(result, BrowserStartRefusal)
    assert result.reason is BrowserRefusalReason.ROUTE_NOT_READY
    assert "missing_field" in result.detail


def test_route_excluded_refused(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site, grant_consent=False)
    env.engine.grant_route_consent(env.session_id, MOCK_REGISTRY_ID, [], False)
    result = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    assert isinstance(result, BrowserStartRefusal)
    assert result.reason is BrowserRefusalReason.ROUTE_EXCLUDED


def test_consent_missing_refused(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site, grant_consent=False)
    result = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    assert isinstance(result, BrowserStartRefusal)
    assert result.reason is BrowserRefusalReason.CONSENT_MISSING


def test_live_gate_required(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site)
    result = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.LIVE, live_gate=None)
    assert isinstance(result, BrowserStartRefusal)
    assert result.reason is BrowserRefusalReason.LIVE_GATE_REQUIRED


def test_live_no_verified_route(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site)
    result = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.LIVE, live_gate=_live_gate())
    assert isinstance(result, BrowserStartRefusal)
    assert result.reason is BrowserRefusalReason.NO_VERIFIED_ROUTE


def test_sandbox_session_created(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site)
    result = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    assert isinstance(result, BrowserSession)
    assert result.status.value == "created"
    assert result.planned_route_id == MOCK_REGISTRY_ID
    assert result.registry_id == MOCK_REGISTRY_ID
    assert result.execution_mode is BrowserExecutionMode.SANDBOX
    assert result.profile_id is not None


def test_unknown_intake_session_refused(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site)
    result = env.manager.create("nope", MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    assert isinstance(result, BrowserStartRefusal)
    assert result.reason is BrowserRefusalReason.UNKNOWN_SESSION


def test_planned_route_mapping_is_centralized(tmp_path, mock_site) -> None:
    from app.browser.route_identity import planned_route_id_for_registry, registry_id_for_planned_route

    assert registry_id_for_planned_route("mock-insurer") == "mock-insurer"
    assert planned_route_id_for_registry("mock-insurer") == "mock-insurer"
