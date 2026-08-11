"""Lite comparisons API (Issue #12, MVP).

Read/evaluate surface for Issue #13. Deterministically recomputes comparison
results from the Issue #11 normalized quotes - no new persistence. Every
endpoint requires ``intake_session_id`` (ownership boundary) and returns safe
models only (no applicant PII).
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ..models.comparison import ComparisonPlanResult, RequestedCoverage
from ..services.normalization import get_quote_normalization_service
from ..services.normalization.service import QuoteNormalizationService

router = APIRouter(prefix="/api/v1/comparisons", tags=["comparisons"])

logger = logging.getLogger(__name__)


def _service(
    service: QuoteNormalizationService = Depends(get_quote_normalization_service),
) -> QuoteNormalizationService:
    return service


def _requested_coverage(
    liability_limit: Optional[str] = Query(None, alias="liability_limit"),
    collision_deductible: Optional[str] = Query(None, alias="collision_deductible"),
    comprehensive_deductible: Optional[str] = Query(None, alias="comprehensive_deductible"),
) -> Optional[RequestedCoverage]:
    """Optional requested-coverage profile from query params (never required)."""
    if not any((liability_limit, collision_deductible, comprehensive_deductible)):
        return None
    return RequestedCoverage(
        third_party_liability_limit=_to_int(liability_limit),
        collision_deductible=_to_decimal(collision_deductible),
        comprehensive_deductible=_to_decimal(comprehensive_deductible),
    )


def _to_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    return int(value)


def _to_decimal(value: Optional[str]) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise HTTPException(status_code=422, detail=f"invalid decimal: {value}") from exc


@router.get("/plans/{plan_id}", response_model=ComparisonPlanResult)
async def compare_plan(
    plan_id: str,
    intake_session_id: str = Query(..., description="Ownership scope (required)"),
    service: QuoteNormalizationService = Depends(_service),
    requested: Optional[RequestedCoverage] = Depends(_requested_coverage),
) -> ComparisonPlanResult:
    """Deterministic comparison of all normalized quotes for a plan."""
    from ..services.comparison import QuoteComparisonService

    quotes = await service.list_by_plan(intake_session_id, plan_id)
    if not quotes:
        raise HTTPException(status_code=404, detail="no normalized quotes for plan")
    return QuoteComparisonService().evaluate(
        quotes,
        requested_coverage=requested,
        intake_session_id=intake_session_id,
        plan_id=plan_id,
    )


@router.get("/routes/{planned_route_id}", response_model=ComparisonPlanResult)
async def compare_route(
    planned_route_id: str,
    intake_session_id: str = Query(..., description="Ownership scope (required)"),
    service: QuoteNormalizationService = Depends(_service),
    requested: Optional[RequestedCoverage] = Depends(_requested_coverage),
) -> ComparisonPlanResult:
    """Deterministic comparison of all normalized quotes for a route."""
    from ..services.comparison import QuoteComparisonService

    quotes = await service.list_by_route(intake_session_id, planned_route_id)
    if not quotes:
        raise HTTPException(status_code=404, detail="no normalized quotes for route")
    return QuoteComparisonService().evaluate(
        quotes,
        requested_coverage=requested,
        intake_session_id=intake_session_id,
        planned_route_id=planned_route_id,
    )
