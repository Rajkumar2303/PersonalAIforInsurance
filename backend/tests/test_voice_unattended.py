"""Issue #9, Prompt 2 - unattended voice execution & hardening tests (hermetic).

Proves the NORMAL production path requires ZERO applicant interruptions: a
quote-ready profile answers every known broker question JIT and a firm quote
is observed. Also proves route-local failure behaviour (a missing field,
human escalation, or terminal statement on one route never blocks other
routes), pre-collected consent, batching of missing fields, the browser
callback -> Issue #8 -> voice integration, and the phone-only route path.

No real calls, no LLM, no external APIs, no BrowserSession dependency.
"""

from __future__ import annotations

from app.models.recovery import (
    AttemptLifecycleStatus,
    RecoveryAction,
    RecoveryDecideRequest,
    RouteOutcomeStatus,
    SourceChannel,
)
from app.models.voice import (
    BrokerQuestionKind,
    VoiceLifecycleStatus,
    VoiceResponseAction,
    VoiceRouteStatus,
)
from app.services.voice.handoff import handoff_context_from_phone_route, handoff_context_from_recovery
from route_planner_helpers import entry, rate_source
from voice_helpers import (
    VOICE_PHONE,
    field_question,
    kind_question,
    make_handoff_context,
    make_voice_env,
    prepare_and_disclose,
    scripted_happy_path_questions,
)

# ---------------------------------------------------------------------------
# Unattended driver
# ---------------------------------------------------------------------------


def run_scripted_call(env, session):
    """Unattended driver: pull scripted broker events until the script ends.

    No applicant interaction anywhere in this loop - the applicant never sits
    at the screen while the route runs.
    """
    decisions = []
    while True:
        question = env.transport.receive_event(session.voice_session_id)
        if question is None:
            break
        decisions.append(env.engine.receive_broker_event(session.voice_session_id, question))
    return decisions


# ---------------------------------------------------------------------------
# 1. Unattended happy path (the NORMAL production path)
# ---------------------------------------------------------------------------


def test_unattended_happy_path_zero_applicant_interruptions(tmp_path):
    env = make_voice_env(tmp_path, events=scripted_happy_path_questions())
    session = prepare_and_disclose(env)
    decisions = run_scripted_call(env, session)

    # 8 known questions + 1 firm quote.
    assert len(decisions) == 9
    # Every known question was answered automatically - zero interruptions.
    session_after = env.engine.get(session.voice_session_id)
    assert session_after.applicant_interruptions == 0
    assert session_after.automated_answers == 8
    # Firm quote observed -> pending normalization, never comparable.
    assert session_after.quote_pending_normalization is True
    assert session_after.terminal_status is None
    assert session_after.route_status == VoiceRouteStatus.QUOTE_PENDING_NORMALIZATION
    # Call completed.
    assert session_after.lifecycle_status == VoiceLifecycleStatus.COMPLETED
    assert env.transport.ended
    # The value-level assertions: JIT values were spoken and discarded.
    assert env.transport.spoken_count == 8
    assert env.transport.last_spoken is None


def test_happy_path_known_questions_never_prompt_for_consent(tmp_path):
    # Pre-collected route consent covers every canonical field -> no REQUEST_CONSENT.
    env = make_voice_env(tmp_path, events=scripted_happy_path_questions())
    session = prepare_and_disclose(env)
    decisions = run_scripted_call(env, session)
    assert not [d for d in decisions if d.action == VoiceResponseAction.REQUEST_CONSENT]
    assert env.engine.get(session.voice_session_id).applicant_interruptions == 0


# ---------------------------------------------------------------------------
# 2. Missing field is route-local - other routes keep running
# ---------------------------------------------------------------------------


