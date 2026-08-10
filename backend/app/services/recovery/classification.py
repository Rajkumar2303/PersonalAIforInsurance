"""Deterministic observation classification (Issue #8).

Maps a generic ``ExecutionObservation`` (adapted from Issue #7 browser
observations, or future Issue #9 voice observations) to a typed
``ClassifiedObservation``: execution-result kind, retryability, lifecycle hint,
reason codes, terminal status, and failover eligibility.

NO LLM. NO insurer-specific branching. Adding a normal new observation is a
localized table entry (or a small special-case handler) - the ``RecoveryEngine``
and the LangGraph topology never change.

HUMAN CHECKPOINT SPLIT (per Issue #8 clarification):
- recoverable checkpoints (identity lookup, consent attestation, household
  driver, other resumable confirmation) -> PAUSED / await_human_checkpoint.
- out-of-scope boundaries (application declaration the automation cannot make,
  electronic signature, payment, purchase/binding, ...) -> TERMINAL /
  manual_handoff, RouteOutcomeStatus = manual_handoff.
- CAPTCHA / bot / access control -> TERMINAL / blocked, no retry, no bypass.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ...models.browser.observation import BrowserObservation
from ...models.recovery import (
    AttemptLifecycleStatus,
    ExecutionObservation,
    ExecutionResultKind,
    RecoveryPolicy,
    RecoveryReasonCode,
    Retryability,
    RouteOutcomeStatus,
    SourceChannel,
)
from pydantic import ConfigDict, Field

from ...models.insurance.base import SensitiveBaseModel

logger = logging.getLogger(__name__)

# Safe-context allowlist: only these keys may flow into LangSmith-traced state /
# API requests. Anything else (e.g. a nested raw browser payload) is dropped so
# traces can never accidentally include applicant values.
_SAFE_CONTEXT_KEYS: frozenset[str] = frozenset(
    {
        "page_signature", "url", "missing_field_paths", "needs_consent_paths",
        "pending_field_paths", "unsupported_value_paths", "error_paths",
        "unknown_external_fields", "checkpoint_type", "must_not_automate",
        "quote_present", "is_firm_quote", "reference_present", "estimate_only",
        "private_reference_handle", "error_type", "consent_state",
        "validation_kind", "resume_session_unavailable", "plan_changed",
        "route_invalid", "session_id", "profile_id", "page",
    }
)


def sanitize_recovery_context(ctx: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Return only allowlisted safe metadata from a caller-provided context.

    Drops nested raw payloads and any key not on the safe allowlist, so a
    misbehaving caller cannot leak applicant values into traced state.
    """
    if not ctx:
        return {}
    return {k: v for k, v in ctx.items() if k in _SAFE_CONTEXT_KEYS}


class ClassifiedObservation(SensitiveBaseModel):
    """Deterministic classification of one execution observation."""

    model_config = ConfigDict(extra="forbid")

    observation_type: str
    execution_result_kind: ExecutionResultKind
    lifecycle_hint: AttemptLifecycleStatus
    retryability: Retryability
    reason_codes: list[str] = Field(default_factory=list)
    action_hint: str
    terminal_status: Optional[str] = None
    fallback_terminal_status: Optional[str] = None
    failover_eligible: bool = False
    consumes_budget: bool = True
    quote_pending_normalization: bool = False


class _Spec:
    """Immutable table row for a deterministic observation mapping."""

    __slots__ = (
        "result_kind", "lifecycle", "retryability", "action", "reason_codes",
        "terminal_status", "fallback_terminal_status", "failover_eligible",
        "consumes_budget", "quote_pending_normalization",
    )

    def __init__(
        self,
        result_kind: ExecutionResultKind,
        lifecycle: AttemptLifecycleStatus,
        retryability: Retryability,
        action: str,
        reason_codes: tuple[RecoveryReasonCode, ...],
        terminal_status: Optional[RouteOutcomeStatus] = None,
        fallback_terminal_status: Optional[RouteOutcomeStatus] = None,
        failover_eligible: bool = False,
        consumes_budget: bool = True,
        quote_pending_normalization: bool = False,
    ) -> None:
        self.result_kind = result_kind
        self.lifecycle = lifecycle
        self.retryability = retryability
        self.action = action
        self.reason_codes = [
            c.value if isinstance(c, RecoveryReasonCode) else str(c)
            for c in reason_codes
        ]
        self.terminal_status = self._status_value(terminal_status)
        self.fallback_terminal_status = self._status_value(fallback_terminal_status)
        self.failover_eligible = failover_eligible
        self.consumes_budget = consumes_budget
        self.quote_pending_normalization = quote_pending_normalization

    @staticmethod
    def _status_value(status: Any) -> Optional[str]:
        """Accept a RouteOutcomeStatus enum OR its string value (for localized
        registrations)."""
        if status is None:
            return None
        return status.value if isinstance(status, RouteOutcomeStatus) else str(status)


