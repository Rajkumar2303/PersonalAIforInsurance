"""Issue #11 - normalized-quotes API tests: read endpoints, normalize, boundary.

The API is read-only except the explicit deterministic ``/normalize`` action.
Tests seed the process-wide evidence service singleton directly, then query the
endpoints through the TestClient. Every endpoint requires ``intake_session_id``
(ownership boundary) and returns safe view models.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.services.evidence import get_evidence_service
from app.services.normalization import get_quote_normalization_service

from normalization_helpers import make_browser_quote

PLAN = "plan-1"
ROUTE = "mock-insurer"
ATTEMPT = "att-100"


@pytest.fixture(autouse=True)
def _reset_singletons():
    get_evidence_service.cache_clear()
    get_quote_normalization_service.cache_clear()
    yield
    get_evidence_service.cache_clear()
    get_quote_normalization_service.cache_clear()


def _seed_quote(coverage: list[str] | None = None, discounts: list[str] | None = None) -> str:
    """Record a browser quote observation and return its quote id."""
    from app.services.evidence.ingest import quote_from_browser_observation

    import asyncio

    svc = get_evidence_service()
    obs = make_browser_quote(
        coverage=coverage or ["Third Party Liability - $2,000,000"],
        discounts=discounts or ["Winter Tire Discount"],
    )
    quote = quote_from_browser_observation(
        "intake-1",
        obs,
        plan_id=PLAN,
        planned_route_id=ROUTE,
        registry_id="mock-insurer",
        distinct_rate_source_id="RS-MOCK-INSURER",
        attempt_id=ATTEMPT,
    )
    stored = asyncio.run(svc.record_quote_observation("intake-1", quote))
    return stored.quote_id


def _normalize(client: TestClient, quote_id: str):
    return client.post(
        "/api/v1/normalized-quotes/normalize",
        params={"intake_session_id": "intake-1"},
        json={"source_quote_observation_id": quote_id},
    )


def test_normalize_endpoint(client: TestClient) -> None:
    quote_id = _seed_quote()
    resp = _normalize(client, quote_id)
    assert resp.status_code == 200
    body = resp.json()
    assert body["source_quote_observation_id"] == quote_id
    assert body["normalization_status"] == "normalized"
    assert body["premium"]["normalized_annual_amount"] == "1234.56"
    keys = {item["item_key"] for item in body["coverage_ledger"]["items"]}
    assert "third_party_liability" in keys
    assert "winter_tires_discount" in keys


def test_normalize_missing_source_404(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/normalized-quotes/normalize",
        params={"intake_session_id": "intake-1"},
        json={"source_quote_observation_id": "nope"},
    )
    assert resp.status_code == 404


def test_get_single_normalized_quote(client: TestClient) -> None:
    quote_id = _seed_quote()
    nq_id = _normalize(client, quote_id).json()["normalized_quote_id"]
    resp = client.get(
        f"/api/v1/normalized-quotes/{nq_id}?intake_session_id=intake-1"
    )
    assert resp.status_code == 200
    assert resp.json()["normalized_quote_id"] == nq_id
    # Cross-session access is denied (ownership boundary).
    missing = client.get(
        f"/api/v1/normalized-quotes/{nq_id}?intake_session_id=other-session"
    )
    assert missing.status_code == 404


def test_list_by_plan_and_route(client: TestClient) -> None:
    _normalize(client, _seed_quote())
    plan = client.get(f"/api/v1/normalized-quotes/plans/{PLAN}?intake_session_id=intake-1")
    assert plan.status_code == 200
    assert len(plan.json()) == 1
    route = client.get(f"/api/v1/normalized-quotes/routes/{ROUTE}?intake_session_id=intake-1")
    assert route.status_code == 200
    assert len(route.json()) == 1
    attempt = client.get(f"/api/v1/normalized-quotes/attempts/{ATTEMPT}?intake_session_id=intake-1")
    assert attempt.status_code == 200
    assert len(attempt.json()) == 1


def test_export_endpoint(client: TestClient) -> None:
    _normalize(client, _seed_quote())
    resp = client.get("/api/v1/normalized-quotes/export?intake_session_id=intake-1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["normalized_quote_count"] == 1
    assert body["normalization_rule_version"] == "1"
    assert body["quotes"][0]["normalization_status"] == "normalized"


def test_intake_session_id_required(client: TestClient) -> None:
    resp = client.get("/api/v1/normalized-quotes/export")
    assert resp.status_code == 422


def test_normalize_is_idempotent_via_api(client: TestClient) -> None:
    quote_id = _seed_quote()
    payload = {"source_quote_observation_id": quote_id}
    first = client.post(
        "/api/v1/normalized-quotes/normalize",
        params={"intake_session_id": "intake-1"},
        json=payload,
    ).json()
    second = client.post(
        "/api/v1/normalized-quotes/normalize",
        params={"intake_session_id": "intake-1"},
        json=payload,
    ).json()
    assert first["normalized_quote_id"] == second["normalized_quote_id"]
