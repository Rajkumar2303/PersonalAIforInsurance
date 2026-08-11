"""Lite comparison service (Issue #12, MVP).

Deterministic only: no LLM, no probabilistic matching, no recommendation
logic. It classifies ``NormalizedQuote`` objects, checks essential-coverage
completeness, detects confirmed duplicate rate sources (Issue #4
``distinct_rate_source_id``), keeps estimates separate, and sorts comparable
firm quotes by normalized annual premium. No browser/evidence/database
business logic lives here.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

from ...models.comparison import (
    ComparisonPlanResult,
    ComparisonReasonCode,
    ComparisonStatus,
    ComparisonSummary,
    CoverageCompleteness,
    QuoteComparisonResult,
    RequestedCoverage,
)
from ...models.normalization import (
    CoverageItemKey,
    CoverageItemState,
    MoneyCoverageValue,
    NormalizationStatus,
    NormalizedQuote,
)

logger = logging.getLogger(__name__)

ESSENTIAL_COVERAGE_KEYS: tuple[CoverageItemKey, ...] = (
    CoverageItemKey.THIRD_PARTY_LIABILITY,
    CoverageItemKey.COLLISION,
    CoverageItemKey.COMPREHENSIVE,
)

_MISSING_REASON_BY_KEY = {
    CoverageItemKey.THIRD_PARTY_LIABILITY: ComparisonReasonCode.MISSING_LIABILITY_LIMIT,
    CoverageItemKey.COLLISION: ComparisonReasonCode.MISSING_COLLISION_DEDUCTIBLE,
    CoverageItemKey.COMPREHENSIVE: ComparisonReasonCode.MISSING_COMPREHENSIVE_DEDUCTIBLE,
}


def _money_amount(item) -> Optional[Decimal]:
    if item is not None and item.value is not None and isinstance(item.value, MoneyCoverageValue):
        return item.value.amount
    return None


class QuoteComparisonService:
    """Classify and organize normalized quotes for the comparison view."""

    def evaluate(
        self,
        normalized_quotes: list[NormalizedQuote],
        *,
        requested_coverage: Optional[RequestedCoverage] = None,
        intake_session_id: str = "",
        plan_id: Optional[str] = None,
        planned_route_id: Optional[str] = None,
    ) -> ComparisonPlanResult:
        """Deterministically classify a set of normalized quotes."""
        classified = [
            self._classify(q, requested_coverage, intake_session_id, plan_id, planned_route_id)
            for q in normalized_quotes
        ]
        classified = self._apply_duplicate_handling(classified)

        comparable = sorted(
            [r for r in classified if r.comparison_status is ComparisonStatus.COMPARABLE],
            key=lambda r: (r.annual_premium, r.normalized_quote_id),
        )
        estimates = [r for r in classified if r.comparison_status is ComparisonStatus.ESTIMATE_ONLY]
        duplicates = [
            r for r in classified if r.comparison_status is ComparisonStatus.DUPLICATE_RATE_SOURCE
        ]
        insufficient = [
            r
            for r in classified
            if r.comparison_status
            in (
                ComparisonStatus.INSUFFICIENT_COVERAGE_INFORMATION,
                ComparisonStatus.COVERAGE_MISMATCH,
                ComparisonStatus.NORMALIZATION_INCOMPLETE,
            )
        ]

        summary = self._build_summary(classified, comparable)

        return ComparisonPlanResult(
            intake_session_id=intake_session_id,
            plan_id=plan_id,
            planned_route_id=planned_route_id,
            requested_coverage=requested_coverage,
            results=classified,
            comparable_quotes=comparable,
            estimates=estimates,
            duplicates=duplicates,
            insufficient=insufficient,
            summary=summary,
        )

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    def _classify(
        self,
        quote: NormalizedQuote,
        requested: Optional[RequestedCoverage],
        intake_session_id: str,
        plan_id: Optional[str],
        planned_route_id: Optional[str],
    ) -> QuoteComparisonResult:
        ledger = quote.coverage_ledger

        # --- essential coverage completeness (unknown != excluded) -----
        missing_keys: list[str] = []
        missing_reasons: list[ComparisonReasonCode] = []
        coverage_summary: dict[str, str] = {}
        known = 0
        for key in ESSENTIAL_COVERAGE_KEYS:
            item = ledger.get(key)
            if item is not None and item.state is CoverageItemState.UNKNOWN:
                missing_reasons.append(ComparisonReasonCode.UNKNOWN_COVERAGE_PRESERVED)
            amount = _money_amount(item)
            if amount is not None:
                known += 1
                coverage_summary[key.value] = (
                    "limit" if key is CoverageItemKey.THIRD_PARTY_LIABILITY else "deductible"
                ) + f" {amount}"
            else:
                missing_keys.append(key.value)
                missing_reasons.append(_MISSING_REASON_BY_KEY[key])
                coverage_summary[key.value] = "unknown"

        total = len(ESSENTIAL_COVERAGE_KEYS)
        if known == total:
            completeness = CoverageCompleteness.COMPLETE
        elif known == 0:
            completeness = CoverageCompleteness.INSUFFICIENT
        else:
            completeness = CoverageCompleteness.PARTIAL

        # --- normalization incomplete ----------------------------------
        if (
            quote.normalization_status
            in (
                NormalizationStatus.INSUFFICIENT_EVIDENCE,
                NormalizationStatus.INVALID_SOURCE,
                NormalizationStatus.NORMALIZATION_FAILED,
                NormalizationStatus.PENDING,
            )
            or quote.premium.normalized_annual_amount is None
        ):
            return self._result(
                quote, intake_session_id, plan_id, planned_route_id,
                ComparisonStatus.NORMALIZATION_INCOMPLETE,
                "quoted_non_comparable",
                [ComparisonReasonCode.NORMALIZATION_INSUFFICIENT],
                missing_keys, coverage_summary, completeness, known, total,
            )

        # --- estimate --------------------------------------------------
        if quote.firm_vs_estimate == "estimate":
            return self._result(
                quote, intake_session_id, plan_id, planned_route_id,
                ComparisonStatus.ESTIMATE_ONLY,
                "estimate_only",
                [ComparisonReasonCode.ESTIMATE_ONLY],
                missing_keys, coverage_summary, completeness, known, total,
            )

        # --- insufficient essential coverage ---------------------------
        if missing_keys:
            return self._result(
                quote, intake_session_id, plan_id, planned_route_id,
                ComparisonStatus.INSUFFICIENT_COVERAGE_INFORMATION,
                "quoted_non_comparable",
                missing_reasons,
                missing_keys, coverage_summary, completeness, known, total,
            )

        # --- requested vs quoted coverage mismatch ----------------------
        mismatch_reasons: list[ComparisonReasonCode] = []
        if requested is not None:
            liability = _money_amount(ledger.get(CoverageItemKey.THIRD_PARTY_LIABILITY))
            collision = _money_amount(ledger.get(CoverageItemKey.COLLISION))
            comprehensive = _money_amount(ledger.get(CoverageItemKey.COMPREHENSIVE))
            if (
                requested.third_party_liability_limit is not None
                and liability is not None
                and int(liability) != requested.third_party_liability_limit
            ):
                mismatch_reasons.append(ComparisonReasonCode.LIABILITY_LIMIT_MISMATCH)
            if (
                requested.collision_deductible is not None
                and collision is not None
                and collision != requested.collision_deductible
            ):
                mismatch_reasons.append(ComparisonReasonCode.COLLISION_DEDUCTIBLE_MISMATCH)
            if (
                requested.comprehensive_deductible is not None
                and comprehensive is not None
                and comprehensive != requested.comprehensive_deductible
            ):
                mismatch_reasons.append(ComparisonReasonCode.COMPREHENSIVE_DEDUCTIBLE_MISMATCH)

        if mismatch_reasons:
            return self._result(
                quote, intake_session_id, plan_id, planned_route_id,
                ComparisonStatus.COVERAGE_MISMATCH,
                "quoted_non_comparable",
                mismatch_reasons,
                missing_keys, coverage_summary, completeness, known, total,
            )

        return self._result(
            quote, intake_session_id, plan_id, planned_route_id,
            ComparisonStatus.COMPARABLE,
            "quoted_comparable",
            [],
            missing_keys, coverage_summary, completeness, known, total,
        )

    def _result(
        self,
        quote: NormalizedQuote,
        intake_session_id: str,
        plan_id: Optional[str],
        planned_route_id: Optional[str],
        status: ComparisonStatus,
        semantics: str,
        reasons: list[ComparisonReasonCode],
        missing_keys: list[str],
        coverage_summary: dict[str, str],
        completeness: CoverageCompleteness,
        known: int,
        total: int,
    ) -> QuoteComparisonResult:
        return QuoteComparisonResult(
            normalized_quote_id=quote.normalized_quote_id,
            intake_session_id=intake_session_id,
            plan_id=plan_id or quote.plan_id,
            planned_route_id=planned_route_id or quote.planned_route_id,
            registry_id=quote.registry_id,
            presented_carrier=quote.presented_carrier,
            distinct_rate_source_id=quote.distinct_rate_source_id,
            aggregator_registry_id=quote.aggregator_registry_id,
            source_quote_observation_id=quote.source_quote_observation_id,
            annual_premium=quote.premium.normalized_annual_amount,
            firm_vs_estimate=quote.firm_vs_estimate,
            comparison_status=status,
            route_outcome_semantics=semantics,
            reason_codes=reasons,
            missing_coverage_keys=missing_keys,
            coverage_summary=coverage_summary,
            coverage_completeness=completeness,
            known_required_fields=known,
            total_required_fields=total,
        )

    # ------------------------------------------------------------------
    # Confirmed duplicate rate sources (Issue #4 distinct_rate_source_id)
    # ------------------------------------------------------------------

    def _apply_duplicate_handling(
        self, classified: list[QuoteComparisonResult]
    ) -> list[QuoteComparisonResult]:
        """Demote non-representative confirmed duplicates to duplicate_rate_source.

        Only EXACT ``distinct_rate_source_id`` matches count (Issue #4 confirmed
        underlying source). ``duplicate_possible`` / unresolved identity is never
        treated as a confirmed duplicate here.
        """
        groups: dict[str, list[QuoteComparisonResult]] = {}
        for result in classified:
            if (
                result.comparison_status is ComparisonStatus.COMPARABLE
                and result.distinct_rate_source_id
            ):
                groups.setdefault(result.distinct_rate_source_id, []).append(result)

        representatives: set[str] = set()
        for source_id, results in groups.items():
            if len(results) < 2:
                representatives.add(results[0].normalized_quote_id)
                continue
            representative = self._pick_representative(results)
            representatives.add(representative.normalized_quote_id)
            for result in results:
                if result.normalized_quote_id == representative.normalized_quote_id:
                    result.is_representative = True
                else:
                    result.comparison_status = ComparisonStatus.DUPLICATE_RATE_SOURCE
                    result.route_outcome_semantics = "duplicate_rate_source"
                    result.reason_codes = [ComparisonReasonCode.DUPLICATE_RATE_SOURCE]

        for result in classified:
            if (
                result.comparison_status is ComparisonStatus.COMPARABLE
                and result.normalized_quote_id in representatives
            ):
                result.is_representative = True

        return classified

    def _pick_representative(self, results: list[QuoteComparisonResult]) -> QuoteComparisonResult:
        """Deterministic representative: direct > complete > lowest premium.

        All candidates here are comparable (firm) quotes. Preference:
        1. firm over estimate (all are firm at this stage)
        2. more complete normalized coverage (more known required fields)
        3. direct/primary result (no aggregator) over an aggregator copy
        4. lowest annual premium (ties broken by id for determinism)
        """
        return min(
            results,
            key=lambda r: (
                -r.known_required_fields,
                0 if r.aggregator_registry_id is None else 1,
                r.annual_premium if r.annual_premium is not None else Decimal("999999999"),
                r.normalized_quote_id,
            ),
        )

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def _build_summary(
        self, classified: list[QuoteComparisonResult], comparable: list[QuoteComparisonResult]
    ) -> ComparisonSummary:
        by_status: dict[ComparisonStatus, int] = {}
        for r in classified:
            by_status[r.comparison_status] = by_status.get(r.comparison_status, 0) + 1

        distinct_sources = {
            r.distinct_rate_source_id
            for r in classified
            if r.distinct_rate_source_id
        }
        routes = {
            r.planned_route_id for r in classified if r.planned_route_id
        }
        lowest = comparable[0].annual_premium if comparable else None
        return ComparisonSummary(
            routes_attempted=len(routes) or len(classified),
            quote_results=len(classified),
            comparable_quotes=by_status.get(ComparisonStatus.COMPARABLE, 0),
            estimates=by_status.get(ComparisonStatus.ESTIMATE_ONLY, 0),
            duplicates=by_status.get(ComparisonStatus.DUPLICATE_RATE_SOURCE, 0),
            insufficient_coverage=by_status.get(
                ComparisonStatus.INSUFFICIENT_COVERAGE_INFORMATION, 0
            ),
            coverage_mismatch=by_status.get(ComparisonStatus.COVERAGE_MISMATCH, 0),
            normalization_incomplete=by_status.get(ComparisonStatus.NORMALIZATION_INCOMPLETE, 0),
            distinct_rate_sources=len(distinct_sources),
            lowest_comparable_annual_premium=lowest,
        )