def _in_progress(action: str) -> _Spec:
    return _Spec(
        result_kind=ExecutionResultKind.IN_PROGRESS,
        lifecycle=AttemptLifecycleStatus.RUNNING,
        retryability=Retryability.NON_RETRYABLE,
        action=action,
        reason_codes=(),
        consumes_budget=False,
    )


# Observation-type -> classification. Keyed by the string observation type
# (BrowserObservationType value or a future voice observation string). Adding a
# new observation is a localized row here.
_TABLE: dict[str, _Spec] = {
    "page_loaded": _in_progress("no_action"),
    "fields_filled": _in_progress("continue_current_session"),
    "route_changed": _in_progress("continue_current_session"),
    "needs_field": _Spec(
        ExecutionResultKind.FIELD_PAUSE, AttemptLifecycleStatus.PAUSED,
        Retryability.REQUIRES_HUMAN, "resume_after_user_input",
        (RecoveryReasonCode.MISSING_FIELD,), consumes_budget=False,
    ),
    "needs_consent": _Spec(  # undecided default; denied handled separately
        ExecutionResultKind.CONSENT_PAUSE, AttemptLifecycleStatus.PAUSED,
        Retryability.REQUIRES_HUMAN, "resume_after_user_input",
        (RecoveryReasonCode.CONSENT_REQUIRED,), consumes_budget=False,
    ),
    "access_control_detected": _Spec(
        ExecutionResultKind.ACCESS_BLOCKED, AttemptLifecycleStatus.TERMINAL,
        Retryability.NON_RETRYABLE, "manual_handoff",
        (RecoveryReasonCode.CAPTCHA_OR_BOT_CONTROL,),
        terminal_status=RouteOutcomeStatus.BLOCKED,
        fallback_terminal_status=RouteOutcomeStatus.BLOCKED,
    ),
    "unknown_external_field": _Spec(
        ExecutionResultKind.UNKNOWN_FIELD, AttemptLifecycleStatus.PAUSED,
        Retryability.REQUIRES_HUMAN, "resume_after_user_input",
        (RecoveryReasonCode.UNKNOWN_REQUIRED_FIELD,), consumes_budget=False,
    ),
    "callback_detected": _Spec(
        ExecutionResultKind.CALLBACK_OBSERVED, AttemptLifecycleStatus.TERMINAL,
        Retryability.NON_RETRYABLE, "prepare_voice_handoff",
        (RecoveryReasonCode.CALLBACK_REQUIRED,),
        terminal_status=RouteOutcomeStatus.CALLBACK_REQUIRED,
    ),
    "manual_contact_detected": _Spec(
        ExecutionResultKind.MANUAL_CONTACT_OBSERVED, AttemptLifecycleStatus.TERMINAL,
        Retryability.NON_RETRYABLE, "manual_handoff",
        (RecoveryReasonCode.MANUAL_CONTACT_REQUIRED,),
        terminal_status=RouteOutcomeStatus.MANUAL_HANDOFF,
    ),
    "complete_without_quote": _Spec(
        ExecutionResultKind.COMPLETE_WITHOUT_QUOTE, AttemptLifecycleStatus.TERMINAL,
        Retryability.NON_RETRYABLE, "stop_terminal",
        (RecoveryReasonCode.UNRESOLVED_RESULT,),
        terminal_status=RouteOutcomeStatus.UNRESOLVED,
        fallback_terminal_status=RouteOutcomeStatus.UNRESOLVED,
    ),
    "unsupported_page": _Spec(
        ExecutionResultKind.UNRESOLVED, AttemptLifecycleStatus.TERMINAL,
        Retryability.NON_RETRYABLE, "stop_terminal",
        (RecoveryReasonCode.UNRESOLVED_RESULT,),
        terminal_status=RouteOutcomeStatus.UNRESOLVED,
        fallback_terminal_status=RouteOutcomeStatus.UNRESOLVED,
    ),
    "value_not_supported": _Spec(
        ExecutionResultKind.VALUE_NOT_SUPPORTED, AttemptLifecycleStatus.TERMINAL,
        Retryability.NON_RETRYABLE, "manual_handoff",
        (RecoveryReasonCode.UNSUPPORTED_DESTINATION_VALUE,),
        fallback_terminal_status=RouteOutcomeStatus.MANUAL_HANDOFF,
        failover_eligible=True,
    ),
    "validation_error": _Spec(
        ExecutionResultKind.VALIDATION_ERROR, AttemptLifecycleStatus.PAUSED,
        Retryability.REQUIRES_HUMAN, "resume_after_user_input",
        (RecoveryReasonCode.WEBSITE_VALIDATION_ERROR,), consumes_budget=False,
    ),
    "authentication_required": _Spec(
        ExecutionResultKind.UNRESOLVED, AttemptLifecycleStatus.TERMINAL,
        Retryability.NON_RETRYABLE, "manual_handoff",
        (RecoveryReasonCode.AUTHENTICATION_REQUIRED,),
        terminal_status=RouteOutcomeStatus.MANUAL_HANDOFF,
        fallback_terminal_status=RouteOutcomeStatus.MANUAL_HANDOFF,
    ),
    "membership_unknown": _Spec(
        ExecutionResultKind.UNRESOLVED, AttemptLifecycleStatus.PAUSED,
        Retryability.REQUIRES_HUMAN, "resume_after_user_input",
        (RecoveryReasonCode.MEMBERSHIP_UNKNOWN,), consumes_budget=False,
    ),
    "broker_requires_manual_review": _Spec(
        ExecutionResultKind.MANUAL_CONTACT_OBSERVED, AttemptLifecycleStatus.TERMINAL,
        Retryability.NON_RETRYABLE, "manual_handoff",
        (RecoveryReasonCode.MANUAL_CONTACT_REQUIRED,),
        terminal_status=RouteOutcomeStatus.MANUAL_HANDOFF,
    ),
    "route_invalid": _Spec(
        ExecutionResultKind.UNRESOLVED, AttemptLifecycleStatus.TERMINAL,
        Retryability.NON_RETRYABLE, "stop_terminal",
        (RecoveryReasonCode.ROUTE_INVALID,),
        terminal_status=RouteOutcomeStatus.UNRESOLVED,
        fallback_terminal_status=RouteOutcomeStatus.UNRESOLVED,
    ),
    "ambiguous_field": _Spec(
        ExecutionResultKind.UNRESOLVED, AttemptLifecycleStatus.PAUSED,
        Retryability.REQUIRES_HUMAN, "resume_after_user_input",
        (), consumes_budget=False,
    ),
    "ambiguous_action": _Spec(
        ExecutionResultKind.UNRESOLVED, AttemptLifecycleStatus.PAUSED,
        Retryability.REQUIRES_HUMAN, "resume_after_user_input",
        (), consumes_budget=False,
    ),
    # --- explicit-evidence observations (never inferred from applicant data) ---
    "explicit_ineligible": _Spec(
        ExecutionResultKind.EXPLICIT_INELIGIBLE, AttemptLifecycleStatus.TERMINAL,
        Retryability.NON_RETRYABLE, "stop_terminal",
        (RecoveryReasonCode.EXPLICIT_INELIGIBILITY,),
        terminal_status=RouteOutcomeStatus.INELIGIBLE,
    ),
    "affinity_restricted": _Spec(
        ExecutionResultKind.AFFINITY_RESTRICTED, AttemptLifecycleStatus.TERMINAL,
        Retryability.NON_RETRYABLE, "stop_terminal",
        (RecoveryReasonCode.AFFINITY_REQUIREMENT_UNSATISFIED,),
        terminal_status=RouteOutcomeStatus.AFFINITY_RESTRICTED,
    ),
    "specialty_only": _Spec(
        ExecutionResultKind.SPECIALTY_ONLY, AttemptLifecycleStatus.TERMINAL,
        Retryability.NON_RETRYABLE, "stop_terminal",
        (RecoveryReasonCode.SPECIALTY_ONLY,),
        terminal_status=RouteOutcomeStatus.SPECIALTY_ONLY,
    ),
    "not_currently_writing": _Spec(
        ExecutionResultKind.NOT_CURRENTLY_WRITING, AttemptLifecycleStatus.TERMINAL,
        Retryability.NON_RETRYABLE, "stop_terminal",
        (RecoveryReasonCode.ROUTE_NOT_CURRENTLY_WRITING,),
        terminal_status=RouteOutcomeStatus.NOT_CURRENTLY_WRITING,
    ),
}

