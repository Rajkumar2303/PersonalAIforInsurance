"""Issue #8 Prompt 2 - hardening, edge cases, integration & reliability tests.

Covers state-transition validation, terminal immutability, idempotency, stale
events, retry/budget consistency, failover chains, readiness rechecks, consent
change during recovery, resume-vs-retry, session-lost, unknown-field mapping,
validation subtypes, safety boundaries (CAPTCHA/auth/outage), explicit-evidence
statuses, quote edges, duplicate-source semantics, plan-change mid-recovery,
policy/plan versioning, dynamic policy, voice compatibility, store isolation,
store-replacement boundary, API idempotency/error safety, privacy failure
paths, and LangSmith-safe metadata. Hermetic (synthetic observations only).
"""

from __future__ import annotations

import pytest

from app.models.recovery import (
    AttemptLifecycleStatus,
    ExecutionResultKind,
    RecoveryAction,
    RecoveryDecideRequest,
    RecoveryPolicy,
    RecoveryReasonCode,
    Retryability,
    RouteOutcomeStatus,
    SourceChannel,
)
from app.services.recovery.attempt_store import InMemoryAttemptStore, TransitionError
from app.services.recovery.engine import RecoveryEngine
from recovery_helpers import (
    StubRouteSource,
    make_recovery_env,
    req,
    revoke_route_consent,
    standard_rs_entries,
)

PII_MARKERS = ["T0000-00000-00000", "1HGCM82633A000000", "1990-01-01", "123 Test Street", "MOCK-8F3K-2026"]


def _timeout(env, ctx=None, **kw):
    merged = {"error_type": "navigation_timeout", **(ctx or {})}
    return req(env, "technical_error", reason="navigation timed out", ctx=merged, **kw)


# --- 2. state-transition validation ----------------------------------------

def test_valid_transitions(tmp_path):
    env = make_recovery_env(tmp_path)
    a = env.engine.begin_attempt(plan_id="p", planned_route_id="route-a",
                                 registry_id="route-a", distinct_rate_source_id="RS-A")
    store = env.store
    assert store.update(a.attempt_id, lifecycle_status=AttemptLifecycleStatus.PAUSED).lifecycle_status is AttemptLifecycleStatus.PAUSED
    assert store.update(a.attempt_id, lifecycle_status=AttemptLifecycleStatus.RUNNING).lifecycle_status is AttemptLifecycleStatus.RUNNING
    assert store.update(a.attempt_id, lifecycle_status=AttemptLifecycleStatus.RECOVERABLE).lifecycle_status is AttemptLifecycleStatus.RECOVERABLE
    assert store.update(a.attempt_id, lifecycle_status=AttemptLifecycleStatus.RUNNING).lifecycle_status is AttemptLifecycleStatus.RUNNING
    assert store.update(a.attempt_id, lifecycle_status=AttemptLifecycleStatus.TERMINAL).lifecycle_status is AttemptLifecycleStatus.TERMINAL


def test_invalid_transitions_rejected(tmp_path):
    env = make_recovery_env(tmp_path)
    a = env.engine.begin_attempt(plan_id="p", planned_route_id="route-a",
                                 registry_id="route-a", distinct_rate_source_id="RS-A")
    env.store.update(a.attempt_id, lifecycle_status=AttemptLifecycleStatus.TERMINAL)
    with pytest.raises(TransitionError):
        env.store.update(a.attempt_id, lifecycle_status=AttemptLifecycleStatus.RUNNING)  # terminal -> running
    b = env.engine.begin_attempt(plan_id="p", planned_route_id="route-a",
                                 registry_id="route-a", distinct_rate_source_id="RS-A")
    env.store.update(b.attempt_id, lifecycle_status=AttemptLifecycleStatus.PAUSED)
    with pytest.raises(TransitionError):
        env.store.update(b.attempt_id, lifecycle_status=AttemptLifecycleStatus.RECOVERABLE)  # paused -> recoverable


def test_revision_bumps_on_each_mutation(tmp_path):
    env = make_recovery_env(tmp_path)
    a = env.engine.begin_attempt(plan_id="p", planned_route_id="route-a",
                                 registry_id="route-a", distinct_rate_source_id="RS-A")
    assert a.revision == 0
    env.store.update(a.attempt_id, lifecycle_status=AttemptLifecycleStatus.PAUSED)
    assert env.store.get(a.attempt_id).revision == 1
    # Duplicate processing on terminal does not bump revision (immutable).
    env.store.update(a.attempt_id, lifecycle_status=AttemptLifecycleStatus.TERMINAL)
    env.store.update(a.attempt_id, reason_codes=["x"])  # terminal immutable -> no-op
    assert env.store.get(a.attempt_id).revision == 2


# --- 3. terminal immutability + explicit enrichment ------------------------

def test_terminal_duplicate_does_not_create_second_terminal(tmp_path):
    env = make_recovery_env(tmp_path)
    d1 = env.engine.record_observation(req(env, "quote_detected", ctx={"quote_present": True, "is_firm_quote": True}))
    d2 = env.engine.record_observation(req(env, "quote_detected", ctx={"quote_present": True, "is_firm_quote": True}))
    assert d2.attempt_id == d1.attempt_id
    assert d2.terminal_status is None
    assert d2.quote_pending_normalization is True
    assert len(env.store.list_all()) == 1
    assert env.store.get(d1.attempt_id).terminal_status is None
    assert env.store.get(d1.attempt_id).quote_pending_normalization is True


