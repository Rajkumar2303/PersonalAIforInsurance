"""Comparison orchestration API (Issue #8.5 integration checkpoint).

- ``POST /api/v1/orchestrate/compare``: start a pollable comparison job.
- ``GET /api/v1/orchestrate/jobs/{job_id}``: safe per-route progress/results.

Polling only - no SSE/WebSockets. The orchestrator is glue over Issue #6/#7/#8;
no business rules are duplicated here. ``execution_mode=mock`` (default) uses
the isolated demo overlay + local mock site; ``live`` uses real services and
the existing Issue #7 live gates.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from ..core.config import Settings, get_settings
from ..core.logging import clear_log_context, set_log_context
from ..models.browser.session import LiveExecutionGate
from ..services.intake.engine import SessionNotFoundError
from ..services.intake import get_intake_engine
from ..services.orchestration import ComparisonJob, ComparisonOrchestrator, get_comparison_orchestrator
from pydantic import BaseModel

router = APIRouter(prefix="/api/v1", tags=["orchestrate"])

logger = logging.getLogger(__name__)


def _orchestrator_dep() -> ComparisonOrchestrator:
    return get_comparison_orchestrator()


class CompareRequest(BaseModel):
    intake_session_id: str
    execution_mode: str = "mock"  # "mock" (default) | "live"
    live_gate: Optional[LiveExecutionGate] = None


@router.post(
    "/orchestrate/compare",
    response_model=ComparisonJob,
    summary="Start a pollable quote-comparison job (mock default; live explicit + gated)",
)
async def compare(
    payload: CompareRequest,
    orchestrator: ComparisonOrchestrator = Depends(_orchestrator_dep),
    settings: Settings = Depends(get_settings),
) -> ComparisonJob:
    request_id = uuid.uuid4().hex
    set_log_context(request_id=request_id, workflow="comparison", workflow_stage="start")
    try:
        # Validate the intake session exists (safe 404 instead of a failed job).
        try:
            get_intake_engine().get_session(payload.intake_session_id)
        except SessionNotFoundError:
            raise HTTPException(status_code=404, detail="intake session not found")
        if payload.execution_mode not in ("mock", "live"):
            raise HTTPException(status_code=422, detail="execution_mode must be 'mock' or 'live'")
        return orchestrator.start_compare(
            payload.intake_session_id, payload.execution_mode, live_gate=payload.live_gate
        )
    finally:
        clear_log_context()


@router.get(
    "/orchestrate/jobs/{job_id}",
    response_model=ComparisonJob,
    summary="Get safe comparison-job progress/results (poll this)",
)
async def get_job(
    job_id: str,
    orchestrator: ComparisonOrchestrator = Depends(_orchestrator_dep),
) -> ComparisonJob:
    job = orchestrator.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="comparison job not found")
    return job


@router.get(
    "/orchestrate/jobs",
    response_model=list[ComparisonJob],
    summary="List recent comparison jobs (safe)",
)
async def list_jobs(
    orchestrator: ComparisonOrchestrator = Depends(_orchestrator_dep),
) -> list[ComparisonJob]:
    return orchestrator.list_jobs()