def _multi_route_env(tmp_path):
    entries = [
        entry("route-a", distinct_rate_source_id="RS-A", public_phone_route=VOICE_PHONE, quote_url=None),
        entry("route-b", distinct_rate_source_id="RS-B", public_phone_route="1-800-MOCK-B", quote_url=None),
    ]
    rate_sources = [
        rate_source("RS-A", related_registry_ids=["route-a"]),
        rate_source("RS-B", related_registry_ids=["route-b"]),
    ]
    env = make_voice_env(
        tmp_path, entries=entries, rate_sources=rate_sources, grant_consent=False, registry_id="route-a"
    )
    env.intake.grant_route_consent(env.session_id, "route-a", [], True)
    env.intake.grant_route_consent(env.session_id, "route-b", [], True)
    return env


def _prepare_route(env, registry_id: str, *, missing_path=None):
    context = make_handoff_context(env)
    # Re-point the context at a specific route.
    context = context.model_copy(
        update={
            "registry_id": registry_id,
            "distinct_rate_source_id": "RS-A" if registry_id == "route-a" else "RS-B",
            "planned_route_id": registry_id,
        }
    )
    session = env.engine.prepare_handoff(context)
    env.engine.disclose_automation(session.voice_session_id, granted=True)
    return env.engine.get(session.voice_session_id)


def test_missing_field_pauses_only_that_route(tmp_path):
    env = _multi_route_env(tmp_path)
    # Route A asks for a missing field -> pauses.
    route_a = _prepare_route(env, "route-a")
    path = "product_data.vehicles[0].use.annual_kilometres"
    decision_a = env.engine.receive_broker_event(route_a.voice_session_id, field_question(path))
    assert decision_a.action == VoiceResponseAction.PAUSE_FOR_APPLICANT
    assert env.engine.get(route_a.voice_session_id).route_status == VoiceRouteStatus.PAUSED_MISSING_INFORMATION
    assert env.engine.get(route_a.voice_session_id).applicant_interruptions == 1

    # Route B runs the full happy path to a quote - NOT blocked by Route A.
    route_b = _prepare_route(env, "route-b")
    for q in scripted_happy_path_questions():
        env.engine.receive_broker_event(route_b.voice_session_id, q)
    b = env.engine.get(route_b.voice_session_id)
    assert b.route_status == VoiceRouteStatus.QUOTE_PENDING_NORMALIZATION
    assert b.applicant_interruptions == 0
    assert b.lifecycle_status == VoiceLifecycleStatus.COMPLETED

    # The orchestrator sees BOTH routes with independent, route-local states.
    summaries = env.engine.route_summaries(env.session_id)
    by_registry = {s.registry_id: s for s in summaries}
    assert set(by_registry) == {"route-a", "route-b"}
    assert by_registry["route-a"].route_status == VoiceRouteStatus.PAUSED_MISSING_INFORMATION
    assert by_registry["route-a"].pending_field_paths == [path]
    assert by_registry["route-b"].route_status == VoiceRouteStatus.QUOTE_PENDING_NORMALIZATION


def test_missing_fields_batchable_no_repeated_ui(tmp_path):
    env = make_voice_env(tmp_path)
    session = prepare_and_disclose(env)
    # Two missing fields (both in the synthetic catalog) discovered -> both
    # collected as canonical paths; the engine never launched a UI prompt.
    env.engine.receive_broker_event(
        session.voice_session_id, field_question("product_data.vehicles[0].use.annual_kilometres")
    )
    env.engine.receive_broker_event(
        session.voice_session_id, field_question("applicant.address.years_at_current_address")
    )
    summary = env.engine.route_summaries(env.session_id)[0]
    assert summary.route_status == VoiceRouteStatus.PAUSED_MISSING_INFORMATION
    assert set(summary.pending_field_paths) == {
        "product_data.vehicles[0].use.annual_kilometres",
        "applicant.address.years_at_current_address",
    }
    assert summary.applicant_interruptions == 2  # each pause is one route-local ask


# ---------------------------------------------------------------------------
# 3. User unavailable / human escalation is exceptional + route-local
# ---------------------------------------------------------------------------


