"""LangGraph intake workflow (Issue #5).

Orchestrates the progressive intake with explicit state transitions:

    advance: initialize -> product_gate -> consent_gate ->
             determine_needed_fields -> select_next_field -> await_user_input
    submit:  validate_answer -> (valid? update_profile : retry) ->
             select_next_field -> await_user_input

The graph carries SAFE METADATA ONLY (``IntakeWorkflowState``) - never a full
profile and never raw applicant values. Raw answers for the submit path are
passed through a ``contextvars``-backed inbox (set by the API, consumed by the
validate/update nodes, cleared afterwards) so they never enter trace-visible
state. Deterministic decisions (missing field, already-known, validation,
consent, sensitivity, path) all live in the engine/schema/catalog - no LLM.
"""

from __future__ import annotations

import contextvars
from typing import Any, Callable, Optional

from langgraph.graph import END, START, StateGraph

from ..core.tracing import set_stage
from ..models.intake.workflow import IntakeWorkflowState
from ..services.intake.engine import IntakeEngine, SessionNotFoundError
from ..services.intake import get_intake_engine

WORKFLOW_NAME = "intake_workflow"
ENTRY_ADVANCE = "advance"
ENTRY_SUBMIT = "submit"

# Raw-answer inbox: (session_id, canonical_path, value). Set by the API before
# invoking the submit graph; consumed by validate/update nodes; never in state.
_pending_answer: contextvars.ContextVar[Optional[dict[str, Any]]] = contextvars.ContextVar(
    "intake_pending_answer", default=None
)


def set_pending_answer(session_id: str, canonical_path: str, value: Any) -> None:
    _pending_answer.set({"session_id": session_id, "canonical_path": canonical_path, "value": value})


def clear_pending_answer() -> None:
    _pending_answer.set(None)


def _pending() -> Optional[dict[str, Any]]:
    return _pending_answer.get()


def _route_entry(state: IntakeWorkflowState) -> str:
    return ENTRY_SUBMIT if state.get("entry") == ENTRY_SUBMIT else ENTRY_ADVANCE


def _after_initialize(state: IntakeWorkflowState) -> str:
    return "product_gate" if state.get("workflow_status") != "session_not_found" else "END"


def _after_product_gate(state: IntakeWorkflowState) -> str:
    return "consent_gate" if state.get("workflow_status") == "product_ok" else "END"


def _after_consent_gate(state: IntakeWorkflowState) -> str:
    return "determine_needed_fields" if state.get("workflow_status") == "consent_ok" else "END"


def _after_validate(state: IntakeWorkflowState) -> str:
    return "update_profile" if state.get("validation_success") else "retry"


