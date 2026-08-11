"""Lite comparability + simple completeness/reasoning (Issue #12, MVP).

Turns ``NormalizedQuote`` objects (Issue #11) into simple comparison-ready
results WITHOUT building a scoring system, recommendations, or personalized
advice. Deterministic rules only - no LLM, no probabilistic matching.

Boundaries:
- ``unknown`` coverage is NEVER treated as ``excluded`` (insufficient, not a
  negative).
- Estimates are kept separate (``estimate_only``) and never enter the
  comparable-price ranking.
- Confirmed duplicate rate sources are identified via
  ``distinct_rate_source_id`` (Issue #4); ``duplicate_possible``/unresolved
  identity is NEVER treated as a confirmed duplicate.
- This is where it becomes acceptable to map results onto the downstream route
  semantics ``quoted_comparable`` / ``quoted_non_comparable`` - but Issue #11
  ``NormalizedQuote`` records are never modified here.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Optional

from pydantic import ConfigDict, Field

from .insurance.auto.coverage import CoverageConfiguration, OwnDamageCoverageType
from .insurance.base import SensitiveBaseModel


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ComparisonStatus(StrEnum):
    """Deterministic comparison classification for one normalized quote."""

    COMPARABLE = "comparable"
    INSUFFICIENT_COVERAGE_INFORMATION = "insufficient_coverage_information"
    ESTIMATE_ONLY = "estimate_only"
    DUPLICATE_RATE_SOURCE = "duplicate_rate_source"
    NORMALIZATION_INCOMPLETE = "normalization_incomplete"
    COVERAGE_MISMATCH = "coverage_mismatch"


class ComparisonReasonCode(StrEnum):
    """Typed, data-driven reasons for every non-comparable result."""

    MISSING_LIABILITY_LIMIT = "missing_liability_limit"
    MISSING_COLLISION_DEDUCTIBLE = "missing_collision_deductible"
    MISSING_COMPREHENSIVE_DEDUCTIBLE = "missing_comprehensive_deductible"
    LIABILITY_LIMIT_MISMATCH = "liability_limit_mismatch"
    COLLISION_DEDUCTIBLE_MISMATCH = "collision_deductible_mismatch"
    COMPREHENSIVE_DEDUCTIBLE_MISMATCH = "comprehensive_deductible_mismatch"
    ESTIMATE_ONLY = "estimate_only"
    DUPLICATE_RATE_SOURCE = "duplicate_rate_source"
    NORMALIZATION_INSUFFICIENT = "normalization_insufficient"
    UNKNOWN_COVERAGE_PRESERVED = "unknown_coverage_preserved"


class CoverageCompleteness(StrEnum):
    """Simple completeness signal (NOT a confidence score)."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"


# ---------------------------------------------------------------------------
# Requested (user) coverage - kept separate from provider-quoted coverage
# ---------------------------------------------------------------------------


class RequestedCoverage(SensitiveBaseModel):
    """The user's requested essential coverage (from the Issue #2 profile)."""

    model_config = ConfigDict(extra="forbid")

    third_party_liability_limit: Optional[int] = None
    collision_deductible: Optional[Decimal] = None
    comprehensive_deductible: Optional[Decimal] = None

    @classmethod
    def from_coverage_configuration(cls, config: CoverageConfiguration) -> "RequestedCoverage":
        """Derive requested essential coverage from the Issue #2 profile."""
        collision = next(
            (d for d in config.own_damage.items if d.coverage_type is OwnDamageCoverageType.COLLISION),
            None,
        )
        comprehensive = next(
            (d for d in config.own_damage.items if d.coverage_type is OwnDamageCoverageType.COMPREHENSIVE),
            None,
        )
        return cls(
            third_party_liability_limit=config.third_party_liability.selected_limit,
            collision_deductible=(
                Decimal(str(collision.deductible))
                if collision and collision.deductible is not None
                else None
            ),
            comprehensive_deductible=(
                Decimal(str(comprehensive.deductible))
                if comprehensive and comprehensive.deductible is not None
                else None
            ),
        )


# ---------------------------------------------------------------------------
# Comparison result
# ---------------------------------------------------------------------------


class QuoteComparisonResult(SensitiveBaseModel):
    """One normalized quote classified for comparison (PII-free)."""

    model_config = ConfigDict(extra="forbid")

    normalized_quote_id: str
    intake_session_id: str
    plan_id: Optional[str] = None
    planned_route_id: Optional[str] = None
    registry_id: Optional[str] = None
    presented_carrier: Optional[str] = None
    distinct_rate_source_id: Optional[str] = None
    aggregator_registry_id: Optional[str] = None
    source_quote_observation_id: str
    annual_premium: Optional[Decimal] = None
    firm_vs_estimate: str = "firm"
    comparison_status: ComparisonStatus
    # Downstream route semantics (Issue #8 vocabulary):
    #   comparable -> quoted_comparable
    #   insufficient/mismatch/normalization_incomplete -> quoted_non_comparable
    #   estimate_only -> estimate_only ; duplicate_rate_source -> duplicate_rate_source
    route_outcome_semantics: str
    reason_codes: list[ComparisonReasonCode] = Field(default_factory=list)
    missing_coverage_keys: list[str] = Field(default_factory=list)
    coverage_summary: dict[str, str] = Field(default_factory=dict)
    coverage_completeness: CoverageCompleteness = CoverageCompleteness.INSUFFICIENT
    known_required_fields: int = 0
    total_required_fields: int = 3
    is_representative: bool = False


class ComparisonSummary(SensitiveBaseModel):
    """Lightweight frontend-ready summary for Issue #13."""

    model_config = ConfigDict(extra="forbid")

    routes_attempted: int = 0
    quote_results: int = 0
    comparable_quotes: int = 0
    estimates: int = 0
    duplicates: int = 0
    insufficient_coverage: int = 0
    coverage_mismatch: int = 0
    normalization_incomplete: int = 0
    distinct_rate_sources: int = 0
    lowest_comparable_annual_premium: Optional[Decimal] = None


class ComparisonPlanResult(SensitiveBaseModel):
    """Comparison output for one plan (or route) - safe for the frontend."""

    model_config = ConfigDict(extra="forbid")

    intake_session_id: str
    plan_id: Optional[str] = None
    planned_route_id: Optional[str] = None
    requested_coverage: Optional[RequestedCoverage] = None
    results: list[QuoteComparisonResult] = Field(default_factory=list)
    comparable_quotes: list[QuoteComparisonResult] = Field(default_factory=list)
    estimates: list[QuoteComparisonResult] = Field(default_factory=list)
    duplicates: list[QuoteComparisonResult] = Field(default_factory=list)
    insufficient: list[QuoteComparisonResult] = Field(default_factory=list)
    summary: ComparisonSummary = Field(default_factory=ComparisonSummary)
