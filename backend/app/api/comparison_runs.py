"""Comparison run API (Issue #13, MVP).

- ``POST /api/v1/comparison-runs``: start a pollable comparison run (idempotent -
  an active run for the same intake session is reused, so double-clicking
  Compare never double-submits).
- ``GET /api/v1/comparison-runs/{run_id}``: poll progress/final result
  (ownership-scoped by ``intake_session_id``).

Polling only - no SSE/WebSockets. No applicant PII travels through these
endpoints (only the intake_session_id is used to resolve the ProfileVault).
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from ..core.logging import clear_log_context, set_log_context
from ..models.browser.session import LiveExecutionGate
from ..models.comparison_run import ComparisonRun
from ..services.comparison_run import ComparisonRunService, get_comparison_run_service
from ..services.intake import get_intake_engine
from ..services.intake.engine import SessionNotFoundError

router = APIRouter(prefix="/api/v1/comparison-runs", tags=["comparison-runs"])

logger = logging.getLogger(__name__)


def _service(
    service: ComparisonRunService = Depends(get_comparison_run_service),
) -> ComparisonRunService:
    return service


class StartComparisonRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intake_session_id: str
    execution_mode: str = "mock"  # mock (default) | live
    # Applicant's EXPLICIT attestation for LIVE execution. Optional: when absent
    # (or not satisfied) a live route is refused with LIVE_GATE_REQUIRED. Never
    # auto-granted - the client must send a satisfied gate to proceed.
    live_gate: Optional[LiveExecutionGate] = None


@router.post("", response_model=ComparisonRun)
async def start_comparison_run(
    payload: StartComparisonRunRequest,
    service: ComparisonRunService = Depends(_service),
) -> ComparisonRun:
    """Start a comparison run (returns quickly; poll GET for progress)."""
    request_id = uuid.uuid4().hex
    set_log_context(request_id=request_id, workflow="comparison_run", workflow_stage="start")
    try:
        if payload.execution_mode not in ("mock", "live"):
            raise HTTPException(status_code=422, detail="execution_mode must be 'mock' or 'live'")
        try:
            get_intake_engine().get_session(payload.intake_session_id)
        except SessionNotFoundError:
            raise HTTPException(status_code=404, detail="intake session not found")
        return service.start_run(
            payload.intake_session_id, payload.execution_mode, live_gate=payload.live_gate
        )
    finally:
        clear_log_context()


@router.get("/{run_id}", response_model=ComparisonRun)
async def get_comparison_run(
    run_id: str,
    intake_session_id: str = Query(..., description="Ownership scope (required)"),
    service: ComparisonRunService = Depends(_service),
) -> ComparisonRun:
    """Get comparison-run progress/final result (poll this)."""
    run = service.get_run(intake_session_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="comparison run not found")
    return run
