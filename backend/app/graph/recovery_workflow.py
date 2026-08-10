"""LangGraph recovery workflow (Issue #8).

Safe orchestration around the deterministic ``RecoveryEngine``. The graph
carries SAFE METADATA ONLY (``RecoveryWorkflowState``): ids, counts, reason /
action / status strings - never applicant values.

    initialize -> load_attempt_history -> classify_observation -> decide -> END

Generic nodes only - never one node per observation type. Adding a new
observation is a localized classification-table entry, not a topology change.
"""

from __future__ import annotations

from typing import Any, Optional

from langgraph.graph import END, START, StateGraph

from ..core.tracing import set_stage
from ..models.recovery import (
    ExecutionObservation,
    RecoveryDecideRequest,
    RecoveryWorkflowState,
)
from ..services.recovery import (
    RecoveryEngine,
    classify_observation,
    get_recovery_engine,
    sanitize_recovery_context,
)

WORKFLOW_NAME = "recovery_workflow"


def build_recovery_workflow(engine: Optional[RecoveryEngine] = None):
    """Build and compile the recovery workflow graph.

    ``engine`` may be injected for tests; defaults to the app singleton.
    """
    resolved: RecoveryEngine = engine or get_recovery_engine()

    def _request(state: RecoveryWorkflowState) -> RecoveryDecideRequest:
        return RecoveryDecideRequest(
            plan_id=state.get("plan_id"),
            planned_route_id=state.get("planned_route_id", ""),
            registry_id=state.get("registry_id"),
            distinct_rate_source_id=state.get("distinct_rate_source_id"),
            intake_session_id=state.get("intake_session_id"),
            source_channel="browser",  # recovery is invoked from browser today
            observation_type=state.get("observation_type", ""),
            reason=state.get("reason"),
            observation_sequence=state.get("observation_sequence"),
            plan_version=state.get("plan_version"),
            safe_context=sanitize_recovery_context(state.get("safe_context")),
        )

    def initialize(state: RecoveryWorkflowState) -> dict[str, Any]:
        set_stage("initialize")
        if not state.get("planned_route_id") or not state.get("observation_type"):
            return {
                "workflow_stage": "initialize",
                "workflow_status": "invalid_request",
                "message": "planned_route_id and observation_type are required",
            }
        # Replace any caller-provided context with the safe allowlist so the
        # traced state can never carry an un-allowlisted raw payload.
        return {
            "workflow_stage": "initialize",
            "workflow_status": "ok",
            "safe_context": sanitize_recovery_context(state.get("safe_context")),
        }

    def load_attempt_history(state: RecoveryWorkflowState) -> dict[str, Any]:
        set_stage("load_attempt_history")
        request = _request(state)
        attempt = resolved.resolve_current_attempt(request)
        registry_id = request.registry_id or attempt.registry_id or request.planned_route_id
        rs_id = request.distinct_rate_source_id or attempt.distinct_rate_source_id
        route_used = resolved.route_attempts_used(request.plan_id, registry_id)
        policy = resolved.policy_for(request)
        return {
            "workflow_stage": "load_attempt_history",
            "workflow_status": "ok",
            "attempts_used": route_used,
            "attempts_remaining": max(0, policy.max_attempts_per_route - route_used),
            "reason_codes": [],
        }

    def classify_node(state: RecoveryWorkflowState) -> dict[str, Any]:
        set_stage("classify_observation")
        request = _request(state)
        execution = ExecutionObservation(
            source_channel="browser",
            observation_type=request.observation_type,
            reason=request.reason,
            safe_context=dict(request.safe_context or {}),
        )
        classified = classify_observation(execution, resolved.policy_for(request))
        return {
            "workflow_stage": "classify_observation",
            "workflow_status": "classified",
            "execution_result_kind": classified.execution_result_kind.value,
            "retryability": classified.retryability.value,
            "reason_codes": classified.reason_codes,
        }

    def decide_node(state: RecoveryWorkflowState) -> dict[str, Any]:
        set_stage("decide")
        request = _request(state)
        decision = resolved.decide(request)
        return {
            "workflow_stage": "decide",
            "workflow_status": "decided",
            "decision_id": decision.decision_id,
            "lifecycle_status": decision.lifecycle_status.value,
            "recommended_action": decision.recommended_action.value,
            "reason_codes": decision.reason_codes,
            "retry_allowed": decision.retry_allowed,
            "attempts_used": decision.attempts_used,
            "attempts_remaining": decision.attempts_remaining,
            "alternative_route_id": decision.alternative_route_id,
            "terminal_status": decision.terminal_status.value if decision.terminal_status else None,
            "quote_pending_normalization": decision.quote_pending_normalization,
        }

    def _after_initialize(state: RecoveryWorkflowState) -> str:
        return "load_attempt_history" if state.get("workflow_status") == "ok" else "END"

    builder = StateGraph(RecoveryWorkflowState)
    builder.add_node("initialize", initialize)
    builder.add_node("load_attempt_history", load_attempt_history)
    builder.add_node("classify_observation", classify_node)
    builder.add_node("decide", decide_node)

    builder.add_edge(START, "initialize")
    builder.add_conditional_edges("initialize", _after_initialize, {"load_attempt_history": "load_attempt_history", "END": END})
    builder.add_edge("load_attempt_history", "classify_observation")
    builder.add_edge("classify_observation", "decide")
    builder.add_edge("decide", END)

    return builder.compile()
