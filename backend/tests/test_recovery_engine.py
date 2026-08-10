"""Issue #8 - recovery engine core decision tests (deterministic, hermetic)."""

from __future__ import annotations

import pytest

from app.models.recovery import (
    AttemptLifecycleStatus,
    RecoveryAction,
    RecoveryDecideRequest,
    RecoveryPolicy,
    RouteOutcomeStatus,
)
from app.services.recovery.attempt_store import TransitionError
from app.services.recovery.engine import RecoveryEngine
from recovery_helpers import (
    StubRouteSource,
    make_recovery_env,
    req,
    standard_rs_entries,
)


# --- pause vs terminal ---------------------------------------------------

def test_missing_field_pause_not_terminal(tmp_path):
    env = make_recovery_env(tmp_path)
    decision = env.engine.record_observation(req(env, "needs_field"))
    assert decision.lifecycle_status is AttemptLifecycleStatus.PAUSED
    assert decision.recommended_action is RecoveryAction.RESUME_AFTER_USER_INPUT
    assert decision.terminal_status is None
    assert decision.retry_allowed is False
    # No attempt budget consumed: a later technical retry still sees 1 remaining.
    d2 = env.engine.record_observation(req(env, "technical_error", reason="nav",
                                           ctx={"error_type": "navigation_timeout"}))
    assert d2.recommended_action is RecoveryAction.RETRY_SAME_ROUTE
    assert d2.attempts_used == 1
    assert d2.attempts_remaining == 1


def test_consent_undecided_pause_denied_terminal_not_ineligible(tmp_path):
    env = make_recovery_env(tmp_path)
    paused = env.engine.record_observation(req(env, "needs_consent", ctx={"consent_state": "undecided"}))
    assert paused.lifecycle_status is AttemptLifecycleStatus.PAUSED
    denied = env.engine.record_observation(
        req(env, "needs_consent", ctx={"consent_state": "denied"}, plan_id="plan-x")
    )
    assert denied.lifecycle_status is AttemptLifecycleStatus.TERMINAL
    assert denied.recommended_action is RecoveryAction.STOP_TERMINAL
    assert denied.terminal_status is None  # never converted to ineligible
    assert "consent_denied" in denied.reason_codes


def test_recoverable_human_checkpoint_paused_resumable(tmp_path):
    env = make_recovery_env(tmp_path)
    decision = env.engine.record_observation(
        req(env, "human_checkpoint", ctx={"checkpoint_type": "identity_lookup", "must_not_automate": False})
    )
    assert decision.lifecycle_status is AttemptLifecycleStatus.PAUSED
    assert decision.recommended_action is RecoveryAction.AWAIT_HUMAN_CHECKPOINT
    assert decision.terminal_status is None


def test_prohibited_human_checkpoint_terminal_manual_handoff(tmp_path):
    env = make_recovery_env(tmp_path)
    for ctype in ("signature", "payment", "purchase", "policy_binding",
                  "renewal", "cancellation", "application_declaration"):
        decision = env.engine.record_observation(
            req(env, "human_checkpoint", ctx={"checkpoint_type": ctype}, plan_id=f"plan-{ctype}")
        )
        assert decision.lifecycle_status is AttemptLifecycleStatus.TERMINAL, ctype
        assert decision.recommended_action in (RecoveryAction.MANUAL_HANDOFF, RecoveryAction.STOP_TERMINAL)
        assert decision.terminal_status is RouteOutcomeStatus.MANUAL_HANDOFF


# --- transient retry ------------------------------------------------------

def test_transient_retry_then_quote(tmp_path):
    env = make_recovery_env(tmp_path)
    a1 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-a",
                                  registry_id="route-a", distinct_rate_source_id="RS-A")
    d1 = env.engine.record_observation(
        req(env, "technical_error", reason="navigation timed out", ctx={"error_type": "navigation_timeout"}),
        a1,
    )
    assert d1.lifecycle_status is AttemptLifecycleStatus.RECOVERABLE
    assert d1.recommended_action is RecoveryAction.RETRY_SAME_ROUTE
    assert d1.retry_allowed is True
    assert d1.attempts_used == 1
    assert d1.attempts_remaining == 1

    a2 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-a",
                                  registry_id="route-a", distinct_rate_source_id="RS-A")
    d2 = env.engine.record_observation(
        req(env, "quote_detected", ctx={"quote_present": True, "is_firm_quote": True}), a2
    )
    assert d2.lifecycle_status is AttemptLifecycleStatus.TERMINAL
    assert d2.quote_pending_normalization is True
    assert d2.retry_allowed is False
    assert d2.terminal_status is None


