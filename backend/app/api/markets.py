"""Read-only market registry endpoints (Issue #3).

Returns PUBLIC market data only - never applicant data. No mutation endpoints.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from ..models.insurance.enums import InsuranceType
from ..models.registry import DistributionType, MarketRegistryEntry, ProductScope
from ..services.market_registry import get_market_registry_service

router = APIRouter(prefix="/api/v1", tags=["markets"])


@router.get(
    "/markets",
    response_model=list[MarketRegistryEntry],
    summary="List Ontario market registry records",
)
async def list_markets(
    product_type: Optional[InsuranceType] = Query(default=None, description="Filter by product type"),
    distribution_type: Optional[DistributionType] = Query(default=None, description="Filter by distribution type"),
    product_scope: Optional[ProductScope] = Query(default=None, description="Filter by product scope"),
) -> list[MarketRegistryEntry]:
    """Return all registry records, optionally filtered."""
    service = get_market_registry_service()
    records = service.list_markets()
    if product_type is not None:
        records = [entry for entry in records if entry.product_type is product_type]
    if distribution_type is not None:
        records = [entry for entry in records if entry.distribution_type is distribution_type]
    if product_scope is not None:
        records = [entry for entry in records if entry.product_scope is product_scope]
    return records


@router.get(
    "/markets/{registry_id}",
    response_model=MarketRegistryEntry,
    summary="Get one registry record by registry_id",
)
async def get_market(registry_id: str) -> MarketRegistryEntry:
    """Return a single registry record, or 404 if unknown."""
    entry = get_market_registry_service().get_by_registry_id(registry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="registry record not found")
    return entry
