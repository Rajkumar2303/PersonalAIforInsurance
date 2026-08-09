"""Demo endpoint that runs the minimal LangGraph workflow end-to-end.

Used to verify LangGraph execution and LangSmith tracing without any
insurance-specific logic.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException

from ..core.config import Settings, get_settings
from ..core.logging import clear_log_context, set_log_context
from ..core.tracing import run_config
from ..graph.workflow import WORKFLOW_NAME, build_demo_workflow
from ..models.demo import DemoWorkflowRequest, DemoWorkflowResponse

router = APIRouter(prefix="/api/v1", tags=["demo"])

logger = logging.getLogger(__name__)


@router.post(
    "/demo/workflow",
    response_model=DemoWorkflowResponse,
    summary="Execute the minimal demo LangGraph workflow",
)
async def run_demo_workflow(
    payload: DemoWorkflowRequest,
    settings: Settings = Depends(get_settings),
) -> DemoWorkflowResponse:
    """Run ``stage_one -> stage_two`` and return the traced result.

    A ``request_id`` is generated and propagated through the workflow
    metadata so the HTTP request correlates with the LangSmith trace.
    """
    request_id = uuid.uuid4().hex
    set_log_context(
        request_id=request_id,
        workflow=WORKFLOW_NAME,
        workflow_stage="start",
        status="running",
    )

    graph = build_demo_workflow()
    config = run_config(settings, request_id=request_id, workflow=WORKFLOW_NAME)
    try:
        result = await graph.ainvoke({"input_text": payload.input_text}, config=config)
        set_log_context(workflow_stage=result.get("stage", "done"), status="success")
        return DemoWorkflowResponse(
            request_id=request_id,
            workflow=WORKFLOW_NAME,
            stages=result.get("steps", []),
            final_output=result.get("final_output", ""),
            status="success",
        )
    except Exception as exc:  # pragma: no cover - defensive
        set_log_context(status="error", error_type=type(exc).__name__)
        logger.exception(
            "demo workflow failed",
            extra={"workflow": WORKFLOW_NAME, "status": "error", "error_type": type(exc).__name__},
        )
        raise HTTPException(
            status_code=500,
            detail={"status": "error", "request_id": request_id, "error_type": type(exc).__name__},
        ) from exc
    finally:
        clear_log_context()
