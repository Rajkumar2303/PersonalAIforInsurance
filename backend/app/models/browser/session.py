"""Browser session models (Issue #7).

A ``BrowserSession`` is the safe, typed state of one browser execution on one
planned web route. It carries SAFE METADATA ONLY - never applicant values.

``planned_route_id`` is the route identity used by the API/executor. Today it is
mapped 1:1 to the registry_id via a single centralized compatibility mapping
(see ``app/browser/route_identity.py``); that mapping is a TEMPORARY
compatibility shim, not a permanent invariant.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Optional

from pydantic import ConfigDict, Field

from ..insurance.base import SensitiveBaseModel
from .action import BrowserActionEvent
from .observation import BrowserObservation, BrowserObservationType


class BrowserExecutionMode(StrEnum):
    """How the browser execution is run.

    - ``sandbox``: local controlled mock quote site; synthetic profiles allowed;
      safe for automated tests.
    - ``live``: official/approved real quote route; participant's own accurate
      information only; requires the live personal-use gate before starting.
    """

    SANDBOX = "sandbox"
    LIVE = "live"


class BrowserSessionStatus(StrEnum):
    """Lifecycle of a browser session.

    NOT a quote terminal status (Issue #8 owns those) - this is the browser
    execution lifecycle: where the session is paused and why.
    """

    CREATED = "created"
    RUNNING = "running"
    PAUSED_NEEDS_FIELD = "paused_needs_field"
    PAUSED_NEEDS_CONSENT = "paused_needs_consent"
    PAUSED_HUMAN_CHECKPOINT = "paused_human_checkpoint"
    PAUSED_UNKNOWN_FIELD = "paused_unknown_field"
    PAUSED_VALUE_NOT_SUPPORTED = "paused_value_not_supported"
    PAUSED_VALIDATION_ERROR = "paused_validation_error"
    PAUSED_AMBIGUOUS = "paused_ambiguous"
    SUCCEEDED = "succeeded"
    STOPPED_ACCESS_CONTROL = "stopped_access_control"
    STOPPED_HUMAN_CHECKPOINT = "stopped_human_checkpoint"
    STOPPED_PROHIBITED = "stopped_prohibited"
    STOPPED_UNEXPECTED_HOST = "stopped_unexpected_host"
    FAILED = "failed"
    CLOSED = "closed"


class BrowserRefusalReason(StrEnum):
    """Why a browser session could not be started (structured refusal)."""

    NOT_AUTO = "not_auto"
    NON_WEB_CHANNEL = "non_web_channel"
    ROUTE_NOT_FOUND = "route_not_found"
    ROUTE_NOT_READY = "route_not_ready"
    ROUTE_EXCLUDED = "route_excluded"
    CONSENT_MISSING = "consent_missing"
    LIVE_GATE_REQUIRED = "live_gate_required"
    AUTOMATION_NOT_PERMITTED = "automation_not_permitted"
    NO_VERIFIED_ROUTE = "no_verified_route"
    UNKNOWN_SESSION = "unknown_session"


class BrowserStartRefusal(SensitiveBaseModel):
    """Structured refusal returned instead of starting a browser session."""

    model_config = ConfigDict(extra="forbid")

    intake_session_id: Optional[str] = None
    registry_id: Optional[str] = None
    planned_route_id: Optional[str] = None
    reason: BrowserRefusalReason
    detail: str
    refused_at: dt.datetime


class LiveExecutionGate(SensitiveBaseModel):
    """Safe attestation metadata required before LIVE browser execution.

    Holds SAFE booleans only - never PII/attestation text:

    - ``personal_use_confirmed``: this is the participant's personal use.
    - ``accurate_information_attested``: the profile uses the participant's own
      accurate information.
    Route-disclosure consent is a separate Issue #5 receipt check.
    """

    model_config = ConfigDict(extra="forbid")

    personal_use_confirmed: bool = False
    accurate_information_attested: bool = False
    attested_at: Optional[dt.datetime] = None

    @property
    def satisfied(self) -> bool:
        return self.personal_use_confirmed and self.accurate_information_attested


class BrowserActionSafety(StrEnum):
    """Safety classification of a clickable/submittable action.

    - ``safe_navigation``: continue/next within the quote flow.
    - ``data_submission``: submits quote-form data to continue.
    - ``human_checkpoint``: must pause for a human (identity, declaration, ...).
    - ``prohibited``: must NOT be automated (signature/payment/purchase/...).
    """

    SAFE_NAVIGATION = "safe_navigation"
    DATA_SUBMISSION = "data_submission"
    HUMAN_CHECKPOINT = "human_checkpoint"
    PROHIBITED = "prohibited"


class BrowserActionResult(SensitiveBaseModel):
    """Outcome of classifying/acting on one clickable control (safe)."""

    model_config = ConfigDict(extra="forbid")

    action_type: str
    safety: BrowserActionSafety
    label: Optional[str] = None
    ok: bool = True
    error_category: Optional[str] = None
    error_message: Optional[str] = None  # safe; never applicant values


class BrowserSession(SensitiveBaseModel):
    """Typed, safe browser session state. No applicant values."""

    model_config = ConfigDict(extra="forbid")

    browser_session_id: str
    plan_id: Optional[str] = None
    planned_route_id: Optional[str] = None  # see module docstring re: mapping
    registry_id: Optional[str] = None
    profile_id: Optional[str] = None  # opaque vault key, never a profile
    intake_session_id: Optional[str] = None
    attempt_id: Optional[str] = None  # Issue #8 recovery attempt for this session
    execution_mode: BrowserExecutionMode = BrowserExecutionMode.SANDBOX
    status: BrowserSessionStatus = BrowserSessionStatus.CREATED
    current_step: int = 0
    current_page_index: int = 0
    current_url: Optional[str] = None  # sanitized (no query string)
    page_signature: Optional[str] = None
    observed_field_ids: list[str] = Field(default_factory=list)  # external ids
    pending_field_paths: list[str] = Field(default_factory=list)  # canonical paths
    # Human-checkpoint kinds the participant has EXPLICITLY approved during
    # this session (e.g. identity_lookup before submitting the licence). Only
    # resumable checkpoints (requires_explicit_human_checkpoint, NOT
    # must_not_automate) can ever be approved; prohibited actions are never
    # here. Kept across pause/resume so the SAME browser_session_id and
    # attempt_id continue after approval.
    checkpoint_approvals: list[str] = Field(default_factory=list)
    checkpoint_type: Optional[str] = None
    last_observation_type: Optional[str] = None
    quote_present: bool = False
    reference_present: bool = False
    started_at: dt.datetime
    updated_at: dt.datetime


class BrowserStepResult(SensitiveBaseModel):
    """Outcome of one browser step (safe metadata + observation)."""

    model_config = ConfigDict(extra="forbid")

    browser_session_id: str
    step: int
    observation_type: BrowserObservationType
    status: BrowserSessionStatus
    page_signature: Optional[str] = None
    filled_field_count: int = 0
    missing_field_count: int = 0
    unknown_field_count: int = 0
    message: Optional[str] = None  # safe; never values
    observation: Optional[BrowserObservation] = None
    # Privacy-safe per-action events emitted during this step (provider,
    # canonical_field path, action, status, correlation ids - NEVER values).
    action_events: list[BrowserActionEvent] = Field(default_factory=list)
