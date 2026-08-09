"""Tests for the read-only market registry API endpoints (Issue #3 Part B)."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient


def test_list_markets(client: TestClient) -> None:
    response = client.get("/api/v1/markets")
    assert response.status_code == 200
    records = response.json()
    assert isinstance(records, list)
    assert len(records) > 0
    for record in records:
        assert record["registry_id"]
        assert record["product_type"] == "auto"


def test_list_markets_filter_by_distribution_type(client: TestClient) -> None:
    response = client.get("/api/v1/markets", params={"distribution_type": "direct"})
    assert response.status_code == 200
    records = response.json()
    assert records
    assert all(r["distribution_type"] == "direct" for r in records)


def test_list_markets_filter_by_product_scope(client: TestClient) -> None:
    response = client.get("/api/v1/markets", params={"product_scope": "collector"})
    assert response.status_code == 200
    records = response.json()
    assert records
    assert all(r["product_scope"] == "collector" for r in records)


def test_list_markets_filter_by_product_type(client: TestClient) -> None:
    response = client.get("/api/v1/markets", params={"product_type": "auto"})
    assert response.status_code == 200
    assert all(r["product_type"] == "auto" for r in response.json())


def test_get_market_by_registry_id(client: TestClient) -> None:
    response = client.get("/api/v1/markets/sonnet")
    assert response.status_code == 200
    body = response.json()
    assert body["registry_id"] == "sonnet"
    assert body["brand_or_program"] == "Sonnet"


def test_get_market_unknown_returns_404(client: TestClient) -> None:
    response = client.get("/api/v1/markets/does-not-exist")
    assert response.status_code == 404


def test_markets_response_has_no_applicant_pii(client: TestClient) -> None:
    response = client.get("/api/v1/markets")
    payload = json.dumps(response.json())
    for forbidden in ("@", "T0000-0000000-0000", "1HGCM82633A000000", "416-555", "M0A 0A0"):
        assert forbidden not in payload