# --- retry exhaustion -----------------------------------------------------

def test_retry_exhaustion_no_third_attempt(tmp_path):
    env = make_recovery_env(tmp_path)
    a1 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-a",
                                  registry_id="route-a", distinct_rate_source_id="RS-A")
    d1 = env.engine.record_observation(req(env, "technical_error", reason="timeout",
                                           ctx={"error_type": "navigation_timeout"}), a1)
    assert d1.recommended_action is RecoveryAction.RETRY_SAME_ROUTE
    a2 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-a",
                                  registry_id="route-a", distinct_rate_source_id="RS-A")
    d2 = env.engine.record_observation(req(env, "technical_error", reason="timeout",
                                           ctx={"error_type": "navigation_timeout"}), a2)
    assert d2.lifecycle_status is AttemptLifecycleStatus.TERMINAL
    assert d2.terminal_status is RouteOutcomeStatus.UNREACHABLE
    assert "retry_budget_exhausted" in d2.reason_codes
    assert "no_alternative_route" in d2.reason_codes
    # No third attempt was created.
    assert len(env.store.list_by_route("plan-1", "route-a")) == 2


# --- alternative failover -------------------------------------------------

def test_alternative_failover_no_distinct_source_inflation(tmp_path):
    env = make_recovery_env(tmp_path)
    env.engine._route_source = StubRouteSource({"route-a": ["route-b"]})
    a1 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-a",
                                  registry_id="route-a", distinct_rate_source_id="RS-TEST-001")
    env.engine.record_observation(req(env, "technical_error", reason="timeout",
                                      ctx={"error_type": "navigation_timeout"}, distinct_rate_source_id="RS-TEST-001"), a1)
    a2 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-a",
                                  registry_id="route-a", distinct_rate_source_id="RS-TEST-001")
    d2 = env.engine.record_observation(req(env, "technical_error", reason="timeout",
                                           ctx={"error_type": "navigation_timeout"}, distinct_rate_source_id="RS-TEST-001"), a2)
    assert d2.recommended_action is RecoveryAction.USE_ALTERNATIVE_ROUTE
    assert d2.alternative_route_id == "route-b"
    assert d2.retry_allowed is True
    assert d2.distinct_rate_source_id == "RS-TEST-001"  # same rate source
    assert "alternate_route_available" in d2.reason_codes  # multiple reasons
    assert "navigation_timeout" in d2.reason_codes


def test_alternative_also_fails_rate_source_budget_exhausted(tmp_path):
    entries, rate_sources = standard_rs_entries()
    env = make_recovery_env(tmp_path, entries=entries, rate_sources=rate_sources)
    env.engine._route_source = StubRouteSource({"route-a": ["route-b"], "route-b": []})
    # A1 (retry), A2 (failover) -> B1
    a1 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-a", registry_id="route-a", distinct_rate_source_id="RS-TEST-001")
    env.engine.record_observation(req(env, "technical_error", reason="timeout", ctx={"error_type": "navigation_timeout"}, distinct_rate_source_id="RS-TEST-001"), a1)
    a2 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-a", registry_id="route-a", distinct_rate_source_id="RS-TEST-001")
    d_fail = env.engine.record_observation(req(env, "technical_error", reason="timeout", ctx={"error_type": "navigation_timeout"}, distinct_rate_source_id="RS-TEST-001"), a2)
    assert d_fail.recommended_action is RecoveryAction.USE_ALTERNATIVE_ROUTE
    # B1 is attempt #3 (rate-source budget = 3) - if it fails, terminal, no loop.
    b1 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-b", registry_id="route-b", distinct_rate_source_id="RS-TEST-001")
    assert b1.attempt_number == 3
    d_b = env.engine.record_observation(req(env, "technical_error", reason="timeout", ctx={"error_type": "navigation_timeout"}, registry_id="route-b", distinct_rate_source_id="RS-TEST-001"), b1)
    assert d_b.lifecycle_status is AttemptLifecycleStatus.TERMINAL
    assert d_b.terminal_status is RouteOutcomeStatus.UNREACHABLE
    # No 4th attempt.
    assert len(env.store.list_by_rate_source("plan-1", "RS-TEST-001")) == 3