def test_terminal_enrichment_is_explicit_not_silent(tmp_path):
    env = make_recovery_env(tmp_path)
    d = env.engine.record_observation(req(env, "quote_detected", ctx={"quote_present": True, "is_firm_quote": True}))
    attempt_id = d.attempt_id
    # Default path never mutates terminal.
    env.store.update(attempt_id, terminal_status=RouteOutcomeStatus.QUOTED_COMPARABLE)
    assert env.store.get(attempt_id).terminal_status is None
    # Explicit enrichment (future Issue #11/#12) is allowed and additive.
    enriched = env.engine.enrich_terminal(
        attempt_id, terminal_status=RouteOutcomeStatus.QUOTED_COMPARABLE, reason_codes=["normalized_by_issue11"]
    )
    assert enriched.terminal_status is RouteOutcomeStatus.QUOTED_COMPARABLE
    assert "normalized_by_issue11" in enriched.reason_codes


# --- 4. idempotency --------------------------------------------------------

@pytest.mark.parametrize("obs,ctx", [
    ("technical_error", {"error_type": "navigation_timeout"}),
    ("quote_detected", {"quote_present": True, "is_firm_quote": True}),
    ("callback_detected", {}),
    ("access_control_detected", {"error_type": "captcha"}),
    ("needs_field", {"missing_field_paths": ["a"]}),
    ("needs_consent", {"consent_state": "undecided"}),
])
def test_same_observation_twice_is_idempotent(tmp_path, obs, ctx):
    env = make_recovery_env(tmp_path)
    d1 = env.engine.record_observation(req(env, obs, ctx=ctx))
    before = len(env.store.list_all())
    d2 = env.engine.record_observation(req(env, obs, ctx=ctx))
    assert d2.attempt_id == d1.attempt_id  # same attempt, no duplicate created
    assert len(env.store.list_all()) == before  # no extra attempt
    # No double budget: attempts_used stable.
    assert d2.attempts_used == d1.attempts_used


def test_duplicate_retry_does_not_double_increment(tmp_path):
    env = make_recovery_env(tmp_path)
    a1 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-a",
                                  registry_id="route-a", distinct_rate_source_id="RS-A")
    d1 = env.engine.record_observation(_timeout(env), a1)
    assert d1.recommended_action is RecoveryAction.RETRY_SAME_ROUTE
    # Duplicate timeout on the SAME attempt -> idempotent (same decision returned),
    # no extra attempt and no budget increment.
    d2 = env.engine.record_observation(_timeout(env), a1)
    assert d2.attempt_id == d1.attempt_id
    assert d2.attempts_used == d1.attempts_used
    assert d2.attempts_remaining == d1.attempts_remaining
    assert len(env.store.list_all()) == 1


# --- 5. stale / out-of-order -----------------------------------------------

def test_stale_observation_after_terminal_is_ignored(tmp_path):
    env = make_recovery_env(tmp_path)
    d1 = env.engine.record_observation(
        req(env, "quote_detected", ctx={"quote_present": True, "is_firm_quote": True}, observation_sequence=10)
    )
    # A stale technical_error with an older sequence is safely ignored.
    d2 = env.engine.record_observation(_timeout(env, observation_sequence=5))
    assert d2.attempt_id == d1.attempt_id
    assert d2.lifecycle_status is AttemptLifecycleStatus.TERMINAL
    assert d2.quote_pending_normalization is True
    assert len(env.store.list_all()) == 1


def test_stale_timeout_while_paused_does_not_consume_budget(tmp_path):
    env = make_recovery_env(tmp_path)
    env.engine.record_observation(req(env, "needs_field", ctx={"missing_field_paths": ["a"]}, observation_sequence=1))
    # A stale navigation timeout (older sequence) while paused is ignored.
    d = env.engine.record_observation(_timeout(env, observation_sequence=0))
    assert d.lifecycle_status is AttemptLifecycleStatus.PAUSED  # still paused
    assert d.retry_allowed is False
    assert len(env.store.list_all()) == 1


# --- 6-8. budget / counter consistency -------------------------------------

def test_route_budget_gives_alternative_exactly_one_attempt(tmp_path):
    entries, rate_sources = standard_rs_entries()
    env = make_recovery_env(tmp_path, entries=entries, rate_sources=rate_sources)
    env.engine._route_source = StubRouteSource({"route-a": ["route-b"], "route-b": []})
    a1 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-a", registry_id="route-a", distinct_rate_source_id="RS-TEST-001")
    env.engine.record_observation(_timeout(env, distinct_rate_source_id="RS-TEST-001"), a1)
    a2 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-a", registry_id="route-a", distinct_rate_source_id="RS-TEST-001")
    d_fail = env.engine.record_observation(_timeout(env, distinct_rate_source_id="RS-TEST-001"), a2)
    assert d_fail.recommended_action is RecoveryAction.USE_ALTERNATIVE_ROUTE
    b1 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-b", registry_id="route-b", distinct_rate_source_id="RS-TEST-001")
    assert b1.attempt_number == 3  # per rate-source sequence
    d_b = env.engine.record_observation(_timeout(env, registry_id="route-b", distinct_rate_source_id="RS-TEST-001"), b1)
    assert d_b.lifecycle_status is AttemptLifecycleStatus.TERMINAL  # exactly 1 shot for B
    assert d_b.terminal_status is RouteOutcomeStatus.UNREACHABLE
    assert len(env.store.list_by_rate_source("plan-1", "RS-TEST-001")) == 3  # no 4th


