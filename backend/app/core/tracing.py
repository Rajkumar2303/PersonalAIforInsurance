"""LangSmith tracing configuration and run/trace correlation helpers.

Tracing is configured purely from environment variables via ``Settings``.
The demo workflow (and every future workflow node) is individually
traceable, and each run is correlated with a ``request_id`` so quote
attempts and evidence can be matched to a LangSmith trace.

Privacy: metadata and tags contain workflow/request identifiers only —
never applicant data (licence numbers, addresses, DOB, VIN, claims,
phone/email, voice data, etc.).
"""

from __future__ import annotations

import os
import uuid
from typing import Any

from .config import Settings

LANGSMITH_TRACING_VAR = "LANGSMITH_TRACING"
LANGSMITH_API_KEY_VAR = "LANGSMITH_API_KEY"
LANGSMITH_PROJECT_VAR = "LANGSMITH_PROJECT"
LANGSMITH_ENDPOINT_VAR = "LANGSMITH_ENDPOINT"


def configure_tracing(settings: Settings) -> None:
    """Apply LangSmith settings to the process environment.

    Idempotent and safe to call when no API key is present (tracing is
    simply disabled / project-named).
    """
    os.environ[LANGSMITH_TRACING_VAR] = "true" if settings.langsmith_tracing else "false"
    os.environ[LANGSMITH_PROJECT_VAR] = settings.langsmith_project
    if settings.langsmith_api_key:
        os.environ[LANGSMITH_API_KEY_VAR] = settings.langsmith_api_key
    if settings.langsmith_endpoint:
        os.environ[LANGSMITH_ENDPOINT_VAR] = settings.langsmith_endpoint


def tracing_enabled(settings: Settings) -> bool:
    """Return True when LangSmith tracing is configured on."""
    return settings.langsmith_tracing


def run_config(
    settings: Settings,
    *,
    request_id: str | None = None,
    run_id: str | None = None,
    workflow: str = "demo_workflow",
    workflow_stage: str | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a LangGraph ``RunnableConfig`` with non-sensitive metadata/tags.

    Args:
        settings: application settings (environment/project source).
        request_id: correlation id propagated from the API layer.
        run_id: explicit LangSmith run id; defaults to ``request_id`` so the
            HTTP request and the LangSmith trace share the same ID.
        workflow: workflow name (e.g. ``demo_workflow``).
        workflow_stage: current stage, if known at build time.
        extra_metadata: additional non-sensitive metadata, e.g.
            ``{"registry_id": ..., "route_type": ...}`` in future milestones.

    Returns:
        A dict suitable for ``graph.invoke(..., config=...)``.
    """
    metadata: dict[str, Any] = {
        "environment": settings.app_env,
        "workflow": workflow,
    }
    if workflow_stage:
        metadata["workflow_stage"] = workflow_stage
    if request_id:
        metadata["request_id"] = request_id
    if extra_metadata:
        metadata.update(extra_metadata)

    config: dict[str, Any] = {
        "metadata": metadata,
        "tags": ["ontario-allquote-agent", workflow, settings.app_env],
    }

    # Correlate the LangSmith trace with our request: set the root run id
    # to the request id so traces are greppable by request_id.
    run_id_value = run_id or request_id
    if run_id_value:
        try:
            config["run_id"] = uuid.UUID(run_id_value)
        except (ValueError, AttributeError):
            pass

    return config


def set_stage(workflow_stage: str) -> None:
    """Annotate the current workflow stage on the active traced run.

    Must be called from inside a traced node/step (LangGraph nodes are
    traced automatically when LangSmith tracing is enabled).
    """
    try:
        from langsmith import get_current_run_tree

        run = get_current_run_tree()
        if run is not None:
            run.update(metadata={"workflow_stage": workflow_stage})
    except Exception:  # pragma: no cover - tracing is best-effort
        pass
