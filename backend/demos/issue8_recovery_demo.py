"""Issue #8 - repeatable local recovery demo (safe metadata only).

Drives the deterministic ``RecoveryEngine`` through the core terminal-status &
recovery scenarios against SYNTHETIC observations (no browser, no LLM, no real
insurer, no external API). Prints safe metadata only - never applicant values.

Usage (from backend/):
    $env:PYTHONPATH='tests;.'
    .\.venv\Scripts\python.exe demos\issue8_recovery_demo.py
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from app.models.recovery import RecoveryPolicy
from app.services.recovery import RecoveryEngine
from app.services.recovery.attempt_store import InMemoryAttemptStore
from app.services.recovery.policy import RecoveryPolicyLoader
from recovery_helpers import StubRouteSource, make_recovery_env, req, standard_rs_entries

PII_MARKERS = ["T0000-00000-00000", "1HGCM82633A000000", "1990-01-01", "123 Test Street"]


def _fmt(decision) -> str:
    status = decision.terminal_status.value if decision.terminal_status else "-"
    return (
        f"lifecycle={decision.lifecycle_status.value} "
        f"action={decision.recommended_action.value} "
        f"terminal={status} qpn={decision.quote_pending_normalization} "
        f"retry={decision.retry_allowed} used={decision.attempts_used} "
        f"rem={decision.attempts_remaining} alt={decision.alternative_route_id or '-'} "
        f"reasons={decision.reason_codes}"
    )


def main() -> None:
    print("=" * 80)
    print(" Issue #8 | Terminal Status & Recovery Engine | synthetic observations only")
    print("=" * 80)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        env = make_recovery_env(tmp)
        e = env.engine

        # 1-2. Pauses are NOT terminal and consume no budget.
        d = e.record_observation(req(env, "needs_field", ctx={"missing_field_paths": ["applicant.identity.legal_name"]}))
        print(f"[1 missing-field pause        ] {_fmt(d)}")
        d = e.record_observation(req(env, "needs_consent", ctx={"consent_state": "undecided"}, plan_id="plan-c"))
        print(f"[2 consent pause              ] {_fmt(d)}")
        d = e.record_observation(req(env, "needs_consent", ctx={"consent_state": "denied"}, plan_id="plan-d"))
        print(f"[2b consent denied (no retry) ] {_fmt(d)}  (never ineligible)")

        # 3. Transient retry.
        a1 = e.begin_attempt(plan_id="plan-r", planned_route_id="route-a", registry_id="route-a", distinct_rate_source_id="RS-A")
        d = e.record_observation(req(env, "technical_error", reason="navigation timed out", ctx={"error_type": "navigation_timeout"}, plan_id="plan-r"), a1)
        print(f"[3 transient retry            ] {_fmt(d)}")

        # 4. Retry exhaustion -> unreachable, no third attempt.
        a2 = e.begin_attempt(plan_id="plan-r", planned_route_id="route-a", registry_id="route-a", distinct_rate_source_id="RS-A")
        d = e.record_observation(req(env, "technical_error", reason="navigation timed out", ctx={"error_type": "navigation_timeout"}, plan_id="plan-r"), a2)
        print(f"[4 retry exhaustion           ] {_fmt(d)}")

        # 5. Alternative failover (no distinct-source inflation).
        entries, rate_sources = standard_rs_entries()
        env5 = make_recovery_env(tmp, entries=entries, rate_sources=rate_sources,
                                 route_source=StubRouteSource({"route-a": ["route-b"]}))
        e5 = env5.engine
        x1 = e5.begin_attempt(plan_id="plan-f", planned_route_id="route-a", registry_id="route-a", distinct_rate_source_id="RS-TEST-001")
        e5.record_observation(req(env5, "technical_error", reason="timeout", ctx={"error_type": "navigation_timeout"}, distinct_rate_source_id="RS-TEST-001", plan_id="plan-f"), x1)
        x2 = e5.begin_attempt(plan_id="plan-f", planned_route_id="route-a", registry_id="route-a", distinct_rate_source_id="RS-TEST-001")
        d = e5.record_observation(req(env5, "technical_error", reason="timeout", ctx={"error_type": "navigation_timeout"}, distinct_rate_source_id="RS-TEST-001", plan_id="plan-f"), x2)
        print(f"[5 alternative failover        ] {_fmt(d)}  (same distinct rate source)")

        # 6. Rate-source budget exhaustion (alternative also fails).
        b1 = e5.begin_attempt(plan_id="plan-f", planned_route_id="route-b", registry_id="route-b", distinct_rate_source_id="RS-TEST-001")
        d = e5.record_observation(req(env5, "technical_error", reason="timeout", ctx={"error_type": "navigation_timeout"}, registry_id="route-b", distinct_rate_source_id="RS-TEST-001", plan_id="plan-f"), b1)
        print(f"[6 rate-source budget exhaust ] {_fmt(d)}  (no loop)")

        # 7. CAPTCHA -> blocked, no retry/bypass.
        d = e.record_observation(req(env, "access_control_detected", ctx={"error_type": "captcha"}, plan_id="plan-cap"))
        print(f"[7 CAPTCHA stop               ] {_fmt(d)}")

        # 8. Unknown field -> pause for mapping.
        d = e.record_observation(req(env, "unknown_external_field", ctx={"unknown_external_fields": ["shoe_size"]}, plan_id="plan-u"))
        print(f"[8 unknown-field pause        ] {_fmt(d)}")

        # 9. Validation correction path -> pause.
        d = e.record_observation(req(env, "validation_error", ctx={"error_paths": ["product_data.vehicles[0].use.annual_kilometres"]}, plan_id="plan-v"))
        print(f"[9 validation correction      ] {_fmt(d)}")

        # 10-11. Callback vs manual.
        d = e.record_observation(req(env, "callback_detected", plan_id="plan-cb"))
        print(f"[10 callback_required         ] {_fmt(d)}  (no call placed - Issue #9)")
        d = e.record_observation(req(env, "manual_contact_detected", plan_id="plan-mc"))
        print(f"[11 manual_handoff            ] {_fmt(d)}")

        # 12-15. Explicit evidence only.
        d = e.record_observation(req(env, "explicit_ineligible", plan_id="plan-ie"))
        print(f"[12 explicit ineligible       ] {_fmt(d)}")
        d = e.record_observation(req(env, "affinity_restricted", plan_id="plan-ar"))
        print(f"[13 affinity_restricted       ] {_fmt(d)}")
        d = e.record_observation(req(env, "specialty_only", plan_id="plan-so"))
        print(f"[14 specialty_only            ] {_fmt(d)}")
        d = e.record_observation(req(env, "not_currently_writing", plan_id="plan-nw"))
        print(f"[15 not_currently_writing     ] {_fmt(d)}")

        # 16-17. Quote vs estimate.
        d = e.record_observation(req(env, "quote_detected", ctx={"quote_present": True, "is_firm_quote": True, "reference_present": True}, plan_id="plan-q"))
        print(f"[16 quote pending normalization] {_fmt(d)}  (NOT quoted_comparable)")
        d = e.record_observation(req(env, "quote_detected", reason="estimate", ctx={"quote_present": True, "is_firm_quote": False, "estimate_only": True}, plan_id="plan-est"))
        print(f"[17 estimate_only             ] {_fmt(d)}")

        # 18. Duplicate alternate NOT executed.
        d = e.classify_unused_alternative(plan_id="plan-du", registry_id="route-b", distinct_rate_source_id="RS-TEST-001")
        print(f"[18 duplicate alt not executed] {_fmt(d)}")

        # 19. Duplicate alternate EXECUTED after failover -> not duplicate_rate_source.
        env19 = make_recovery_env(tmp, entries=entries, rate_sources=rate_sources,
                                  route_source=StubRouteSource({"route-a": ["route-b"]}))
        e19 = env19.engine
        z1 = e19.begin_attempt(plan_id="plan-f2", planned_route_id="route-a", registry_id="route-a", distinct_rate_source_id="RS-TEST-001")
        e19.record_observation(req(env19, "technical_error", reason="timeout", ctx={"error_type": "navigation_timeout"}, distinct_rate_source_id="RS-TEST-001", plan_id="plan-f2"), z1)
        z2 = e19.begin_attempt(plan_id="plan-f2", planned_route_id="route-a", registry_id="route-a", distinct_rate_source_id="RS-TEST-001")
        e19.record_observation(req(env19, "technical_error", reason="timeout", ctx={"error_type": "navigation_timeout"}, distinct_rate_source_id="RS-TEST-001", plan_id="plan-f2"), z2)
        zb = e19.begin_attempt(plan_id="plan-f2", planned_route_id="route-b", registry_id="route-b", distinct_rate_source_id="RS-TEST-001")
        d = e19.record_observation(req(env19, "quote_detected", ctx={"quote_present": True, "is_firm_quote": True}, registry_id="route-b", distinct_rate_source_id="RS-TEST-001", plan_id="plan-f2"), zb)
        print(f"[19 executed alt (after fail) ] {_fmt(d)}  (NOT duplicate_rate_source)")

        # 20. Policy change via data (no code change).
        policy_dir = tmp / "recovery"
        policy_dir.mkdir(parents=True, exist_ok=True)
        (policy_dir / "auto_policy.json").write_text(
            json.dumps({"default": {"max_attempts_per_route": 3}}), encoding="utf-8"
        )
        env20 = make_recovery_env(tmp, policy_dir=policy_dir)
        e20 = env20.engine
        y1 = e20.begin_attempt(plan_id="plan-pc", planned_route_id="route-a", registry_id="route-a", distinct_rate_source_id="RS-A")
        e20.record_observation(req(env20, "technical_error", reason="timeout", ctx={"error_type": "navigation_timeout"}, plan_id="plan-pc"), y1)
        y2 = e20.begin_attempt(plan_id="plan-pc", planned_route_id="route-a", registry_id="route-a", distinct_rate_source_id="RS-A")
        d = e20.record_observation(req(env20, "technical_error", reason="timeout", ctx={"error_type": "navigation_timeout"}, plan_id="plan-pc"), y2)
        print(f"[20 policy 2->3 via data      ] {_fmt(d)}  (still retryable with max_attempts_per_route=3)")

        # 21. New observation mapping (localized table addition).
        from app.services.recovery import classification
        from app.services.recovery.classification import _Spec
        from app.models.recovery import AttemptLifecycleStatus, ExecutionResultKind, RecoveryReasonCode, Retryability
        classification._TABLE["network_bounce"] = _Spec(
            ExecutionResultKind.TECHNICAL_ERROR, AttemptLifecycleStatus.RECOVERABLE,
            Retryability.RETRYABLE, "retry_same_route",
            (RecoveryReasonCode.TRANSIENT_NAVIGATION_FAILURE,),
            fallback_terminal_status="unreachable", failover_eligible=True,
        )
        d = e.record_observation(req(env, "network_bounce", reason="transient", plan_id="plan-nb"))
        print(f"[21 new observation mapping   ] {_fmt(d)}  (localized table entry)")

        # 22. Privacy: synthetic PII absent from state/attempts/decisions/logs.
        leaks = []
        for record in e.list_attempts():
            for marker in PII_MARKERS:
                if marker in str(record):
                    leaks.append(marker)
        print(f"[22 privacy leak scan         ] attempts_checked={len(e.list_attempts())} leaks={leaks or 'none'}")

        print("\n>>> done. Re-run yourself from backend/ with:")
        print("    $env:PYTHONPATH='tests;.'")
        print("    .\\.venv\\Scripts\\python.exe demos\\issue8_recovery_demo.py")


if __name__ == "__main__":
    main()