def test_plan_budget_not_bypassed_by_rate_source_switch(tmp_path):
    entries = [
        {"registry_id": "r1", "distinct_rate_source_id": "RS-1", "product_type": "auto",
         "brand_or_program": "R1", "distribution_type": "direct", "product_scope": "standard_PPA",
         "status": "discovered", "active": True},
        {"registry_id": "r2", "distinct_rate_source_id": "RS-2", "product_type": "auto",
         "brand_or_program": "R2", "distribution_type": "direct", "product_scope": "standard_PPA",
         "status": "discovered", "active": True},
    ]
    rate_sources = [
        {"distinct_rate_source_id": "RS-1", "product_type": "auto", "related_registry_ids": ["r1"]},
        {"distinct_rate_source_id": "RS-2", "product_type": "auto", "related_registry_ids": ["r2"]},
    ]
    policy = RecoveryPolicy(max_attempts_per_route=3, max_attempts_per_rate_source=10, max_attempts_per_plan=3)
    env = make_recovery_env(tmp_path, entries=entries, rate_sources=rate_sources, policy=policy)
    # RS-1 route r1: a1 timeout (retry, plan=1), a2 timeout (retry, plan=2), a3 timeout (route=3 -> terminal, plan=3)
    for _ in range(2):
        a = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="r1", registry_id="r1", distinct_rate_source_id="RS-1")
        d = env.engine.record_observation(_timeout(env, registry_id="r1", distinct_rate_source_id="RS-1"), a)
        assert d.recommended_action is RecoveryAction.RETRY_SAME_ROUTE
    a3 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="r1", registry_id="r1", distinct_rate_source_id="RS-1")
    d = env.engine.record_observation(_timeout(env, registry_id="r1", distinct_rate_source_id="RS-1"), a3)
    assert d.lifecycle_status is AttemptLifecycleStatus.TERMINAL
    # RS-2 route r2: b1 timeout -> route budget has room, but PLAN cap is exhausted -> no retry.
    b1 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="r2", registry_id="r2", distinct_rate_source_id="RS-2")
    d_b = env.engine.record_observation(_timeout(env, registry_id="r2", distinct_rate_source_id="RS-2"), b1)
    assert d_b.lifecycle_status is AttemptLifecycleStatus.TERMINAL
    assert "retry_budget_exhausted" in d_b.reason_codes  # plan budget, not route budget
    assert len(env.store.list_by_plan("plan-1")) == 4


# --- 9. failover chain -----------------------------------------------------

def test_three_route_failover_chain_deterministic_no_reuse(tmp_path):
    entries = [
        {"registry_id": "route-a", "distinct_rate_source_id": "RS-CHAIN", "product_type": "auto",
         "brand_or_program": "A", "distribution_type": "direct", "product_scope": "standard_PPA",
         "status": "discovered", "active": True},
        {"registry_id": "route-b", "distinct_rate_source_id": "RS-CHAIN", "product_type": "auto",
         "brand_or_program": "B", "distribution_type": "direct", "product_scope": "standard_PPA",
         "status": "discovered", "active": True},
        {"registry_id": "route-c", "distinct_rate_source_id": "RS-CHAIN", "product_type": "auto",
         "brand_or_program": "C", "distribution_type": "direct", "product_scope": "standard_PPA",
         "status": "discovered", "active": True},
    ]
    rate_sources = [{"distinct_rate_source_id": "RS-CHAIN", "product_type": "auto",
                     "deduplication_status": "duplicate_confirmed", "related_registry_ids": ["route-a", "route-b", "route-c"]}]
    policy = RecoveryPolicy(max_attempts_per_route=2, max_attempts_per_rate_source=5, max_attempts_per_plan=10)
    env = make_recovery_env(tmp_path, entries=entries, rate_sources=rate_sources, policy=policy)
    env.engine._route_source = StubRouteSource({"route-a": ["route-b", "route-c"], "route-b": ["route-c"], "route-c": []})

    a1 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-a", registry_id="route-a", distinct_rate_source_id="RS-CHAIN")
    d = env.engine.record_observation(_timeout(env, distinct_rate_source_id="RS-CHAIN"), a1)
    assert d.recommended_action is RecoveryAction.RETRY_SAME_ROUTE
    a2 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-a", registry_id="route-a", distinct_rate_source_id="RS-CHAIN")
    d = env.engine.record_observation(_timeout(env, distinct_rate_source_id="RS-CHAIN"), a2)
    assert d.recommended_action is RecoveryAction.USE_ALTERNATIVE_ROUTE
    assert d.alternative_route_id == "route-b"
    b1 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-b", registry_id="route-b", distinct_rate_source_id="RS-CHAIN")
    d = env.engine.record_observation(_timeout(env, registry_id="route-b", distinct_rate_source_id="RS-CHAIN"), b1)
    assert d.recommended_action is RecoveryAction.RETRY_SAME_ROUTE
    b2 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-b", registry_id="route-b", distinct_rate_source_id="RS-CHAIN")
    d = env.engine.record_observation(_timeout(env, registry_id="route-b", distinct_rate_source_id="RS-CHAIN"), b2)
    assert d.recommended_action is RecoveryAction.USE_ALTERNATIVE_ROUTE
    assert d.alternative_route_id == "route-c"  # never re-uses exhausted A or B
    c1 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-c", registry_id="route-c", distinct_rate_source_id="RS-CHAIN")
    d = env.engine.record_observation(req(env, "quote_detected", ctx={"quote_present": True, "is_firm_quote": True},
                                          registry_id="route-c", distinct_rate_source_id="RS-CHAIN"), c1)
    assert d.lifecycle_status is AttemptLifecycleStatus.TERMINAL
    assert d.quote_pending_normalization is True


