"""Issue #10, Prompt 2 - hermetic E2E evidence tests (§35/§36/§37).

These tests prove evidence is collected AUTOMATICALLY during normal execution:
- Browser: synthetic intake -> route plan -> mock browser -> quote -> recovery
  -> automatic evidence -> persisted timeline (no manual EvidenceService calls).
- Voice: phone-only route -> mock voice -> disclosure -> JIT fields -> firm
  quote -> recovery -> automatic evidence.
- Callback -> voice: browser callback_required -> voice child attempt -> firm
  quote, with the browser attempt immutable and the voice attempt linked via
  parent_attempt_id.

No real insurers, no phone calls, no LLM, no applicant data.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from app.models.browser.session import BrowserExecutionMode
from app.models.evidence import EvidenceEventType
from app.models.recovery import (
    RecoveryDecideRequest,
    SourceChannel,
)
from app.services.evidence.repository import InMemoryEvidenceRepository
from app.services.evidence.service import EvidenceService
from app.services.evidence.sink import EvidenceServiceSink
from app.services.recovery.engine import RecoveryEngine
from app.services.voice.handoff import handoff_context_from_recovery

from evidence_helpers import (
    SENSITIVE_MARKERS,
    make_sink_env,
)
from voice_helpers import (
    VOICE_PHONE,
    make_voice_env,
    prepare_and_disclose,
    scripted_happy_path_questions,
)


async def _close_browser(env, session_id: str) -> None:
    try:
        await env.manager.close(session_id)
    except Exception:
        pass
    try:
        await env.browser_manager.stop()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# §35 - Browser E2E evidence (no manual evidence calls)
# ---------------------------------------------------------------------------


async def test_browser_e2e_automatic_evidence_timeline(tmp_path, mock_site) -> None:
    from app.browser.mock_site import MOCK_REGISTRY_ID
    from app.graph.browser_workflow import build_browser_workflow
    from browser_helpers import make_browser_env

    env, sink = make_sink_env()
    recovery = RecoveryEngine(evidence_sink=sink)
    benv = make_browser_env(tmp_path, mock_site, evidence_sink=sink, recovery=recovery)
    bs = benv.manager.create(benv.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await build_browser_workflow(benv.manager).ainvoke(
            {"entry": "run", "browser_session_id": bs.browser_session_id, "max_steps": 15}
        )
        assert state["workflow_status"] == "succeeded"
        session = benv.manager.get(bs.browser_session_id)
        # Recovery decision for the terminal quote observation (normal path).
        from app.browser.adapters import GenericQuoteSiteAdapter
        from app.services.recovery.classification import browser_observation_to_execution

        last = benv.manager.last_result(bs.browser_session_id)
        execution = browser_observation_to_execution(last.observation)
        recovery.record_observation(
            RecoveryDecideRequest(
                plan_id=session.plan_id,
                planned_route_id=session.planned_route_id or MOCK_REGISTRY_ID,
                registry_id=MOCK_REGISTRY_ID,
                distinct_rate_source_id="RS-MOCK-INSURER",
                intake_session_id=benv.session_id,
                source_channel=SourceChannel.BROWSER,
                observation_type=execution.observation_type,
                reason=execution.reason,
                observation_sequence=1,
                safe_context=execution.safe_context,
            )
        )

        # Read the persisted timeline through the evidence service (no manual
        # evidence writes happened anywhere in this test).
        records = await env.service.list_by_intake(benv.session_id)
        events = {r.event_type for r in records}
        assert EvidenceEventType.ROUTE_PLANNED in events
        assert EvidenceEventType.ATTEMPT_STARTED in events
        assert EvidenceEventType.BROWSER_QUOTE_OBSERVED in events
        assert EvidenceEventType.RECOVERY_DECISION in events
        assert EvidenceEventType.ATTEMPT_COMPLETED in events
        quotes = await env.service.list_quote_observations(benv.session_id)
        assert len(quotes) == 1
        assert quotes[0].firm_vs_estimate == "firm"
        # Export is safe and readable.
        view = await env.service.export(benv.session_id)
        assert view.evidence_count == len(records)
        assert view.quote_count == 1
        for marker in SENSITIVE_MARKERS:
            assert marker not in view.model_dump_json()
    finally:
        await _close_browser(benv, bs.browser_session_id)
        sink.close()


# ---------------------------------------------------------------------------
# §36 - Voice E2E evidence
# ---------------------------------------------------------------------------


async def test_voice_e2e_automatic_evidence_timeline(tmp_path: Path) -> None:
    env, sink = make_sink_env()
    venv = make_voice_env(tmp_path, events=scripted_happy_path_questions(), evidence_sink=sink)
    session = prepare_and_disclose(venv)
    while True:
        q = venv.transport.receive_event(session.voice_session_id)
        if q is None:
            break
        venv.engine.receive_broker_event(session.voice_session_id, q)

    records = await env.service.list_by_intake(venv.session_id)
    events = {r.event_type for r in records}
    assert EvidenceEventType.VOICE_SESSION_STARTED in events
    assert EvidenceEventType.VOICE_CHECKPOINT_OBSERVED in events
    assert EvidenceEventType.FIELD_INTERACTION_OBSERVED in events
    assert EvidenceEventType.VOICE_QUOTE_OBSERVED in events
    assert EvidenceEventType.RECOVERY_DECISION in events
    assert EvidenceEventType.ATTEMPT_COMPLETED in events
    quotes = await env.service.list_quote_observations(venv.session_id)
    assert len(quotes) == 1 and quotes[0].firm_vs_estimate == "firm"
    # Timeline is monotonic per attempt.
    attempt_id = quotes[0].attempt_id
    attempt_records = await env.service.list_by_attempt(venv.session_id, attempt_id)
    seqs = [r.sequence for r in attempt_records]
    assert seqs == sorted(seqs)
    for r in records:
        for marker in SENSITIVE_MARKERS:
            assert marker not in r.model_dump_json()


# ---------------------------------------------------------------------------
# §37 - Callback -> voice E2E evidence (browser immutable, voice child)
# ---------------------------------------------------------------------------


async def test_callback_to_voice_e2e_lineage(tmp_path, mock_site) -> None:
    from app.browser.mock_site import MOCK_REGISTRY_ID
    from app.graph.browser_workflow import build_browser_workflow
    from app.services.recovery.classification import browser_observation_to_execution
    from browser_helpers import make_browser_env

    env, sink = make_sink_env()
    shared_recovery = RecoveryEngine(evidence_sink=sink)
    # Browser leg: callback scenario on the mock site.
    benv = make_browser_env(
        tmp_path, mock_site, scenario="callback", evidence_sink=sink, recovery=shared_recovery
    )
    bs = benv.manager.create(benv.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await build_browser_workflow(benv.manager).ainvoke(
            {"entry": "run", "browser_session_id": bs.browser_session_id, "max_steps": 15}
        )
        assert state["workflow_status"] == "succeeded"
        bsess = benv.manager.get(bs.browser_session_id)
        browser_attempt_id = bsess.attempt_id
        assert browser_attempt_id is not None
        last = benv.manager.last_result(bs.browser_session_id)
        assert last.observation_type.value == "callback_detected"

        # Drive the browser callback through recovery (normal path).
        execution = browser_observation_to_execution(last.observation)
        decision = shared_recovery.record_observation(
            RecoveryDecideRequest(
                plan_id=bsess.plan_id,
                planned_route_id=bsess.planned_route_id or MOCK_REGISTRY_ID,
                registry_id=MOCK_REGISTRY_ID,
                distinct_rate_source_id="RS-MOCK-INSURER",
                intake_session_id=benv.session_id,
                source_channel=SourceChannel.BROWSER,
                observation_type=execution.observation_type,
                reason=execution.reason,
                observation_sequence=1,
                safe_context=execution.safe_context,
            )
        )
        assert decision.terminal_status is not None
        assert decision.terminal_status.value == "callback_required"

        # Voice leg: continuation on a SHARED recovery engine (same store).
        venv = make_voice_env(
            tmp_path,
            events=scripted_happy_path_questions(),
            evidence_sink=sink,
            recovery=shared_recovery,
        )
        context = handoff_context_from_recovery(
            decision=decision,
            intake_session_id=venv.session_id,
            registry_id=venv.registry_id,
            distinct_rate_source_id="RS-VOICE",
            provider_phone_route=VOICE_PHONE,
        )
        vs = venv.engine.prepare_handoff(context)
        venv.engine.disclose_automation(vs.voice_session_id, granted=True)
        while True:
            q = venv.transport.receive_event(vs.voice_session_id)
            if q is None:
                break
            venv.engine.receive_broker_event(vs.voice_session_id, q)
        voice = venv.engine.get(vs.voice_session_id)
        assert voice.quote_pending_normalization is True

        # --- evidence assertions -------------------------------------
        browser_records = await env.service.list_by_attempt(benv.session_id, browser_attempt_id)
        browser_events = {r.event_type for r in browser_records}
        # Browser attempt timeline: started -> callback -> decision -> completed.
        assert EvidenceEventType.ATTEMPT_STARTED in browser_events
        assert EvidenceEventType.CALLBACK_OBSERVED in browser_events
        assert EvidenceEventType.RECOVERY_DECISION in browser_events
        assert EvidenceEventType.ATTEMPT_COMPLETED in browser_events
        # Browser attempt NEVER carries a voice/quote observation.
        assert EvidenceEventType.VOICE_QUOTE_OBSERVED not in browser_events
        assert EvidenceEventType.BROWSER_QUOTE_OBSERVED not in browser_events
        cb_decisions = [
            r for r in browser_records if r.event_type is EvidenceEventType.RECOVERY_DECISION
        ]
        assert cb_decisions and cb_decisions[-1].payload.terminal_status == "callback_required"

        # Voice attempt has its OWN attempt linked to the browser attempt.
        voice_attempt_id = voice.recovery_attempt_id
        assert voice_attempt_id != browser_attempt_id
        voice_records = await env.service.list_by_attempt(venv.session_id, voice_attempt_id)
        voice_events = {r.event_type for r in voice_records}
        assert EvidenceEventType.VOICE_SESSION_STARTED in voice_events
        assert EvidenceEventType.VOICE_QUOTE_OBSERVED in voice_events
        # The voice attempt record carries parent_attempt_id = browser attempt.
        started = next(r for r in voice_records if r.event_type is EvidenceEventType.ATTEMPT_STARTED)
        assert started.parent_attempt_id == browser_attempt_id
        # The quote row belongs to the voice attempt and links to the browser.
        quotes = await env.service.list_quote_observations(venv.session_id, voice_attempt_id)
        assert len(quotes) == 1
        assert quotes[0].attempt_id == voice_attempt_id
        assert quotes[0].parent_attempt_id == browser_attempt_id

        # Full export reconstructs the chain (routes/attempts/timeline/hashes).
        export = await env.service.export(benv.session_id)
        assert browser_attempt_id in export.distinct_attempts
        for marker in SENSITIVE_MARKERS:
            assert marker not in export.model_dump_json()
    finally:
        await _close_browser(benv, bs.browser_session_id)
        sink.close()
