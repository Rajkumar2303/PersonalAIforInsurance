"""Issue #10 - evidence API tests: read endpoints, ownership boundary, export.

The API is read-only (evidence WRITE is explicit via EvidenceService adapters).
Tests seed the process-wide service singleton directly, then query the
endpoints through the TestClient.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.models.evidence import AuditEventName, EvidenceEventType
from app.services.evidence import get_evidence_service

from evidence_helpers import page_observation, quote_observation

PLAN = "plan-1"
ROUTE = "mock-insurer"
REG = "mock-insurer"
DRS = "RS-MOCK-INSURER"
ATTEMPT = "att-100"


@pytest.fixture(autouse=True)
def _reset_evidence_service():
    get_evidence_service.cache_clear()
    yield
    get_evidence_service.cache_clear()


async def _seed() -> str:
    svc = get_evidence_service()
    await svc.record_route_planned("intake-1", _route_plan())
    await svc.record_attempt(
        "intake-1", event_type=EvidenceEventType.ATTEMPT_STARTED, channel="browser",
        plan_id=PLAN, planned_route_id=ROUTE, registry_id=REG,
        distinct_rate_source_id=DRS, attempt_id=ATTEMPT,
    )
    record = await svc.record_browser_observation(
        "intake-1", page_observation(), browser_session_id="bs-1",
        plan_id=PLAN, planned_route_id=ROUTE, registry_id=REG,
        distinct_rate_source_id=DRS, attempt_id=ATTEMPT,
    )
    await svc.record_browser_quote(
        "intake-1", quote_observation(), plan_id=PLAN, planned_route_id=ROUTE,
        registry_id=REG, distinct_rate_source_id=DRS, attempt_id=ATTEMPT,
    )
    await svc.record_audit_event(
        "intake-1", event_name=AuditEventName.ATTEMPT_STARTED, actor="system"
    )
    return record.evidence_id


def _route_plan():
    import datetime as dt

    from app.models.route_planner import (
        InsuranceType,
        PlannedRoute,
        RoutePlan,
        RoutePlanSummary,
    )

    return RoutePlan(
        session_id=PLAN,
        insurance_type=InsuranceType.AUTO,
        routes=[
            PlannedRoute(
                registry_id=REG, brand_or_program="Mock", distribution_type="direct",
                product_scope="standard_PPA", deduplication_status="primary",
                route_status="ready", is_ready=True, rank=1,
            )
        ],
        summary=RoutePlanSummary(),
        generated_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
    )


def test_evidence_routes_are_registered(client: TestClient) -> None:
    import asyncio

    asyncio.run(_seed())
    assert client.get(f"/api/v1/evidence/export?intake_session_id=intake-1").status_code == 200


def test_list_by_plan(client: TestClient) -> None:
    import asyncio

    asyncio.run(_seed())
    resp = client.get(f"/api/v1/evidence/plans/{PLAN}?intake_session_id=intake-1")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list) and len(body) >= 1
    assert all(rec["plan_id"] == PLAN for rec in body)
    # API view is safe: no raw fields.
    raw = resp.text
    assert "Q-2026" not in raw


def test_list_by_route(client: TestClient) -> None:
    import asyncio

    asyncio.run(_seed())
    resp = client.get(f"/api/v1/evidence/routes/{ROUTE}?intake_session_id=intake-1")
    assert resp.status_code == 200
    assert all(rec["planned_route_id"] == ROUTE for rec in resp.json())


def test_list_by_attempt(client: TestClient) -> None:
    import asyncio

    asyncio.run(_seed())
    resp = client.get(f"/api/v1/evidence/attempts/{ATTEMPT}?intake_session_id=intake-1")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) >= 2
    assert all(rec["attempt_id"] == ATTEMPT for rec in body)
    # Monotonic sequences in the timeline.
    seqs = [rec["sequence"] for rec in body]
    assert seqs == sorted(seqs)


def test_get_single_evidence(client: TestClient) -> None:
    import asyncio

    eid = asyncio.run(_seed())
    resp = client.get(f"/api/v1/evidence/{eid}?intake_session_id=intake-1")
    assert resp.status_code == 200
    assert resp.json()["evidence_id"] == eid
    assert resp.json()["payload_kind"] in {"page_observation", "barrier", "safe_metadata"}
    # Unknown id -> 404.
    missing = client.get("/api/v1/evidence/nope?intake_session_id=intake-1")
    assert missing.status_code == 404


def test_attempt_quotes_endpoint(client: TestClient) -> None:
    import asyncio

    asyncio.run(_seed())
    resp = client.get(f"/api/v1/evidence/attempts/{ATTEMPT}/quotes?intake_session_id=intake-1")
    assert resp.status_code == 200
    quotes = resp.json()
    assert len(quotes) == 1
    assert quotes[0]["annual_premium"] == "1234.56"
    assert quotes[0]["firm_vs_estimate"] == "firm"


def test_audit_endpoint(client: TestClient) -> None:
    import asyncio

    asyncio.run(_seed())
    resp = client.get("/api/v1/evidence/audit?intake_session_id=intake-1")
    assert resp.status_code == 200
    body = resp.json()
    assert any(e["event_name"] == "attempt_started" for e in body)


def test_export_endpoint(client: TestClient) -> None:
    import asyncio

    asyncio.run(_seed())
    resp = client.get("/api/v1/evidence/export?intake_session_id=intake-1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["intake_session_id"] == "intake-1"
    assert body["quote_count"] == 1
    assert PLAN in body["distinct_plans"]
    assert ATTEMPT in body["distinct_attempts"]
    # The API keeps even the opaque reference handle internal (view omits it).
    assert "opaque-ref-hash" not in resp.text
    # reference_present is exposed as a boolean only.
    assert body["quotes"][0]["reference_present"] is True
    assert "Q-2026" not in resp.text  # raw reference never exported


def test_ownership_boundary_blocks_other_sessions(client: TestClient) -> None:
    import asyncio

    eid = asyncio.run(_seed())
    # Same evidence_id but wrong intake_session_id -> 404.
    resp = client.get(f"/api/v1/evidence/{eid}?intake_session_id=other-session")
    assert resp.status_code == 404
    assert client.get(f"/api/v1/evidence/plans/{PLAN}?intake_session_id=other-session").json() == []
    assert client.get(f"/api/v1/evidence/attempts/{ATTEMPT}?intake_session_id=other-session").json() == []
    exp = client.get("/api/v1/evidence/export?intake_session_id=other-session").json()
    assert exp["evidence_count"] == 0 and exp["quote_count"] == 0


def test_missing_intake_session_query_is_rejected(client: TestClient) -> None:
    assert client.get(f"/api/v1/evidence/plans/{PLAN}").status_code == 422


def test_evidence_health_endpoint(client: TestClient) -> None:
    resp = client.get("/api/v1/evidence/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["evidence_status"] in {"durable", "persistence_failed", "disabled"}
    assert body["evidence_backend"] in {"in_memory", "postgres", "disabled"}