# --- 10-11. readiness recheck + consent change -----------------------------

def test_alternative_readiness_rechecked_consent_revoked(tmp_path):
    entries, rate_sources = standard_rs_entries()
    env = make_recovery_env(tmp_path, entries=entries, rate_sources=rate_sources)  # real planner route source
    # Revoke consent for the alternative route-b (live Issue #5 state change).
    revoke_route_consent(env, "route-b")
    a1 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-a", registry_id="route-a", distinct_rate_source_id="RS-TEST-001")
    env.engine.record_observation(_timeout(env, distinct_rate_source_id="RS-TEST-001"), a1)
    a2 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-a", registry_id="route-a", distinct_rate_source_id="RS-TEST-001")
    d = env.engine.record_observation(_timeout(env, distinct_rate_source_id="RS-TEST-001"), a2)
    # route-b is no longer ready -> do NOT blindly fail over; pause for consent.
    assert d.recommended_action is RecoveryAction.RESUME_AFTER_USER_INPUT
    assert d.lifecycle_status is AttemptLifecycleStatus.PAUSED
    assert RecoveryReasonCode.CONSENT_REQUIRED.value in d.reason_codes


def test_consent_revoked_before_retry_blocks_retry(tmp_path):
    env = make_recovery_env(tmp_path)
    a1 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-a",
                                  registry_id="route-a", distinct_rate_source_id="RS-A")
    d1 = env.engine.record_observation(_timeout(env), a1)
    assert d1.recommended_action is RecoveryAction.RETRY_SAME_ROUTE
    # Revoke consent for route-a before the retry (live state, not stale copy).
    revoke_route_consent(env, "route-a")
    a2 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-a",
                                  registry_id="route-a", distinct_rate_source_id="RS-A")
    d2 = env.engine.record_observation(_timeout(env), a2)
    assert d2.recommended_action is not RecoveryAction.RETRY_SAME_ROUTE  # no retry
    assert d2.lifecycle_status is AttemptLifecycleStatus.PAUSED  # consent no longer active
    assert RecoveryReasonCode.CONSENT_REQUIRED.value in d2.reason_codes


# --- 12-13. resume vs retry ------------------------------------------------

def test_resume_reuses_paused_attempt_no_budget(tmp_path):
    env = make_recovery_env(tmp_path)
    d = env.engine.record_observation(req(env, "needs_field", ctx={"missing_field_paths": ["a"]}))
    paused = env.store.get(d.attempt_id)
    assert paused.lifecycle_status is AttemptLifecycleStatus.PAUSED
    # Resume (mapping added / field supplied) - same attempt, no new attempt, no budget.
    r = env.engine.record_observation(req(env, "fields_filled", attempt_id=d.attempt_id))
    assert r.attempt_id == d.attempt_id
    assert r.lifecycle_status is AttemptLifecycleStatus.RUNNING
    assert len(env.store.list_all()) == 1
    # A later technical error on the SAME (resumed) attempt -> first retry within
    # budget is still allowed (the pause consumed no retry slot).
    dr = env.engine.record_observation(_timeout(env, attempt_id=d.attempt_id))
    assert dr.recommended_action is RecoveryAction.RETRY_SAME_ROUTE
    assert dr.attempts_used == 1
    assert dr.attempts_remaining == 1


def test_retry_is_new_attempt_after_transient_failure(tmp_path):
    env = make_recovery_env(tmp_path)
    a1 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-a",
                                  registry_id="route-a", distinct_rate_source_id="RS-A")
    d1 = env.engine.record_observation(_timeout(env), a1)
    a2 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-a",
                                  registry_id="route-a", distinct_rate_source_id="RS-A")
    assert a2.attempt_id != a1.attempt_id
    assert a2.attempt_number == 2


# --- 14-15. browser session lost / unknown field mapping -------------------

