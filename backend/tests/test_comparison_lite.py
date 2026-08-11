"""Issue #12 - lite comparability tests (focused, deterministic).

Covers the required acceptance cases A-K plus the aggregator scenario (three
distinct rate sources from four quote responses) and the partial-coverage demo
(never rank an insufficient-coverage quote as cheapest).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.models.comparison import (
    ComparisonReasonCode,
    ComparisonStatus,
    RequestedCoverage,
)
from app.models.normalization import CoverageItemKey, NormalizationStatus
from app.services.comparison import QuoteComparisonService
from app.services.evidence import get_evidence_service
from app.services.normalization import get_quote_normalization_service

from comparison_helpers import make_quote
from evidence_helpers import SENSITIVE_MARKERS

SVC = QuoteComparisonService()


def _evaluate(quotes, requested=None, **kwargs):
    return SVC.evaluate(quotes, requested_coverage=requested, intake_session_id="intake-1", **kwargs)


# --- A: fully known matching coverage -> comparable -------------------------


def test_fully_known_coverage_is_comparable():
    quote = make_quote(normalized_quote_id="nq-a")
    result = _evaluate([quote])
    assert result.comparable_quotes[0].comparison_status == ComparisonStatus.COMPARABLE
    assert result.comparable_quotes[0].route_outcome_semantics == "quoted_comparable"
    assert result.comparable_quotes[0].coverage_completeness.value == "complete"
    assert result.comparable_quotes[0].annual_premium == Decimal("2000.00")


def test_fully_known_matching_requested_is_comparable():
    quote = make_quote(normalized_quote_id="nq-a")
    requested = RequestedCoverage(
        third_party_liability_limit=2000000,
        collision_deductible=Decimal("1000"),
        comprehensive_deductible=Decimal("500"),
    )
    result = _evaluate([quote], requested=requested)
    assert result.comparable_quotes[0].comparison_status == ComparisonStatus.COMPARABLE


# --- B: missing liability -> insufficient coverage ---------------------------


def test_missing_liability_is_insufficient():
    quote = make_quote(normalized_quote_id="nq-b", tpl=None)
    result = _evaluate([quote])
    r = result.results[0]
    assert r.comparison_status == ComparisonStatus.INSUFFICIENT_COVERAGE_INFORMATION
    assert r.route_outcome_semantics == "quoted_non_comparable"
    assert ComparisonReasonCode.MISSING_LIABILITY_LIMIT in r.reason_codes
    assert "third_party_liability" in r.missing_coverage_keys
    assert not result.comparable_quotes


# --- C: unknown collision deductible -> insufficient -------------------------


def test_unknown_collision_deductible_is_insufficient():
    quote = make_quote(
        normalized_quote_id="nq-c", unknown_keys=frozenset({CoverageItemKey.COLLISION})
    )
    result = _evaluate([quote])
    r = result.results[0]
    assert r.comparison_status == ComparisonStatus.INSUFFICIENT_COVERAGE_INFORMATION
    assert ComparisonReasonCode.UNKNOWN_COVERAGE_PRESERVED in r.reason_codes
    assert ComparisonReasonCode.MISSING_COLLISION_DEDUCTIBLE in r.reason_codes
    assert "collision" in r.missing_coverage_keys


# --- D: coverage mismatch -> non-comparable + reason -------------------------


def test_liability_limit_mismatch_is_non_comparable():
    quote = make_quote(normalized_quote_id="nq-d", tpl=Decimal("1000000"))
    requested = RequestedCoverage(third_party_liability_limit=2000000)
    result = _evaluate([quote], requested=requested)
    r = result.results[0]
    assert r.comparison_status == ComparisonStatus.COVERAGE_MISMATCH
    assert r.route_outcome_semantics == "quoted_non_comparable"
    assert ComparisonReasonCode.LIABILITY_LIMIT_MISMATCH in r.reason_codes


def test_collision_deductible_mismatch_reason():
    quote = make_quote(normalized_quote_id="nq-d2", collision=Decimal("500"))
    requested = RequestedCoverage(collision_deductible=Decimal("1000"))
    result = _evaluate([quote], requested=requested)
    assert ComparisonReasonCode.COLLISION_DEDUCTIBLE_MISMATCH in result.results[0].reason_codes


def test_comprehensive_deductible_mismatch_reason():
    quote = make_quote(normalized_quote_id="nq-d3", comprehensive=Decimal("1000"))
    requested = RequestedCoverage(comprehensive_deductible=Decimal("500"))
    result = _evaluate([quote], requested=requested)
    assert ComparisonReasonCode.COMPREHENSIVE_DEDUCTIBLE_MISMATCH in result.results[0].reason_codes


# --- E: estimate -> estimate_only, kept out of comparable --------------------


def test_estimate_is_estimate_only_and_not_ranked():
    quote = make_quote(normalized_quote_id="nq-e", firm_vs_estimate="estimate")
    result = _evaluate([quote])
    assert result.estimates[0].comparison_status == ComparisonStatus.ESTIMATE_ONLY
    assert result.estimates[0].route_outcome_semantics == "estimate_only"
    assert ComparisonReasonCode.ESTIMATE_ONLY in result.estimates[0].reason_codes
    assert result.comparable_quotes == []
    assert result.summary.estimates == 1


# --- F: confirmed same distinct_rate_source_id -> duplicate handling ---------


def test_confirmed_duplicate_rate_source_demoted_to_duplicate():
    direct = make_quote(
        normalized_quote_id="nq-f1", presented_carrier="Carrier A", annual_premium=Decimal("2100.00")
    )
    aggregator = make_quote(
        normalized_quote_id="nq-f2",
        presented_carrier="Carrier A (Aggregator)",
        annual_premium=Decimal("2100.00"),
        aggregator_registry_id="agg-1",
    )
    result = _evaluate([direct, aggregator])
    by_status = {r.comparison_status for r in result.results}
    assert by_status == {
        ComparisonStatus.COMPARABLE,
        ComparisonStatus.DUPLICATE_RATE_SOURCE,
    }
    dup = next(r for r in result.results if r.comparison_status is ComparisonStatus.DUPLICATE_RATE_SOURCE)
    assert dup.normalized_quote_id == "nq-f2"  # aggregator demoted, direct representative
    assert ComparisonReasonCode.DUPLICATE_RATE_SOURCE in dup.reason_codes
    assert result.summary.duplicates == 1
    assert result.summary.distinct_rate_sources == 1


# --- G: duplicate_possible / unresolved -> NOT auto-duplicate ----------------


def test_different_sources_are_not_duplicates():
    a = make_quote(normalized_quote_id="nq-g1", distinct_rate_source_id="RS-ONE")
    b = make_quote(normalized_quote_id="nq-g2", distinct_rate_source_id="RS-TWO")
    result = _evaluate([a, b])
    assert {r.comparison_status for r in result.results} == {ComparisonStatus.COMPARABLE}
    assert result.summary.duplicates == 0
    assert result.summary.distinct_rate_sources == 2


def test_unresolved_source_identity_not_confirmed_duplicate():
    # Two results with no confirmed distinct_rate_source_id are never collapsed.
    a = make_quote(normalized_quote_id="nq-g3", distinct_rate_source_id=None)
    b = make_quote(normalized_quote_id="nq-g4", distinct_rate_source_id=None)
    result = _evaluate([a, b])
    assert {r.comparison_status for r in result.results} == {ComparisonStatus.COMPARABLE}
    assert result.summary.duplicates == 0


# --- H: 3 comparable quotes sorted ascending by annual premium ---------------


def test_comparable_quotes_sorted_by_annual_premium():
    quotes = [
        make_quote(
            normalized_quote_id="nq-h1", annual_premium=Decimal("2300.00"),
            presented_carrier="C", distinct_rate_source_id="RS-H1",
        ),
        make_quote(
            normalized_quote_id="nq-h2", annual_premium=Decimal("2100.00"),
            presented_carrier="A", distinct_rate_source_id="RS-H2",
        ),
        make_quote(
            normalized_quote_id="nq-h3", annual_premium=Decimal("2250.00"),
            presented_carrier="B", distinct_rate_source_id="RS-H3",
        ),
    ]
    result = _evaluate(quotes)
    premiums = [q.annual_premium for q in result.comparable_quotes]
    assert premiums == [Decimal("2100.00"), Decimal("2250.00"), Decimal("2300.00")]
    assert result.summary.lowest_comparable_annual_premium == Decimal("2100.00")
    assert result.summary.distinct_rate_sources == 3


# --- I: unknown coverage != excluded ------------------------------------------


def test_unknown_is_not_excluded():
    quote = make_quote(
        normalized_quote_id="nq-i", unknown_keys=frozenset({CoverageItemKey.COMPREHENSIVE})
    )
    result = _evaluate([quote])
    r = result.results[0]
    # Unknown -> insufficient (NOT comparable, NOT a negative "excluded").
    assert r.comparison_status == ComparisonStatus.INSUFFICIENT_COVERAGE_INFORMATION
    assert r.coverage_summary["comprehensive"] == "unknown"
    assert ComparisonReasonCode.UNKNOWN_COVERAGE_PRESERVED in r.reason_codes
    assert result.comparable_quotes == []


# --- Aggregator scenario (§15): 4 quote responses, 3 distinct rate sources ---


def test_aggregator_scenario_three_distinct_sources():
    direct_a = make_quote(
        normalized_quote_id="nq-a1", presented_carrier="Carrier A (Direct)",
        annual_premium=Decimal("2100.00"), distinct_rate_source_id="RS-A",
    )
    agg_a = make_quote(
        normalized_quote_id="nq-a2", presented_carrier="Carrier A (Aggregator)",
        annual_premium=Decimal("2100.00"), distinct_rate_source_id="RS-A",
        aggregator_registry_id="agg-1",
    )
    agg_b = make_quote(
        normalized_quote_id="nq-b1", presented_carrier="Carrier B (Aggregator)",
        annual_premium=Decimal("2250.00"), distinct_rate_source_id="RS-B",
        aggregator_registry_id="agg-1",
    )
    direct_c = make_quote(
        normalized_quote_id="nq-c1", presented_carrier="Carrier C (Direct)",
        annual_premium=Decimal("2300.00"), distinct_rate_source_id="RS-C",
    )
    result = _evaluate([direct_a, agg_a, agg_b, direct_c])
    assert result.summary.quote_results == 4
    assert result.summary.distinct_rate_sources == 3
    assert result.summary.comparable_quotes == 3
    assert result.summary.duplicates == 1
    comparable_carriers = [q.presented_carrier for q in result.comparable_quotes]
    assert comparable_carriers == ["Carrier A (Direct)", "Carrier B (Aggregator)", "Carrier C (Direct)"]
    # Raw results are still all present.
    assert len(result.results) == 4


# --- Partial coverage demo (§16): insufficient not ranked as cheapest --------


def test_partial_coverage_not_ranked_as_cheapest():
    a = make_quote(normalized_quote_id="nq-p1", annual_premium=Decimal("2000.00"), presented_carrier="A")
    b = make_quote(
        normalized_quote_id="nq-p2", annual_premium=Decimal("1850.00"), presented_carrier="B",
        tpl=None,
    )
    result = _evaluate([a, b])
    # B is cheaper but insufficient -> NOT in the comparable ranking.
    assert [q.normalized_quote_id for q in result.comparable_quotes] == ["nq-p1"]
    assert result.summary.lowest_comparable_annual_premium == Decimal("2000.00")


# --- Normalization-incomplete ------------------------------------------------


def test_normalization_incomplete_is_non_comparable():
    quote = make_quote(
        normalized_quote_id="nq-n1", annual_premium=None,
        normalization_status=NormalizationStatus.INSUFFICIENT_EVIDENCE,
    )
    result = _evaluate([quote])
    assert result.results[0].comparison_status == ComparisonStatus.NORMALIZATION_INCOMPLETE
    assert result.results[0].route_outcome_semantics == "quoted_non_comparable"
    assert ComparisonReasonCode.NORMALIZATION_INSUFFICIENT in result.results[0].reason_codes


# --- K: PII scan --------------------------------------------------------------


def test_comparison_output_contains_no_sensitive_markers():
    quote = make_quote(normalized_quote_id="nq-k", presented_carrier="Carrier A")
    result = _evaluate([quote])
    text = result.model_dump_json()
    for marker in SENSITIVE_MARKERS:
        assert marker.lower() not in text.lower()


# ----------------------------------------------------------------------------
# API / ownership (J)
# ----------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_singletons():
    get_evidence_service.cache_clear()
    get_quote_normalization_service.cache_clear()
    yield
    get_evidence_service.cache_clear()
    get_quote_normalization_service.cache_clear()


def _seed_normalized_quote() -> None:
    import asyncio

    from app.services.evidence.ingest import quote_from_browser_observation

    from normalization_helpers import make_browser_quote

    svc = get_evidence_service()
    obs = make_browser_quote(
        annual=Decimal("2000.00"),
        coverage=[
            "Third Party Liability - $2,000,000",
            "Collision - $1,000 deductible",
            "Comprehensive - $500 deductible",
        ],
    )
    quote = quote_from_browser_observation(
        "intake-1",
        obs,
        plan_id="plan-1",
        planned_route_id="route-1",
        registry_id="mock-a",
        distinct_rate_source_id="RS-A",
        attempt_id="att-1",
    )
    stored = asyncio.run(svc.record_quote_observation("intake-1", quote))
    asyncio.run(get_quote_normalization_service().normalize("intake-1", stored.quote_id))


def test_comparison_api_plan_endpoint(client: TestClient) -> None:
    _seed_normalized_quote()
    resp = client.get("/api/v1/comparisons/plans/plan-1?intake_session_id=intake-1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"]["quote_results"] == 1
    assert body["summary"]["comparable_quotes"] == 1
    assert body["comparable_quotes"][0]["annual_premium"] == "2000.00"


def test_comparison_api_ownership_boundary(client: TestClient) -> None:
    _seed_normalized_quote()
    resp = client.get("/api/v1/comparisons/plans/plan-1?intake_session_id=other-session")
    assert resp.status_code == 404


def test_comparison_api_intake_session_required(client: TestClient) -> None:
    resp = client.get("/api/v1/comparisons/plans/plan-1")
    assert resp.status_code == 422


def test_comparison_api_no_sensitive_markers(client: TestClient) -> None:
    _seed_normalized_quote()
    resp = client.get("/api/v1/comparisons/plans/plan-1?intake_session_id=intake-1")
    assert resp.status_code == 200
    text = resp.text
    for marker in SENSITIVE_MARKERS:
        assert marker.lower() not in text.lower()
