"""Recovery engine models (Issue #8).

Terminal-status & recovery orchestration for quote attempts.

STATE SEPARATION (kept distinct on purpose):
- ``RouteReadiness`` / ``PlannedRoute`` (Issue #6): planning state.
- ``BrowserObservation`` (Issue #7): execution observation.
- ``AttemptLifecycleStatus`` (Issue #8): attempt lifecycle.
- ``RouteOutcomeStatus`` (Issue #8 / later coverage ledger): final outcome.

They are NOT collapsed into one enum. E.g. ``RouteReadiness.ready`` does not
mean ``RouteOutcomeStatus.quoted_*``; a ``technical_error`` observation does
not automatically mean ``unreachable`` (a retry may still be permitted).

COMPARABILITY BOUNDARY: Issue #8 never assigns ``quoted_comparable`` /
``quoted_non_comparable``. A quote observation is recorded as
``ExecutionResultKind.QUOTE_OBSERVED`` with ``quote_pending_normalization=True``
and ``RouteOutcomeStatus=None`` - Issues #11/#12 finalize comparability.

PRIVACY: all models are ``SensitiveBaseModel`` (redacted repr/safe_dict). They
carry ids, canonical paths, counts, and safe flags only - never applicant
values, raw quote references, or form values.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Any, Optional, TypedDict

from pydantic import ConfigDict, Field

from .insurance.base import SensitiveBaseModel


class AttemptLifecycleStatus(StrEnum):
    """Issue #8 attempt lifecycle (NOT a planning or observation enum)."""

    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    RECOVERABLE = "recoverable"
    TERMINAL = "terminal"


class ExecutionResultKind(StrEnum):
    """What the execution produced (NOT a final coverage status)."""

    QUOTE_OBSERVED = "quote_observed"
    ESTIMATE_OBSERVED = "estimate_observed"
    CALLBACK_OBSERVED = "callback_observed"
    MANUAL_CONTACT_OBSERVED = "manual_contact_observed"
    ACCESS_BLOCKED = "access_blocked"
    EXPLICIT_INELIGIBLE = "explicit_ineligible"
    AFFINITY_RESTRICTED = "affinity_restricted"
    SPECIALTY_ONLY = "specialty_only"
    NOT_CURRENTLY_WRITING = "not_currently_writing"
    VALIDATION_ERROR = "validation_error"
    UNKNOWN_FIELD = "unknown_field"
    VALUE_NOT_SUPPORTED = "value_not_supported"
    TECHNICAL_ERROR = "technical_error"
    COMPLETE_WITHOUT_QUOTE = "complete_without_quote"
    HUMAN_CHECKPOINT_PAUSED = "human_checkpoint_paused"
    HUMAN_CHECKPOINT_PROHIBITED = "human_checkpoint_prohibited"
    CONSENT_PAUSE = "consent_pause"
    FIELD_PAUSE = "field_pause"
    UNRESOLVED = "unresolved"
    IN_PROGRESS = "in_progress"


class RouteOutcomeStatus(StrEnum):
    """Final coverage-ledger outcome. Issue #8 sets ONLY on explicit evidence.

    ``quoted_comparable`` / ``quoted_non_comparable`` are NEVER set by Issue #8
    (deferred to Issues #11/#12 comparability).
    """

    QUOTED_COMPARABLE = "quoted_comparable"
    QUOTED_NON_COMPARABLE = "quoted_non_comparable"
    ESTIMATE_ONLY = "estimate_only"
    CALLBACK_REQUIRED = "callback_required"
    MANUAL_HANDOFF = "manual_handoff"
    INELIGIBLE = "ineligible"
    AFFINITY_RESTRICTED = "affinity_restricted"
    SPECIALTY_ONLY = "specialty_only"
    DUPLICATE_RATE_SOURCE = "duplicate_rate_source"
    NOT_CURRENTLY_WRITING = "not_currently_writing"
    BLOCKED = "blocked"
    UNREACHABLE = "unreachable"
    UNRESOLVED = "unresolved"


class Retryability(StrEnum):
    """Deterministic retry classification for an observation."""

    RETRYABLE = "retryable"
    NON_RETRYABLE = "non_retryable"
    REQUIRES_HUMAN = "requires_human"
    UNKNOWN = "unknown"