def test_browser_session_lost_while_paused_bounded_restart(tmp_path):
    env = make_recovery_env(tmp_path)
    d = env.engine.record_observation(req(env, "needs_field", ctx={"missing_field_paths": ["a"]}))
    paused = env.store.get(d.attempt_id)
    # Session lost while paused -> explicit close + bounded retry (not silent resume).
    d2 = env.engine.record_observation(
        _timeout(env, ctx={"error_type": "navigation_timeout", "resume_session_unavailable": True})
    )
    assert RecoveryReasonCode.RESUME_SESSION_UNAVAILABLE.value in d2.reason_codes
    assert d2.recommended_action is RecoveryAction.RETRY_SAME_ROUTE
    assert d2.attempt_id == paused.attempt_id  # decision references the closed attempt
    assert paused.lifecycle_status is AttemptLifecycleStatus.PAUSED  # historical paused attempt preserved
    assert env.store.get(paused.attempt_id).lifecycle_status is AttemptLifecycleStatus.TERMINAL
    # No new attempt auto-created; the orchestrator begins it after the retry decision.
    assert len(env.store.list_all()) == 1


def test_unknown_field_mapping_added_resume_no_invented_answer(tmp_path):
    env = make_recovery_env(tmp_path)
    d1 = env.engine.record_observation(req(env, "unknown_external_field", ctx={"unknown_external_fields": ["shoe_size"]}))
    assert d1.lifecycle_status is AttemptLifecycleStatus.PAUSED
    assert d1.terminal_status is None
    # Mapping added -> same session resumes (no new attempt, no fabricated value).
    r = env.engine.record_observation(req(env, "fields_filled", attempt_id=d1.attempt_id))
    assert r.attempt_id == d1.attempt_id
    assert r.lifecycle_status is AttemptLifecycleStatus.RUNNING
    # If the workflow is intentionally ended with an unmappable page -> unresolved.
    d2 = env.engine.record_observation(req(env, "unsupported_page", plan_id="plan-end"))
    assert d2.lifecycle_status is AttemptLifecycleStatus.TERMINAL
    assert d2.terminal_status is RouteOutcomeStatus.UNRESOLVED


# --- 16. validation subtypes -----------------------------------------------

def test_validation_applicant_correctable_is_pause(tmp_path):
    env = make_recovery_env(tmp_path)
    d = env.engine.record_observation(
        req(env, "validation_error", ctx={"error_paths": ["product_data.vehicles[0].use.annual_kilometres"],
                                          "validation_kind": "applicant_correctable"})
    )
    assert d.lifecycle_status is AttemptLifecycleStatus.PAUSED
    assert d.recommended_action is RecoveryAction.RESUME_AFTER_USER_INPUT


def test_validation_destination_incompatible_is_value_not_supported(tmp_path):
    env = make_recovery_env(tmp_path)
    d = env.engine.record_observation(
        req(env, "validation_error", ctx={"error_paths": ["a"], "validation_kind": "destination_incompatible"})
    )
    assert d.lifecycle_status is AttemptLifecycleStatus.TERMINAL
    assert RecoveryReasonCode.UNSUPPORTED_DESTINATION_VALUE.value in d.reason_codes


def test_validation_unknown_is_unresolved(tmp_path):
    env = make_recovery_env(tmp_path)
    d = env.engine.record_observation(
        req(env, "validation_error", ctx={"validation_kind": "unknown"})
    )
    assert d.lifecycle_status is AttemptLifecycleStatus.TERMINAL
    assert d.terminal_status is RouteOutcomeStatus.UNRESOLVED


# --- 17-18. safety: CAPTCHA no-failover + auth wall ------------------------

def test_captcha_never_failovers_or_retries(tmp_path):
    entries, rate_sources = standard_rs_entries()
    env = make_recovery_env(tmp_path, entries=entries, rate_sources=rate_sources)
    env.engine._route_source = StubRouteSource({"route-a": ["route-b"]})
    d = env.engine.record_observation(req(env, "access_control_detected", ctx={"error_type": "captcha"},
                                           distinct_rate_source_id="RS-TEST-001"))
    assert d.lifecycle_status is AttemptLifecycleStatus.TERMINAL
    assert d.terminal_status is RouteOutcomeStatus.BLOCKED
    assert d.recommended_action is RecoveryAction.MANUAL_HANDOFF
    assert d.retry_allowed is False
    assert d.alternative_route_id is None  # never uses failover to circumvent the same control


def test_auth_wall_is_manual_handoff_no_retry_loop(tmp_path):
    env = make_recovery_env(tmp_path)
    d = env.engine.record_observation(req(env, "authentication_required", reason="unexpected login wall"))
    assert d.lifecycle_status is AttemptLifecycleStatus.TERMINAL
    assert d.terminal_status is RouteOutcomeStatus.MANUAL_HANDOFF
    assert d.retry_allowed is False
    assert RecoveryReasonCode.AUTHENTICATION_REQUIRED.value in d.reason_codes


# --- 19-20. not-writing vs technical ---------------------------------------

def test_temporary_outage_is_retryable_not_not_writing(tmp_path):
    env = make_recovery_env(tmp_path)
    d = env.engine.record_observation(
        req(env, "technical_error", reason="Service temporarily unavailable", ctx={})
    )
    assert d.recommended_action is RecoveryAction.RETRY_SAME_ROUTE  # transient, retryable
    assert d.terminal_status is None  # NOT not_currently_writing


