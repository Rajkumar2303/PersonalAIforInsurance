"""Normalized-quotes & coverage-ledger API (Issue #11, Prompt 1).

Read-only surface for canonical normalized quotes. Every endpoint requires
``intake_session_id`` as a query parameter (ownership boundary) and returns
API-safe view models - never applicant values, never raw quote references,
never raw DOM/page text.

Endpoints:
- ``GET /api/v1/normalized-quotes/{normalized_quote_id}``
- ``GET /api/v1/normalized-quotes/plans/{plan_id}``
- ``GET /api/v1/normalized-quotes/routes/{planned_route_id}``
- ``GET /api/v1/normalized-quotes/attempts/{attempt_id}``
- ``GET /api/v1/normalized-quotes/export``
- ``POST /api/v1/normalized-quotes/normalize`` (deterministic, idempotent)

Normalization WRITE happens only through the explicit, validated
``QuoteNormalizationService`` - there is deliberately no free-form write
endpoint.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from ..models.normalization import NormalizedExportView, NormalizedQuoteView
from ..services.normalization import (
    QuoteNormalizationError,
    QuoteNormalizationService,
    get_quote_normalization_service,
)
from ..services.normalization.service import _export_view, _quote_view

router = APIRouter(prefix="/api/v1/normalized-quotes", tags=["normalized-quotes"])

logger = logging.getLogger(__name__)


def _service(
    service: QuoteNormalizationService = Depends(get_quote_normalization_service),
) -> QuoteNormalizationService:
    return service


@router.post("/normalize", response_model=NormalizedQuoteView)
async def normalize_quote(
    intake_session_id: str = Query(..., description="Ownership scope (required)"),
    source_quote_observation_id: str = Body(..., embed=True),
    service: QuoteNormalizationService = Depends(_service),
) -> NormalizedQuoteView:
    """Normalize one durable quote observation (deterministic, idempotent).

    The source quote must already exist in the Issue #10 evidence store for the
    given intake session. Never assigns comparable/non-comparable statuses.
    """
    try:
        quote = await service.normalize(intake_session_id, source_quote_observation_id)
    except QuoteNormalizationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _quote_view(quote)


@router.get("/plans/{plan_id}", response_model=list[NormalizedQuoteView])
async def list_plan_normalized_quotes(
    plan_id: str,
    intake_session_id: str = Query(..., description="Ownership scope (required)"),
    service: QuoteNormalizationService = Depends(_service),
) -> list[NormalizedQuoteView]:
    quotes = await service.list_by_plan(intake_session_id, plan_id)
    return [_quote_view(q) for q in quotes]


@router.get("/routes/{planned_route_id}", response_model=list[NormalizedQuoteView])
async def list_route_normalized_quotes(
    planned_route_id: str,
    intake_session_id: str = Query(..., description="Ownership scope (required)"),
    service: QuoteNormalizationService = Depends(_service),
) -> list[NormalizedQuoteView]:
    quotes = await service.list_by_route(intake_session_id, planned_route_id)
    return [_quote_view(q) for q in quotes]


@router.get("/attempts/{attempt_id}", response_model=list[NormalizedQuoteView])
async def list_attempt_normalized_quotes(
    attempt_id: str,
    intake_session_id: str = Query(..., description="Ownership scope (required)"),
    service: QuoteNormalizationService = Depends(_service),
) -> list[NormalizedQuoteView]:
    quotes = await service.list_by_attempt(intake_session_id, attempt_id)
    return [_quote_view(q) for q in quotes]


@router.get("/export", response_model=NormalizedExportView)
async def export_normalized_quotes(
    intake_session_id: str = Query(..., description="Ownership scope (required)"),
    service: QuoteNormalizationService = Depends(_service),
) -> NormalizedExportView:
    quotes = await service.list_by_intake(intake_session_id)
    return _export_view(intake_session_id, quotes, service.rule_version)


@router.get("/{normalized_quote_id}", response_model=NormalizedQuoteView)
async def get_normalized_quote(
    normalized_quote_id: str,
    intake_session_id: str = Query(..., description="Ownership scope (required)"),
    service: QuoteNormalizationService = Depends(_service),
) -> NormalizedQuoteView:
    quote = await service.get(intake_session_id, normalized_quote_id)
    if quote is None:
        raise HTTPException(status_code=404, detail="normalized quote not found")
    return _quote_view(quote)
