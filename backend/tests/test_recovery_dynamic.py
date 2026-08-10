"""Issue #8 - dynamic-change tests: policy data, failover toggles, new
observation mappings, and market (alternative) changes all work without
RecoveryEngine code modification."""

from __future__ import annotations

import json

from app.models.recovery import (
    AttemptLifecycleStatus,
    RecoveryAction,
    RecoveryPolicy,
    RouteOutcomeStatus,
)
from app.services.recovery import classification
from app.services.recovery.attempt_store import InMemoryAttemptStore
from app.services.recovery.engine import PlannerRouteSource, RecoveryEngine
from app.services.recovery.policy import RecoveryPolicyLoader
from recovery_helpers import StubRouteSource, make_recovery_env, req, standard_rs_entries


def _two_timeout_flow(env, distinct_rate_source_id="RS-A"):
    a1 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-a", registry_id="route-a",
                                  distinct_rate_source_id=distinct_rate_source_id)
    d1 = env.engine.record_observation(req(env, "technical_error", reason="timeout",
                                           ctx={"error_type": "navigation_timeout"},
                                           distinct_rate_source_id=distinct_rate_source_id), a1)
    a2 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-a", registry_id="route-a",
                                  distinct_rate_source_id=distinct_rate_source_id)
    d2 = env.engine.record_observation(req(env, "technical_error", reason="timeout",
                                           ctx={"error_type": "navigation_timeout"},
                                           distinct_rate_source_id=distinct_rate_source_id), a2)
    return d1, d2


# --- Scenario A: max route attempts 2 -> 3 via policy data -----------------

def test_policy_change_route_attempts_changes_behavior(tmp_path):
    # Default policy (max 2): after 2 timeouts -> terminal.
    env_default = make_recovery_env(tmp_path)
    _d1, d2 = _two_timeout_flow(env_default)
    assert d2.lifecycle_status is AttemptLifecycleStatus.TERMINAL

    # Policy max 3 (data-only change): after 2 timeouts -> still retryable.
    env_three = make_recovery_env(tmp_path, policy=RecoveryPolicy(max_attempts_per_route=3))
    d1, d2 = _two_timeout_flow(env_three)
    assert d1.recommended_action is RecoveryAction.RETRY_SAME_ROUTE
    assert d2.recommended_action is RecoveryAction.RETRY_SAME_ROUTE
    assert d2.retry_allowed is True
    # And a third attempt is now permitted.
    a3 = env_three.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-a", registry_id="route-a",
                                        distinct_rate_source_id="RS-A")
    d3 = env_three.engine.record_observation(req(env_three, "quote_detected",
                                                 ctx={"quote_present": True, "is_firm_quote": True}), a3)
    assert d3.lifecycle_status is AttemptLifecycleStatus.TERMINAL
    assert d3.quote_pending_normalization is True


def test_policy_change_via_data_file(tmp_path):
    directory = tmp_path / "recovery"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "auto_policy.json").write_text(
        json.dumps({"default": {"max_attempts_per_route": 3}}), encoding="utf-8"
    )
    env = make_recovery_env(tmp_path, policy_dir=directory)
    assert env.engine.policy_for(req(env, "needs_field")).max_attempts_per_route == 3


# --- Scenario B: alternative failover enable/disable via policy -------------

def test_failover_enabled_vs_disabled_by_policy(tmp_path):
    enabled = make_recovery_env(tmp_path, route_source=StubRouteSource({"route-a": ["route-b"]}))
    _d1, d2 = _two_timeout_flow(enabled, distinct_rate_source_id="RS-TEST-001")
    assert d2.recommended_action is RecoveryAction.USE_ALTERNATIVE_ROUTE
    assert d2.alternative_route_id == "route-b"

    disabled = make_recovery_env(
        tmp_path,
        route_source=StubRouteSource({"route-a": ["route-b"]}),
        policy=RecoveryPolicy(alternative_route_after_exhaustion=False),
    )
    _d1, d2 = _two_timeout_flow(disabled, distinct_rate_source_id="RS-TEST-001")
    assert d2.recommended_action is RecoveryAction.STOP_TERMINAL
    assert d2.alternative_route_id is None
    assert d2.terminal_status is RouteOutcomeStatus.UNREACHABLE


# --- Scenario C + dynamic observation: new retryable observation is a
# localized table addition, not an engine/graph change ----------------------

def test_new_observation_mapping_is_localized_table_addition(tmp_path, monkeypatch):
    from app.models.recovery import (
        AttemptLifecycleStatus,
        ExecutionResultKind,
        RecoveryReasonCode,
        Retryability,
    )
    from app.services.recovery.classification import _Spec

    monkeypatch.setitem(
        classification._TABLE,
        "network_bounce",
        _Spec(
            ExecutionResultKind.TECHNICAL_ERROR,
            AttemptLifecycleStatus.RECOVERABLE,
            Retryability.RETRYABLE,
            "retry_same_route",
            (RecoveryReasonCode.TRANSIENT_NAVIGATION_FAILURE,),
            fallback_terminal_status="unreachable",
            failover_eligible=True,
        ),
    )
    env = make_recovery_env(tmp_path)
    decision = env.engine.record_observation(req(env, "network_bounce", reason="transient"))
    assert decision.lifecycle_status is AttemptLifecycleStatus.RECOVERABLE
    assert decision.recommended_action is RecoveryAction.RETRY_SAME_ROUTE
    assert decision.retry_allowed is True


def test_unregistered_observation_is_conservative_pause(tmp_path):
    env = make_recovery_env(tmp_path)
    decision = env.engine.record_observation(req(env, "brand_new_unknown_obs"))
    assert decision.lifecycle_status is AttemptLifecycleStatus.PAUSED
    assert decision.terminal_status is None
    assert "unresolved_result" in decision.reason_codes


# --- Dynamic market change: Issue #6 data feeds alternatives ----------------

def test_market_change_consumed_via_planner_data(tmp_path):
    entries, rate_sources = standard_rs_entries()
    env = make_recovery_env(tmp_path, entries=entries, rate_sources=rate_sources)
    alternatives = env.engine._route_source.alternatives_for(
        plan_id="plan-1", registry_id="route-a", distinct_rate_source_id="RS-TEST-001",
        intake_session_id=env.session_id,
    )
    assert alternatives == ["route-b"]

    # Market change: route-b removed from Issue #6 data -> recovery sees no alternative.
    entries_b = [e for e in entries if e["registry_id"] != "route-b"]
    rate_sources_b = [r for r in rate_sources if r["distinct_rate_source_id"] == "RS-TEST-001"]
    rate_sources_b[0] = {**rate_sources_b[0], "deduplication_status": "unique",
                          "related_registry_ids": ["route-a"]}
    env2 = make_recovery_env(tmp_path, entries=entries_b, rate_sources=rate_sources_b)
    alternatives2 = env2.engine._route_source.alternatives_for(
        plan_id="plan-1", registry_id="route-a", distinct_rate_source_id="RS-TEST-001",
        intake_session_id=env2.session_id,
    )
    assert alternatives2 == []


def test_engine_constructed_with_data_loader_uses_file_policy(tmp_path):
    directory = tmp_path / "recovery"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "auto_policy.json").write_text(
        json.dumps({"default": {"max_attempts_per_rate_source": 5}}), encoding="utf-8"
    )
    engine = RecoveryEngine(
        store=InMemoryAttemptStore(),
        policy_loader=RecoveryPolicyLoader(policy_dir=directory),
        route_source=StubRouteSource(),
    )
    assert engine._policy.max_attempts_per_rate_source == 5