def test_failover_quote_preserves_execution_not_duplicate(tmp_path):
    env = make_recovery_env(tmp_path)
    env.engine._route_source = StubRouteSource({"route-a": ["route-b"], "route-b": []})
    a1 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-a", registry_id="route-a", distinct_rate_source_id="RS-TEST-001")
    env.engine.record_observation(req(env, "technical_error", reason="timeout", ctx={"error_type": "navigation_timeout"}, distinct_rate_source_id="RS-TEST-001"), a1)
    a2 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-a", registry_id="route-a", distinct_rate_source_id="RS-TEST-001")
    d = env.engine.record_observation(req(env, "technical_error", reason="timeout", ctx={"error_type": "navigation_timeout"}, distinct_rate_source_id="RS-TEST-001"), a2)
    assert d.alternative_route_id == "route-b"
    b1 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-b", registry_id="route-b", distinct_rate_source_id="RS-TEST-001")
    dq = env.engine.record_observation(req(env, "quote_detected", ctx={"quote_present": True, "is_firm_quote": True}, registry_id="route-b", distinct_rate_source_id="RS-TEST-001"), b1)
    assert dq.lifecycle_status is AttemptLifecycleStatus.TERMINAL
    assert dq.quote_pending_normalization is True  # executed route - NOT duplicate_rate_source
    executed = env.store.get(b1.attempt_id)
    assert executed.terminal_status is None
    assert executed.quote_pending_normalization is True


# --- safety ----------------------------------------------------------------

def test_captcha_blocked_no_retry_no_bypass(tmp_path):
    env = make_recovery_env(tmp_path)
    decision = env.engine.record_observation(req(env, "access_control_detected", ctx={"error_type": "captcha"}))
    assert decision.lifecycle_status is AttemptLifecycleStatus.TERMINAL
    assert decision.terminal_status is RouteOutcomeStatus.BLOCKED
    assert decision.retry_allowed is False
    assert decision.recommended_action is RecoveryAction.MANUAL_HANDOFF
    # Duplicate submission is idempotent: still one attempt, still blocked.
    again = env.engine.record_observation(req(env, "access_control_detected", ctx={"error_type": "captcha"}))
    assert again.terminal_status is RouteOutcomeStatus.BLOCKED
    assert len(env.store.list_all()) == 1


def test_unexpected_host_is_safety_stop(tmp_path):
    env = make_recovery_env(tmp_path)
    decision = env.engine.record_observation(req(env, "technical_error", reason="redirected", ctx={"error_type": "unexpected_host"}))
    assert decision.lifecycle_status is AttemptLifecycleStatus.TERMINAL
    assert decision.terminal_status is RouteOutcomeStatus.BLOCKED
    assert "unexpected_host" in decision.reason_codes


# --- unknown field / validation -------------------------------------------

def test_unknown_field_pause_then_intentional_end_unresolved(tmp_path):
    env = make_recovery_env(tmp_path)
    d1 = env.engine.record_observation(req(env, "unknown_external_field", ctx={"unknown_external_fields": ["shoe_size"]}))
    assert d1.lifecycle_status is AttemptLifecycleStatus.PAUSED
    assert d1.terminal_status is None
    # After mapping added, resume proceeds (paused, not terminal, no budget consumed).
    resume = env.engine.record_observation(req(env, "fields_filled"))
    assert resume.lifecycle_status is AttemptLifecycleStatus.RUNNING
    # If the bounded workflow is intentionally ended with an unmapped page:
    d2 = env.engine.record_observation(req(env, "unsupported_page", plan_id="plan-end"))
    assert d2.lifecycle_status is AttemptLifecycleStatus.TERMINAL
    assert d2.terminal_status is RouteOutcomeStatus.UNRESOLVED


def test_validation_correction_is_pause_not_restart(tmp_path):
    env = make_recovery_env(tmp_path)
    decision = env.engine.record_observation(
        req(env, "validation_error", ctx={"error_paths": ["product_data.vehicles[0].use.annual_kilometres"]})
    )
    assert decision.lifecycle_status is AttemptLifecycleStatus.PAUSED
    assert decision.recommended_action is RecoveryAction.RESUME_AFTER_USER_INPUT
    assert decision.terminal_status is None