def test_user_unavailable_human_checkpoint_does_not_block_other_route(tmp_path):
    env = _multi_route_env(tmp_path)
    # Route A hits an identity checkpoint -> applicant_required (user absent).
    route_a = _prepare_route(env, "route-a")
    env.engine.receive_broker_event(
        route_a.voice_session_id, kind_question(BrokerQuestionKind.IDENTITY_CHECKPOINT)
    )
    a = env.engine.get(route_a.voice_session_id)
    assert a.route_status == VoiceRouteStatus.APPLICANT_REQUIRED
    assert a.pending_checkpoint == BrokerQuestionKind.IDENTITY_CHECKPOINT.value
    assert a.applicant_interruptions == 1
    # Resumable safe context preserved (session persists, pending checkpoint).
    assert env.engine.get(route_a.voice_session_id).lifecycle_status == VoiceLifecycleStatus.AWAITING_HUMAN

    # Route B still completes unattended.
    route_b = _prepare_route(env, "route-b")
    for q in scripted_happy_path_questions():
        env.engine.receive_broker_event(route_b.voice_session_id, q)
    assert env.engine.get(route_b.voice_session_id).route_status == VoiceRouteStatus.QUOTE_PENDING_NORMALIZATION


def test_human_escalation_never_automated(tmp_path):
    env = make_voice_env(tmp_path)
    for _kind in (BrokerQuestionKind.IDENTITY_CHECKPOINT, BrokerQuestionKind.DECLARATION,
                  BrokerQuestionKind.ADVICE_REQUEST, BrokerQuestionKind.APPLICANT_REQUIRED):
        session = prepare_and_disclose(env)
        decision = env.engine.receive_broker_event(session.voice_session_id, kind_question(_kind))
        assert decision.action == VoiceResponseAction.TRANSFER_TO_APPLICANT
        assert env.engine.get(session.voice_session_id).route_status == VoiceRouteStatus.APPLICANT_REQUIRED
        assert env.transport.last_spoken is None  # never answered


# ---------------------------------------------------------------------------
# 4. Consent is re-checked JIT; revocation wins immediately
# ---------------------------------------------------------------------------


def test_pre_collected_consent_avoids_redundant_prompts(tmp_path):
    # grant_consent=True = the frontend already authorized route disclosure.
    env = make_voice_env(tmp_path, events=scripted_happy_path_questions())
    session = prepare_and_disclose(env)
    decisions = run_scripted_call(env, session)
    consent_pauses = [d for d in decisions if d.action == VoiceResponseAction.REQUEST_CONSENT]
    assert consent_pauses == []


def test_consent_revocation_wins_immediately_mid_call(tmp_path):
    env = make_voice_env(tmp_path)
    session = prepare_and_disclose(env)
    path = "applicant.address.postal_code"
    first = env.engine.receive_broker_event(session.voice_session_id, field_question(path))
    assert first.action == VoiceResponseAction.DISCLOSE_VALUE
    from voice_helpers import revoke_route_consent

    revoke_route_consent(env, env.registry_id)
    blocked = env.engine.receive_broker_event(session.voice_session_id, field_question(path))
    assert blocked.action == VoiceResponseAction.REQUEST_CONSENT
    assert env.engine.get(session.voice_session_id).route_status == VoiceRouteStatus.PAUSED_MISSING_INFORMATION


# ---------------------------------------------------------------------------
# 5. Route-local failure scenarios A-J (Issue #8 remains authority)
# ---------------------------------------------------------------------------


