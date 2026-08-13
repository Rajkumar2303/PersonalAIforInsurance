"""Tests for the read-only rate-source deduplication API endpoints (Issue #4)."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient


def test_list_rate_sources(client: TestClient) -> None:
    response = client.get("/api/v1/rate-sources")
    assert response.status_code == 200
    # Two verified rate sources: Square One (Zurich) and Sonnet. We pin the
    # exact records instead of loosening to "non-empty" so a silent regression
    # (e.g. a guessed source) cannot slip through unnoticed.
    records = response.json()
    assert len(records) == 2
    by_id = {r["distinct_rate_source_id"]: r for r in records}
    zurich = by_id["RS-ZURICH-AUTO"]
    assert zurich["insurer_group"] == "Zurich"
    assert "square-one" in zurich["related_registry_ids"]
    sonnet = by_id["RS-SONNET-AUTO"]
    assert sonnet["insurer_group"] == "Sonnet"
    assert "sonnet" in sonnet["related_registry_ids"]


def test_get_rate_source_unknown_returns_404(client: TestClient) -> None:
    assert client.get("/api/v1/rate-sources/nope").status_code == 404


def test_duplicate_candidates(client: TestClient) -> None:
    # aviva-direct and rbc-insurance share the Aviva group -> candidate surfaced.
    response = client.get("/api/v1/markets/aviva-direct/duplicates")
    assert response.status_code == 200
    candidates = response.json()
    assert any(c["registry_id"] == "rbc-insurance" for c in candidates)


def test_duplicate_candidates_unknown_returns_404(client: TestClient) -> None:
    assert client.get("/api/v1/markets/nope/duplicates").status_code == 404


def test_dedup_metrics_honest_for_real_seed(client: TestClient) -> None:
    response = client.get("/api/v1/dedup/metrics")
    assert response.status_code == 200
    body = response.json()
    assert body["raw_route_count"] == 31
    # Square One (Zurich) + Sonnet are the two verified rate sources.
    assert body["confirmed_rate_sources"] == 2
    assert body["confirmed_duplicates"] == 0
    # square-one and sonnet map to distinct rate sources so they are no longer
    # unresolved (all other routes remain unverified -> unresolved).
    assert body["unresolved_mappings"] == 29


def test_dedup_responses_have_no_applicant_pii(client: TestClient) -> None:
    metrics = json.dumps(client.get("/api/v1/dedup/metrics").json())
    candidates = json.dumps(client.get("/api/v1/markets/aviva-direct/duplicates").json())
    for payload in (metrics, candidates):
        for forbidden in ("@", "T0000-00000-00000", "1HGCM82633A000000", "416-555", "M0A 0A0"):
            assert forbidden not in payload