# --- callback / manual ----------------------------------------------------

def test_callback_requires_voice_handoff_no_call(tmp_path):
    env = make_recovery_env(tmp_path)
    decision = env.engine.record_observation(req(env, "callback_detected"))
    assert decision.lifecycle_status is AttemptLifecycleStatus.TERMINAL
    assert decision.terminal_status is RouteOutcomeStatus.CALLBACK_REQUIRED
    assert decision.recommended_action is RecoveryAction.PREPARE_VOICE_HANDOFF


def test_manual_contact_is_manual_handoff(tmp_path):
    env = make_recovery_env(tmp_path)
    decision = env.engine.record_observation(req(env, "manual_contact_detected"))
    assert decision.lifecycle_status is AttemptLifecycleStatus.TERMINAL
    assert decision.terminal_status is RouteOutcomeStatus.MANUAL_HANDOFF
    assert decision.recommended_action is RecoveryAction.MANUAL_HANDOFF


# --- explicit evidence statuses -------------------------------------------

def test_explicit_ineligible_only_from_explicit_evidence(tmp_path):
    env = make_recovery_env(tmp_path)
    decision = env.engine.record_observation(req(env, "explicit_ineligible", reason="we cannot offer coverage for this risk"))
    assert decision.lifecycle_status is AttemptLifecycleStatus.TERMINAL
    assert decision.terminal_status is RouteOutcomeStatus.INELIGIBLE


def test_negative_profile_traits_never_infer_ineligible(tmp_path):
    """The STANDARD complete persona (age/claims/convictions present) does NOT
    cause Issue #8 to infer ineligibility for a routine observation."""
    env = make_recovery_env(tmp_path)  # complete_starter persona is in the vault
    d1 = env.engine.record_observation(req(env, "needs_field"))
    assert d1.terminal_status is None
    assert d1.lifecycle_status is AttemptLifecycleStatus.PAUSED
    d2 = env.engine.record_observation(req(env, "technical_error", reason="timeout", ctx={"error_type": "navigation_timeout"}, plan_id="p2"))
    assert d2.terminal_status is None  # retryable, not ineligible


def test_affinity_specialty_not_writing_explicit_only(tmp_path):
    env = make_recovery_env(tmp_path)
    aff = env.engine.record_observation(req(env, "affinity_restricted"))
    assert aff.terminal_status is RouteOutcomeStatus.AFFINITY_RESTRICTED
    spec = env.engine.record_observation(req(env, "specialty_only", plan_id="p2"))
    assert spec.terminal_status is RouteOutcomeStatus.SPECIALTY_ONLY
    nw = env.engine.record_observation(req(env, "not_currently_writing", plan_id="p3"))
    assert nw.terminal_status is RouteOutcomeStatus.NOT_CURRENTLY_WRITING
    # A technical site failure must NOT become not_currently_writing.
    tech = env.engine.record_observation(req(env, "technical_error", reason="timeout", ctx={"error_type": "navigation_timeout"}, plan_id="p4"))
    assert tech.terminal_status is None


# --- quote / estimate -----------------------------------------------------

def test_quote_observation_stops_retry_pending_normalization(tmp_path):
    env = make_recovery_env(tmp_path)
    decision = env.engine.record_observation(
        req(env, "quote_detected", ctx={"quote_present": True, "is_firm_quote": True, "reference_present": True})
    )
    assert decision.lifecycle_status is AttemptLifecycleStatus.TERMINAL
    assert decision.retry_allowed is False
    assert decision.quote_pending_normalization is True
    assert decision.terminal_status is None
    assert decision.terminal_status not in (RouteOutcomeStatus.QUOTED_COMPARABLE, RouteOutcomeStatus.QUOTED_NON_COMPARABLE)


def test_estimate_only_never_upgraded_to_firm(tmp_path):
    env = make_recovery_env(tmp_path)
    decision = env.engine.record_observation(
        req(env, "quote_detected", reason="estimate", ctx={"quote_present": True, "is_firm_quote": False, "estimate_only": True})
    )
    assert decision.terminal_status is RouteOutcomeStatus.ESTIMATE_ONLY
    assert decision.quote_pending_normalization is False
    assert "estimate_observed" in decision.reason_codes