def test_route_local_failures_issue8_authority(tmp_path):
    scenarios = [
        # (kind, expected_route_status, expected_terminal_status_or_None)
        (BrokerQuestionKind.BROKER_UNAVAILABLE, VoiceRouteStatus.FAILED, "unreachable"),
        (BrokerQuestionKind.BROKER_UNAVAILABLE, VoiceRouteStatus.FAILED, "unreachable"),  # call disconnected
        (BrokerQuestionKind.CALLBACK_REQUEST, VoiceRouteStatus.CALLBACK_SCHEDULED, "callback_required"),
        (BrokerQuestionKind.UNKNOWN, VoiceRouteStatus.MANUAL_HANDOFF, None),
        (BrokerQuestionKind.IDENTITY_CHECKPOINT, VoiceRouteStatus.APPLICANT_REQUIRED, None),
        (BrokerQuestionKind.QUOTE_DISCLOSURE, VoiceRouteStatus.QUOTE_PENDING_NORMALIZATION, None),
        (BrokerQuestionKind.ESTIMATE_DISCLOSURE, VoiceRouteStatus.ESTIMATE_ONLY, "estimate_only"),
        (BrokerQuestionKind.INELIGIBILITY, VoiceRouteStatus.FAILED, "ineligible"),
        (BrokerQuestionKind.NOT_CURRENTLY_WRITING, VoiceRouteStatus.FAILED, "not_currently_writing"),
        (BrokerQuestionKind.SPECIALTY_ONLY, VoiceRouteStatus.FAILED, "specialty_only"),
    ]
    for kind, expected_status, terminal in scenarios:
        # A FRESH env per scenario so each route gets its own Issue #8 attempt
        # (one attempt per route; sharing would reflect prior terminal states).
        env = make_voice_env(tmp_path)
        session = prepare_and_disclose(env)
        env.engine.receive_broker_event(session.voice_session_id, kind_question(kind))
        voice = env.engine.get(session.voice_session_id)
        assert voice.route_status == expected_status, f"{kind}: {voice.route_status}"
        if terminal:
            assert voice.terminal_status == terminal, f"{kind}: {voice.terminal_status}"
        # Never a comparable status anywhere.
        assert voice.terminal_status not in {"quoted_comparable", "quoted_non_comparable"}
        # Issue #8 recorded an attempt for this route.
        assert env.recovery_store.list_all()
        # Route-local: a second session on the same env/route is unaffected...
        other = prepare_and_disclose(env)
        assert env.engine.get(other.voice_session_id).lifecycle_status in (
            VoiceLifecycleStatus.AWAITING_DISCLOSURE,
            VoiceLifecycleStatus.ACTIVE,
        )


def test_failed_route_does_not_affect_completed_route(tmp_path):
    env = _multi_route_env(tmp_path)
    route_a = _prepare_route(env, "route-a")
    env.engine.receive_broker_event(route_a.voice_session_id, kind_question(BrokerQuestionKind.INELIGIBILITY))
    assert env.engine.get(route_a.voice_session_id).route_status == VoiceRouteStatus.FAILED
    route_b = _prepare_route(env, "route-b")
    env.engine.receive_broker_event(route_b.voice_session_id, kind_question(BrokerQuestionKind.QUOTE_DISCLOSURE))
    assert env.engine.get(route_b.voice_session_id).route_status == VoiceRouteStatus.QUOTE_PENDING_NORMALIZATION


# ---------------------------------------------------------------------------
# 6. Browser callback -> Issue #8 -> prepare_voice_handoff -> voice
#    (voice continuation gets its OWN attempt; browser attempt immutable)
# ---------------------------------------------------------------------------


def _browser_callback_required(env):
    """Drive a browser callback_detected observation through Issue #8."""
    recovery = env.recovery.record_observation(
        RecoveryDecideRequest(
            planned_route_id=env.registry_id,
            registry_id=env.registry_id,
            distinct_rate_source_id="RS-VOICE",
            intake_session_id=env.session_id,
            source_channel=SourceChannel.BROWSER,
            observation_type="callback_detected",
        )
    )
    assert recovery.terminal_status is RouteOutcomeStatus.CALLBACK_REQUIRED
    assert recovery.recommended_action is RecoveryAction.PREPARE_VOICE_HANDOFF
    browser_attempt = env.recovery_store.list_all()[-1]
    assert browser_attempt.terminal_status is RouteOutcomeStatus.CALLBACK_REQUIRED
    return recovery, browser_attempt


