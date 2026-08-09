"""Health/liveness endpoint."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness probe")
async def health() -> dict[str, str]:
    """Return a simple OK payload used by the frontend status indicator."""
    return {"status": "ok"}
