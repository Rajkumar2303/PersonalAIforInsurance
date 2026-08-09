"""Intake workflow typed state (Issue #5).

SAFE METADATA ONLY - never a full InsuranceProfile and never raw applicant
values (licence number, VIN, DOB, address, claims, etc.). Raw values are
retrieved only by the trusted profile vault / engine, keyed by ``profile_id``.
"""

from __future__ import annotations

from typing import Optional, TypedDict


class IntakeWorkflowState(TypedDict, total=False):
    """Safe metadata carried through the LangGraph intake workflow."""

    # entry / correlation
    entry: str  # "advance" | "submit"
    session_id: str
    profile_id: Optional[str]
    insurance_type: Optional[str]
    request_id: Optional[str]

    # orchestration stage
    workflow_stage: str
    workflow_status: str
    message: Optional[str]
    last_error: Optional[str]  # safe (path only), never the rejected value

    # current field (metadata only)
    current_field_id: Optional[str]
    current_canonical_path: Optional[str]
    field_id: Optional[str]
    canonical_path: Optional[str]
    validation_success: Optional[bool]

    # counts (safe)
    missing_field_count: int
    completed_field_count: int

    # consent / route (safe identifiers only)
    consent_scope: Optional[str]
    route_registry_id: Optional[str]
