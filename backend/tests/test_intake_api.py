"""Tests for the intake API endpoints (Issue #5, section 24).

Uses the real app (real AUTO field catalog, in-memory vault) - hermetic, no
network. Verifies safe responses, product gate, progressive intake via the
LangGraph-backed endpoints, consent, disclosure, and deletion.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

SYNTHETIC_LEGAL_NAME = "Test Applicant"
SYNTHETIC_POSTAL = "M0A 0A0"


def _create_auto(client: TestClient) -> dict:
    response = client.post("/api/v1/intake/sessions", json={"insurance_type": "auto"})
    assert response.status_code == 200
    body = response.json()
    assert body["gate"]["status"] == "started"
    return body["session"]


def test_create_auto_session(client: TestClient) -> None:
    session = _create_auto(client)
    assert session["insurance_type"] == "auto"
    assert session["status"] == "active"


def test_create_unsupported_home_session(client: TestClient) -> None:
    response = client.post("/api/v1/intake/sessions", json={"insurance_type": "home"})
    assert response.status_code == 200
    body = response.json()
    assert body["gate"]["status"] == "product_not_implemented"
    assert body["gate"]["is_supported"] is False
    assert body["session"]["status"] == "product_rejected"


def test_unsupported_session_does_not_ask_auto_fields(client: TestClient) -> None:
    response = client.post("/api/v1/intake/sessions", json={"insurance_type": "home"})
    session_id = response.json()["session"]["session_id"]
    question = client.get(f"/api/v1/intake/sessions/{session_id}/next-question")
    assert question.status_code == 200
    assert question.json()["workflow_status"] == "product_rejected"
    assert question.json()["question"] is None


def test_get_session_and_next_question(client: TestClient) -> None:
    session = _create_auto(client)
    got = client.get(f"/api/v1/intake/sessions/{session['session_id']}")
    assert got.status_code == 200
    assert got.json()["session_id"] == session["session_id"]
    question = client.get(f"/api/v1/intake/sessions/{session['session_id']}/next-question")
    assert question.status_code == 200
    payload = question.json()
    assert payload["question"]["field_id"] == "legal_name"
    # question payload never includes other profile values
    text = json.dumps(payload)
    assert "M0A 0A0" not in text


def test_submit_answer_flow(client: TestClient) -> None:
    session = _create_auto(client)
    sid = session["session_id"]
    first = client.post(
        f"/api/v1/intake/sessions/{sid}/answers",
        json={"canonical_path": "applicant.identity.legal_name", "value": SYNTHETIC_LEGAL_NAME},
    )
    assert first.status_code == 200
    assert first.json()["validation_success"] is True
    assert first.json()["next_question"]["field_id"] == "postal_code"
    second = client.post(
        f"/api/v1/intake/sessions/{sid}/answers",
        json={"canonical_path": "applicant.address.postal_code", "value": SYNTHETIC_POSTAL},
    )
    assert second.status_code == 200
    assert second.json()["validation_success"] is True
    assert second.json()["next_question"]["field_id"] == "driver_name_on_licence"


def test_invalid_answer_returns_safe_retry(client: TestClient) -> None:
    session = _create_auto(client)
    sid = session["session_id"]
    response = client.post(
        f"/api/v1/intake/sessions/{sid}/answers",
        json={"canonical_path": "applicant.identity.legal_name", "value": ""},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["validation_success"] is False
    assert body["retry_eligible"] is True
    assert "Test Applicant" not in json.dumps(body)


def test_request_fields_external(client: TestClient) -> None:
    session = _create_auto(client)
    sid = session["session_id"]
    client.post(
        f"/api/v1/intake/sessions/{sid}/answers",
        json={"canonical_path": "applicant.identity.legal_name", "value": SYNTHETIC_LEGAL_NAME},
    )
    client.post(
        f"/api/v1/intake/sessions/{sid}/answers",
        json={"canonical_path": "applicant.address.postal_code", "value": SYNTHETIC_POSTAL},
    )
    outcome = client.post(
        f"/api/v1/intake/sessions/{sid}/request-fields",
        json={"requested_paths": ["applicant.address.years_at_current_address"], "source_context": "browser"},
    )
    assert outcome.status_code == 200
    assert outcome.json()[0]["state"] == "requested"
    # answer the discovered field
    answer = client.post(
        f"/api/v1/intake/sessions/{sid}/answers",
        json={"canonical_path": "applicant.address.years_at_current_address", "value": 7},
    )
    assert answer.status_code == 200
    assert answer.json()["validation_success"] is True
    # reuse: no re-ask
    reuse = client.post(
        f"/api/v1/intake/sessions/{sid}/request-fields",
        json={"requested_paths": ["applicant.address.years_at_current_address"], "source_context": "aviva"},
    )
    assert reuse.json()[0]["already_known"] is True


def test_unknown_field_returns_unsupported(client: TestClient) -> None:
    session = _create_auto(client)
    outcome = client.post(
        f"/api/v1/intake/sessions/{session['session_id']}/request-fields",
        json={"requested_paths": ["applicant.address.mystery_field"], "source_context": "broker"},
    )
    assert outcome.status_code == 200
    assert outcome.json()[0]["state"] == "unsupported"


def test_profile_summary_masked(client: TestClient) -> None:
    session = _create_auto(client)
    sid = session["session_id"]
    client.post(
        f"/api/v1/intake/sessions/{sid}/answers",
        json={"canonical_path": "applicant.identity.legal_name", "value": SYNTHETIC_LEGAL_NAME},
    )
    client.post(
        f"/api/v1/intake/sessions/{sid}/answers",
        json={"canonical_path": "applicant.address.postal_code", "value": SYNTHETIC_POSTAL},
    )
    summary = client.get(f"/api/v1/intake/sessions/{sid}/profile-summary")
    assert summary.status_code == 200
    text = json.dumps(summary.json())
    assert "Test Applicant" not in text
    assert "M0A 0A0" not in text


def test_route_disclosure_and_consent(client: TestClient) -> None:
    session = _create_auto(client)
    sid = session["session_id"]
    client.post(
        f"/api/v1/intake/sessions/{sid}/answers",
        json={"canonical_path": "applicant.identity.legal_name", "value": SYNTHETIC_LEGAL_NAME},
    )
    client.post(
        f"/api/v1/intake/sessions/{sid}/answers",
        json={"canonical_path": "applicant.address.postal_code", "value": SYNTHETIC_POSTAL},
    )
    disclosure = client.post(
        f"/api/v1/intake/sessions/{sid}/route-disclosure", json={"registry_id": "td-insurance"}
    )
    assert disclosure.status_code == 200
    body = disclosure.json()
    assert body["registry_id"] == "td-insurance"
    assert "Test Applicant" not in json.dumps(body)
    paths = [item["canonical_path"] for item in body["items"]]
    assert "applicant.identity.legal_name" in paths
    decision = client.post(
        f"/api/v1/intake/sessions/{sid}/consent/route",
        json={"registry_id": "td-insurance", "paths": paths, "granted": True},
    )
    assert decision.status_code == 200
    assert decision.json()["granted"] is True
    assert decision.json()["excluded"] is False


def test_household_driver_consent(client: TestClient) -> None:
    session = _create_auto(client)
    receipt = client.post(
        f"/api/v1/intake/sessions/{session['session_id']}/consent",
        json={"scope": "household_driver", "driver_label": "driver_1"},
    )
    assert receipt.status_code == 200
    assert receipt.json()["scope"] == "household_driver"
    assert receipt.json()["granted"] is True


def test_delete_session(client: TestClient) -> None:
    session = _create_auto(client)
    deleted = client.delete(f"/api/v1/intake/sessions/{session['session_id']}")
    assert deleted.status_code == 200
    assert deleted.json()["status"] == "deleted"
    got = client.get(f"/api/v1/intake/sessions/{session['session_id']}")
    assert got.status_code == 404


def test_unknown_session_404(client: TestClient) -> None:
    assert client.get("/api/v1/intake/sessions/nope").status_code == 404
    assert client.post("/api/v1/intake/sessions/nope/answers", json={"canonical_path": "x", "value": 1}).status_code == 404
