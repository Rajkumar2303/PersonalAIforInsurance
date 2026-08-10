"""LangGraph browser workflow (Issue #7).

Safe orchestration around the deterministic ``BrowserExecutor``. The graph
carries SAFE METADATA ONLY (``BrowserWorkflowState``): ids, registry id,
canonical field paths, counts, page signature, action/observation/checkpoint
types, and a sanitized URL - never applicant values.

    initialize -> validate_route -> (run: launch | resume: step) -> step loop
        -> END when paused/succeeded/stopped/failed or max_steps reached
"""

from __future__ import annotations

from typing import Any, Optional

from langgraph.graph import END, START, StateGraph

from ..browser import BrowserSessionManager, get_browser_manager
from ..core.config import get_settings
from ..core.tracing import set_stage
from ..models.browser.session import BrowserSessionStatus
from ..models.browser.workflow import BrowserWorkflowState

WORKFLOW_NAME = "browser_workflow"

_TERMINAL_STATUSES = {
    BrowserSessionStatus.PAUSED_NEEDS_FIELD,
    BrowserSessionStatus.PAUSED_NEEDS_CONSENT,
    BrowserSessionStatus.PAUSED_HUMAN_CHECKPOINT,
    BrowserSessionStatus.PAUSED_UNKNOWN_FIELD,
    BrowserSessionStatus.PAUSED_VALUE_NOT_SUPPORTED,
    BrowserSessionStatus.PAUSED_VALIDATION_ERROR,
    BrowserSessionStatus.PAUSED_AMBIGUOUS,
    BrowserSessionStatus.SUCCEEDED,
    BrowserSessionStatus.STOPPED_ACCESS_CONTROL,
    BrowserSessionStatus.STOPPED_HUMAN_CHECKPOINT,
    BrowserSessionStatus.STOPPED_PROHIBITED,
    BrowserSessionStatus.STOPPED_UNEXPECTED_HOST,
    BrowserSessionStatus.FAILED,
    BrowserSessionStatus.CLOSED,
}


def build_browser_workflow(manager: Optional[BrowserSessionManager] = None):
    """Build and compile the browser workflow graph.

    ``manager`` may be injected for tests; defaults to the app singleton.
    """
    resolved: BrowserSessionManager = manager or get_browser_manager()

    def initialize(state: BrowserWorkflowState) -> dict[str, Any]:
        set_stage("initialize")
        return {"workflow_stage": "initialize", "workflow_status": "ok"}

    def validate_route(state: BrowserWorkflowState) -> dict[str, Any]:
        set_stage("validate_route")
        session_id = state.get("browser_session_id", "")
        try:
            session = resolved.get(session_id)
        except Exception:
            return {"workflow_stage": "validate_route", "workflow_status": "session_not_found",
                    "message": "browser session not found"}
        return {
            "workflow_stage": "validate_route",
            "workflow_status": "session_ok",
            "registry_id": session.registry_id,
            "execution_mode": session.execution_mode.value,
            "max_steps": state.get("max_steps") or get_settings().browser_max_steps,
        }

    async def launch(state: BrowserWorkflowState) -> dict[str, Any]:
        set_stage("launch")
        result = await resolved.start_session(state.get("browser_session_id", ""))
        return _result_to_state(result)

    async def browser_step(state: BrowserWorkflowState) -> dict[str, Any]:
        set_stage("browser_step")
        result = await resolved.step_session(state.get("browser_session_id", ""))
        return _result_to_state(result)

    def _result_to_state(result: Any) -> dict[str, Any]:
        status = result.status.value
        obs = result.observation
        return {
            "workflow_stage": "browser_step",
            "workflow_status": status,
            "current_step": result.step,
            "observation_type": result.observation_type.value,
            "page_signature": result.page_signature or (obs.page_signature if obs else None),
            "current_url": obs.url if obs else None,
            "filled_field_count": result.filled_field_count,
            "missing_field_count": result.missing_field_count,
            "unknown_field_count": result.unknown_field_count,
            "pending_field_paths": obs.pending_field_paths if obs else [],
            "checkpoint_type": obs.checkpoint.checkpoint_type if obs and obs.checkpoint else None,
            "quote_present": bool(obs and obs.quote and obs.quote.quote_present),
            "reference_present": bool(obs and obs.quote and obs.quote.reference_present),
            "message": result.message,
        }

    def _after_validate(state: BrowserWorkflowState) -> str:
        if state.get("workflow_status") != "session_ok":
            return "END"
        return "browser_step" if state.get("entry") == "resume" else "launch"

    def _after_launch(state: BrowserWorkflowState) -> str:
        status = state.get("workflow_status")
        if status in {s.value for s in _TERMINAL_STATUSES}:
            return "END"
        if (state.get("current_step") or 0) >= (state.get("max_steps") or get_settings().browser_max_steps):
            return "END"
        return "browser_step"

    def _after_step(state: BrowserWorkflowState) -> str:
        status = state.get("workflow_status")
        if status in {s.value for s in _TERMINAL_STATUSES}:
            return "END"
        if (state.get("current_step") or 0) >= (state.get("max_steps") or get_settings().browser_max_steps):
            return "END"
        return "browser_step"

    builder = StateGraph(BrowserWorkflowState)
    builder.add_node("initialize", initialize)
    builder.add_node("validate_route", validate_route)
    builder.add_node("launch", launch)
    builder.add_node("browser_step", browser_step)

    builder.add_edge(START, "initialize")
    builder.add_edge("initialize", "validate_route")
    builder.add_conditional_edges("validate_route", _after_validate, {"launch": "launch", "browser_step": "browser_step", "END": END})
    builder.add_conditional_edges("launch", _after_launch, {"browser_step": "browser_step", "END": END})
    builder.add_conditional_edges("browser_step", _after_step, {"browser_step": "browser_step", "END": END})

    return builder.compile()
