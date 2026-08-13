"""Hermetic test for the narrow normalized-quote endpoint.

``GET /api/v1/browser/sessions/{browser_session_id}/quote`` reuses the existing
extraction + evidence + normalization pipeline and returns only the safe
frontend projection. The mock quote page here carries an explicit premium AND
coverage list (limits + deductibles + discount + validity) so normalization is
proven. Synthetic data only (local mock quote site).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
import pytest

import app.api.browser as browser_api
from app.browser.mock_site import MOCK_REGISTRY_ID
from app.browser.session import BrowserExecutionMode
from app.main import create_app
from app.services.normalization.repository import InMemoryNormalizationRepository
from app.services.normalization.service import QuoteNormalizationService

from browser_helpers import make_browser_env
from evidence_helpers import SENSITIVE_MARKERS, make_sink_env


@asynccontextmanager
async def _client(env, evidence_service, normalization_service):
    app = create_app()
    app.dependency_overrides[browser_api._manager_dep] = lambda: env.manager
    app.dependency_overrides[browser_api.get_evidence_service] = lambda: evidence_service
    app.dependency_overrides[browser_api.get_quote_normalization_service] = lambda: normalization_service
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _stop(env, sid):
    try:
        await env.manager.close(sid)
    except Exception:
        pass
    try:
        await env.browser_manager.stop()
    except Exception:
        pass


@pytest.fixture()
async def quote_env(tmp_path, mock_site):
    ev_env, sink = make_sink_env()
    env = make_browser_env(tmp_path, mock_site, evidence_sink=sink)
    normalization = QuoteNormalizationService(ev_env.service, InMemoryNormalizationRepository())
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    # Drive the generic mock flow (applicant -> vehicle -> commute -> quote with
    # coverage) until the explicit quote result.
    from app.graph.browser_workflow import build_browser_workflow

    await build_browser_workflow(env.manager).ainvoke(
        {"entry": "run", "browser_session_id": bs.browser_session_id, "max_steps": 15}
    )
    last = env.manager.last_result(bs.browser_session_id)
    assert last.observation_type.value == "quote_detected"
    yield env, ev_env, normalization, bs
    await _stop(env, bs.browser_session_id)


async def test_quote_endpoint_returns_normalized_result(quote_env) -> None:
    env, ev_env, normalization, bs = quote_env
    async with _client(env, ev_env.service, normalization) as client:
        resp = await client.get(f"/api/v1/browser/sessions/{bs.browser_session_id}/quote")
        assert resp.status_code == 200
        body = resp.json()
        # provider + attempt correlation
        assert body["registry_id"] == MOCK_REGISTRY_ID
        assert body["attempt_id"] == bs.attempt_id
        # explicitly observed premium + frequency (annual $1,234.56 mock quote)
        assert float(body["premium"]["normalized_annual_amount"]) == 1234.56
        assert body["premium"]["currency"] == "CAD"
        # coverage limits / deductibles normalized (not inferred)
        items = body["coverage_ledger"]["items"]
        assert items, "coverage should be normalized into ledger items"
        keys = {i["item_key"] for i in items}
        assert any("third_party_liability" in k for k in keys)
        # timestamp present
        assert body["normalized_at"]
        # redacted reference: only the opaque normalized id is exposed, never
        # the raw mock reference
        assert "MOCK-8F3K-2026" not in resp.text
        # no applicant values anywhere in the response
        for marker in SENSITIVE_MARKERS:
            assert marker not in resp.text


async def test_quote_endpoint_unknown_session_and_no_quote(quote_env) -> None:
    env, ev_env, normalization, bs = quote_env
    async with _client(env, ev_env.service, normalization) as client:
        # Different / unknown browser session cannot retrieve the quote.
        other = await client.get("/api/v1/browser/sessions/does-not-exist/quote")
        assert other.status_code == 404
        # A session with no quote yet returns not-found (not-ready).
        fresh = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
        try:
            no_quote = await client.get(f"/api/v1/browser/sessions/{fresh.browser_session_id}/quote")
            assert no_quote.status_code == 404
        finally:
            await _stop(env, fresh.browser_session_id)


async def test_quote_endpoint_never_proceeds_to_purchase(quote_env) -> None:
    """The endpoint is read-only: it returns a normalized quote view only and
    cannot trigger payment/purchase/binding (no such controls exist here)."""
    env, ev_env, normalization, bs = quote_env
    async with _client(env, ev_env.service, normalization) as client:
        body = (await client.get(f"/api/v1/browser/sessions/{bs.browser_session_id}/quote")).json()
        assert "payment" not in body
        assert "purchase" not in body
        assert "binding" not in body
        # Only GET is exposed - POST/approve are separate endpoints.
        assert (await client.post(f"/api/v1/browser/sessions/{bs.browser_session_id}/quote")).status_code == 405
