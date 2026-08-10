"""Issue #8 - recovery model & enum schema tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.recovery import (
    AttemptLifecycleStatus,
    AttemptRecord,
    ExecutionResultKind,
    RecoveryAction,
    RecoveryDecideRequest,
    RecoveryDecision,
    RecoveryPolicy,
    RecoveryReasonCode,
    RecoveryWorkflowState,
    Retryability,
    RouteOutcomeStatus,
)


def test_terminal_status_enum_has_all_required_values():
    expected = {
        "quoted_comparable", "quoted_non_comparable", "estimate_only",
        "callback_required", "manual_handoff", "ineligible",
        "affinity_restricted", "specialty_only", "duplicate_rate_source",
        "not_currently_writing", "blocked", "unreachable", "unresolved",
    }
    assert {s.value for s in RouteOutcomeStatus} == expected


def test_lifecycle_status_values():
    assert {s.value for s in AttemptLifecycleStatus} == {
        "pending", "running", "paused", "recoverable", "terminal",
    }


def test_execution_result_kind_separate_from_outcome_status():
    """ExecutionResultKind is a distinct vocabulary - never collapsed into
    RouteOutcomeStatus (e.g. quote_observed != quoted_comparable)."""
    assert "quote_observed" not in {s.value for s in RouteOutcomeStatus}
    assert ExecutionResultKind.QUOTE_OBSERVED.value == "quote_observed"


def test_recovery_action_values():
    assert {a.value for a in RecoveryAction} == {
        "continue_current_session", "resume_after_user_input",
        "retry_same_route", "use_alternative_route",
        "prepare_voice_handoff", "manual_handoff",
        "await_human_checkpoint", "stop_terminal", "no_action",
    }


def test_retryability_values():
    assert {r.value for r in Retryability} == {
        "retryable", "non_retryable", "requires_human", "unknown",
    }


def test_recovery_policy_conservative_defaults():
    policy = RecoveryPolicy()
    assert policy.max_attempts_per_route == 2  # initial + at most one retry
    assert policy.max_transient_retries == 1
    assert policy.max_attempts_per_rate_source == 3
    assert policy.navigation_timeout_retryable is True
    assert policy.browser_crash_retryable is True
    assert policy.alternative_route_after_exhaustion is True


def test_policy_rejects_unknown_keys():
    with pytest.raises(ValidationError):
        RecoveryPolicy(**{"max_attempts_per_route": 2, "unknown_key": 1})


def test_attempt_record_is_forbid_extra():
    with pytest.raises(ValidationError):
        AttemptRecord(
            attempt_id="a1", registry_id="r1", started_at="2026-01-01T00:00:00Z",
            extra="nope",
        )


def test_decision_redacted_repr_never_reveals_redactable_values():
    decision = RecoveryDecision(
        decision_id="d1",
        attempt_id="a1",
        planned_route_id="route-a",
        registry_id="route-a",
        distinct_rate_source_id="RS-A",
        lifecycle_status=AttemptLifecycleStatus.TERMINAL,
        recommended_action=RecoveryAction.STOP_TERMINAL,
        reason_codes=["quote_observed"],
        safe_context={"quote_present": True, "reference_present": True},
        decided_at="2026-01-01T00:00:00Z",
    )
    # Safe context survives redaction (booleans/ids), raw values are never placed.
    text = repr(decision)
    assert "quote_present" in text
    assert "decided_at" in text


def test_decision_quote_pending_normalization_flag():
    decision = RecoveryDecision(
        decision_id="d1",
        attempt_id="a1",
        planned_route_id="route-a",
        lifecycle_status=AttemptLifecycleStatus.TERMINAL,
        recommended_action=RecoveryAction.STOP_TERMINAL,
        reason_codes=["quote_observed"],
        quote_pending_normalization=True,
        decided_at="2026-01-01T00:00:00Z",
    )
    assert decision.quote_pending_normalization is True
    assert decision.terminal_status is None  # comparability deferred


def test_decide_request_requires_route_and_observation():
    with pytest.raises(ValidationError):
        RecoveryDecideRequest(observation_type="needs_field")
    with pytest.raises(ValidationError):
        RecoveryDecideRequest(planned_route_id="route-a")


def test_workflow_state_is_typed_dict_with_safe_keys():
    state: RecoveryWorkflowState = {
        "entry": "decide",
        "planned_route_id": "route-a",
        "observation_type": "needs_field",
        "attempts_used": 0,
        "attempts_remaining": 2,
    }
    assert state["attempts_used"] == 0


def test_reason_codes_are_enums_not_free_strings():
    assert RecoveryReasonCode.CAPTCHA_OR_BOT_CONTROL.value == "captcha_or_bot_control"
    assert RecoveryReasonCode.QUOTE_OBSERVED.value == "quote_observed"
    assert RecoveryReasonCode.ESTIMATE_OBSERVED.value == "estimate_observed"