# --- duplicate rate source -------------------------------------------------

def test_unused_duplicate_alternative_classified_duplicate_rate_source(tmp_path):
    env = make_recovery_env(tmp_path)
    decision = env.engine.classify_unused_alternative(
        plan_id="plan-1", registry_id="route-b", distinct_rate_source_id="RS-TEST-001"
    )
    assert decision.lifecycle_status is AttemptLifecycleStatus.TERMINAL
    assert decision.terminal_status is RouteOutcomeStatus.DUPLICATE_RATE_SOURCE
    assert decision.recommended_action is RecoveryAction.STOP_TERMINAL


def test_executed_alternative_not_duplicate_rate_source(tmp_path):
    """An alternative actually used after failover keeps its execution status."""
    env = make_recovery_env(tmp_path)
    env.engine._route_source = StubRouteSource({"route-a": ["route-b"]})
    a1 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-a", registry_id="route-a", distinct_rate_source_id="RS-TEST-001")
    env.engine.record_observation(req(env, "technical_error", reason="timeout", ctx={"error_type": "navigation_timeout"}, distinct_rate_source_id="RS-TEST-001"), a1)
    a2 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-a", registry_id="route-a", distinct_rate_source_id="RS-TEST-001")
    d = env.engine.record_observation(req(env, "technical_error", reason="timeout", ctx={"error_type": "navigation_timeout"}, distinct_rate_source_id="RS-TEST-001"), a2)
    assert d.alternative_route_id == "route-b"
    b1 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-b", registry_id="route-b", distinct_rate_source_id="RS-TEST-001")
    dq = env.engine.record_observation(req(env, "quote_detected", ctx={"quote_present": True, "is_firm_quote": True}, registry_id="route-b", distinct_rate_source_id="RS-TEST-001"), b1)
    assert dq.quote_pending_normalization is True
    executed = env.store.get(b1.attempt_id)
    assert executed.terminal_status is None  # NOT duplicate_rate_source


# --- value not supported ---------------------------------------------------

def test_value_not_supported_failover_then_manual_handoff(tmp_path):
    env = make_recovery_env(tmp_path)
    env.engine._route_source = StubRouteSource({"route-a": ["route-b"]})
    decision = env.engine.record_observation(req(env, "value_not_supported", ctx={"unsupported_value_paths": ["a"]}))
    assert decision.recommended_action is RecoveryAction.USE_ALTERNATIVE_ROUTE
    assert decision.alternative_route_id == "route-b"
    # No alternative available -> manual handoff, never a fabricated value.
    env2 = make_recovery_env(tmp_path)
    d2 = env2.engine.record_observation(req(env2, "value_not_supported", ctx={"unsupported_value_paths": ["a"]}))
    assert d2.lifecycle_status is AttemptLifecycleStatus.TERMINAL
    assert d2.terminal_status is RouteOutcomeStatus.MANUAL_HANDOFF


# --- idempotency / terminal immutability ----------------------------------

def test_terminal_attempt_is_immutable(tmp_path):
    env = make_recovery_env(tmp_path)
    decision = env.engine.record_observation(req(env, "access_control_detected"))
    attempt_id = decision.attempt_id
    before = env.store.get(attempt_id)
    # Attempting to silently mutate a terminal attempt is rejected (hardened).
    with pytest.raises(TransitionError):
        env.store.update(attempt_id, lifecycle_status=AttemptLifecycleStatus.PAUSED)
    after = env.store.get(attempt_id)
    assert after.lifecycle_status is AttemptLifecycleStatus.TERMINAL
    assert after.terminal_status is RouteOutcomeStatus.BLOCKED
    assert after == before


def test_duplicate_observation_does_not_double_count(tmp_path):
    env = make_recovery_env(tmp_path)
    d1 = env.engine.record_observation(req(env, "quote_detected", ctx={"quote_present": True, "is_firm_quote": True}))
    d2 = env.engine.record_observation(req(env, "quote_detected", ctx={"quote_present": True, "is_firm_quote": True}))
    assert d2.attempt_id == d1.attempt_id  # same attempt, no new attempt created
    assert d2.terminal_status is None
    assert d2.quote_pending_normalization is True
    assert len(env.store.list_all()) == 1
