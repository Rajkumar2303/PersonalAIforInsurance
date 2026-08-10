"""Issue #8 - deterministic observation classification tests."""

from __future__ import annotations

import pytest

from app.models.recovery import (
    AttemptLifecycleStatus,
    ExecutionObservation,
    ExecutionResultKind,
    RecoveryPolicy,
    Retryability,
    RouteOutcomeStatus,
    SourceChannel,
)
from app.services.recovery import classify_observation


def classify(obs_type: str, reason: str | None = None, ctx: dict | None = None,
             policy: RecoveryPolicy | None = None):
    return classify_observation(
        ExecutionObservation(
            source_channel=SourceChannel.BROWSER,
            observation_type=obs_type,
            reason=reason,
            safe_context=dict(ctx or {}),
        ),
        policy or RecoveryPolicy(),
    )


def test_needs_field_is_paused_not_terminal():
    c = classify("needs_field", ctx={"missing_field_paths": ["applicant.identity.legal_name"]})
    assert c.lifecycle_hint is AttemptLifecycleStatus.PAUSED
    assert c.execution_result_kind is ExecutionResultKind.FIELD_PAUSE
    assert c.reason_codes == ["missing_field"]
    assert c.action_hint == "resume_after_user_input"
    assert c.consumes_budget is False
    assert c.terminal_status is None


def test_needs_consent_undecided_is_paused():
    c = classify("needs_consent", ctx={"needs_consent_paths": ["a"]})
    assert c.lifecycle_hint is AttemptLifecycleStatus.PAUSED
    assert c.reason_codes == ["consent_required"]


def test_consent_denied_is_terminal_stop_never_ineligible():
    c = classify("needs_consent", ctx={"consent_state": "denied"})
    assert c.lifecycle_hint is AttemptLifecycleStatus.TERMINAL
    assert c.reason_codes == ["consent_denied"]
    assert c.terminal_status is None  # not ineligible


def test_recoverable_human_checkpoint_is_paused():
    for ctype in ("identity_lookup", "consent_attestation", "household_driver_consent"):
        c = classify("human_checkpoint", ctx={"checkpoint_type": ctype, "must_not_automate": False})
        assert c.lifecycle_hint is AttemptLifecycleStatus.PAUSED, ctype
        assert c.action_hint == "await_human_checkpoint"
        assert c.terminal_status is None


def test_prohibited_checkpoints_are_terminal_manual_handoff():
    for ctype in ("signature", "payment", "purchase", "policy_binding",
                  "renewal", "cancellation", "application_declaration"):
        c = classify("human_checkpoint", ctx={"checkpoint_type": ctype})
        assert c.lifecycle_hint is AttemptLifecycleStatus.TERMINAL, ctype
        assert c.terminal_status == "manual_handoff"
        assert c.action_hint == "manual_handoff"
        assert c.retryability is Retryability.NON_RETRYABLE


def test_must_not_automate_flag_forces_prohibited():
    c = classify("human_checkpoint", ctx={"checkpoint_type": "unknown_kind", "must_not_automate": True})
    assert c.lifecycle_hint is AttemptLifecycleStatus.TERMINAL
    assert c.terminal_status == "manual_handoff"


def test_access_control_is_blocked_no_retry():
    c = classify("access_control_detected", ctx={"error_type": "captcha"})
    assert c.lifecycle_hint is AttemptLifecycleStatus.TERMINAL
    assert c.terminal_status == "blocked"
    assert c.reason_codes == ["captcha_or_bot_control"]
    assert c.retryability is Retryability.NON_RETRYABLE


def test_unknown_external_field_is_paused_for_mapping():
    c = classify("unknown_external_field", ctx={"unknown_external_fields": ["shoe_size"]})
    assert c.lifecycle_hint is AttemptLifecycleStatus.PAUSED
    assert c.reason_codes == ["unknown_required_field"]
    assert c.terminal_status is None


def test_quote_observed_never_comparable():
    c = classify("quote_detected", ctx={"quote_present": True, "is_firm_quote": True, "reference_present": True})
    assert c.lifecycle_hint is AttemptLifecycleStatus.TERMINAL
    assert c.execution_result_kind is ExecutionResultKind.QUOTE_OBSERVED
    assert c.quote_pending_normalization is True
    assert c.terminal_status is None  # NOT quoted_comparable / quoted_non_comparable
    assert c.reason_codes == ["quote_observed"]