def test_a_callback_voice_continuation_own_attempt(tmp_path):
    env = make_voice_env(tmp_path)
    recovery, browser_attempt = _browser_callback_required(env)
    # Voice continuation prepared from the browser callback decision.
    context = handoff_context_from_recovery(
        decision=recovery,
        intake_session_id=env.session_id,
        registry_id=env.registry_id,
        distinct_rate_source_id="RS-VOICE",
        provider_phone_route=VOICE_PHONE,
    )
    assert context.source_attempt_id == recovery.attempt_id
    session = env.engine.prepare_handoff(context)
    voice = env.engine.get(session.voice_session_id)
    # The voice continuation has its OWN Issue #8 attempt identity.
    assert voice.recovery_attempt_id
    assert voice.recovery_attempt_id != browser_attempt.attempt_id
    voice_attempt = next(
        a for a in env.recovery_store.list_all() if a.attempt_id == voice.recovery_attempt_id
    )
    assert voice_attempt.channel is SourceChannel.VOICE
    assert voice_attempt.parent_attempt_id == browser_attempt.attempt_id  # linked lineage
    # The browser attempt is still terminal callback_required and immutable.
    assert browser_attempt.lifecycle_status is AttemptLifecycleStatus.TERMINAL
    assert browser_attempt.terminal_status is RouteOutcomeStatus.CALLBACK_REQUIRED


def test_b_voice_continuation_firm_quote_pending_independent(tmp_path):
    env = make_voice_env(tmp_path)
    recovery, browser_attempt = _browser_callback_required(env)
    browser_snapshot = browser_attempt.model_dump()
    context = handoff_context_from_recovery(
        decision=recovery,
        intake_session_id=env.session_id,
        registry_id=env.registry_id,
        distinct_rate_source_id="RS-VOICE",
        provider_phone_route=VOICE_PHONE,
    )
    session = env.engine.prepare_handoff(context)
    env.engine.disclose_automation(session.voice_session_id, granted=True)
    for q in scripted_happy_path_questions():
        env.engine.receive_broker_event(session.voice_session_id, q)
    voice = env.engine.get(session.voice_session_id)
    # The voice result is quote_pending_normalization - NOT callback_required.
    assert voice.route_status == VoiceRouteStatus.QUOTE_PENDING_NORMALIZATION
    assert voice.quote_pending_normalization is True
    assert voice.terminal_status is None
    assert voice.applicant_interruptions == 0
    # The voice attempt is terminal quote_pending_normalization.
    voice_attempt = env.recovery_store.get(voice.recovery_attempt_id)
    assert voice_attempt.quote_pending_normalization is True
    assert voice_attempt.terminal_status is None
    # Original browser attempt is UNCHANGED.
    assert env.recovery_store.get(browser_attempt.attempt_id).model_dump() == browser_snapshot
    assert env.recovery_store.get(browser_attempt.attempt_id).terminal_status is RouteOutcomeStatus.CALLBACK_REQUIRED


def test_c_voice_continuation_estimate_only_own_attempt(tmp_path):
    env = make_voice_env(tmp_path)
    recovery, browser_attempt = _browser_callback_required(env)
    context = handoff_context_from_recovery(
        decision=recovery,
        intake_session_id=env.session_id,
        registry_id=env.registry_id,
        distinct_rate_source_id="RS-VOICE",
        provider_phone_route=VOICE_PHONE,
    )
    session = env.engine.prepare_handoff(context)
    env.engine.disclose_automation(session.voice_session_id, granted=True)
    env.engine.receive_broker_event(
        session.voice_session_id, kind_question(BrokerQuestionKind.ESTIMATE_DISCLOSURE)
    )
    voice = env.engine.get(session.voice_session_id)
    assert voice.route_status == VoiceRouteStatus.ESTIMATE_ONLY
    assert voice.terminal_status == "estimate_only"
    assert voice.recovery_attempt_id != browser_attempt.attempt_id
    voice_attempt = env.recovery_store.get(voice.recovery_attempt_id)
    assert voice_attempt.terminal_status is RouteOutcomeStatus.ESTIMATE_ONLY
    # Browser attempt untouched.
    assert env.recovery_store.get(browser_attempt.attempt_id).terminal_status is RouteOutcomeStatus.CALLBACK_REQUIRED


