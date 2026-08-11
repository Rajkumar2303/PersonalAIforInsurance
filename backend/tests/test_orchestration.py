"""Issue #8.5 - comparison orchestration + demo-overlay separation tests.

Covers:
- full mock E2E through the orchestrator (quote received + duplicate source)
- live mode never sees/executes mock routes (real-registry separation)
- regression: demo overlay never pollutes real market counts / dedup
- safe catalog endpoint (catalog-driven, no values)
- mock-only persona endpoint (live refused)
- mode-aware route-disclosure consent (mock overlay vs real registry)
- job payload privacy (no applicant values)
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import httpx

import app.api.orchestrate as orchestrate_api
from app.demo.personas import standard_auto_persona
from app.demo.runtime import DemoRuntime
from app.services.market_registry import get_market_registry_service
from app.services.orchestration import ComparisonOrchestrator
from app.main import create_app
from demo_overlay_helpers import MOCK_ALT, MOCK_PRIMARY, MOCK_RATE_SOURCE, make_demo_env

MARKERS = ["T0000-0000000-0000", "1HGCM82633A000000", "M0A 0A0", "Test Applicant", "1990-01-01", "123 Test Street"]


async def _run_to_done(orchestrator: ComparisonOrchestrator, session_id: str, mode: str, live_gate=None):
    job = orchestrator.start_compare(session_id, mode, live_gate=live_gate)
    for _ in range(200):
        job = orchestrator.get_job(job.job_id)
        if job.status in ("done", "failed"):
            break
        await asyncio.sleep(0.2)
    return job


async def _stop_runtime(runtime: DemoRuntime) -> None:
    try:
        await runtime.shutdown()
    except Exception:
        pass


# --- mode-aware plan endpoint (separation) ------------------------------

async def test_planner_plan_mode_separation(tmp_path, mock_site) -> None:
    _runtime, session_id = make_demo_env(tmp_path, mock_site)
    async with _client() as client:
        mock_plan = (await client.get(f"/api/v1/planner/plan?session_id={session_id}&mode=mock")).json()
        mock_ids = {r["registry_id"] for r in mock_plan["routes"]}
        assert MOCK_PRIMARY in mock_ids
        assert MOCK_ALT in mock_ids

        live_plan = (await client.get(f"/api/v1/planner/plan?session_id={session_id}&mode=live")).json()
        live_ids = {r["registry_id"] for r in live_plan["routes"]}
        assert MOCK_PRIMARY not in live_ids
        assert MOCK_ALT not in live_ids
        # The real registry is large; mock is never injected into it.
        assert len(live_ids) > 10


# --- full mock E2E ------------------------------------------------------

async def test_mock_compare_quote_received(tmp_path, mock_site) -> None:
    runtime, session_id = make_demo_env(tmp_path, mock_site)
    try:
        orchestrator = ComparisonOrchestrator(mock_runtime=runtime)
        job = await _run_to_done(orchestrator, session_id, "mock")
        assert job.status == "done", job.error
        by_id = {r.registry_id: r for r in job.routes}

        primary = by_id[MOCK_PRIMARY]
        assert primary.status == "quote_received"
        assert primary.quote_pending_normalization is True
        assert primary.quote_received is True
        assert primary.annual_amount_parsed == 1234.56
        assert "quote_observed" in primary.reason_codes
        assert primary.attempted is True
        assert primary.terminal_status is None  # never comparable

        alternative = by_id[MOCK_ALT]
        assert alternative.status == "duplicate_rate_source"
        assert alternative.terminal_status == "duplicate_rate_source"
        assert alternative.attempted is False
        assert alternative.annual_amount_parsed is None
    finally:
        await _stop_runtime(runtime)


# --- live separation ----------------------------------------------------

async def test_live_mode_never_sees_mock_routes(tmp_path, mock_site) -> None:
    runtime, session_id = make_demo_env(tmp_path, mock_site)
    try:
        # Default orchestrator = REAL singletons (real registry, real planner).
        orchestrator = ComparisonOrchestrator()
        job = await _run_to_done(orchestrator, session_id, "live")
        assert job.status == "done", job.error
        assert job.execution_mode == "live"
        ids = {r.registry_id for r in job.routes}
        assert MOCK_PRIMARY not in ids
        assert MOCK_ALT not in ids
        # No quote can be produced: no verified route configs exist in live.
        assert all(r.status in ("not_ready", "consent_required", "refused") for r in job.routes)
    finally:
        await _stop_runtime(runtime)


# --- separation regression (explicit user requirement) ------------------

def test_demo_overlay_never_pollutes_real_registry(tmp_path, mock_site) -> None:
    runtime, _session_id = make_demo_env(tmp_path, mock_site)
    try:
        real = get_market_registry_service()
        real_ids = {e.registry_id for e in real.list_markets()}
        assert MOCK_PRIMARY not in real_ids
        assert MOCK_ALT not in real_ids

        demo_ids = {e.registry_id for e in runtime.registry.list_markets()}
        assert MOCK_PRIMARY in demo_ids
        assert MOCK_ALT in demo_ids

        # Rate source count on the REAL registry is unaffected by mock overlay.
        real_sources = {e.distinct_rate_source_id for e in real.list_markets() if e.distinct_rate_source_id}
        assert MOCK_RATE_SOURCE not in real_sources

        # A live planner over the real registry never plans mock routes.
        from app.services.route_planner import get_route_planner

        plan = get_route_planner().plan(_session_id)
        assert all(r.registry_id != MOCK_PRIMARY and r.registry_id != MOCK_ALT for r in plan.routes)
    finally:
        import asyncio

        asyncio.run(_stop_runtime(runtime))


# --- safe catalog endpoint ----------------------------------------------

@asynccontextmanager
async def _client():
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_catalog_endpoint_is_safe_and_data_driven() -> None:
    async with _client() as client:
        resp = await client.get("/api/v1/intake/catalog?product=auto")
        assert resp.status_code == 200
        fields = resp.json()
        paths = {f["canonical_path"] for f in fields}
        assert "applicant.identity.legal_name" in paths
        assert "product_data.vehicles[0].risk.winter_tires" in paths
        assert "product_data.vehicles[0].use.carpool" in paths
        assert "product_data.vehicles[0].use.annual_kilometres" in paths
        # Only safe metadata - never values.
        assert all(
            set(f.keys()) == {
                "field_id", "canonical_path", "question", "short_label", "input_type",
                "collection_group", "intake_phase", "sensitivity", "choices", "priority",
                "seed_required", "item_unit", "item_unit_required",
                "household_attestation_required", "help_text",
            }
            for f in fields
        )
        for f in fields:
            serialized = json.dumps(f)
            assert not any(marker in serialized for marker in MARKERS)


# --- mock-only persona endpoint -----------------------------------------

async def test_persona_endpoint_mock_only() -> None:
    async with _client() as client:
        ok = await client.get("/api/v1/demo/personas/standard-auto?mode=mock")
        assert ok.status_code == 200
        persona = ok.json()
        assert persona["applicant.identity.legal_name"] == "Test Applicant"
        assert persona["product_data.vehicles[0].use.annual_kilometres"] == 12000

        refused = await client.get("/api/v1/demo/personas/standard-auto?mode=live")
        assert refused.status_code == 403
        # The persona is the canonical backend source (reusable by tests).
        assert standard_auto_persona() == persona


# --- mode-aware route disclosure consent --------------------------------

async def test_route_consent_mode_param(tmp_path, mock_site) -> None:
    runtime, session_id = make_demo_env(tmp_path, mock_site, grant_consent=False)
    try:
        # Demo engine can disclose + consent mock routes.
        disclosure = runtime.intake.create_route_disclosure(session_id, MOCK_PRIMARY)
        assert disclosure.registry_id == MOCK_PRIMARY
        consent = runtime.intake.grant_route_consent(session_id, MOCK_PRIMARY, [], True)
        assert consent.granted is True
        # The real engine still cannot resolve the mock route (live registry).
        from app.services.intake.engine import RouteNotFoundError
        from app.services.intake import get_intake_engine

        try:
            get_intake_engine().grant_route_consent(session_id, MOCK_PRIMARY, [], True)
            raise AssertionError("real engine must not know the mock route")
        except RouteNotFoundError:
            pass
    finally:
        await _stop_runtime(runtime)


async def test_route_consent_api_mode_param(tmp_path, mock_site) -> None:
    _runtime, session_id = make_demo_env(tmp_path, mock_site, grant_consent=False)
    async with _client() as client:
        mock_ok = await client.post(
            f"/api/v1/intake/sessions/{session_id}/consent/route?mode=mock",
            json={"registry_id": MOCK_PRIMARY, "paths": [], "granted": True},
        )
        assert mock_ok.status_code == 200
        assert mock_ok.json()["granted"] is True

        live_refused = await client.post(
            f"/api/v1/intake/sessions/{session_id}/consent/route?mode=live",
            json={"registry_id": MOCK_PRIMARY, "paths": [], "granted": True},
        )
        assert live_refused.status_code == 404


# --- compare API end-to-end (polling) -----------------------------------

@asynccontextmanager
async def _client_with_orchestrator(runtime: DemoRuntime):
    app = create_app()
    orchestrator = ComparisonOrchestrator(mock_runtime=runtime)
    app.dependency_overrides[orchestrate_api._orchestrator_dep] = lambda: orchestrator
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, orchestrator


async def test_compare_api_polling_returns_quote(tmp_path, mock_site) -> None:
    runtime, session_id = make_demo_env(tmp_path, mock_site)
    try:
        async with _client_with_orchestrator(runtime) as (client, orchestrator):
            started = await client.post(
                "/api/v1/orchestrate/compare",
                json={"intake_session_id": session_id, "execution_mode": "mock"},
            )
            assert started.status_code == 200
            job_id = started.json()["job_id"]

            job = None
            for _ in range(200):
                resp = await client.get(f"/api/v1/orchestrate/jobs/{job_id}")
                assert resp.status_code == 200
                job = resp.json()
                if job["status"] in ("done", "failed"):
                    break
                await asyncio.sleep(0.2)

            assert job["status"] == "done", job.get("error")
            by_id = {r["registry_id"]: r for r in job["routes"]}
            assert by_id[MOCK_PRIMARY]["status"] == "quote_received"
            assert by_id[MOCK_PRIMARY]["quote_pending_normalization"] is True
            assert by_id[MOCK_PRIMARY]["annual_amount_parsed"] == 1234.56
            assert by_id[MOCK_ALT]["status"] == "duplicate_rate_source"

            # Polling-only contract: job never carries applicant values.
            serialized = json.dumps(job)
            assert not any(marker in serialized for marker in MARKERS)
    finally:
        await _stop_runtime(runtime)


# --- job payload privacy -------------------------------------------------

async def test_job_payload_privacy(tmp_path, mock_site) -> None:
    runtime, session_id = make_demo_env(tmp_path, mock_site)
    try:
        orchestrator = ComparisonOrchestrator(mock_runtime=runtime)
        job = await _run_to_done(orchestrator, session_id, "mock")
        serialized = json.dumps(job.model_dump(mode="json"))
        assert not any(marker in serialized for marker in MARKERS)
        # Plan rows expose canonical paths + public market data only.
        plan_paths = [b for row in job.plan for b in row.blockers]
        assert isinstance(plan_paths, list)
    finally:
        await _stop_runtime(runtime)
