"""Typed state for the demo LangGraph workflow.

Issue 1: a minimal, insurance-free workflow state used to validate
orchestration and tracing. Future milestones add richer workflow state
(e.g. market discovery, route planning, quote attempts, evidence).
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict


class DemoWorkflowState(TypedDict, total=False):
    """Minimal typed workflow state.

    ``steps`` uses an additive reducer so nodes append their stage names
    to a single list across the graph run.
    """

    input_text: str
    stage: str
    steps: Annotated[list[str], operator.add]
    final_output: str
