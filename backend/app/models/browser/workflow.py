"""LangGraph browser workflow state (Issue #7).

SAFE METADATA ONLY - ids, registry id, canonical field paths, counts, page
signature, action/observation/checkpoint types, and a sanitized URL. It NEVER
contains applicant field values, raw DOM, request bodies, cookies, or tokens.
"""

from __future__ import annotations

from typing import Optional, TypedDict


class BrowserWorkflowState(TypedDict, total=False):
    """Safe browser workflow state (never applicant values)."""

    entry: str
    browser_session_id: str
    plan_id: Optional[str]
    planned_route_id: str
    registry_id: Optional[str]
    intake_session_id: str
    profile_id: Optional[str]
    execution_mode: str
    workflow_stage: str
    workflow_status: str
    message: Optional[str]

    # step counters / safe metadata
    current_step: int
    max_steps: int
    current_url: Optional[str]  # sanitized
    page_signature: Optional[str]
    filled_field_count: int
    missing_field_count: int
    unknown_field_count: int
    pending_field_paths: list[str]
    checkpoint_type: Optional[str]
    action_type: Optional[str]
    observation_type: Optional[str]
    quote_present: bool
    reference_present: bool