class RecoveryAction(StrEnum):
    """Issue #8 CHOOSES the action; it never executes the action itself.

    Browser retries are executed by the Issue #7 browser layer, phone calls by
    Issue #9, user answers by Issue #5.
    """

    CONTINUE_CURRENT_SESSION = "continue_current_session"
    RESUME_AFTER_USER_INPUT = "resume_after_user_input"
    RETRY_SAME_ROUTE = "retry_same_route"
    USE_ALTERNATIVE_ROUTE = "use_alternative_route"
    PREPARE_VOICE_HANDOFF = "prepare_voice_handoff"
    MANUAL_HANDOFF = "manual_handoff"
    AWAIT_HUMAN_CHECKPOINT = "await_human_checkpoint"
    STOP_TERMINAL = "stop_terminal"
    NO_ACTION = "no_action"


class RecoveryReasonCode(StrEnum):
    """Explainable reason codes for every decision (enums, not vague strings)."""

    MISSING_FIELD = "missing_field"
    CONSENT_REQUIRED = "consent_required"
    CONSENT_DENIED = "consent_denied"
    HUMAN_CHECKPOINT = "human_checkpoint"
    CAPTCHA_OR_BOT_CONTROL = "captcha_or_bot_control"
    AUTHENTICATION_REQUIRED = "authentication_required"
    UNEXPECTED_HOST = "unexpected_host"
    UNKNOWN_REQUIRED_FIELD = "unknown_required_field"
    UNSUPPORTED_DESTINATION_VALUE = "unsupported_destination_value"
    WEBSITE_VALIDATION_ERROR = "website_validation_error"
    NAVIGATION_TIMEOUT = "navigation_timeout"
    BROWSER_CRASH = "browser_crash"
    TRANSIENT_NAVIGATION_FAILURE = "transient_navigation_failure"
    RETRY_BUDGET_EXHAUSTED = "retry_budget_exhausted"
    ALTERNATE_ROUTE_AVAILABLE = "alternate_route_available"
    NO_ALTERNATIVE_ROUTE = "no_alternative_route"
    CALLBACK_REQUIRED = "callback_required"
    MANUAL_CONTACT_REQUIRED = "manual_contact_required"
    ROUTE_NOT_CURRENTLY_WRITING = "route_not_currently_writing"
    EXPLICIT_INELIGIBILITY = "explicit_ineligibility"
    AFFINITY_REQUIREMENT_UNSATISFIED = "affinity_requirement_unsatisfied"
    SPECIALTY_ONLY = "specialty_only"
    UNRESOLVED_RESULT = "unresolved_result"
    QUOTE_OBSERVED = "quote_observed"
    ESTIMATE_OBSERVED = "estimate_observed"
    DUPLICATE_RATE_SOURCE = "duplicate_rate_source"
    RESUME_SESSION_UNAVAILABLE = "resume_session_unavailable"
    MEMBERSHIP_UNKNOWN = "membership_unknown"
    ROUTE_INVALID = "route_invalid"


class SourceChannel(StrEnum):
    """Where an execution observation came from (Issue #9 adds voice)."""

    BROWSER = "browser"
    VOICE = "voice"
    PHONE = "phone"
    MANUAL = "manual"


class ExecutionObservation(SensitiveBaseModel):
    """Generic execution observation (Issue #8) - adapts Issue #7 browser
    observations and future Issue #9 voice observations."""

    model_config = ConfigDict(extra="forbid")

    source_channel: SourceChannel = SourceChannel.BROWSER
    observation_type: str  # BrowserObservationType value, or future voice type
    reason: Optional[str] = None  # safe text only
    safe_context: dict[str, Any] = Field(default_factory=dict)  # safe metadata


class RecoveryPolicy(SensitiveBaseModel):
    """Deterministic, data-driven retry/failover policy. Conservative defaults.

    Changing these values (config/data) must change behavior WITHOUT modifying
    the ``RecoveryEngine`` code.
    """

    model_config = ConfigDict(extra="forbid")

    # Version of the recovery policy used (from data/config) - for auditability.
    version: Optional[str] = None
    # Initial attempt + at most (max_attempts_per_route - 1) retries.
    max_attempts_per_route: int = 2
    # Extra cap on same-route transient retries (never exceeds route max).
    max_transient_retries: int = 1
    navigation_timeout_retryable: bool = True
    browser_crash_retryable: bool = True
    alternative_route_after_exhaustion: bool = True
    # Total attempts across all routes sharing one distinct rate source.
    max_attempts_per_rate_source: int = 3
    # Total attempts across the whole plan (hard global cap).
    max_attempts_per_plan: int = 6