def build_intake_workflow(engine: Optional[IntakeEngine] = None):
    """Build and compile the intake workflow graph.

    ``engine`` may be injected for tests; defaults to the app singleton.
    """
    resolved: IntakeEngine = engine or get_intake_engine()

    def initialize(state: IntakeWorkflowState) -> dict[str, Any]:
        set_stage("initialize")
        session_id = state.get("session_id", "")
        try:
            session = resolved.get_session(session_id)
        except SessionNotFoundError:
            return {
                "workflow_stage": "initialize",
                "workflow_status": "session_not_found",
                "message": "session not found",
            }
        return {
            "workflow_stage": "initialize",
            "workflow_status": session.status.value,
            "profile_id": session.profile_id,
            "insurance_type": session.insurance_type.value,
        }

    def product_gate(state: IntakeWorkflowState) -> dict[str, Any]:
        set_stage("product_gate")
        session = resolved.get_session(state.get("session_id", ""))
        if not resolved.check_supported(session):
            return {
                "workflow_stage": "product_gate",
                "workflow_status": "product_rejected",
                "message": "product not implemented",
            }
        return {"workflow_stage": "product_gate", "workflow_status": "product_ok"}

    def consent_gate(state: IntakeWorkflowState) -> dict[str, Any]:
        set_stage("consent_gate")
        session = resolved.get_session(state.get("session_id", ""))
        if not resolved.has_collection_consent(session):
            return {
                "workflow_stage": "consent_gate",
                "workflow_status": "consent_pending",
                "message": "collection consent required",
                "consent_scope": "collection",
            }
        return {"workflow_stage": "consent_gate", "workflow_status": "consent_ok"}

    def determine_needed_fields(state: IntakeWorkflowState) -> dict[str, Any]:
        set_stage("determine_needed_fields")
        session = resolved.get_session(state.get("session_id", ""))
        missing, completed = resolved.compute_missing_counts(session)
        return {
            "workflow_stage": "determine_needed_fields",
            "missing_field_count": missing,
            "completed_field_count": completed,
        }

    def select_next_field(state: IntakeWorkflowState) -> dict[str, Any]:
        set_stage("select_next_field")
        session = resolved.get_session(state.get("session_id", ""))
        field_id, path = resolved.select_next(session)
        if field_id is None:
            return {
                "workflow_stage": "select_next_field",
                "workflow_status": session.status.value,
                "current_field_id": None,
                "current_canonical_path": None,
            }
        return {
            "workflow_stage": "select_next_field",
            "workflow_status": "awaiting_input",
            "current_field_id": field_id,
            "current_canonical_path": path,
        }

    def await_user_input(state: IntakeWorkflowState) -> dict[str, Any]:
        set_stage("await_user_input")
        field_id = state.get("current_field_id")
        if field_id is None:
            return {
                "workflow_stage": "await_user_input",
                "workflow_status": state.get("workflow_status", "intake_ready"),
                "message": state.get("message") or "no question available",
            }
        question = resolved.question_payload_for(field_id)
        return {
            "workflow_stage": "await_user_input",
            "workflow_status": "awaiting_input",
            "message": question.question if question else None,
            "current_field_id": field_id,
            "current_canonical_path": question.canonical_path if question else None,
        }

    def validate_answer(state: IntakeWorkflowState) -> dict[str, Any]:
        set_stage("validate_answer")
        pending = _pending()
        if pending is None:
            return {
                "workflow_stage": "validate_answer",
                "validation_success": False,
                "last_error": "no pending answer",
            }
        try:
            result = resolved.validate_value(pending["session_id"], pending["canonical_path"], pending["value"])
        except SessionNotFoundError:
            return {"workflow_stage": "validate_answer", "workflow_status": "session_not_found"}
        return {
            "workflow_stage": "validate_answer",
            "validation_success": result["validation_success"],
            "field_id": result["field_id"],
            "canonical_path": result["canonical_path"],
            "last_error": result["error_message"],
        }

    def update_profile(state: IntakeWorkflowState) -> dict[str, Any]:
        set_stage("update_profile")
        pending = _pending()
        if pending is None:
            return {"workflow_stage": "update_profile", "validation_success": False, "last_error": "no pending answer"}
        try:
            submit = resolved.submit_answer(pending["session_id"], pending["canonical_path"], pending["value"])
        except SessionNotFoundError:
            return {"workflow_stage": "update_profile", "workflow_status": "session_not_found"}
        return {
            "workflow_stage": "update_profile",
            "validation_success": submit.validation_success,
            "field_id": submit.field_id,
            "canonical_path": submit.canonical_path,
            "workflow_status": submit.workflow_status,
            "last_error": submit.error_message,
        }

    def intake_ready(state: IntakeWorkflowState) -> dict[str, Any]:
        set_stage("intake_ready")
        return {"workflow_stage": "intake_ready", "workflow_status": "intake_ready", "message": "intake complete"}

    builder = StateGraph(IntakeWorkflowState)
    builder.add_node("initialize", initialize)
    builder.add_node("product_gate", product_gate)
    builder.add_node("consent_gate", consent_gate)
    builder.add_node("determine_needed_fields", determine_needed_fields)
    builder.add_node("select_next_field", select_next_field)
    builder.add_node("await_user_input", await_user_input)
    builder.add_node("validate_answer", validate_answer)
    builder.add_node("update_profile", update_profile)
    builder.add_node("intake_ready", intake_ready)

    builder.add_conditional_edges(
        START,
        _route_entry,
        {"submit": "validate_answer", "advance": "initialize"},
    )
    builder.add_conditional_edges(
        "initialize",
        _after_initialize,
        {"product_gate": "product_gate", "END": END},
    )
    builder.add_conditional_edges(
        "product_gate",
        _after_product_gate,
        {"consent_gate": "consent_gate", "END": END},
    )
    builder.add_conditional_edges(
        "consent_gate",
        _after_consent_gate,
        {"determine_needed_fields": "determine_needed_fields", "END": END},
    )
    builder.add_edge("determine_needed_fields", "select_next_field")
    builder.add_edge("select_next_field", "await_user_input")
    builder.add_edge("await_user_input", END)

    builder.add_conditional_edges(
        "validate_answer",
        _after_validate,
        {"update_profile": "update_profile", "retry": "select_next_field"},
    )
    builder.add_edge("update_profile", "select_next_field")

    return builder.compile()


def _run_intake_graph(
    engine: Optional[IntakeEngine],
    state: dict[str, Any],
    config: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    graph = build_intake_workflow(engine)
    result = graph.invoke(state, config=config or {})
    return dict(result)
