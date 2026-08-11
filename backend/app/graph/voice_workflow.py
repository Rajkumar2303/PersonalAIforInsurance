"""LangGraph voice workflow (Issue #9, Prompt 1).

Safe orchestration around the deterministic ``VoiceEngine``. The graph
carries SAFE METADATA ONLY (``VoiceWorkflowState``): ids, registry id,
canonical field paths, status/action strings, and a sanitized safe-context -
never applicant values.

    prepare_handoff -> automation_disclosure -> receive_broker_event
        -> classify_and_respond -> emit_observation -> END

Every node annotates its stage for LangSmith. Terminal/retry/failover/handoff
decisions still belong to the Issue #8 recovery engine (the ``emit_observation``
node delegates to it) - never ``quoted_comparable`` / ``quoted_non_comparable``.
"""

from __future__ import annotations

from typing import Any, Optional

from langgraph.graph import END, START, StateGraph

from ..core.tracing import set_stage
from ..models.voice import BrokerQuestion, VoiceWorkflowState
from ..services.voice import VoiceEngine, get_voice_engine

WORKFLOW_NAME = "voice_workflow"


def build_voice_workflow(engine: Optional[VoiceEngine] = None):
    """Build and compile the voice workflow graph.

    ``engine`` may be injected for tests; defaults to the app singleton.
    """
    resolved: VoiceEngine = engine or get_voice_engine()

    def initialize(state: VoiceWorkflowState) -> dict[str, Any]:
        set_stage("initialize")
        return {"workflow_stage": "initialize", "workflow_status": "ok"}

    def prepare_handoff(state: VoiceWorkflowState) -> dict[str, Any]:
        set_stage("prepare_handoff")
        context = state.get("handoff_context")
        session = resolved.prepare_handoff(context)
        return {
            "workflow_stage": "prepare_handoff",
            "workflow_status": "prepared",
            "voice_session_id": session.voice_session_id,
            "registry_id": session.registry_id,
            "distinct_rate_source_id": session.distinct_rate_source_id,
            "planned_route_id": session.planned_route_id,
            "intake_session_id": session.intake_session_id,
            "lifecycle_status": session.lifecycle_status,
            "disclosure_status": session.disclosure_status,
        }

    def automation_disclosure(state: VoiceWorkflowState) -> dict[str, Any]:
        set_stage("automation_disclosure")
        voice_session_id = state.get("voice_session_id", "")
        granted = bool(state.get("disclosure_granted", True))
        session = resolved.disclose_automation(voice_session_id, granted=granted)
        return {
            "workflow_stage": "automation_disclosure",
            "workflow_status": "ok",
            "lifecycle_status": session.lifecycle_status,
            "disclosure_status": session.disclosure_status,
        }

    def receive_broker_event(state: VoiceWorkflowState) -> dict[str, Any]:
        set_stage("receive_broker_event")
        voice_session_id = state.get("voice_session_id", "")
        question = BrokerQuestion(**state.get("broker_question", {}))
        return {
            "workflow_stage": "receive_broker_event",
            "workflow_status": "ok",
            "broker_question_kind": question.kind.value,
            "canonical_path": question.canonical_path,
        }

    def classify_and_respond(state: VoiceWorkflowState) -> dict[str, Any]:
        set_stage("classify_and_respond")
        voice_session_id = state.get("voice_session_id", "")
        question = BrokerQuestion(**state.get("broker_question", {}))
        decision = resolved.receive_broker_event(voice_session_id, question)
        session = resolved.get(voice_session_id)
        return {
            "workflow_stage": "classify_and_respond",
            "workflow_status": "ok",
            "action": decision.action,
            "lifecycle_status": decision.lifecycle_status,
            "disclosure_status": decision.disclosure_status,
            "canonical_path": decision.canonical_path,
            "checkpoint_kind": decision.checkpoint_kind,
            "value_present": decision.value_present,
            "route_status": session.route_status,
        }

    def emit_observation(state: VoiceWorkflowState) -> dict[str, Any]:
        set_stage("emit_observation")
        voice_session_id = state.get("voice_session_id", "")
        otype = state.get("observation_type", "")
        reason = state.get("reason")
        recovery = resolved.emit_observation(
            voice_session_id,
            otype,
            reason=reason,
            extra_safe_context=state.get("safe_context"),
        )
        session = resolved.get(voice_session_id)
        return {
            "workflow_stage": "emit_observation",
            "workflow_status": "ok",
            "observation_type": otype,
            "route_status": session.route_status,
            "recovery_recommended_action": recovery.recommended_action.value,
            "recovery_lifecycle_status": recovery.lifecycle_status.value,
            "recovery_terminal_status": (
                recovery.terminal_status.value if recovery.terminal_status else None
            ),
            "quote_pending_normalization": recovery.quote_pending_normalization,
        }

    graph = StateGraph(VoiceWorkflowState)
    graph.add_node("initialize", initialize)
    graph.add_node("prepare_handoff", prepare_handoff)
    graph.add_node("automation_disclosure", automation_disclosure)
    graph.add_node("receive_broker_event", receive_broker_event)
    graph.add_node("classify_and_respond", classify_and_respond)
    graph.add_node("emit_observation", emit_observation)

    graph.add_edge(START, "initialize")
    graph.add_edge("initialize", "prepare_handoff")
    graph.add_edge("prepare_handoff", "automation_disclosure")
    graph.add_edge("automation_disclosure", "receive_broker_event")
    graph.add_edge("receive_broker_event", "classify_and_respond")
    graph.add_edge("classify_and_respond", "emit_observation")
    graph.add_edge("emit_observation", END)

    return graph.compile()
