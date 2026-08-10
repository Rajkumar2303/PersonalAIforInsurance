"""Recovery API (Issue #8).

Minimal, safe interface for the terminal-status & recovery engine.

- ``POST /api/v1/recovery/decisions``: record an execution observation and get
  the deterministic ``RecoveryDecision`` (paused / retry / failover / terminal /
  handoff). Issue #8 CHOOSES the action; it never executes it.
- ``GET /api/v1/attempts/{attempt_id}``: one attempt record.
- ``GET /api/v1/route-plans/{plan_id}/attempts``: attempts for a plan.

Requests carry ids + observation metadata; responses carry safe attempt /
recovery metadata only - never applicant values.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException

from ..core.config import Settings, get_settings
from ..core.logging import clear_log_context, set_log_context
from ..core.tracing import run_config
from ..graph.recovery_workflow import WORKFLOW_NAME, build_recovery_workflow
from ..models.recovery import AttemptRecord, RecoveryDecideRequest, RecoveryDecision
from ..services.recovery import RecoveryEngine, get_recovery_engine, sanitize_recovery_context

router = APIRouter(prefix="/api/v1", tags=["recovery"])

logger = logging.getLogger(__name__)


def _engine_dep() -> RecoveryEngine:
    return get_recovery_engine()


@router.post(
    "/recovery/decisions",
    response_model=RecoveryDecision,
    summary="Record an execution observation and get the deterministic recovery decision",
)
async def decide(
    payload: RecoveryDecideRequest,
    engine: RecoveryEngine = Depends(_engine_dep),
    settings: Settings = Depends(get_settings),
) -> RecoveryDecision:
    request_id = uuid.uuid4().hex
    set_log_context(request_id=request_id, workflow=WORKFLOW_NAME, workflow_stage="decide")
    try:
        # Sanitize the caller context to a safe allowlist (LangSmith safety).
        safe_ctx = sanitize_recovery_context(payload.safe_context)
        safe_payload = payload.model_copy(update={"safe_context": safe_ctx})
        config = run_config(
            settings,
            request_id=request_id,
            workflow=WORKFLOW_NAME,
            extra_metadata={
                "planned_route_id": payload.planned_route_id,
                "registry_id": payload.registry_id,
                "observation_type": payload.observation_type,
                "workflow_stage": "decide",
            },
        )
        await build_recovery_workflow(engine).ainvoke(
            {
                "entry": "decide",
                "request_id": request_id,
                "plan_id": payload.plan_id,
                "planned_route_id": payload.planned_route_id,
                "registry_id": payload.registry_id,
                "distinct_rate_source_id": payload.distinct_rate_source_id,
                "intake_session_id": payload.intake_session_id,
                "observation_type": payload.observation_type,
                "reason": payload.reason,
                "observation_sequence": payload.observation_sequence,
                "plan_version": payload.plan_version,
                "safe_context": safe_ctx,
            },
            config=config,
        )
        return engine.record_observation(safe_payload)
    finally:
        clear_log_context()


@router.get(
    "/attempts/{attempt_id}",
    response_model=AttemptRecord,
    summary="Get one safe attempt record",
)
async def get_attempt(attempt_id: str, engine: RecoveryEngine = Depends(_engine_dep)) -> AttemptRecord:
    attempt = engine.get_attempt(attempt_id)
    if attempt is None:
        raise HTTPException(status_code=404, detail="attempt not found")
    return attempt


@router.get(
    "/route-plans/{plan_id}/attempts",
    response_model=list[AttemptRecord],
    summary="List safe attempt records for a route plan",
)
async def plan_attempts(plan_id: str, engine: RecoveryEngine = Depends(_engine_dep)) -> list[AttemptRecord]:
    return engine.list_attempts(plan_id)