def test_d_voice_continuation_no_answer_own_outcome(tmp_path):
    env = make_voice_env(tmp_path)
    recovery, browser_attempt = _browser_callback_required(env)
    context = handoff_context_from_recovery(
        decision=recovery,
        intake_session_id=env.session_id,
        registry_id=env.registry_id,
        distinct_rate_source_id="RS-VOICE",
        provider_phone_route=VOICE_PHONE,
    )
    session = env.engine.prepare_handoff(context)
    env.engine.disclose_automation(session.voice_session_id, granted=True)
    env.engine.receive_broker_event(
        session.voice_session_id, kind_question(BrokerQuestionKind.BROKER_UNAVAILABLE)
    )
    voice = env.engine.get(session.voice_session_id)
    # Its own Issue #8 outcome (unreachable / manual), NOT callback_required.
    assert voice.recovery_attempt_id != browser_attempt.attempt_id
    assert voice.route_status in (VoiceRouteStatus.FAILED, VoiceRouteStatus.MANUAL_HANDOFF)
    voice_attempt = env.recovery_store.get(voice.recovery_attempt_id)
    assert voice_attempt.terminal_status in (
        RouteOutcomeStatus.UNREACHABLE,
        RouteOutcomeStatus.MANUAL_HANDOFF,
        RouteOutcomeStatus.UNRESOLVED,
    )
    assert voice_attempt.terminal_status is not RouteOutcomeStatus.CALLBACK_REQUIRED
    # Browser attempt unchanged.
    assert env.recovery_store.get(browser_attempt.attempt_id).terminal_status is RouteOutcomeStatus.CALLBACK_REQUIRED


def test_e_browser_terminal_attempt_never_mutated(tmp_path):
    env = make_voice_env(tmp_path)
    recovery, browser_attempt = _browser_callback_required(env)
    browser_snapshot = browser_attempt.model_dump()
    # Run several independent voice continuations after the browser terminal.
    for _ in range(2):
        context = handoff_context_from_recovery(
            decision=recovery,
            intake_session_id=env.session_id,
            registry_id=env.registry_id,
            distinct_rate_source_id="RS-VOICE",
            provider_phone_route=VOICE_PHONE,
        )
        session = env.engine.prepare_handoff(context)
        env.engine.disclose_automation(session.voice_session_id, granted=True)
        for q in scripted_happy_path_questions():
            env.engine.receive_broker_event(session.voice_session_id, q)
    # No terminal-attempt mutation: the browser attempt is byte-for-byte identical.
    assert env.recovery_store.get(browser_attempt.attempt_id).model_dump() == browser_snapshot
    assert env.recovery_store.get(browser_attempt.attempt_id).lifecycle_status is AttemptLifecycleStatus.TERMINAL
    assert env.recovery_store.get(browser_attempt.attempt_id).terminal_status is RouteOutcomeStatus.CALLBACK_REQUIRED


def test_callback_browser_to_voice_integration(tmp_path):
    """Full chain: browser observation -> callback_required -> handoff -> voice
    continuation with its own attempt -> quote observation -> Issue #8."""
    env = make_voice_env(tmp_path)
    recovery, browser_attempt = _browser_callback_required(env)
    context = handoff_context_from_recovery(
        decision=recovery,
        intake_session_id=env.session_id,
        registry_id=env.registry_id,
        distinct_rate_source_id="RS-VOICE",
        provider_phone_route=VOICE_PHONE,
    )
    session = env.engine.prepare_handoff(context)
    env.engine.disclose_automation(session.voice_session_id, granted=True)
    for q in scripted_happy_path_questions():
        env.engine.receive_broker_event(session.voice_session_id, q)
    voice = env.engine.get(session.voice_session_id)
    assert voice.route_status == VoiceRouteStatus.QUOTE_PENDING_NORMALIZATION
    assert voice.source_attempt_id == recovery.attempt_id  # correlated
    assert voice.applicant_interruptions == 0
    # Browser attempt is callback_required + prepare_voice_handoff (immutable).
    assert env.recovery_store.get(browser_attempt.attempt_id).terminal_status is RouteOutcomeStatus.CALLBACK_REQUIRED
    assert env.recovery_store.get(browser_attempt.attempt_id).recovery_action is RecoveryAction.PREPARE_VOICE_HANDOFF


