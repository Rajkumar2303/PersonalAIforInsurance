"""Demo endpoints.

- ``POST /api/v1/demo/workflow``: minimal LangGraph end-to-end (Issue 1).
- ``GET /api/v1/demo/personas/standard-auto``: canonical SYNTHETIC AUTO persona
  for populating the local web demo form. **Mock/dev only** - refused for
  live-mode usage. Never real applicant data.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ..core.config import Settings, get_settings
from ..core.logging import clear_log_context, set_log_context
from ..core.tracing import run_config
from ..demo.personas import standard_auto_persona
from ..graph.workflow import WORKFLOW_NAME, build_demo_workflow
from ..models.demo import (
    DemoEnvironmentStatus,
    DemoWorkflowRequest,
    DemoWorkflowResponse,
)

router = APIRouter(prefix="/api/v1", tags=["demo"])

logger = logging.getLogger(__name__)


@router.get(
    "/demo/env",
    response_model=DemoEnvironmentStatus,
    summary="Demo environment check (DEMO vs OPTIONAL/LIVE requirements)",
)
async def demo_env(
    settings: Settings = Depends(get_settings),
) -> DemoEnvironmentStatus:
    """Lightweight startup/environment check.

    The demo mode NEVER requires external credentials (no OpenAI key, no
    telephony, no cloud Postgres, no live insurer credentials). Presence flags
    below are booleans only - never secrets/values.
    """
    from ..services.market_registry import get_market_registry_service

    live_providers = len(get_market_registry_service().verified_records()) > 0
    runtime = None
    mock_url: str | None = None
    if settings.mock_site_enabled:
        try:
            from ..demo.runtime import get_demo_runtime

            runtime = get_demo_runtime().start_mock_site()
            mock_url = runtime.base_url
        except Exception:  # pragma: no cover - port conflict; report not-ready
            mock_url = None
    return DemoEnvironmentStatus(
        demo_ready=settings.mock_site_enabled,
        demo_requires_external_credentials=False,
        mock_site_enabled=settings.mock_site_enabled,
        mock_site_url=mock_url,
        comparison_max_concurrency=settings.comparison_max_concurrency,
        comparison_route_timeout_seconds=settings.comparison_route_timeout_seconds,
        database_configured=bool(settings.database_url),
        langsmith_configured=bool(settings.langsmith_api_key),
        llm_configured=bool(settings.llm_api_key),
        live_providers_configured=live_providers,
    )


@router.get(
    "/demo/personas/standard-auto",
    response_model=dict[str, Any],
    summary="Synthetic standard AUTO persona (canonical path -> value) for the local demo form",
)
async def standard_auto_persona_endpoint(
    mode: str = Query(default="mock", description="execution mode; only 'mock' is permitted"),
) -> dict[str, Any]:
    """Return canonical_path/value pairs to populate the local demo form.

    Enabled only in dev/mock mode; refused for live-mode usage. Values are
    clearly synthetic (never real applicant data) and are consumed by the
    frontend's in-memory state only - never persisted to localStorage.
    """
    if mode != "mock":
        raise HTTPException(
            status_code=403,
            detail="demo persona is unavailable for live-mode usage",
        )
    return standard_auto_persona()


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
