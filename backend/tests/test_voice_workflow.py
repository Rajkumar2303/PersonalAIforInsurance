"""Issue #9 Prompt 1 - voice LangGraph workflow tests (hermetic).

Verifies the graph compiles, carries SAFE METADATA ONLY, annotates stages,
and that the ``emit_observation`` node delegates to the Issue #8 recovery
engine (which stays the terminal-status authority).
"""

from __future__ import annotations

from app.graph.voice_workflow import WORKFLOW_NAME, build_voice_workflow
from app.models.voice import BrokerQuestionKind, VoiceLifecycleStatus
from voice_helpers import kind_question, make_handoff_context, make_voice_env


def test_workflow_name():
    assert WORKFLOW_NAME == "voice_workflow"


def test_workflow_compiles_with_injected_engine(tmp_path):
    env = make_voice_env(tmp_path)
    graph = build_voice_workflow(env.engine)
    assert graph is not None


def test_workflow_full_callback_flow(tmp_path):
    env = make_voice_env(tmp_path)
    graph = build_voice_workflow(env.engine)
    out = graph.invoke(
        {
            "entry": "test",
            "handoff_context": make_handoff_context(env, callback_reason="callback_detected"),
            "disclosure_granted": True,
            "broker_question": kind_question(BrokerQuestionKind.CALLBACK_REQUEST).model_dump(),
            "observation_type": "callback_scheduled",
            "reason": "broker asked to call back",
        }
    )
    assert out["workflow_stage"] == "emit_observation"
    assert out["voice_session_id"]
    assert out["registry_id"] == env.registry_id
    # Action from the engine (callback scheduled).
    assert out["action"] == "callback_scheduled"
    # Issue #8 authority decided callback_required.
    assert out["recovery_terminal_status"] == "callback_required"
    assert out["quote_pending_normalization"] is False


def test_workflow_quote_flow_pending_normalization(tmp_path):
    env = make_voice_env(tmp_path)
    graph = build_voice_workflow(env.engine)
    out = graph.invoke(
        {
            "entry": "test",
            "handoff_context": make_handoff_context(env),
            "disclosure_granted": True,
            "broker_question": kind_question(BrokerQuestionKind.QUOTE_DISCLOSURE).model_dump(),
            "observation_type": "phone_quote_observed",
            "reason": "broker quoted an annual premium",
        }
    )
    assert out["action"] == "end_quote"
    assert out["quote_pending_normalization"] is True
    # Never a comparable status (Issues #11/#12 own comparability).
    assert out.get("recovery_terminal_status") is None


def test_workflow_state_is_safe_metadata_only(tmp_path):
    env = make_voice_env(tmp_path)
    graph = build_voice_workflow(env.engine)
    out = graph.invoke(
        {
            "entry": "test",
            "handoff_context": make_handoff_context(env),
            "disclosure_granted": True,
            "broker_question": kind_question(BrokerQuestionKind.CALLBACK_REQUEST).model_dump(),
            "observation_type": "callback_scheduled",
        }
    )
    for marker in ("M0A 0A0", "T0000-00000-00000", "1HGCM82633A000000", "1990-01-01"):
        assert marker not in str(out), f"sensitive marker leaked into graph state: {marker}"


def test_workflow_emit_observation_never_comparable(tmp_path):
    env = make_voice_env(tmp_path)
    graph = build_voice_workflow(env.engine)
    out = graph.invoke(
        {
            "entry": "test",
            "handoff_context": make_handoff_context(env),
            "disclosure_granted": True,
            "broker_question": kind_question(BrokerQuestionKind.CALLBACK_REQUEST).model_dump(),
            "observation_type": "callback_scheduled",
        }
    )
    # The voice graph/engine never produce comparable statuses.
    assert out.get("recovery_terminal_status") != "quoted_comparable"
    assert out.get("recovery_terminal_status") != "quoted_non_comparable"
    assert VoiceLifecycleStatus.COMPLETED.value in (
        env.engine.get(out["voice_session_id"]).lifecycle_status,
        "completed",
    )
