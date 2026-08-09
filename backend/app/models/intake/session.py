"""Intake session + safe response/result models (Issue #5).

``IntakeSession`` carries ONLY safe metadata and field-lifecycle state - never
raw applicant answers. Raw values live in the profile vault.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Optional

from pydantic import ConfigDict, Field

from ..insurance.base import SensitiveBaseModel
from ..insurance.enums import InsuranceType


class IntakeSessionStatus(StrEnum):
    """Lifecycle of an intake session (NOT a quote terminal status)."""

    NEW = "new"
    ACTIVE = "active"
    COLLECTING = "collecting"
    CONSENT_PENDING = "consent_pending"
    PRODUCT_REJECTED = "product_rejected"
    STARTER_COMPLETE = "starter_complete"
    COMPLETE = "complete"
    DELETED = "deleted"


class FieldRequestState(StrEnum):
    """Lifecycle of a single requested field (issue section 10)."""

    UNKNOWN = "unknown"
    REQUESTED = "requested"
    ANSWERED = "answered"
    DECLINED = "declined"
    INVALID_PENDING_RETRY = "invalid_pending_retry"
    UNSUPPORTED = "unsupported"


class IntakeSession(SensitiveBaseModel):
    """Typed session state - safe metadata + field lifecycle only."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    profile_id: Optional[str] = None
    insurance_type: InsuranceType
    status: IntakeSessionStatus = IntakeSessionStatus.NEW
    current_field_id: Optional[str] = None
    current_canonical_path: Optional[str] = None
    requested_fields: list[str] = Field(default_factory=list)  # field_ids
    completed_fields: list[str] = Field(default_factory=list)  # canonical paths
    declined_fields: list[str] = Field(default_factory=list)
    invalid_retries: dict[str, int] = Field(default_factory=dict)
    validation_retry_count: int = 0
    created_at: dt.datetime
    updated_at: dt.datetime


class ProductGateResult(SensitiveBaseModel):
    """Result of the product gate (issue section 1)."""

    model_config = ConfigDict(extra="forbid")

    insurance_type: InsuranceType
    is_supported: bool
    status: str  # "started" | "product_not_implemented"


class SafeQuestion(SensitiveBaseModel):
    """A question payload for the applicant.

    Contains ONLY the question metadata for one field - it never includes other
    profile values, and for sensitive fields it identifies the category/path
    without exposing any existing sensitive value.
    """

    model_config = ConfigDict(extra="forbid")

    field_id: str
    canonical_path: str
    question: str
    short_label: str
    input_type: str
    sensitivity: str
    intake_phase: str
    choices: list[str] = Field(default_factory=list)
    help_text: Optional[str] = None
    workflow_status: str = "awaiting_input"


class SubmitAnswerResult(SensitiveBaseModel):
    """Result of submitting one answer (issue sections 11, 25)."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    field_id: Optional[str] = None
    canonical_path: Optional[str] = None
    validation_success: bool
    error_message: Optional[str] = None  # safe; never contains the rejected value
    retry_eligible: bool = False
    workflow_status: str
    next_question: Optional[SafeQuestion] = None


class FieldRequestOutcome(SensitiveBaseModel):
    """Per-path result of an external field request (issue sections 9, 12).

    The service answers: already_known / missing / unsupported_path /
    consent_required / human_checkpoint_required.
    """

    model_config = ConfigDict(extra="forbid")

    requested_path: str
    canonical_path: Optional[str] = None
    field_id: Optional[str] = None
    state: FieldRequestState
    already_known: bool = False
    consent_required: bool = False
    human_checkpoint_required: bool = False
    checkpoint_kind: Optional[str] = None
    unsupported_reason: Optional[str] = None
    source_context: Optional[str] = None


class ProfileSummaryField(SensitiveBaseModel):
    """Presence metadata for one canonical field - NEVER the value."""

    model_config = ConfigDict(extra="forbid")

    canonical_path: str
    field_id: str
    label: str
    sensitivity: str
    intake_phase: str
    has_value: bool


class ProfileSummary(SensitiveBaseModel):
    """Safe profile summary: presence flags + counts, no values."""

    model_config = ConfigDict(extra="forbid")

    profile_id: Optional[str] = None
    insurance_type: InsuranceType
    status: str
    completed_field_count: int = 0
    missing_field_count: int = 0
    starter_complete: bool = False
    live_quote_ready: bool = False
    fields: list[ProfileSummaryField] = Field(default_factory=list)