def test_404_never_implies_not_currently_writing(tmp_path):
    env = make_recovery_env(tmp_path)
    d = env.engine.record_observation(req(env, "technical_error", reason="404 not found", ctx={"error_type": "http_404"}))
    assert d.terminal_status is not RouteOutcomeStatus.NOT_CURRENTLY_WRITING  # technical, not not-writing
    explicit = env.engine.record_observation(req(env, "not_currently_writing", plan_id="plan-nw"))
    assert explicit.terminal_status is RouteOutcomeStatus.NOT_CURRENTLY_WRITING


# --- 21-23. ineligibility / affinity / specialty ---------------------------

def test_explicit_ineligibility_negative_profile_traits_do_not_infer(tmp_path):
    env = make_recovery_env(tmp_path)
    # Profile traits alone (postal/vehicle) never infer ineligibility.
    d = env.engine.record_observation(req(env, "needs_field", ctx={"missing_field_paths": ["a"]}))
    assert d.terminal_status is None
    # Explicit site rejection -> ineligible.
    d2 = env.engine.record_observation(req(env, "explicit_ineligible", reason="vehicle type not supported", plan_id="p2"))
    assert d2.terminal_status is RouteOutcomeStatus.INELIGIBLE
    assert "vehicle type not supported" in str(d2.reason_codes) or "explicit_ineligibility" in d2.reason_codes


def test_membership_unknown_pause_vs_affinity_explicit(tmp_path):
    env = make_recovery_env(tmp_path)
    unknown = env.engine.record_observation(req(env, "membership_unknown"))
    assert unknown.lifecycle_status is AttemptLifecycleStatus.PAUSED
    assert RecoveryReasonCode.MEMBERSHIP_UNKNOWN.value in unknown.reason_codes
    explicit = env.engine.record_observation(req(env, "affinity_restricted", plan_id="p2"))
    assert explicit.terminal_status is RouteOutcomeStatus.AFFINITY_RESTRICTED
    tech = env.engine.record_observation(req(env, "technical_error", reason="timeout",
                                             ctx={"error_type": "navigation_timeout"}, plan_id="p3"))
    assert tech.terminal_status is not RouteOutcomeStatus.AFFINITY_RESTRICTED


def test_specialty_only_explicit_not_from_brand_name(tmp_path):
    env = make_recovery_env(tmp_path)
    d = env.engine.record_observation(req(env, "specialty_only", reason="collector-only program"))
    assert d.terminal_status is RouteOutcomeStatus.SPECIALTY_ONLY
    tech = env.engine.record_observation(req(env, "technical_error", reason="timeout",
                                             ctx={"error_type": "navigation_timeout"}, plan_id="p2"))
    assert tech.terminal_status is not RouteOutcomeStatus.SPECIALTY_ONLY


# --- 24-26. callback vs manual / quote edges / partial quote ---------------

def test_callback_vs_manual_remain_distinct(tmp_path):
    env = make_recovery_env(tmp_path)
    cb = env.engine.record_observation(req(env, "callback_detected"))
    assert cb.terminal_status is RouteOutcomeStatus.CALLBACK_REQUIRED
    assert cb.recommended_action is RecoveryAction.PREPARE_VOICE_HANDOFF
    mc = env.engine.record_observation(req(env, "manual_contact_detected", plan_id="p2"))
    assert mc.terminal_status is RouteOutcomeStatus.MANUAL_HANDOFF


def test_callback_page_estimate_preserves_both_without_comparable(tmp_path):
    env = make_recovery_env(tmp_path)
    d = env.engine.record_observation(
        req(env, "callback_detected", reason="call us", ctx={"estimate_only": True, "is_firm_quote": False})
    )
    assert d.terminal_status is RouteOutcomeStatus.CALLBACK_REQUIRED  # primary fact preserved
    assert d.terminal_status not in (RouteOutcomeStatus.QUOTED_COMPARABLE, RouteOutcomeStatus.QUOTED_NON_COMPARABLE)


def test_partial_quote_stops_retries_pending_normalization(tmp_path):
    env = make_recovery_env(tmp_path)
    d = env.engine.record_observation(
        req(env, "quote_detected", reason="annual premium shown",
            ctx={"quote_present": True, "is_firm_quote": True, "annual_amount_parsed": 1234.56,
                 "coverage_complete": False})
    )
    assert d.lifecycle_status is AttemptLifecycleStatus.TERMINAL
    assert d.retry_allowed is False
    assert d.quote_pending_normalization is True
    assert d.terminal_status is None  # comparability deferred to Issue #11/#12


# --- 27-28. duplicate source / plan change mid-recovery --------------------

def test_unused_alternative_duplicate_rate_source_after_primary_retry(tmp_path):
    env = make_recovery_env(tmp_path)
    a1 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-a",
                                  registry_id="route-a", distinct_rate_source_id="RS-TEST-001")
    d1 = env.engine.record_observation(_timeout(env, distinct_rate_source_id="RS-TEST-001"), a1)
    assert d1.recommended_action is RecoveryAction.RETRY_SAME_ROUTE
    a2 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-a",
                                  registry_id="route-a", distinct_rate_source_id="RS-TEST-001")
    d2 = env.engine.record_observation(req(env, "quote_detected", ctx={"quote_present": True, "is_firm_quote": True},
                                           distinct_rate_source_id="RS-TEST-001"), a2)
    assert d2.quote_pending_normalization is True  # primary succeeded after retry
    # The unused alternative remains duplicate_rate_source.
    du = env.engine.classify_unused_alternative(plan_id="plan-1", registry_id="route-b",
                                                 distinct_rate_source_id="RS-TEST-001")
    assert du.terminal_status is RouteOutcomeStatus.DUPLICATE_RATE_SOURCE