class AttemptRecord(SensitiveBaseModel):
    """One safe attempt history record. NO applicant PII, NO raw quote ref."""

    model_config = ConfigDict(extra="forbid")

    attempt_id: str
    plan_id: Optional[str] = None
    planned_route_id: Optional[str] = None
    registry_id: Optional[str] = None
    distinct_rate_source_id: Optional[str] = None
    attempt_number: int = 1
    channel: SourceChannel = SourceChannel.BROWSER
    started_at: dt.datetime
    ended_at: Optional[dt.datetime] = None
    lifecycle_status: AttemptLifecycleStatus = AttemptLifecycleStatus.RUNNING
    # Monotonic revision guard: every state change bumps this. Used to detect
    # stale/out-of-order observations and to keep terminal state stable.
    revision: int = 0
    # Last processed observation (dedup/stale guard).
    last_observation_key: Optional[str] = None
    last_observation_sequence: Optional[int] = None
    # Provenance for later audit (why retry/failover occurred).
    policy_version: Optional[str] = None
    plan_version: Optional[str] = None
    observation_type: Optional[str] = None
    execution_result_kind: Optional[str] = None
    reason_codes: list[str] = Field(default_factory=list)
    terminal_status: Optional[RouteOutcomeStatus] = None
    recovery_action: Optional[RecoveryAction] = None
    parent_attempt_id: Optional[str] = None
    alternative_of_attempt_id: Optional[str] = None
    quote_pending_normalization: bool = False
    notes: Optional[str] = None  # safe, non-sensitive enrichment note only


class RecoveryDecision(SensitiveBaseModel):
    """The deterministic, explainable decision for one observation.

    ``safe_context`` is a dict of SAFE metadata (paths, counts, checkpoint /
    error type, flags). Never applicant values.
    """

    model_config = ConfigDict(extra="forbid")

    decision_id: str
    attempt_id: str
    plan_id: Optional[str] = None
    planned_route_id: Optional[str] = None
    registry_id: Optional[str] = None
    distinct_rate_source_id: Optional[str] = None
    lifecycle_status: AttemptLifecycleStatus
    recommended_action: RecoveryAction
    reason_codes: list[str] = Field(default_factory=list)
    retry_allowed: bool = False
    attempts_used: int = 0
    attempts_remaining: int = 0
    alternative_route_id: Optional[str] = None
    terminal_status: Optional[RouteOutcomeStatus] = None
    quote_pending_normalization: bool = False
    # Provenance: recovery policy + plan versions at decision time.
    policy_version: Optional[str] = None
    plan_version: Optional[str] = None
    safe_context: dict[str, Any] = Field(default_factory=dict)
    decided_at: dt.datetime


class RecoveryDecideRequest(SensitiveBaseModel):
    """Safe input for the recovery engine (ids + observation metadata only)."""

    model_config = ConfigDict(extra="forbid")

    attempt_id: Optional[str] = None
    plan_id: Optional[str] = None
    planned_route_id: str
    registry_id: Optional[str] = None
    distinct_rate_source_id: Optional[str] = None
    intake_session_id: Optional[str] = None
    source_channel: SourceChannel = SourceChannel.BROWSER
    observation_type: str
    reason: Optional[str] = None
    observation_sequence: Optional[int] = None
    plan_version: Optional[str] = None
    safe_context: dict[str, Any] = Field(default_factory=dict)
    policy: Optional[RecoveryPolicy] = None  # optional override (tests/dynamic)


class RecoveryWorkflowState(TypedDict, total=False):
    """SAFE METADATA ONLY - ids, counts, action/status strings. No values."""

    entry: str
    request_id: Optional[str]
    plan_id: Optional[str]
    planned_route_id: Optional[str]
    registry_id: Optional[str]
    distinct_rate_source_id: Optional[str]
    intake_session_id: Optional[str]
    observation_type: Optional[str]
    reason: Optional[str]
    observation_sequence: Optional[int]
    safe_context: Optional[dict[str, Any]]
    policy_version: Optional[str]
    plan_version: Optional[str]
    workflow_stage: str
    workflow_status: str
    message: Optional[str]
    execution_result_kind: Optional[str]
    retryability: Optional[str]
    decision_id: Optional[str]
    lifecycle_status: Optional[str]
    recommended_action: Optional[str]
    reason_codes: Optional[list[str]]
    retry_allowed: Optional[bool]
    attempts_used: int
    attempts_remaining: int
    alternative_route_id: Optional[str]
    terminal_status: Optional[str]
    quote_pending_normalization: Optional[bool]
