"""Tests for the read-only route planner API (Issue #6).

Uses the real app (real registry seed, real data-driven requirements, real
intake engine) - hermetic, no network. Verifies the plan is safe (paths only),
product-aware, and that missing-field requests integrate with Issue #5.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient


def _create_auto_session(client: TestClient) -> str:
    response = client.post("/api/v1/intake/sessions", json={"insurance_type": "auto"})
    assert response.status_code == 200
    return response.json()["session"]["session_id"]


def test_plan_endpoint_returns_safe_plan(client: TestClient) -> None:
    session_id = _create_auto_session(client)
    response = client.get(f"/api/v1/planner/plan?session_id={session_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["insurance_type"] == "auto"
    assert body["session_id"] == session_id
    assert body["summary"]["planned_route_count"] > 0
    text = json.dumps(body)
    for marker in ("T0000-00000-00000", "1HGCM82633A000000", "M0A 0A0", "Test Applicant"):
        assert marker not in text


def test_plan_endpoint_unknown_session_404(client: TestClient) -> None:
    assert client.get("/api/v1/planner/plan?session_id=nope").status_code == 404


def test_plan_endpoint_non_auto_is_not_applicable(client: TestClient) -> None:
    session = client.post("/api/v1/intake/sessions", json={"insurance_type": "home"}).json()["session"]
    response = client.get(f"/api/v1/planner/plan?session_id={session['session_id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["insurance_type"] == "home"
    assert body["routes"] == []


def test_request_missing_fields_endpoint(client: TestClient) -> None:
    session_id = _create_auto_session(client)
    response = client.post(f"/api/v1/planner/plan/{session_id}/request-missing")
    assert response.status_code == 200
    outcomes = response.json()
    assert isinstance(outcomes, list)
    # at least the default required paths are requested (legal_name etc.)
    assert outcomes  # non-empty list of FieldRequestOutcomes


def test_request_missing_fields_unknown_session_404(client: TestClient) -> None:
    assert client.post("/api/v1/planner/plan/nope/request-missing").status_code == 404
