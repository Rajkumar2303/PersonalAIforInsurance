"""Read-only route planner API (Issue #6).

Returns deterministic route plans containing CANONICAL FIELD PATHS and PUBLIC
market data only - never applicant values. No mutation of the profile or any
browser/voice execution.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query

from ..core.config import Settings, get_settings
from ..core.logging import clear_log_context, set_log_context
from ..core.tracing import run_config
from ..graph.route_planner_workflow import WORKFLOW_NAME, build_route_planner_workflow
from ..models.intake.session import FieldRequestOutcome
from ..models.route_planner import RoutePlan
from ..services.intake.engine import SessionNotFoundError
from ..services.route_planner import get_route_planner

router = APIRouter(prefix="/api/v1", tags=["planner"])

logger = logging.getLogger(__name__)


def _planner_for_mode(mode: str):
    """Resolve the planner by execution mode (Issue #8.5).

    ``mock`` uses the isolated demo overlay (synthetic routes only). ``live``
    (default) uses the real planner over the real market registry - unchanged.
    """
    if mode == "mock":
        from ..demo.runtime import get_demo_runtime

        return get_demo_runtime().planner
    return get_route_planner()


@router.get("/planner/plan", response_model=RoutePlan, summary="Build a deterministic route plan for an intake session")
async def plan(
    session_id: str,
    mode: str = Query(default="live", description="execution mode: live (real registry) or mock (demo overlay)"),
    settings: Settings = Depends(get_settings),
) -> RoutePlan:
    request_id = uuid.uuid4().hex
    set_log_context(request_id=request_id, workflow=WORKFLOW_NAME, workflow_stage="plan")
    planner = _planner_for_mode(mode)
    try:
        planner.plan(session_id)  # validates the session exists
    except SessionNotFoundError:
        clear_log_context()
        raise HTTPException(status_code=404, detail="intake session not found")
    try:
        config = run_config(
            settings,
            request_id=request_id,
            workflow=WORKFLOW_NAME,
            extra_metadata={"session_id": session_id, "workflow_stage": "plan", "execution_mode": mode},
        )
        await build_route_planner_workflow(planner).ainvoke({"entry": "plan", "session_id": session_id}, config=config)
        return planner.plan(session_id)
    finally:
        clear_log_context()


@router.post(
    "/planner/plan/{session_id}/request-missing",
    response_model=list[FieldRequestOutcome],
    summary="Request the union of missing required route fields (Issue #5 integration)",
)
async def request_missing(session_id: str) -> list[FieldRequestOutcome]:
    request_id = uuid.uuid4().hex
    set_log_context(request_id=request_id, workflow=WORKFLOW_NAME, workflow_stage="request_missing")
    try:
        planner = get_route_planner()
        try:
            return planner.request_missing_fields(session_id)
        except SessionNotFoundError:
            raise HTTPException(status_code=404, detail="intake session not found")
    finally:
        clear_log_context()