# Recoverable (resumable) checkpoint types -> paused / await_human_checkpoint.
_RECOVERABLE_CHECKPOINT_TYPES: frozenset[str] = frozenset(
    {
        "identity_lookup",
        "consent_attestation",
        "household_driver_consent",
    }
)

# Out-of-scope automation boundaries. The automation must STOP (never pretend
# it can resume automatically): terminal / manual_handoff.
_OUT_OF_SCOPE_CHECKPOINT_TYPES: frozenset[str] = frozenset(
    {
        "application_declaration",
        "signature",
        "payment",
        "purchase",
        "policy_binding",
        "renewal",
        "cancellation",
    }
)

# Retryable technical-error signals (checked against reason + safe_context).
_RETRYABLE_TECHNICAL_SIGNALS: tuple[tuple[str, RecoveryReasonCode], ...] = (
    ("navigation_timeout", RecoveryReasonCode.NAVIGATION_TIMEOUT),
    ("navigation timed out", RecoveryReasonCode.NAVIGATION_TIMEOUT),
    ("browser_crash", RecoveryReasonCode.BROWSER_CRASH),
    ("browser crashed", RecoveryReasonCode.BROWSER_CRASH),
    ("temporarily unavailable", RecoveryReasonCode.TRANSIENT_NAVIGATION_FAILURE),
    ("temporary outage", RecoveryReasonCode.TRANSIENT_NAVIGATION_FAILURE),
)


