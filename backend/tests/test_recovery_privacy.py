"""Issue #8 - privacy tests: synthetic PII absent from attempts, decisions,
state, logs, API, and errors - including failure paths."""

from __future__ import annotations

import pytest

from app.models.recovery import RecoveryDecision, RouteOutcomeStatus
from app.services.recovery import classify_observation
from app.services.recovery.engine import RecoveryEngine
from recovery_helpers import StubRouteSource, make_recovery_env, req

PII_MARKERS = [
    "T0000-0000000-0000",  # licence
    "1HGCM82633A000000",   # VIN
    "1990-01-01",          # DOB
    "123 Test Street",     # street address
    "test.applicant@example.com",  # email
    "MOCK-8F3K-2026",      # quote reference
]


def _all_failure_observations(env):
    return [
        req(env, "technical_error", reason="navigation timed out", ctx={"error_type": "navigation_timeout"}),
        req(env, "technical_error", reason="browser crashed", ctx={"error_type": "browser_crash"}),
        req(env, "technical_error", reason="odd failure", ctx={}),
        req(env, "validation_error", ctx={"error_paths": ["product_data.vehicles[0].use.annual_kilometres"]}),
        req(env, "unknown_external_field", ctx={"unknown_external_fields": ["shoe_size"]}),
        req(env, "access_control_detected", ctx={"error_type": "captcha"}),
        req(env, "quote_detected", ctx={"quote_present": True, "is_firm_quote": True, "reference_present": True}),
    ]


def test_decisions_attempts_never_contain_pii_across_failure_paths(tmp_path):
    for spec in _all_failure_observations(make_recovery_env(tmp_path)):
        env = make_recovery_env(tmp_path)
        decision = env.engine.record_observation(spec)
        attempt = env.store.get(decision.attempt_id)
        for marker in PII_MARKERS:
            assert marker not in str(decision), (spec.observation_type, marker)
            assert marker not in str(attempt), (spec.observation_type, marker)
            assert marker not in str(decision.safe_dict()), (spec.observation_type, marker)
            assert marker not in str(attempt.safe_dict()), (spec.observation_type, marker)


def test_failover_and_exhaustion_privacy(tmp_path):
    env = make_recovery_env(tmp_path)
    env.engine._route_source = StubRouteSource({"route-a": ["route-b"], "route-b": []})
    a1 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-a", registry_id="route-a", distinct_rate_source_id="RS-TEST-001")
    d1 = env.engine.record_observation(req(env, "technical_error", reason="timeout", ctx={"error_type": "navigation_timeout"}, distinct_rate_source_id="RS-TEST-001"), a1)
    a2 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-a", registry_id="route-a", distinct_rate_source_id="RS-TEST-001")
    d2 = env.engine.record_observation(req(env, "technical_error", reason="timeout", ctx={"error_type": "navigation_timeout"}, distinct_rate_source_id="RS-TEST-001"), a2)
    b1 = env.engine.begin_attempt(plan_id="plan-1", planned_route_id="route-b", registry_id="route-b", distinct_rate_source_id="RS-TEST-001")
    d3 = env.engine.record_observation(req(env, "technical_error", reason="timeout", ctx={"error_type": "navigation_timeout"}, registry_id="route-b", distinct_rate_source_id="RS-TEST-001"), b1)
    for decision in (d1, d2, d3):
        for marker in PII_MARKERS:
            assert marker not in str(decision)
    for record in env.store.list_all():
        for marker in PII_MARKERS:
            assert marker not in str(record)


def test_redaction_masks_pii_typed_context_even_if_present(tmp_path):
    env = make_recovery_env(tmp_path)
    decision = env.engine.record_observation(
        req(env, "quote_detected", ctx={
            "quote_present": True, "is_firm_quote": True,
            # Simulate a misbehaving caller; SensitiveBaseModel redacts these.
            "address": "123 Test Street",
            "licence": "T0000-0000000-0000",
            "date_of_birth": "1990-01-01",
        })
    )
    text = str(decision)
    assert "123 Test Street" not in text
    assert "T0000-0000000-0000" not in text
    assert "1990-01-01" not in text


def test_logs_do_not_contain_pii(tmp_path, caplog):
    import logging
    env = make_recovery_env(tmp_path)
    with caplog.at_level(logging.INFO):
        env.engine.record_observation(
            req(env, "quote_detected", ctx={"quote_present": True, "is_firm_quote": True, "reference_present": True})
        )
    for marker in PII_MARKERS:
        assert marker not in caplog.text


def test_errors_identify_path_not_value(tmp_path):
    env = make_recovery_env(tmp_path)
    decision = env.engine.record_observation(
        req(env, "validation_error", ctx={"error_paths": ["product_data.vehicles[0].use.annual_kilometres"]})
    )
    # The decision identifies the canonical PATH (paths-not-values), never a value.
    assert decision.lifecycle_status.value == "paused"
    assert "annual_kilometres" in str(decision)  # canonical path is allowed evidence
    for marker in PII_MARKERS:
        assert marker not in str(decision)
    # A missing-attempt error is a generic key, not PII.
    with pytest.raises(KeyError):
        env.store.update("does-not-exist", lifecycle_status="paused")


def test_quote_reference_never_leaks_raw_value(tmp_path):
    env = make_recovery_env(tmp_path)
    decision = env.engine.record_observation(
        req(env, "quote_detected", ctx={"quote_present": True, "is_firm_quote": True,
                                        "reference_present": True,
                                        "private_reference_handle": "deadbeef"})
    )
    text = str(decision)
    assert "reference_present" in text  # safe flag present
    assert "MOCK-8F3K-2026" not in text  # raw reference never leaks
    assert decision.terminal_status is None
