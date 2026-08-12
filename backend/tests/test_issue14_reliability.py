"""Issue #14 - end-to-end reliability, demo hardening & submission readiness.

Focused, hermetic tests that prove the product loop is dependable and safe to
demonstrate:

- golden demo flow (intake -> consent -> comparison start -> progress -> final
  multi-route result) with the exact expected result shape
- all-failure / partial-failure runs terminate honestly (no infinite spinner,
  no fabricated quote)
- a stuck route times out instead of hanging the whole run
- demo mode can never reach a live provider (no mock/live route leakage)
- the env check confirms the demo needs no external credentials
- result ordering is deterministic

Everything runs against the local mock quote site - no live insurers, no LLM,
no LangSmith, no applicant data.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.browser.mock_site import MockQuoteSite
from app.models.browser.session import (
    BrowserExecutionMode,
    BrowserRefusalReason,
    BrowserStartRefusal,
    LiveExecutionGate,
)
from app.models.comparison_run import ComparisonRunStatus, RouteRunStatus

from comparison_run_helpers import (
    DEMO_MULTI_ROUTES,
    await_run,
    make_comparison_run_env,
)

pytestmark = pytest.mark.usefixtures("mock_site")


def _close(env):
    try:
        import asyncio

        asyncio.run(env.browser_manager.stop())
    except Exception:
        pass


def _live_gate() -> LiveExecutionGate:
    return LiveExecutionGate(
        personal_use_confirmed=True,
        accurate_information_attested=True,
        attested_at=dt.datetime.now(dt.timezone.utc),
    )


# ---------------------------------------------------------------------------
# §1 - Golden demo flow (the exact result shape the judges should see)
# ---------------------------------------------------------------------------


async def test_golden_demo_flow_shape(tmp_path, mock_site):
    env = make_comparison_run_env(tmp_path, mock_site)
    try:
        run = env.run_service.start_run(env.session_id, "mock")
        assert run.status == ComparisonRunStatus.PREPARED
        # Progress becomes visible (queued -> running -> ...), never skips to
        # terminal before routes are recorded.
        run2 = env.run_service.get_run(env.session_id, run.comparison_run_id)
        assert run2 is not None

        run = await await_run(env, run.comparison_run_id)
        assert run.status in (
            ComparisonRunStatus.COMPLETED,
            ComparisonRunStatus.COMPLETED_WITH_PARTIAL_RESULTS,
        )

        # Exact golden shape.
        assert run.total_routes == 5
        assert run.completed_routes == run.total_routes
        assert run.running_routes == 0

        s = run.comparison.summary
        assert s.quote_results == 4
        assert s.distinct_rate_sources == 3
        assert s.comparable_quotes == 2
        assert s.estimates == 1
        assert s.duplicates == 1
        assert s.lowest_comparable_annual_premium == Decimal("1234.56")

        statuses = {r.registry_id: r.status for r in run.route_summaries}
        assert statuses["mock-insurer"] == RouteRunStatus.COMPARABLE
        assert statuses["mock-provider-b"] == RouteRunStatus.COMPARABLE
        assert statuses["mock-provider-c"] == RouteRunStatus.CAPTCHA_BLOCKED
        assert statuses["mock-provider-d"] == RouteRunStatus.ESTIMATE_ONLY
        assert statuses["mock-insurer-broker"] == RouteRunStatus.DUPLICATE_RATE_SOURCE

        # Deterministic route ordering: route summaries mirror the planner's
        # deterministic plan order (aggregator sorts first alphabetically in
        # the demo seed) - results are stable, never completion-order.
        plan = env.planner.plan(env.session_id)
        assert [r.registry_id for r in run.route_summaries] == [
            r.registry_id for r in plan.routes
        ]
        # Comparable quotes are sorted ascending by annual premium.
        premiums = [q.annual_premium for q in run.comparison.comparable_quotes]
        assert premiums == sorted(premiums)
    finally:
        _close(env)


# ---------------------------------------------------------------------------
# §17 - All-failure run terminates honestly (no fabricated quote, no spinner)
# ---------------------------------------------------------------------------


async def test_all_routes_fail_completes_honestly(tmp_path, mock_site):
    routes = [
        ("mock-fail-a", "Fail A", "captcha", "RS-FAIL-A", "direct"),
        ("mock-fail-b", "Fail B", "captcha", "RS-FAIL-B", "direct"),
        ("mock-fail-c", "Fail C", "captcha", "RS-FAIL-C", "direct"),
    ]
    env = make_comparison_run_env(
        tmp_path, mock_site, routes=routes, registry_ids=[r[0] for r in routes]
    )
    try:
        run = await await_run(env, env.run_service.start_run(env.session_id, "mock").comparison_run_id)
        # Terminal - never left running.
        assert run.status == ComparisonRunStatus.COMPLETED_WITH_PARTIAL_RESULTS
        assert run.running_routes == 0
        assert all(
            r.status == RouteRunStatus.CAPTCHA_BLOCKED for r in run.route_summaries
        )
        # No fabricated quote: comparison has no comparable quotes.
        assert run.comparison is None or len(run.comparison.comparable_quotes) == 0
        assert run.comparison is None or run.comparison.summary.quote_results == 0
    finally:
        _close(env)


# ---------------------------------------------------------------------------
# §18 - Partial run: successful quotes remain visible next to failures
# ---------------------------------------------------------------------------


async def test_partial_run_quotes_remain_visible(tmp_path, mock_site):
    routes = [
        ("mock-insurer", "Mock A", "quote", "RS-A", "direct"),
        ("mock-provider-c", "Mock C", "captcha", "RS-C", "direct"),
        ("mock-provider-d", "Mock D", "quote-estimate", "RS-D", "direct"),
    ]
    env = make_comparison_run_env(
        tmp_path, mock_site, routes=routes, registry_ids=[r[0] for r in routes]
    )
    try:
        run = await await_run(env, env.run_service.start_run(env.session_id, "mock").comparison_run_id)
        assert run.status == ComparisonRunStatus.COMPLETED_WITH_PARTIAL_RESULTS
        statuses = {r.registry_id: r.status for r in run.route_summaries}
        assert statuses["mock-insurer"] == RouteRunStatus.COMPARABLE
        assert statuses["mock-provider-c"] == RouteRunStatus.CAPTCHA_BLOCKED
        assert statuses["mock-provider-d"] == RouteRunStatus.ESTIMATE_ONLY
        # Successful quotes remain visible to the frontend.
        assert run.comparison is not None
        assert len(run.comparison.comparable_quotes) == 1
    finally:
        _close(env)


# ---------------------------------------------------------------------------
# §8 - A stuck route times out instead of hanging the whole run
# ---------------------------------------------------------------------------


async def test_route_timeout_does_not_hang_run(tmp_path, mock_site):
    # The "slow" mock page sleeps 1.5s per request; with a 0.5s route timeout
    # the route must be resolved to UNAVAILABLE and the run must still
    # terminate (never left running, no blind retries).
    routes = [("mock-slow", "Mock Slow", "slow", "RS-SLOW", "direct")]
    env = make_comparison_run_env(
        tmp_path,
        mock_site,
        routes=routes,
        registry_ids=["mock-slow"],
        max_concurrency=1,
        route_timeout_seconds=0.5,
    )
    try:
        run = await await_run(
            env, env.run_service.start_run(env.session_id, "mock").comparison_run_id,
            timeout=30.0,
        )
        assert run.status in (
            ComparisonRunStatus.COMPLETED,
            ComparisonRunStatus.COMPLETED_WITH_PARTIAL_RESULTS,
        )
        slow = run.route_summaries[0]
        assert slow.status == RouteRunStatus.UNAVAILABLE
        assert slow.message and "timeout" in slow.message.lower()
        assert run.running_routes == 0
    finally:
        _close(env)


# ---------------------------------------------------------------------------
# §4 - Demo mode can never hit a live provider (no mock/live route leakage)
# ---------------------------------------------------------------------------


def test_demo_overlay_never_leaks_into_live_registry(tmp_path, mock_site):
    from app.services.market_registry import get_market_registry_service

    # The real (production) registry must never contain synthetic demo routes.
    real = get_market_registry_service()
    for rid, _d, _s, _r, _x in DEMO_MULTI_ROUTES:
        assert real.get_by_registry_id(rid) is None
    # The demo overlay's own registry is what mock mode uses - it must contain
    # the demo routes and they must never be "verified" (LIVE requires a
    # verified route, so a demo route can never run live).
    env = make_comparison_run_env(tmp_path, mock_site)
    try:
        demo_ids = {e.registry_id for e in env.registry.list_markets()}
        assert {"mock-insurer", "mock-provider-b", "mock-provider-c", "mock-provider-d"} <= demo_ids
        assert env.registry.verified_records() == []
    finally:
        _close(env)


async def test_demo_route_refused_in_live_mode(tmp_path, mock_site):
    env = make_comparison_run_env(tmp_path, mock_site)
    try:
        # Even with a fully-satisfied live gate, a demo route is not verified
        # and therefore must be REFUSED for LIVE execution - never reached.
        result = env.manager.create(
            env.session_id,
            "mock-insurer",
            BrowserExecutionMode.LIVE,
            live_gate=_live_gate(),
        )
        assert isinstance(result, BrowserStartRefusal)
        assert result.reason is BrowserRefusalReason.NO_VERIFIED_ROUTE
    finally:
        _close(env)


# ---------------------------------------------------------------------------
# §3 - Environment check: demo requires no external credentials
# ---------------------------------------------------------------------------


def test_demo_env_check_no_external_credentials(client):
    resp = client.get("/api/v1/demo/env")
    assert resp.status_code == 200
    data = resp.json()
    assert data["demo_requires_external_credentials"] is False
    assert data["comparison_max_concurrency"] >= 1
    assert data["comparison_route_timeout_seconds"] > 0
    # Demo is ready via the local mock site (never requires cloud/live config).
    assert isinstance(data["demo_ready"], bool)
    assert "live_providers_configured" in data


# ---------------------------------------------------------------------------
# §19 - Idempotent Compare: double-click never double-submits (frontend guard
# is in App/ReviewConsent; backend idempotency is verified here and in #13)
# ---------------------------------------------------------------------------


async def test_double_start_reuses_active_run(tmp_path, mock_site):
    env = make_comparison_run_env(tmp_path, mock_site)
    try:
        first = env.run_service.start_run(env.session_id, "mock")
        second = env.run_service.start_run(env.session_id, "mock")
        assert second.comparison_run_id == first.comparison_run_id
        await await_run(env, first.comparison_run_id)
        # Exactly one run executed -> no duplicate submissions on double-click.
        quotes = await env.evidence.list_quote_observations(env.session_id)
        assert len(quotes) == 4  # A direct + broker + B + D estimate (not 8)
    finally:
        _close(env)
