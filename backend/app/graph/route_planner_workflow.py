"""LangGraph route-planner workflow (Issue #6).

Safe orchestration around the deterministic ``RoutePlanner`` service. The graph
carries SAFE METADATA ONLY (``RoutePlanWorkflowState``): counts and registry
ids - never applicant values. The full ``RoutePlan`` (canonical paths + public
market data) is returned by the planner service, not placed in traced state.

    initialize -> product_gate -> plan_routes -> finalize
"""

from __future__ import annotations

from typing import Any, Optional

from langgraph.graph import END, START, StateGraph

from ..core.tracing import set_stage
from ..models.route_planner import RoutePlanWorkflowState
from ..services.route_planner import RoutePlanner, get_route_planner
from ..services.intake.engine import SessionNotFoundError

WORKFLOW_NAME = "route_planner_workflow"


def build_route_planner_workflow(planner: Optional[RoutePlanner] = None):
    """Build and compile the route-planner workflow graph.

    ``planner`` may be injected for tests; defaults to the app singleton.
    """
    resolved: RoutePlanner = planner or get_route_planner()

    def initialize(state: RoutePlanWorkflowState) -> dict[str, Any]:
        set_stage("initialize")
        session_id = state.get("session_id", "")
        try:
            session = resolved._profile_source.get_session(session_id)
        except SessionNotFoundError:
            return {"workflow_stage": "initialize", "workflow_status": "session_not_found", "message": "session not found"}
        return {
            "workflow_stage": "initialize",
            "workflow_status": "session_ok",
            "insurance_type": session.insurance_type.value,
        }

    def product_gate(state: RoutePlanWorkflowState) -> dict[str, Any]:
        set_stage("product_gate")
        session = resolved._profile_source.get_session(state.get("session_id", ""))
        if not resolved._profile_source.check_supported(session):
            return {
                "workflow_stage": "product_gate",
                "workflow_status": "product_not_applicable",
                "message": "route planning is AUTO-only in Issue #6",
            }
        return {"workflow_stage": "product_gate", "workflow_status": "product_ok"}

    def plan_routes(state: RoutePlanWorkflowState) -> dict[str, Any]:
        set_stage("plan_routes")
        plan = resolved.plan(state.get("session_id", ""))
        ready_ids = [route.registry_id for route in plan.routes if route.is_ready]
        return {
            "workflow_stage": "plan_routes",
            "workflow_status": "planned",
            "planned_route_count": len(plan.routes),
            "ready_route_count": plan.summary.ready_count,
            "blocked_route_count": plan.summary.blocked_count,
            "ready_registry_ids": ready_ids,
            "missing_field_path_count": plan.summary.missing_field_paths_count,
        }

    def finalize(state: RoutePlanWorkflowState) -> dict[str, Any]:
        set_stage("finalize")
        return {
            "workflow_stage": "finalize",
            "workflow_status": "complete",
            "message": "route plan ready",
        }

    def _after_initialize(state: RoutePlanWorkflowState) -> str:
        return "product_gate" if state.get("workflow_status") == "session_ok" else "END"

    def _after_product_gate(state: RoutePlanWorkflowState) -> str:
        return "plan_routes" if state.get("workflow_status") == "product_ok" else "END"

    builder = StateGraph(RoutePlanWorkflowState)
    builder.add_node("initialize", initialize)
    builder.add_node("product_gate", product_gate)
    builder.add_node("plan_routes", plan_routes)
    builder.add_node("finalize", finalize)

    builder.add_edge(START, "initialize")
    builder.add_conditional_edges("initialize", _after_initialize, {"product_gate": "product_gate", "END": END})
    builder.add_conditional_edges("product_gate", _after_product_gate, {"plan_routes": "plan_routes", "END": END})
    builder.add_edge("plan_routes", "finalize")
    builder.add_edge("finalize", END)

    return builder.compile()
