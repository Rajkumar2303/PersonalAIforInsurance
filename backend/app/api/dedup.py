"""Read-only rate-source deduplication endpoints (Issue #4).

Public market/evidence data only - never applicant data. No mutation endpoints.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..models.dedup import DistinctRateSource, DuplicateCandidate
from ..services.deduplication import DedupLookupError, get_deduplication_service

router = APIRouter(prefix="/api/v1", tags=["deduplication"])


@router.get("/rate-sources", response_model=list[DistinctRateSource], summary="List known distinct rate sources")
async def list_rate_sources() -> list[DistinctRateSource]:
    return get_deduplication_service().list_rate_sources()


@router.get("/rate-sources/{rate_source_id}", response_model=DistinctRateSource, summary="Get one distinct rate source")
async def get_rate_source(rate_source_id: str) -> DistinctRateSource:
    rate_source = get_deduplication_service().get_rate_source(rate_source_id)
    if rate_source is None:
        raise HTTPException(status_code=404, detail="rate source not found")
    return rate_source


@router.get(
    "/markets/{registry_id}/duplicates",
    response_model=list[DuplicateCandidate],
    summary="Surface possible duplicate routes for a registry record",
)
async def duplicate_candidates(registry_id: str) -> list[DuplicateCandidate]:
    service = get_deduplication_service()
    try:
        return service.find_duplicate_candidates(registry_id)
    except DedupLookupError as exc:
        raise HTTPException(status_code=404, detail="registry record not found") from exc


@router.get("/dedup/metrics", response_model=dict[str, int], summary="Deduplication metrics foundation")
async def dedup_metrics() -> dict[str, int]:
    return get_deduplication_service().metrics()