# ---------------------------------------------------------------------------
# 7. Phone-only route (RoutePlanner -> Issue #9; no BrowserSession)
# ---------------------------------------------------------------------------


def test_phone_only_route_planner_to_voice(tmp_path):
    env = make_voice_env(tmp_path)
    plan = env.planner.plan(env.session_id)
    route = next(r for r in plan.routes if r.registry_id == env.registry_id)
    # No online channel (quote_url=None); phone/callback present.
    kinds = {c.kind.value for c in route.channels}
    assert "phone" in kinds
    assert "online" not in kinds
    assert route.is_ready

    context = handoff_context_from_phone_route(
        route=route,
        intake_session_id=env.session_id,
        registry_id=env.registry_id,
    )
    assert context.provider_phone_route == VOICE_PHONE
    session = env.engine.prepare_handoff(context)
    env.engine.disclose_automation(session.voice_session_id, granted=True)
    for q in scripted_happy_path_questions():
        env.engine.receive_broker_event(session.voice_session_id, q)
    voice = env.engine.get(session.voice_session_id)
    assert voice.route_status == VoiceRouteStatus.QUOTE_PENDING_NORMALIZATION
    assert voice.applicant_interruptions == 0
    # No BrowserSession was ever involved.


# ---------------------------------------------------------------------------
# 8. Route-local pause / resume API contract
# ---------------------------------------------------------------------------


def test_route_local_pause_idempotent_and_resume(tmp_path):
    env = make_voice_env(tmp_path)
    session = prepare_and_disclose(env)
    paused = env.engine.pause(session.voice_session_id, reason="operator_pause")
    assert paused.route_status == VoiceRouteStatus.PAUSED_MISSING_INFORMATION
    assert paused.lifecycle_status == VoiceLifecycleStatus.PAUSED_FOR_APPLICANT
    # Idempotent: pausing again is a safe no-op.
    again = env.engine.pause(session.voice_session_id, reason="operator_pause")
    assert again.lifecycle_status == VoiceLifecycleStatus.PAUSED_FOR_APPLICANT
    # Resume with no pending fields -> active / running.
    resumed = env.engine.resume(session.voice_session_id)
    assert resumed.lifecycle_status == VoiceLifecycleStatus.ACTIVE
    assert env.engine.get(session.voice_session_id).route_status == VoiceRouteStatus.RUNNING


# ---------------------------------------------------------------------------
# 9. Quote boundary: firm -> pending normalization, estimate -> estimate_only
# ---------------------------------------------------------------------------


def test_quote_boundary_never_comparable(tmp_path):
    # A fresh env per case so each lands on its own Issue #8 attempt.
    firm_env = make_voice_env(tmp_path)
    firm = prepare_and_disclose(firm_env)
    firm_env.engine.receive_broker_event(firm.voice_session_id, kind_question(BrokerQuestionKind.QUOTE_DISCLOSURE))
    firm_s = firm_env.engine.get(firm.voice_session_id)
    assert firm_s.quote_pending_normalization is True
    assert firm_s.terminal_status is None
    assert firm_s.route_status == VoiceRouteStatus.QUOTE_PENDING_NORMALIZATION

    est_env = make_voice_env(tmp_path)
    est = prepare_and_disclose(est_env)
    est_env.engine.receive_broker_event(est.voice_session_id, kind_question(BrokerQuestionKind.ESTIMATE_DISCLOSURE))
    est_s = est_env.engine.get(est.voice_session_id)
    assert est_s.quote_pending_normalization is False
    assert est_s.terminal_status == "estimate_only"
    assert est_s.route_status == VoiceRouteStatus.ESTIMATE_ONLY
    for s in (firm_s, est_s):
        assert s.terminal_status not in {"quoted_comparable", "quoted_non_comparable"}