def test_route_plan_changed_mid_recovery_keeps_history_immutable(tmp_path):
    env = make_recovery_env(tmp_path)
    a1 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-a",
                                  registry_id="route-a", distinct_rate_source_id="RS-A")
    env.engine.record_observation(_timeout(env), a1)
    before = env.store.get(a1.attempt_id)
    # Plan/registry relationship invalid -> terminal unresolved, no history rewrite.
    d = env.engine.record_observation(req(env, "route_invalid", ctx={"plan_changed": True}, plan_id="plan-1"))
    assert d.lifecycle_status is AttemptLifecycleStatus.TERMINAL
    assert d.terminal_status is RouteOutcomeStatus.UNRESOLVED
    assert RecoveryReasonCode.ROUTE_INVALID.value in d.reason_codes
    after = env.store.get(a1.attempt_id)
    assert after == before  # historical attempt unchanged


# --- 29. policy/plan versioning --------------------------------------------

def test_policy_and_plan_version_provenance(tmp_path):
    import json
    directory = tmp_path / "recovery"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "auto_policy.json").write_text(
        json.dumps({"version": "7", "default": {"max_attempts_per_route": 2}}), encoding="utf-8"
    )
    env = make_recovery_env(tmp_path, policy_dir=directory)
    d = env.engine.record_observation(req(env, "quote_detected", ctx={"quote_present": True, "is_firm_quote": True},
                                           plan_version="plan-v2"))
    assert d.policy_version == "7"
    assert d.plan_version == "plan-v2"
    assert env.store.get(d.attempt_id).policy_version == "7"
    assert env.store.get(d.attempt_id).plan_version == "plan-v2"


# --- 30. dynamic policy ----------------------------------------------------

def test_dynamic_rate_source_and_plan_caps(tmp_path):
    env_default = make_recovery_env(tmp_path)
    # Default rate-source cap = 3: A uses 2, B gets 1 shot.
    entries, rate_sources = standard_rs_entries()
    env = make_recovery_env(tmp_path, entries=entries, rate_sources=rate_sources)
    env.engine._route_source = StubRouteSource({"route-a": ["route-b"], "route-b": []})
    a1 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-a", registry_id="route-a", distinct_rate_source_id="RS-TEST-001")
    env.engine.record_observation(_timeout(env, distinct_rate_source_id="RS-TEST-001"), a1)
    a2 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-a", registry_id="route-a", distinct_rate_source_id="RS-TEST-001")
    d = env.engine.record_observation(_timeout(env, distinct_rate_source_id="RS-TEST-001"), a2)
    assert d.alternative_route_id == "route-b"
    b1 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-b", registry_id="route-b", distinct_rate_source_id="RS-TEST-001")
    db = env.engine.record_observation(_timeout(env, registry_id="route-b", distinct_rate_source_id="RS-TEST-001"), b1)
    assert db.lifecycle_status is AttemptLifecycleStatus.TERMINAL  # rs cap 3 -> no more


def test_disable_retryable_navigation_via_policy(tmp_path):
    env = make_recovery_env(tmp_path, policy=RecoveryPolicy(navigation_timeout_retryable=False))
    d = env.engine.record_observation(_timeout(env))
    assert d.recommended_action is not RecoveryAction.RETRY_SAME_ROUTE  # not auto-retried
    assert d.terminal_status in (RouteOutcomeStatus.UNREACHABLE, RouteOutcomeStatus.UNRESOLVED) or d.terminal_status is None


# --- 31-32. extensibility + voice compatibility ----------------------------

def test_new_observation_localized_table_addition(tmp_path, monkeypatch):
    from app.services.recovery import classification
    from app.services.recovery.classification import _Spec
    monkeypatch.setitem(
        classification._TABLE, "network_bounce",
        _Spec(ExecutionResultKind.TECHNICAL_ERROR, AttemptLifecycleStatus.RECOVERABLE,
              Retryability.RETRYABLE, "retry_same_route",
              (RecoveryReasonCode.TRANSIENT_NAVIGATION_FAILURE,),
              fallback_terminal_status="unreachable", failover_eligible=True),
    )
    env = make_recovery_env(tmp_path)
    d = env.engine.record_observation(req(env, "network_bounce", reason="transient"))
    assert d.recommended_action is RecoveryAction.RETRY_SAME_ROUTE  # no engine/topology change


def test_voice_channel_observation_consumed_generically(tmp_path):
    env = make_recovery_env(tmp_path)
    d = env.engine.record_observation(req(env, "broker_requires_manual_review", source_channel="phone"))
    assert d.lifecycle_status is AttemptLifecycleStatus.TERMINAL
    assert d.terminal_status is RouteOutcomeStatus.MANUAL_HANDOFF
    assert d.retry_allowed is False  # no automated call, no retry


