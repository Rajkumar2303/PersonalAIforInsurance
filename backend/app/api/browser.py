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
from ..models.normalization import NormalizedQuoteView
from ..services.evidence import EvidenceService, get_evidence_service
from ..services.normalization import (
    QuoteNormalizationError,
    get_quote_normalization_service,
)
from ..services.normalization.service import _quote_view

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


class CheckpointApprovalRequest(BaseModel):
    """Explicit participant approval of one resumable HUMAN checkpoint.

    Carries ONLY the checkpoint kind (e.g. ``identity_lookup``) - never any
    applicant value. MUST-NOT-AUTOMATE checkpoints are rejected by the
    manager; the automation must never perform those actions.
    """

    checkpoint_type: str


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


@router.post("/browser/sessions/{session_id}/approve-checkpoint", response_model=BrowserSession,
             summary="Explicitly approve a resumable human checkpoint (e.g. identity_lookup before licence submission)")
async def approve_checkpoint(session_id: str, payload: CheckpointApprovalRequest,
                             manager: BrowserSessionManager = Depends(_manager_dep)) -> BrowserSession:
    try:
        manager.get(session_id)
    except Exception:
        raise _404("browser session not found")
    request_id = uuid.uuid4().hex
    set_log_context(request_id=request_id, workflow=WORKFLOW_NAME, workflow_stage="approve_checkpoint")
    try:
        try:
            return manager.approve_checkpoint(session_id, payload.checkpoint_type)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    finally:
        clear_log_context()


@router.get("/browser/sessions/{session_id}/quote", response_model=NormalizedQuoteView,
            summary="Retrieve the completed Sonnet result (normalized LIVE quote) for a direct browser session")
async def session_quote(
    session_id: str,
    manager: BrowserSessionManager = Depends(_manager_dep),
    evidence: EvidenceService = Depends(get_evidence_service),
    normalization: QuoteNormalizationService = Depends(get_quote_normalization_service),
) -> NormalizedQuoteView:
    """Narrow Sonnet-live adapter: returns the normalized quote for a direct
    browser session that ended with an explicit quote result.

    Reuses the existing pipeline - extraction (RawQuoteObservation persisted to
    the evidence store by the session manager), evidence (QuoteObservation),
    and normalization (QuoteNormalizationService) - and returns only the safe
    API projection. No applicant values, no raw references. Never proceeds
    toward payment/purchase/binding (the browser is closed after the result).
    """
    try:
        session = manager.get(session_id)
    except Exception:
        raise _404("browser session not found")
    step = manager.last_result(session_id)
    has_quote = bool(
        step and step.observation and step.observation.quote and step.observation.quote.quote_present
    )
    if not has_quote:
        raise _404("no quote result for this browser session")
    quotes = await evidence.list_quote_observations(
        session.intake_session_id or "", session.attempt_id
    )
    if not quotes:
        raise _404("quote evidence was not persisted for this attempt")
    try:
        normalized = await normalization.normalize(
            session.intake_session_id or "", quotes[-1].quote_id
        )
    except QuoteNormalizationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _quote_view(normalized)


@router.delete("/browser/sessions/{session_id}", response_model=BrowserSession,
               summary="Close a browser session (clean browser lifecycle)")
async def delete_session(session_id: str, manager: BrowserSessionManager = Depends(_manager_dep)) -> BrowserSession:
    try:
        return await manager.close(session_id)
    except Exception:
        raise _404("browser session not found")
