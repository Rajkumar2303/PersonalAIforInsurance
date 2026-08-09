"""Minimal LangGraph demo workflow.

Two nodes with an explicit state transition. Intentionally contains no
insurance-specific logic — it exists to validate LangGraph orchestration,
typed state, and LangSmith tracing for the project foundation.

Each node is individually traceable when LangSmith tracing is enabled;
stages are annotated on the active trace run via ``set_stage``.
"""

from __future__ import annotations

import logging

from langgraph.graph import END, START, StateGraph

from ..core.tracing import set_stage
from .state import DemoWorkflowState

logger = logging.getLogger(__name__)

WORKFLOW_NAME = "demo_workflow"
STAGE_ONE = "stage_one"
STAGE_TWO = "stage_two"


def stage_one(state: DemoWorkflowState) -> dict:
    """First node: normalize the input and record the stage."""
    set_stage(STAGE_ONE)
    text = (state.get("input_text") or "").strip() or "hello"
    logger.info(
        "demo stage one executed",
        extra={"workflow": WORKFLOW_NAME, "workflow_stage": STAGE_ONE},
    )
    return {"stage": STAGE_ONE, "steps": [STAGE_ONE], "input_text": text}


def stage_two(state: DemoWorkflowState) -> dict:
    """Second node: finalize the output."""
    set_stage(STAGE_TWO)
    final_output = f"processed: {state.get('input_text', '')}"
    logger.info(
        "demo stage two executed",
        extra={"workflow": WORKFLOW_NAME, "workflow_stage": STAGE_TWO},
    )
    return {"stage": STAGE_TWO, "steps": [STAGE_TWO], "final_output": final_output}


def build_demo_workflow():
    """Build and compile the demo workflow graph.

    Returns:
        A compiled ``CompiledStateGraph`` ready for ``invoke``/``ainvoke``.
    """
    builder = StateGraph(DemoWorkflowState)
    builder.add_node(STAGE_ONE, stage_one)
    builder.add_node(STAGE_TWO, stage_two)
    builder.add_edge(START, STAGE_ONE)
    builder.add_edge(STAGE_ONE, STAGE_TWO)
    builder.add_edge(STAGE_TWO, END)
    return builder.compile()
