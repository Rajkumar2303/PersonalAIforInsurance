"""Pydantic v2 models for the demo workflow endpoint."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class DemoWorkflowRequest(BaseModel):
    """Request body for the demo workflow endpoint."""

    model_config = ConfigDict(extra="forbid")

    input_text: str = Field(default="hello", min_length=0, max_length=500)


class DemoWorkflowResponse(BaseModel):
    """Result of running the demo LangGraph workflow."""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    workflow: str
    stages: list[str]
    final_output: str
    status: str
