"""Browser agent API (Issue #7).

Minimal, safe interface for browser sessions. Requests reference
``plan_id`` / ``planned_route_id`` / ``execution_mode`` - NEVER a full
``InsuranceProfile``. Responses carry safe session metadata + observations,
never filled applicant values. Browser start returns a structured refusal
when the route is not executable.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..browser import BrowserSessionManager, get_browser_manager
from ..core.config import Settings, get_settings
from ..core.logging import clear_log_context, set_log_context
from ..core.tracing import run_config
from ..graph.browser_workflow import WORKFLOW_NAME, build_browser_workflow
from ..models.browser.session import (
    BrowserExecutionMode,
    BrowserSession,
    BrowserStartRefusal,
    BrowserStepResult,
    LiveExecutionGate,
)

router = APIRouter(prefix="/api/v1", tags=["browser"])

logger = logging.getLogger(__name__)


def _manager_dep() -> BrowserSessionManager:
    return get_browser_manager()


class StartSessionRequest(BaseModel):
    intake_session_id: str
    planned_route_id: str
    execution_mode: BrowserExecutionMode = BrowserExecutionMode.SANDBOX
    plan_id: Optional[str] = None
    live_gate: Optional[LiveExecutionGate] = None


class BrowserStartResponse(BaseModel):
    started: bool
    session: Optional[BrowserSession] = None
    refusal: Optional[BrowserStartRefusal] = None


class BrowserRunResponse(BaseModel):
    session: BrowserSession
    step: Optional[BrowserStepResult] = None


def _404(detail: str) -> HTTPException:
    return HTTPException(status_code=404, detail=detail)


@router.post("/browser/sessions", response_model=BrowserStartResponse,
             summary="Start a browser session for a ready web route (or a structured refusal)")
async def start_session(payload: StartSessionRequest, manager: BrowserSessionManager = Depends(_manager_dep)) -> BrowserStartResponse:
    request_id = uuid.uuid4().hex
    set_log_context(request_id=request_id, workflow=WORKFLOW_NAME, workflow_stage="create")
    try:
        result = manager.create(
            intake_session_id=payload.intake_session_id,
            planned_route_id=payload.planned_route_id,
            execution_mode=payload.execution_mode,
            live_gate=payload.live_gate,
            plan_id=payload.plan_id,
        )
        if isinstance(result, BrowserSession):
            return BrowserStartResponse(started=True, session=result)
        return BrowserStartResponse(started=False, refusal=result)
    finally:
        clear_log_context()


@router.get("/browser/sessions/{session_id}", response_model=BrowserSession,
            summary="Get a browser session (safe metadata only)")
async def get_session(session_id: str, manager: BrowserSessionManager = Depends(_manager_dep)) -> BrowserSession:
    try:
        return manager.get(session_id)
    except Exception:
        raise _404("browser session not found")


@router.post("/browser/sessions/{session_id}/run", response_model=BrowserRunResponse,
             summary="Run the browser workflow for a session (bounded loop)")
async def run_session(session_id: str, manager: BrowserSessionManager = Depends(_manager_dep),
                      settings: Settings = Depends(get_settings)) -> BrowserRunResponse:
    try:
        manager.get(session_id)
    except Exception:
        raise _404("browser session not found")
    request_id = uuid.uuid4().hex
    set_log_context(request_id=request_id, workflow=WORKFLOW_NAME, workflow_stage="run")
    try:
        config = run_config(
            settings,
            request_id=request_id,
            workflow=WORKFLOW_NAME,
            extra_metadata={"browser_session_id": session_id, "workflow_stage": "run"},
        )
        await build_browser_workflow(manager).ainvoke(
            {"entry": "run", "browser_session_id": session_id}, config=config
        )
        return BrowserRunResponse(session=manager.get(session_id), step=manager.last_result(session_id))
    finally:
        clear_log_context()


@router.post("/browser/sessions/{session_id}/resume", response_model=BrowserRunResponse,
             summary="Resume a paused browser session (same page, no restart)")
async def resume_session(session_id: str, manager: BrowserSessionManager = Depends(_manager_dep),
                         settings: Settings = Depends(get_settings)) -> BrowserRunResponse:
    try:
        manager.get(session_id)
    except Exception:
        raise _404("browser session not found")
    request_id = uuid.uuid4().hex
    set_log_context(request_id=request_id, workflow=WORKFLOW_NAME, workflow_stage="resume")
    try:
        config = run_config(
            settings,
            request_id=request_id,
            workflow=WORKFLOW_NAME,
            extra_metadata={"browser_session_id": session_id, "workflow_stage": "resume"},
        )
        await build_browser_workflow(manager).ainvoke(
            {"entry": "resume", "browser_session_id": session_id}, config=config
        )
        return BrowserRunResponse(session=manager.get(session_id), step=manager.last_result(session_id))
    finally:
        clear_log_context()


@router.delete("/browser/sessions/{session_id}", response_model=BrowserSession,
               summary="Close a browser session (clean browser lifecycle)")
async def delete_session(session_id: str, manager: BrowserSessionManager = Depends(_manager_dep)) -> BrowserSession:
    try:
        return await manager.close(session_id)
    except Exception:
        raise _404("browser session not found")
