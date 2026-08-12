"""Issue #13 - comparison-run tests (focused, hermetic).

Covers the key acceptance cases: multi-route E2E with a CAPTCHA failure,
route isolation, bounded concurrency, normalization auto-trigger, aggregator/
duplicate-rate-source, API ownership, and PII safety.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.comparison_run import ComparisonRunStatus, RouteRunStatus
from app.services.comparison_run import get_comparison_run_service

from comparison_run_helpers import (
    DEMO_MULTI_ROUTES,
    await_run,
    make_comparison_run_env,
)
from evidence_helpers import SENSITIVE_MARKERS

pytestmark = pytest.mark.usefixtures("mock_site")


def _start_and_run(env, **kwargs):
    run = env.run_service.start_run(env.session_id, "mock")
    return run


async def _full_run(env, **kwargs):
    run = _start_and_run(env, **kwargs)
    return await await_run(env, run.comparison_run_id)


def _close(env):
    try:
        import asyncio
        asyncio.run(env.browser_manager.stop())
    except Exception:
        pass


# ---------------------------------------------------------------------------
# §29 - Hermetic multi-route E2E (CAPTCHA failure must not stop other routes)
# ---------------------------------------------------------------------------


async def test_multi_route_e2e_captcha_does_not_stop_run(tmp_path, mock_site):
    env = make_comparison_run_env(tmp_path, mock_site)
    try:
        run = await _full_run(env)
        assert run.status in (
            ComparisonRunStatus.COMPLETED,
            ComparisonRunStatus.COMPLETED_WITH_PARTIAL_RESULTS,
        )

        statuses = {r.registry_id: r.status for r in run.route_summaries}
        # Successful routes complete despite the CAPTCHA route.
        assert statuses["mock-insurer"] == RouteRunStatus.COMPARABLE
        assert statuses["mock-provider-b"] == RouteRunStatus.COMPARABLE
        # CAPTCHA route reported (not cancelled, not solving).
        assert statuses["mock-provider-c"] == RouteRunStatus.CAPTCHA_BLOCKED
        # Estimate kept separate.
        assert statuses["mock-provider-d"] == RouteRunStatus.ESTIMATE_ONLY
        # Aggregator duplicate visible, not an independent comparable market.
        assert statuses["mock-insurer-broker"] == RouteRunStatus.DUPLICATE_RATE_SOURCE

        # Evidence recorded + auto-normalized + compared.
        assert run.comparison is not None
        assert all(
            r.normalized_quote_id or r.source_quote_observation_id
            for r in run.route_summaries
            if r.status in (RouteRunStatus.COMPARABLE, RouteRunStatus.ESTIMATE_ONLY)
        )
        # Comparable quotes sorted ascending by annual premium.
        premiums = [q.annual_premium for q in run.comparison.comparable_quotes]
        assert premiums == sorted(premiums)
        # Summary counts: 4 quote/estimate responses, 3 distinct rate sources,
        # 1 duplicate, 1 estimate.
        s = run.comparison.summary
        assert s.quote_results == 4
        assert s.distinct_rate_sources == 3
        assert s.duplicates == 1
        assert s.estimates == 1
    finally:
        _close(env)


# ---------------------------------------------------------------------------
# §30 - Route isolation: a route exception never stops other routes
# ---------------------------------------------------------------------------


async def test_route_isolation_browser_exception(tmp_path, mock_site):
    # Add a route with NO browser config so manager.create raises -> route fails.
    routes = DEMO_MULTI_ROUTES + [("mock-exception", "Mock Boom", "quote", "RS-BOOM", "direct")]
    env = make_comparison_run_env(
        tmp_path, mock_site, routes=routes,
        registry_ids=[r[0] for r in routes] + ["mock-exception"],
        no_config_registry_ids=["mock-exception"],
    )
    try:
        run = await _full_run(env)
        assert run.status == ComparisonRunStatus.COMPLETED_WITH_PARTIAL_RESULTS
        boom = next(r for r in run.route_summaries if r.registry_id == "mock-exception")
        # Missing config -> the browser route fails locally (failed/unresolved),
        # and that failure never stops the other routes.
        assert boom.status in (RouteRunStatus.FAILED, RouteRunStatus.UNRESOLVED)
        # Other routes still produce results.
        assert any(r.status == RouteRunStatus.COMPARABLE for r in run.route_summaries)
        assert any(r.status == RouteRunStatus.ESTIMATE_ONLY for r in run.route_summaries)
    finally:
        _close(env)


# ---------------------------------------------------------------------------
# §31 - Bounded concurrency
# ---------------------------------------------------------------------------


async def test_bounded_concurrency(tmp_path, mock_site):
    env = make_comparison_run_env(tmp_path, mock_site, max_concurrency=1)
    assert env.run_service._max_concurrency == 1
    run = await _full_run(env)
    assert run.status in (
        ComparisonRunStatus.COMPLETED,
        ComparisonRunStatus.COMPLETED_WITH_PARTIAL_RESULTS,
    )
    # Serial execution still completes all routes.
    assert run.completed_routes == run.total_routes


# ---------------------------------------------------------------------------
# §32 - Normalization auto-trigger (no manual normalize calls)
# ---------------------------------------------------------------------------


async def test_normalization_auto_triggered_by_run(tmp_path, mock_site):
    env = make_comparison_run_env(tmp_path, mock_site)
    try:
        run = await _full_run(env)
        quotes = await env.evidence.list_quote_observations(env.session_id)
        assert quotes, "evidence should contain quote observations"
        # Normalized quotes exist (created by the run, not the test).
        normalized = await env.normalization.list_by_intake(env.session_id)
        assert len(normalized) == len(quotes)
        assert run.comparison is not None
        assert run.comparison.comparable_quotes
    finally:
        _close(env)


# ---------------------------------------------------------------------------
# §33 - Aggregator yields multiple results; confirmed duplicate visible
# ---------------------------------------------------------------------------


async def test_aggregator_multiple_results_and_duplicate(tmp_path, mock_site):
    env = make_comparison_run_env(tmp_path, mock_site)
    try:
        run = await _full_run(env)
        s = run.comparison.summary
        # 4 quote/estimate responses (A direct, aggregator A, B, D estimate)
        assert s.quote_results == 4
        # 3 distinct rate sources (A, B, D) - aggregator is NOT independent.
        assert s.distinct_rate_sources == 3
        assert s.duplicates == 1
        dup = next(r for r in run.comparison.duplicates)
        assert dup.distinct_rate_source_id == "RS-MOCK-INSURER"
        # Representative is the direct provider.
        rep = next(r for r in run.comparison.comparable_quotes if r.registry_id == "mock-insurer")
        assert rep.is_representative is True
    finally:
        _close(env)


# ---------------------------------------------------------------------------
# §28 - PII safety
# ---------------------------------------------------------------------------

# Opaque system-generated ids (hex UUIDs, hashes). Random substrings inside
# them (e.g. "1990" inside a run_id) are NOT applicant content, so privacy
# scans must never treat them as leakage - same allowlist principle as
# evidence_helpers._OPAQUE_FIELDS.
_OPAQUE_KEYS = ("content_hash", "idempotency_key")


def _content_only(value, parts=None):
    """Collect content strings from the run dict, EXCLUDING opaque generated
    ids, so a random hex run_id/plan_id substring can never trip the scan."""
    if parts is None:
        parts = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key.endswith("_id") or key in _OPAQUE_KEYS:
                continue
            _content_only(child, parts)
    elif isinstance(value, list):
        for child in value:
            _content_only(child, parts)
    elif value is not None:
        parts.append(str(value))
    return parts


async def test_comparison_run_contains_no_sensitive_markers(tmp_path, mock_site):
    env = make_comparison_run_env(tmp_path, mock_site)
    try:
        run = await _full_run(env)
        content = "\n".join(_content_only(run.model_dump(mode="json")))
        for marker in SENSITIVE_MARKERS:
            assert marker.lower() not in content.lower()
        for route in run.route_summaries:
            assert route.registry_id in run.model_dump_json()
    finally:
        _close(env)


# ---------------------------------------------------------------------------
# API (idempotency + ownership) - §34
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_run_service():
    from app.services.comparison_run import reset_comparison_run_service
    reset_comparison_run_service()
    yield
    reset_comparison_run_service()


async def test_comparison_run_api_idempotent_and_ownership(tmp_path, mock_site, client):
    env = make_comparison_run_env(tmp_path, mock_site)
    try:
        # Idempotency at the service level (the API delegates to it): starting
        # twice reuses the same active run -> no duplicate submissions.
        first = env.run_service.start_run(env.session_id, "mock")
        second = env.run_service.start_run(env.session_id, "mock")
        assert second.comparison_run_id == first.comparison_run_id

        # Ownership boundary: another intake session cannot read the run.
        assert env.run_service.get_run("other-session", first.comparison_run_id) is None
        assert env.run_service.get_run(env.session_id, first.comparison_run_id) is not None
        await await_run(env, first.comparison_run_id)
    finally:
        _close(env)


async def test_comparison_run_api_endpoint_validation(client):
    # Missing intake session -> 422 (no intake_session_id body) is not possible;
    # POST with unknown session -> 404.
    resp = client.post(
        "/api/v1/comparison-runs",
        json={"intake_session_id": "does-not-exist", "execution_mode": "mock"},
    )
    assert resp.status_code == 404
    # Ownership required on GET.
    resp2 = client.get("/api/v1/comparison-runs/some-run")
    assert resp2.status_code == 422