def _human_checkpoint_spec(ctx: dict[str, Any]) -> _Spec:
    """Human checkpoint: recoverable pause vs out-of-scope boundary."""
    checkpoint_type = str(ctx.get("checkpoint_type", "") or "")
    must_not_automate = bool(ctx.get("must_not_automate", False))
    if must_not_automate or checkpoint_type in _OUT_OF_SCOPE_CHECKPOINT_TYPES:
        return _Spec(
            ExecutionResultKind.HUMAN_CHECKPOINT_PROHIBITED,
            AttemptLifecycleStatus.TERMINAL,
            Retryability.NON_RETRYABLE,
            "manual_handoff",
            (RecoveryReasonCode.HUMAN_CHECKPOINT,),
            terminal_status=RouteOutcomeStatus.MANUAL_HANDOFF,
            fallback_terminal_status=RouteOutcomeStatus.MANUAL_HANDOFF,
        )
    return _Spec(
        ExecutionResultKind.HUMAN_CHECKPOINT_PAUSED,
        AttemptLifecycleStatus.PAUSED,
        Retryability.REQUIRES_HUMAN,
        "await_human_checkpoint",
        (RecoveryReasonCode.HUMAN_CHECKPOINT,),
        consumes_budget=False,
    )


def _needs_consent_spec(ctx: dict[str, Any]) -> _Spec:
    """Consent: undecided -> pause; denied -> terminal stop (never ineligible)."""
    if ctx.get("consent_state") == "denied":
        return _Spec(
            ExecutionResultKind.CONSENT_PAUSE, AttemptLifecycleStatus.TERMINAL,
            Retryability.NON_RETRYABLE, "stop_terminal",
            (RecoveryReasonCode.CONSENT_DENIED,),
        )
    return _TABLE["needs_consent"]


