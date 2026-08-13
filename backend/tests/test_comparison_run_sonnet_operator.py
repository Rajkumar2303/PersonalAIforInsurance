"""Sonnet is operator-managed in LIVE comparison runs - no duplicate execution.

Proves that clicking Compare Quotes NEVER creates a Sonnet browser attempt in
live mode, that a Square One-style route still executes, that Sonnet stays
visible in the route ledger as operator-ready, and that starting it via the
direct browser-session path creates exactly ONE Sonnet attempt.

Hermetic: local mock quote site only; no real Sonnet / Square One, no LLM, no
LangSmith.
"""

from __future__ import annotations

import asyncio

import pytest

from app.models.browser.session import LiveExecutionGate
from app.models.comparison_run import RouteRunStatus

from comparison_run_helpers import await_run, make_comparison_run_env

pytestmark = pytest.mark.usefixtures("mock_site")

# Sonnet (operator-managed) + one Square-One-style verified route.
SONNET_PLUS_SQUARE_ONE = [
    ("sonnet", "Sonnet", "sonnet", "RS-SONNET", "direct"),
    ("mock-insurer", "Mock Square One", "quote", "RS-MOCK-INSURER", "direct"),
]


def _sonnet_attempts(env):
    return [a for a in env.recovery.list_attempts() if a.registry_id == "sonnet"]


def _close(env):
    try:
        asyncio.run(env.browser_manager.stop())
    except Exception:
        pass


async def test_live_compare_zero_sonnet_attempts_square_one_runs(tmp_path, mock_site) -> None:
    env = make_comparison_run_env(
        tmp_path, mock_site,
        routes=SONNET_PLUS_SQUARE_ONE,
        registry_ids=["sonnet", "mock-insurer"],
        verified_registry_ids=["sonnet", "mock-insurer"],
    )
    gate = LiveExecutionGate(
        personal_use_confirmed=True,
        accurate_information_attested=True,
        attested_at=None,
    )
    run = env.run_service.start_run(env.session_id, "live", live_gate=gate)
    try:
        run = await await_run(env, run.comparison_run_id)
    finally:
        _close(env)

    statuses = {r.registry_id: r.status for r in run.route_summaries}
    # Sonnet stays VISIBLE and operator-ready, but is NOT executed by Compare.
    assert statuses["sonnet"] == RouteRunStatus.OPERATOR_MANAGED
    sonnet_summary = next(r for r in run.route_summaries if r.registry_id == "sonnet")
    assert sonnet_summary.message == "Ready for controlled live run"
    # Square One-style route still executes through the one-shot path.
    assert statuses["mock-insurer"] in (
        RouteRunStatus.COMPARABLE,
        RouteRunStatus.NON_COMPARABLE,
        RouteRunStatus.QUOTE_PENDING_NORMALIZATION,
        RouteRunStatus.ESTIMATE_ONLY,
    )
    # Compare created ZERO Sonnet browser attempts.
    assert _sonnet_attempts(env) == []


async def test_direct_sonnet_live_start_creates_exactly_one_attempt(tmp_path, mock_site) -> None:
    from pathlib import Path
    from urllib.parse import urlsplit

    from app.browser.config import BrowserRouteConfigLoader
    from app.browser.mock_site import mock_scenario_url
    from app.browser.session import BrowserExecutionMode
    from app.services.recovery.engine import RecoveryEngine
    from browser_helpers import make_browser_env

    routes_dir = Path(__file__).resolve().parents[1] / "data" / "browser" / "routes"
    cfg = BrowserRouteConfigLoader(config_dir=routes_dir).load("sonnet")
    host = urlsplit(mock_site.url("/")).hostname
    cfg = cfg.model_copy(update={"start_url": mock_scenario_url(mock_site, "sonnet"),
                                 "allowed_hosts": [host]})
    recovery = RecoveryEngine()
    env = make_browser_env(
        tmp_path, mock_site, registry_id="sonnet",
        entry_overrides={
            "quote_url": mock_scenario_url(mock_site, "sonnet"),
            "status": "verified",
            "last_verified_at": "2026-08-12T00:00:00+00:00",
        },
        route_config=cfg,
        recovery=recovery,
    )
    gate = LiveExecutionGate(personal_use_confirmed=True, accurate_information_attested=True)
    bs = env.manager.create(env.session_id, "sonnet", BrowserExecutionMode.LIVE, live_gate=gate)
    try:
        r1 = await env.manager.start_session(bs.browser_session_id)
        assert r1.status.value == "paused_human_checkpoint"
        env.manager.approve_checkpoint(bs.browser_session_id, "identity_lookup")
        r2 = await env.manager.step_session(bs.browser_session_id)
        assert r2.status.value in ("running", "succeeded")
    finally:
        try:
            await env.manager.close(bs.browser_session_id)
        except Exception:
            pass
        try:
            await env.browser_manager.stop()
        except Exception:
            pass
    # Run Sonnet Live created exactly ONE Sonnet attempt.
    attempts = [a for a in recovery.list_attempts() if a.registry_id == "sonnet"]
    assert len(attempts) == 1
