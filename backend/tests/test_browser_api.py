"""Issue #7 - browser API tests (minimal safe interface, privacy-checked).

Uses httpx ASGITransport so the browser session manager and the Playwright
browser share the same event loop and can be cleaned up cleanly.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import httpx

import app.api.browser as browser_api
from app.browser.mock_site import MOCK_REGISTRY_ID
from app.main import create_app
from app.models.browser.session import BrowserExecutionMode
from browser_helpers import make_browser_env

MARKERS = ["T0000-00000-00000", "1HGCM82633A000000", "1990-01-01", "123 Test Street"]


@asynccontextmanager
async def _client(env):
    app = create_app()
    app.dependency_overrides[browser_api._manager_dep] = lambda: env.manager
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _start_payload(env, mode: str = "sandbox") -> dict:
    return {
        "intake_session_id": env.session_id,
        "planned_route_id": MOCK_REGISTRY_ID,
        "execution_mode": mode,
    }


async def _stop(env) -> None:
    try:
        await env.browser_manager.stop()
    except Exception:
        pass


async def test_start_session_created(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site)
    async with _client(env) as client:
        resp = await client.post("/api/v1/browser/sessions", json=_start_payload(env))
        assert resp.status_code == 200
        body = resp.json()
        assert body["started"] is True
        assert body["session"]["registry_id"] == MOCK_REGISTRY_ID
        assert body["session"]["status"] == "created"
    await _stop(env)


async def test_start_session_refusal_when_consent_missing(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site, grant_consent=False)
    async with _client(env) as client:
        resp = await client.post("/api/v1/browser/sessions", json=_start_payload(env))
        assert resp.status_code == 200
        body = resp.json()
        assert body["started"] is False
        assert body["refusal"]["reason"] == "consent_missing"
    await _stop(env)


async def test_get_session(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site)
    async with _client(env) as client:
        created = (await client.post("/api/v1/browser/sessions", json=_start_payload(env))).json()
        session_id = created["session"]["browser_session_id"]
        resp = await client.get(f"/api/v1/browser/sessions/{session_id}")
        assert resp.status_code == 200
        assert resp.json()["browser_session_id"] == session_id
        assert (await client.get("/api/v1/browser/sessions/nope")).status_code == 404
    await _stop(env)


async def test_run_and_delete_session(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site)
    async with _client(env) as client:
        created = (await client.post("/api/v1/browser/sessions", json=_start_payload(env))).json()
        session_id = created["session"]["browser_session_id"]
        run = await client.post(f"/api/v1/browser/sessions/{session_id}/run")
        assert run.status_code == 200
        body = run.json()
        assert body["session"]["status"] == "succeeded"
        assert body["session"]["quote_present"] is True
        assert body["step"]["observation_type"] == "quote_detected"

        deleted = await client.delete(f"/api/v1/browser/sessions/{session_id}")
        assert deleted.status_code == 200
        assert deleted.json()["status"] == "closed"
    await _stop(env)


async def test_api_responses_contain_no_applicant_values(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site)
    async with _client(env) as client:
        created = (await client.post("/api/v1/browser/sessions", json=_start_payload(env))).json()
        session_id = created["session"]["browser_session_id"]
        run = (await client.post(f"/api/v1/browser/sessions/{session_id}/run")).json()
        payloads = [str(created), str(run)]
        for payload in payloads:
            for marker in MARKERS:
                assert marker not in payload, f"{marker} leaked into API response"
    await _stop(env)


async def test_approve_checkpoint_endpoint_pauses_before_licence_submission(tmp_path, mock_site) -> None:
    """The real sonnet.json pauses before the licence-submission click; the
    approve-checkpoint endpoint records the participant's explicit approval and
    resume then continues on the same session."""
    from pathlib import Path

    from app.browser.config import BrowserRouteConfigLoader
    from app.browser.mock_site import mock_scenario_url

    routes_dir = Path(__file__).resolve().parents[1] / "data" / "browser" / "routes"
    from urllib.parse import urlsplit

    cfg = BrowserRouteConfigLoader(config_dir=routes_dir).load("sonnet")
    host = urlsplit(mock_site.url("/")).hostname
    cfg = cfg.model_copy(update={"start_url": mock_scenario_url(mock_site, "sonnet"),
                                 "allowed_hosts": [host]})
    env = make_browser_env(
        tmp_path, mock_site, registry_id="sonnet",
        entry_overrides={"quote_url": mock_scenario_url(mock_site, "sonnet")},
        route_config=cfg,
    )
    async with _client(env) as client:
        created = (await client.post("/api/v1/browser/sessions", json={
            "intake_session_id": env.session_id,
            "planned_route_id": "sonnet",
            "execution_mode": "sandbox",
        })).json()
        assert created["started"] is True
        session_id = created["session"]["browser_session_id"]

        # The bounded run stops BEFORE the licence-submission click.
        run = await client.post(f"/api/v1/browser/sessions/{session_id}/run")
        assert run.status_code == 200
        body = run.json()
        assert body["session"]["status"] == "paused_human_checkpoint"
        assert body["step"]["observation_type"] == "human_checkpoint"
        assert body["step"]["observation"]["checkpoint"]["checkpoint_type"] == "identity_lookup"

        # Explicit participant approval of a resumable checkpoint.
        ok = await client.post(f"/api/v1/browser/sessions/{session_id}/approve-checkpoint",
                               json={"checkpoint_type": "identity_lookup"})
        assert ok.status_code == 200
        assert "identity_lookup" in ok.json()["checkpoint_approvals"]

        # A MUST-NOT-AUTOMATE checkpoint can never be approved via the API.
        bad = await client.post(f"/api/v1/browser/sessions/{session_id}/approve-checkpoint",
                                json={"checkpoint_type": "payment"})
        assert bad.status_code == 422

        # Unknown checkpoint kinds are rejected.
        unknown = await client.post(f"/api/v1/browser/sessions/{session_id}/approve-checkpoint",
                                    json={"checkpoint_type": "not-a-kind"})
        assert unknown.status_code == 422

        # Resume on the SAME session continues to the explicit quote result.
        resume = await client.post(f"/api/v1/browser/sessions/{session_id}/resume")
        assert resume.status_code == 200
        resumed = resume.json()
        assert resumed["session"]["browser_session_id"] == session_id
        assert resumed["session"]["status"] == "succeeded"
        assert resumed["step"]["observation_type"] == "quote_detected"
    await _stop(env)