def _quote_spec(ctx: dict[str, Any], reason: Optional[str]) -> _Spec:
    """Quote observation: estimate-only (explicit evidence) vs pending normalization.

    Issue #8 never assigns quoted_comparable / quoted_non_comparable.
    """
    estimate_evidence = bool(ctx.get("estimate_only", False))
    if not estimate_evidence and reason:
        estimate_evidence = "estimate" in reason.lower() and not bool(ctx.get("is_firm_quote", False))
    if estimate_evidence:
        return _Spec(
            ExecutionResultKind.ESTIMATE_OBSERVED, AttemptLifecycleStatus.TERMINAL,
            Retryability.NON_RETRYABLE, "stop_terminal",
            (RecoveryReasonCode.ESTIMATE_OBSERVED,),
            terminal_status=RouteOutcomeStatus.ESTIMATE_ONLY,
            fallback_terminal_status=RouteOutcomeStatus.ESTIMATE_ONLY,
        )
    return _Spec(
        ExecutionResultKind.QUOTE_OBSERVED, AttemptLifecycleStatus.TERMINAL,
        Retryability.NON_RETRYABLE, "stop_terminal",
        (RecoveryReasonCode.QUOTE_OBSERVED,),
        quote_pending_normalization=True,
    )


def _technical_error_spec(ctx: dict[str, Any], reason: Optional[str],
                          policy: RecoveryPolicy) -> _Spec:
    """Technical error: deterministic retryability; NOT everything is retryable."""
    text = f"{reason or ''} {ctx.get('error_type', '')}".lower()
    if "unexpected_host" in text or ctx.get("error_type") == "unexpected_host":
        return _Spec(
            ExecutionResultKind.TECHNICAL_ERROR, AttemptLifecycleStatus.TERMINAL,
            Retryability.NON_RETRYABLE, "stop_terminal",
            (RecoveryReasonCode.UNEXPECTED_HOST,),
            fallback_terminal_status=RouteOutcomeStatus.BLOCKED,
        )
    for signal, reason_code in _RETRYABLE_TECHNICAL_SIGNALS:
        if signal in text:
            if reason_code is RecoveryReasonCode.NAVIGATION_TIMEOUT:
                allowed = policy.navigation_timeout_retryable
            elif reason_code is RecoveryReasonCode.BROWSER_CRASH:
                allowed = policy.browser_crash_retryable
            else:  # transient (e.g. temporary outage) - retryable by nature
                allowed = True
            retryability = Retryability.RETRYABLE if allowed else Retryability.UNKNOWN
            return _Spec(
                ExecutionResultKind.TECHNICAL_ERROR, AttemptLifecycleStatus.RECOVERABLE,
                retryability, "retry_same_route", (reason_code,),
                fallback_terminal_status=RouteOutcomeStatus.UNREACHABLE,
                failover_eligible=True,
            )
    # Unknown technical failure: NOT auto-retried (conservative). May failover.
    return _Spec(
        ExecutionResultKind.TECHNICAL_ERROR, AttemptLifecycleStatus.TERMINAL,
        Retryability.UNKNOWN, "manual_handoff",
        (RecoveryReasonCode.TRANSIENT_NAVIGATION_FAILURE,),
        fallback_terminal_status=RouteOutcomeStatus.UNREACHABLE,
        failover_eligible=True,
    )


def _validation_error_spec(ctx: dict[str, Any]) -> _Spec:
    """Validation error hardening (sect 16):

    - applicant-correctable (default) -> pause/resume for Issue #5 correction.
    - destination_incompatible -> value_not_supported (failover/manual).
    - unknown -> unresolved/manual review.
    Never retry identical invalid data.
    """
    kind = ctx.get("validation_kind")
    if kind == "destination_incompatible":
        return _Spec(
            ExecutionResultKind.VALUE_NOT_SUPPORTED, AttemptLifecycleStatus.TERMINAL,
            Retryability.NON_RETRYABLE, "manual_handoff",
            (RecoveryReasonCode.UNSUPPORTED_DESTINATION_VALUE,),
            fallback_terminal_status=RouteOutcomeStatus.MANUAL_HANDOFF,
            failover_eligible=True,
        )
    if kind == "unknown":
        return _Spec(
            ExecutionResultKind.UNRESOLVED, AttemptLifecycleStatus.TERMINAL,
            Retryability.NON_RETRYABLE, "manual_handoff",
            (RecoveryReasonCode.WEBSITE_VALIDATION_ERROR,),
            terminal_status=RouteOutcomeStatus.UNRESOLVED,
            fallback_terminal_status=RouteOutcomeStatus.UNRESOLVED,
        )
    return _TABLE["validation_error"]  # applicant-correctable -> pause/resume


