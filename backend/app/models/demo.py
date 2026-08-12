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


class DemoEnvironmentStatus(BaseModel):
    """Lightweight startup/environment check (Issue #14).

    Clearly separates what the DEMO REQUIRES (nothing external) from what is
    OPTIONAL/LIVE (Postgres, LangSmith, LLM, telephony, live providers). This
    endpoint never exposes secrets - only booleans about configured options.
    """

    model_config = ConfigDict(extra="forbid")

    demo_ready: bool
    demo_requires_external_credentials: bool = False
    mock_site_enabled: bool
    mock_site_url: str | None = None
    comparison_max_concurrency: int
    comparison_route_timeout_seconds: float
    # OPTIONAL / LIVE (presence flags only - never values):
    database_configured: bool = False
    langsmith_configured: bool = False
    llm_configured: bool = False
    live_providers_configured: bool = False
