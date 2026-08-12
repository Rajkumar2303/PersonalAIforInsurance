"""Issue #8 - recovery API tests (safe metadata only)."""

from __future__ import annotations

import pytest

from app.api.recovery import _engine_dep
from app.services.recovery.engine import RecoveryEngine


@pytest.fixture()
def recovery_client(client):
    """TestClient with a fresh in-memory recovery engine injected."""
    engine = RecoveryEngine()
    client.app.dependency_overrides[_engine_dep] = lambda: engine
    yield client
    client.app.dependency_overrides.clear()


def _quote_payload(**overrides):
    payload = {
        "plan_id": "plan-1",
        "planned_route_id": "route-a",
        "registry_id": "route-a",
        "distinct_rate_source_id": "RS-A",
        "observation_type": "quote_detected",
        "safe_context": {"quote_present": True, "is_firm_quote": True, "reference_present": True},
    }
    payload.update(overrides)
    return payload


def test_decide_quote(recovery_client):
    resp = recovery_client.post("/api/v1/recovery/decisions", json=_quote_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert body["lifecycle_status"] == "terminal"
    assert body["recommended_action"] == "stop_terminal"
    assert body["terminal_status"] is None
    assert body["quote_pending_normalization"] is True
    assert body["reason_codes"] == ["quote_observed"]
    assert body["attempt_id"]


def test_decide_pause(recovery_client):
    resp = recovery_client.post(
        "/api/v1/recovery/decisions",
        json={"plan_id": "plan-1", "planned_route_id": "route-a", "registry_id": "route-a",
              "distinct_rate_source_id": "RS-A", "observation_type": "needs_field",
              "safe_context": {"missing_field_paths": ["a"]}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["lifecycle_status"] == "paused"
    assert body["recommended_action"] == "resume_after_user_input"
    assert body["terminal_status"] is None


def test_decide_invalid_missing_route(recovery_client):
    resp = recovery_client.post("/api/v1/recovery/decisions", json={"observation_type": "needs_field"})
    assert resp.status_code == 422


def test_get_attempt(recovery_client):
    created = recovery_client.post("/api/v1/recovery/decisions", json=_quote_payload()).json()
    resp = recovery_client.get(f"/api/v1/attempts/{created['attempt_id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["attempt_id"] == created["attempt_id"]
    assert body["lifecycle_status"] == "terminal"


def test_get_attempt_not_found(recovery_client):
    resp = recovery_client.get("/api/v1/attempts/nope")
    assert resp.status_code == 404


def test_plan_attempts(recovery_client):
    recovery_client.post(
        "/api/v1/recovery/decisions",
        json=_quote_payload(plan_id="plan-A", planned_route_id="route-a", registry_id="route-a"),
    )
    recovery_client.post(
        "/api/v1/recovery/decisions",
        json=_quote_payload(
            plan_id="plan-A", planned_route_id="route-b", registry_id="route-b",
            distinct_rate_source_id="RS-B",
            safe_context={"quote_present": True, "is_firm_quote": False, "estimate_only": True},
        ),
    )
    resp = recovery_client.get("/api/v1/route-plans/plan-A/attempts")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert all(a["plan_id"] == "plan-A" for a in body)


def test_api_response_no_pii(recovery_client):
    resp = recovery_client.post(
        "/api/v1/recovery/decisions",
        json=_quote_payload(safe_context={"quote_present": True, "is_firm_quote": True,
                                          "reference_present": True, "private_reference_handle": "abc123"}),
    )
    text = resp.text
    for marker in ("T0000-00000-00000", "1HGCM82633A000000", "1990-01-01", "123 Test Street"):
        assert marker not in text


def test_api_same_request_twice_is_idempotent(recovery_client):
    payload = _quote_payload()
    r1 = recovery_client.post("/api/v1/recovery/decisions", json=payload).json()
    r2 = recovery_client.post("/api/v1/recovery/decisions", json=payload).json()
    assert r2["attempt_id"] == r1["attempt_id"]  # same attempt, no duplicate
    assert r2["attempts_used"] == r1["attempts_used"]  # no double budget
    attempts = recovery_client.get("/api/v1/route-plans/plan-1/attempts").json()
    assert len(attempts) == 1


def test_api_error_safety(recovery_client):
    # Unknown attempt -> 404.
    assert recovery_client.get("/api/v1/attempts/nope").status_code == 404
    # Malformed observation -> 422.
    assert recovery_client.post("/api/v1/recovery/decisions", json={"observation_type": "needs_field"}).status_code == 422
    # Unknown plan -> 200 empty list (plans are not pre-registered).
    assert recovery_client.get("/api/v1/route-plans/does-not-exist/attempts").status_code == 200
    # Terminal reprocessing -> 200, idempotent.
    r1 = recovery_client.post("/api/v1/recovery/decisions", json=_quote_payload()).json()
    assert recovery_client.post("/api/v1/recovery/decisions", json=_quote_payload()).status_code == 200


def test_api_sanitizes_raw_payload_context(recovery_client):
    resp = recovery_client.post(
        "/api/v1/recovery/decisions",
        json=_quote_payload(safe_context={"quote_present": True, "is_firm_quote": True,
                                          "raw_payload": {"licence": "T0000-00000-00000"}}),
    )
    body = resp.json()
    assert "raw_payload" not in str(body)
    assert "T0000-00000-00000" not in str(body)
    assert "quote_present" in str(body)  # safe allowlisted keys preserved


def test_api_unknown_policy_version_is_safe_string(recovery_client):
    # policy version is free-form provenance, never a lookup - must be safe.
    payload = _quote_payload()
    payload["policy"] = {"version": "v-unknown-1", "max_attempts_per_route": 2}
    resp = recovery_client.post("/api/v1/recovery/decisions", json=payload)
    assert resp.status_code == 200
    assert resp.json()["policy_version"] == "v-unknown-1"