def classify_observation(
    execution: ExecutionObservation,
    policy: Optional[RecoveryPolicy] = None,
) -> ClassifiedObservation:
    """Deterministically classify one execution observation."""
    policy = policy or RecoveryPolicy()
    otype = execution.observation_type
    ctx = execution.safe_context or {}
    if otype == "human_checkpoint":
        spec = _human_checkpoint_spec(ctx)
    elif otype == "needs_consent":
        spec = _needs_consent_spec(ctx)
    elif otype == "quote_detected":
        spec = _quote_spec(ctx, execution.reason)
    elif otype == "technical_error":
        spec = _technical_error_spec(ctx, execution.reason, policy)
    elif otype == "validation_error":
        spec = _validation_error_spec(ctx)
    else:
        spec = _TABLE.get(otype)
        if spec is None:
            # Unknown observation type: conservative terminal-unresolved pause
            # that requires developer mapping (never a fabricated answer).
            spec = _Spec(
                ExecutionResultKind.UNRESOLVED, AttemptLifecycleStatus.PAUSED,
                Retryability.REQUIRES_HUMAN, "resume_after_user_input",
                (RecoveryReasonCode.UNRESOLVED_RESULT,), consumes_budget=False,
            )
    return ClassifiedObservation(
        observation_type=otype,
        execution_result_kind=spec.result_kind,
        lifecycle_hint=spec.lifecycle,
        retryability=spec.retryability,
        reason_codes=list(spec.reason_codes),
        action_hint=spec.action,
        terminal_status=spec.terminal_status,
        fallback_terminal_status=spec.fallback_terminal_status,
        failover_eligible=spec.failover_eligible,
        consumes_budget=spec.consumes_budget,
        quote_pending_normalization=spec.quote_pending_normalization,
    )


# --- Issue #7 browser observation adapter -------------------------------

def browser_observation_to_execution(obs: BrowserObservation) -> ExecutionObservation:
    """Adapt an Issue #7 ``BrowserObservation`` into a generic execution obs.

    Extracts SAFE metadata only - never applicant values or raw quote refs.
    """
    ctx: dict[str, Any] = {}
    if obs.page_signature:
        ctx["page_signature"] = obs.page_signature
    if obs.url:
        ctx["url"] = obs.url
    if obs.missing_field_paths:
        ctx["missing_field_paths"] = list(obs.missing_field_paths)
    if obs.needs_consent_paths:
        ctx["needs_consent_paths"] = list(obs.needs_consent_paths)
    if obs.pending_field_paths:
        ctx["pending_field_paths"] = list(obs.pending_field_paths)
    if obs.unsupported_value_paths:
        ctx["unsupported_value_paths"] = list(obs.unsupported_value_paths)
    if obs.error_paths:
        ctx["error_paths"] = list(obs.error_paths)
    if obs.checkpoint:
        ctx["checkpoint_type"] = obs.checkpoint.checkpoint_type
        ctx["must_not_automate"] = obs.checkpoint.must_not_automate
    if obs.quote and obs.quote.quote_present:
        ctx["quote_present"] = True
        ctx["is_firm_quote"] = obs.quote.raw.is_firm_quote
        ctx["reference_present"] = obs.quote.reference_present
        if obs.quote.raw.private_reference_handle:
            ctx["private_reference_handle"] = obs.quote.raw.private_reference_handle
        validity = (obs.quote.raw.validity_text or "").lower()
        ctx["estimate_only"] = (not obs.quote.raw.is_firm_quote) and "estimate" in validity
    return ExecutionObservation(
        source_channel=SourceChannel.BROWSER,
        observation_type=obs.observation_type.value,
        reason=obs.message,
        safe_context=ctx,
    )