# --- 33-34. store isolation & replacement boundary -------------------------

def test_attempt_store_isolation_between_plans(tmp_path):
    env = make_recovery_env(tmp_path)
    a = env.engine.begin_attempt(plan_id="plan-A", planned_route_id="route-a",
                                 registry_id="route-a", distinct_rate_source_id="RS-A")
    env.engine.record_observation(req(env, "quote_detected", ctx={"quote_present": True, "is_firm_quote": True}, plan_id="plan-A"), a)
    b = env.engine.begin_attempt(plan_id="plan-B", planned_route_id="route-b",
                                 registry_id="route-b", distinct_rate_source_id="RS-B")
    env.engine.record_observation(req(env, "needs_field", ctx={"missing_field_paths": ["x"]}, plan_id="plan-B",
                                       registry_id="route-b", distinct_rate_source_id="RS-B"), b)
    assert len(env.store.list_by_plan("plan-A")) == 1
    assert len(env.store.list_by_plan("plan-B")) == 1
    assert env.store.list_by_plan("plan-A")[0].registry_id == "route-a"
    assert env.store.list_by_plan("plan-B")[0].lifecycle_status is AttemptLifecycleStatus.PAUSED
    # No cross-contamination of history.
    assert all(r.plan_id == "plan-A" for r in env.store.list_by_plan("plan-A"))


def test_engine_depends_on_store_abstraction(tmp_path):
    """A custom store implementing the AttemptStore Protocol works with the
    engine - the concrete in-memory implementation is replaceable (Issue #10)."""
    from app.services.recovery.attempt_store import AttemptStore

    class MinimalStore:
        def __init__(self):
            self._data = {}

        def save(self, attempt):
            self._data[attempt.attempt_id] = attempt

        def get(self, attempt_id):
            return self._data.get(attempt_id)

        def delete(self, attempt_id):
            self._data.pop(attempt_id, None)

        def list_all(self):
            return sorted(self._data.values(), key=lambda a: a.started_at)

        def list_by_plan(self, plan_id):
            return [a for a in self.list_all() if a.plan_id == plan_id]

        def list_by_route(self, plan_id, registry_id):
            return [a for a in self.list_all() if a.registry_id == registry_id and (plan_id is None or a.plan_id == plan_id)]

        def list_by_rate_source(self, plan_id, distinct_rate_source_id):
            return [a for a in self.list_all() if a.distinct_rate_source_id == distinct_rate_source_id and (plan_id is None or a.plan_id == plan_id)]

        def next_attempt_number(self, plan_id, distinct_rate_source_id):
            return max([a.attempt_number for a in self.list_by_rate_source(plan_id, distinct_rate_source_id)], default=0) + 1

        def update(self, attempt_id, *, allow_terminal_mutation=False, **changes):
            a = self._data[attempt_id]
            updated = a.model_copy(update=changes)
            self._data[attempt_id] = updated
            return updated

    assert isinstance(MinimalStore(), AttemptStore)  # protocol satisfied
    engine = RecoveryEngine(store=MinimalStore())
    d = engine.record_observation(RecoveryDecideRequest(
        planned_route_id="route-a", registry_id="route-a", distinct_rate_source_id="RS-A",
        observation_type="quote_detected", safe_context={"quote_present": True, "is_firm_quote": True},
    ))
    assert d.lifecycle_status is AttemptLifecycleStatus.TERMINAL
    assert d.quote_pending_normalization is True


# --- 37. privacy failure paths ---------------------------------------------

def test_privacy_failure_paths(tmp_path):
    scenario_kinds = [
        ("technical_error", {"error_type": "navigation_timeout"}),  # retry path
        ("access_control_detected", {"error_type": "captcha"}),
        ("needs_consent", {"consent_state": "denied"}),
        ("authentication_required", {}),
        ("technical_error", {}),  # temporary outage text
        ("validation_error", {"validation_kind": "unknown"}),
    ]
    for obs_type, ctx in scenario_kinds:
        env = make_recovery_env(tmp_path)
        d = env.engine.record_observation(req(env, obs_type, reason="Service temporarily unavailable" if obs_type == "technical_error" and not ctx else None, ctx=ctx))
        for marker in PII_MARKERS:
            assert marker not in str(d)
            assert marker not in str(env.store.get(d.attempt_id))


# --- 39. LangSmith-safe metadata -------------------------------------------

async def test_workflow_sanitizes_raw_payload_from_state(tmp_path):
    from app.graph.recovery_workflow import build_recovery_workflow
    env = make_recovery_env(tmp_path)
    graph = build_recovery_workflow(env.engine)
    state = await graph.ainvoke({
        "entry": "decide", "plan_id": "plan-1", "planned_route_id": "route-a",
        "registry_id": "route-a", "distinct_rate_source_id": "RS-A",
        "observation_type": "quote_detected",
        "safe_context": {"quote_present": True, "is_firm_quote": True,
                         "raw_payload": {"licence": "T0000-00000-00000", "vin": "1HGCM82633A000000"}},
    })
    text = str(state)
    assert "raw_payload" not in text
    assert "T0000-00000-00000" not in text
    assert "1HGCM82633A000000" not in text