def test_estimate_observed_only_with_evidence():
    c = classify("quote_detected", reason="estimate only",
                 ctx={"quote_present": True, "is_firm_quote": False, "estimate_only": True})
    assert c.execution_result_kind is ExecutionResultKind.ESTIMATE_OBSERVED
    assert c.terminal_status == "estimate_only"
    assert c.quote_pending_normalization is False


def test_callback_and_manual_distinct():
    cb = classify("callback_detected")
    assert cb.terminal_status == "callback_required"
    assert cb.action_hint == "prepare_voice_handoff"
    mc = classify("manual_contact_detected")
    assert mc.terminal_status == "manual_handoff"
    assert mc.action_hint == "manual_handoff"
    assert cb.terminal_status != mc.terminal_status


def test_navigation_timeout_retryable_when_policy_allows():
    c = classify("technical_error", reason="navigation timed out", ctx={"error_type": "navigation_timeout"})
    assert c.lifecycle_hint is AttemptLifecycleStatus.RECOVERABLE
    assert c.retryability is Retryability.RETRYABLE
    assert c.action_hint == "retry_same_route"
    assert c.reason_codes == ["navigation_timeout"]
    assert c.failover_eligible is True
    assert c.fallback_terminal_status == "unreachable"


def test_navigation_timeout_not_retryable_when_policy_disallows():
    policy = RecoveryPolicy(navigation_timeout_retryable=False)
    c = classify("technical_error", reason="navigation timed out", ctx={"error_type": "navigation_timeout"}, policy=policy)
    assert c.retryability is Retryability.UNKNOWN  # never auto-retried


def test_browser_crash_retryable_when_policy_allows():
    c = classify("technical_error", reason="browser crashed", ctx={"error_type": "browser_crash"})
    assert c.retryability is Retryability.RETRYABLE
    assert c.reason_codes == ["browser_crash"]


def test_unknown_technical_error_not_auto_retried():
    c = classify("technical_error", reason="weird network thing", ctx={})
    assert c.retryability is Retryability.UNKNOWN
    assert c.lifecycle_hint is AttemptLifecycleStatus.TERMINAL  # conservative
    assert c.failover_eligible is True
    assert c.fallback_terminal_status == "unreachable"


def test_unexpected_host_is_non_retryable_safety_stop():
    c = classify("technical_error", reason="redirected", ctx={"error_type": "unexpected_host"})
    assert c.retryability is Retryability.NON_RETRYABLE
    assert c.reason_codes == ["unexpected_host"]
    assert c.fallback_terminal_status == "blocked"


def test_complete_without_quote_is_unresolved():
    c = classify("complete_without_quote")
    assert c.lifecycle_hint is AttemptLifecycleStatus.TERMINAL
    assert c.terminal_status == "unresolved"
    assert c.reason_codes == ["unresolved_result"]


def test_unsupported_page_is_unresolved():
    c = classify("unsupported_page")
    assert c.terminal_status == "unresolved"


def test_value_not_supported_is_failover_eligible_not_fabricated():
    c = classify("value_not_supported", ctx={"unsupported_value_paths": ["a"]})
    assert c.lifecycle_hint is AttemptLifecycleStatus.TERMINAL
    assert c.failover_eligible is True
    assert c.fallback_terminal_status == "manual_handoff"
    assert c.reason_codes == ["unsupported_destination_value"]


def test_validation_error_is_paused_for_user_correction():
    c = classify("validation_error", ctx={"error_paths": ["product_data.vehicles[0].use.annual_kilometres"]})
    assert c.lifecycle_hint is AttemptLifecycleStatus.PAUSED
    assert c.action_hint == "resume_after_user_input"
    assert c.reason_codes == ["website_validation_error"]


def test_explicit_evidence_statuses_never_inferred():
    assert classify("explicit_ineligible").terminal_status == "ineligible"
    assert classify("affinity_restricted").terminal_status == "affinity_restricted"
    assert classify("specialty_only").terminal_status == "specialty_only"
    assert classify("not_currently_writing").terminal_status == "not_currently_writing"
    # A mere technical failure must NOT become not_currently_writing.
    assert classify("technical_error", reason="timeout", ctx={"error_type": "navigation_timeout"}).terminal_status is None


def test_unknown_observation_type_is_paused_unresolved_no_fabrication():
    c = classify("brand_new_observation")
    assert c.lifecycle_hint is AttemptLifecycleStatus.PAUSED
    assert c.terminal_status is None
    assert "unresolved_result" in c.reason_codes
