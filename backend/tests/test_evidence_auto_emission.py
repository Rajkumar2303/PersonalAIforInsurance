"""Issue #10, Prompt 2 - automatic evidence emission tests (hermetic).

Proves that recovery decisions, voice observations, browser observations, and
route/attempt lineage are recorded AUTOMATICALLY through the ``EvidenceSink``
with no manual EvidenceService calls, and that idempotency, persistence-failure
policy, concurrency, and dynamic fields behave correctly.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from app.models.evidence import (
    AuditEventName,
    EvidenceEventType,
    PageObservationEvidence,
    QuoteObservation,
    QuoteObservationEvidence,
)
from app.models.recovery import (
    AttemptLifecycleStatus,
    RecoveryAction,
    RecoveryDecideRequest,
    SourceChannel,
)
from app.services.evidence.ingest import EvidenceDraft, field_interaction_draft
from app.services.evidence.sink import EvidenceServiceSink, EvidenceWriteStatus
from app.services.recovery.attempt_store import InMemoryAttemptStore
from app.services.recovery.engine import RecoveryEngine
from app.services.recovery.policy import RecoveryPolicyLoader

from evidence_helpers import (
    FailingEvidenceSink,
    SENSITIVE_MARKERS,
    make_evidence_env,
    make_sink_env,
    page_observation,
    quote_observation,
)
from voice_helpers import (
    make_voice_env,
    prepare_and_disclose,
    scripted_happy_path_questions,
)


# ---------------------------------------------------------------------------
# Recovery auto-emission
# ---------------------------------------------------------------------------


def _recovery_env(tmp_path: Path, sink):
    loader = RecoveryPolicyLoader(policy_dir=tmp_path / "auto_recovery")
    return RecoveryEngine(
        store=InMemoryAttemptStore(),
        policy=loader.load(),
        evidence_sink=sink,
    )


def _request(attempt_id: str = None, observation_type: str = "quote_detected") -> RecoveryDecideRequest:
    return RecoveryDecideRequest(
        attempt_id=attempt_id,
        plan_id="plan-1",
        planned_route_id="mock-insurer",
        registry_id="mock-insurer",
        distinct_rate_source_id="RS-MOCK-INSURER",
        intake_session_id="intake-1",
        source_channel=SourceChannel.BROWSER,
        observation_type=observation_type,
        reason="quote observed",
        observation_sequence=1,
        safe_context={"reason_code": "quote_detected"},
    )


async def test_recovery_auto_emits_attempt_and_decision(tmp_path: Path) -> None:
    env, sink = make_sink_env()
    recovery = _recovery_env(tmp_path, sink)
    attempt = recovery.begin_attempt(
        plan_id="plan-1", planned_route_id="mock-insurer", registry_id="mock-insurer",
        distinct_rate_source_id="RS-MOCK-INSURER", channel=SourceChannel.BROWSER,
        intake_session_id="intake-1",
    )
    decision = recovery.record_observation(_request(attempt.attempt_id))

    records = await env.service.list_by_intake("intake-1")
    events = [r.event_type for r in records]
    assert EvidenceEventType.ATTEMPT_STARTED in events
    assert EvidenceEventType.RECOVERY_DECISION in events
    assert EvidenceEventType.ATTEMPT_COMPLETED in events  # terminal quote -> completed
    started = next(r for r in records if r.event_type is EvidenceEventType.ATTEMPT_STARTED)
    assert started.attempt_id == attempt.attempt_id
    decision_record = next(r for r in records if r.event_type is EvidenceEventType.RECOVERY_DECISION)
    assert decision_record.payload.quote_pending_normalization is True
    assert decision_record.attempt_id == attempt.attempt_id
    for r in records:
        assert await env.service.verify_integrity("intake-1", r.evidence_id)


async def test_recovery_idempotent_observation_single_decision_record(tmp_path: Path) -> None:
    env, sink = make_sink_env()
    recovery = _recovery_env(tmp_path, sink)
    attempt = recovery.begin_attempt(
        plan_id="plan-1", planned_route_id="mock-insurer", registry_id="mock-insurer",
        distinct_rate_source_id="RS-MOCK-INSURER", channel=SourceChannel.BROWSER,
        intake_session_id="intake-1",
    )
    req = _request(attempt.attempt_id, observation_type="access_control_detected")
    recovery.record_observation(req)
    recovery.record_observation(req)  # duplicate -> idempotent at evidence level
    records = await env.service.list_by_intake("intake-1")
    decisions = [r for r in records if r.event_type is EvidenceEventType.RECOVERY_DECISION]
    assert len(decisions) == 1


# ---------------------------------------------------------------------------
# Voice auto-emission
# ---------------------------------------------------------------------------


async def test_voice_auto_emits_full_timeline(tmp_path: Path) -> None:
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
    assert EvidenceEventType.VOICE_CHECKPOINT_OBSERVED in events  # disclosure
    assert EvidenceEventType.FIELD_INTERACTION_OBSERVED in events  # JIT field disclosures
    assert EvidenceEventType.VOICE_QUOTE_OBSERVED in events
    assert EvidenceEventType.RECOVERY_DECISION in events
    assert EvidenceEventType.ATTEMPT_STARTED in events
    assert EvidenceEventType.ATTEMPT_COMPLETED in events
    quotes = await env.service.list_quote_observations(venv.session_id)
    assert len(quotes) == 1
    assert quotes[0].firm_vs_estimate == "firm"
    # No applicant values anywhere in the auto-emitted records.
    for r in records:
        blob = r.model_dump_json()
        for marker in SENSITIVE_MARKERS:
            assert marker not in blob


async def test_voice_estimate_stays_estimate(tmp_path: Path) -> None:
    from voice_helpers import kind_question
    from app.models.voice import BrokerQuestionKind

    env, sink = make_sink_env()
    venv = make_voice_env(tmp_path, evidence_sink=sink)
    session = prepare_and_disclose(venv)
    venv.engine.receive_broker_event(session.voice_session_id, kind_question(BrokerQuestionKind.ESTIMATE_DISCLOSURE))
    records = await env.service.list_by_intake(venv.session_id)
    assert any(r.event_type is EvidenceEventType.VOICE_ESTIMATE_OBSERVED for r in records)
    quotes = await env.service.list_quote_observations(venv.session_id)
    assert len(quotes) == 1 and quotes[0].firm_vs_estimate == "estimate"


# ---------------------------------------------------------------------------
# Browser auto-emission (local mock site)
# ---------------------------------------------------------------------------


async def test_browser_auto_emits_observation_and_quote(tmp_path: Path, mock_site) -> None:
    from app.browser.mock_site import MOCK_REGISTRY_ID
    from app.graph.browser_workflow import build_browser_workflow
    from app.models.browser.session import BrowserExecutionMode
    from app.services.recovery.engine import RecoveryEngine
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
        sess = benv.manager.get(bs.browser_session_id)
        assert sess.quote_present is True
        assert sess.attempt_id is not None
        records = await env.service.list_by_intake(benv.session_id)
        events = {r.event_type for r in records}
        assert EvidenceEventType.ROUTE_PLANNED in events
        assert EvidenceEventType.ATTEMPT_STARTED in events
        assert EvidenceEventType.BROWSER_QUOTE_OBSERVED in events
        # Every browser record carries the attempt + registry + config lineage.
        for r in records:
            if r.attempt_id is not None:
                assert r.attempt_id == sess.attempt_id
            if r.registry_id:
                assert r.registry_id == MOCK_REGISTRY_ID
        quotes = await env.service.list_quote_observations(benv.session_id)
        assert len(quotes) == 1
        assert quotes[0].annual_premium == Decimal("1234.56")
        assert quotes[0].firm_vs_estimate == "firm"
    finally:
        try:
            await benv.manager.close(bs.browser_session_id)
        except Exception:
            pass
        try:
            await benv.browser_manager.stop()
        except Exception:
            pass
        sink.close()


# ---------------------------------------------------------------------------
# Idempotency under auto-emission
# ---------------------------------------------------------------------------


async def test_auto_emission_idempotent_redelivery() -> None:
    env, sink = make_sink_env()
    d = EvidenceDraft(
        event_type=EvidenceEventType.PAGE_OBSERVED,
        payload=PageObservationEvidence(page_signature="sig-x"),
        plan_id="plan-1", planned_route_id="mock-insurer", registry_id="mock-insurer",
        distinct_rate_source_id="RS-MOCK-INSURER", attempt_id="att-1",
    )
    r1 = sink.record("intake-1", d)
    r2 = sink.record("intake-1", d)
    assert r1.durable and r2.durable
    assert r1.record_id == r2.record_id
    assert len(await env.service.list_by_attempt("intake-1", "att-1")) == 1


async def test_auto_emission_different_observations_separate_records() -> None:
    env, sink = make_sink_env()
    d1 = EvidenceDraft(event_type=EvidenceEventType.PAGE_OBSERVED, payload=PageObservationEvidence(page_signature="a"), plan_id="plan-1", planned_route_id="mock-insurer", registry_id="mock-insurer", distinct_rate_source_id="RS-MOCK-INSURER", attempt_id="att-1")
    d2 = EvidenceDraft(event_type=EvidenceEventType.PAGE_OBSERVED, payload=PageObservationEvidence(page_signature="b"), plan_id="plan-1", planned_route_id="mock-insurer", registry_id="mock-insurer", distinct_rate_source_id="RS-MOCK-INSURER", attempt_id="att-1")
    sink.record("intake-1", d1)
    sink.record("intake-1", d2)
    assert len(await env.service.list_by_attempt("intake-1", "att-1")) == 2


# ---------------------------------------------------------------------------
# Persistence-failure policy (§11/§12/§29): explicit, route-local, no retry
# ---------------------------------------------------------------------------


def test_failing_sink_never_raises_and_reports_status(tmp_path: Path) -> None:
    failing = FailingEvidenceSink()
    venv = make_voice_env(tmp_path, events=scripted_happy_path_questions(), evidence_sink=failing)
    session = prepare_and_disclose(venv)
    # Provider flow completes normally despite every evidence write failing.
    while True:
        q = venv.transport.receive_event(session.voice_session_id)
        if q is None:
            break
        venv.engine.receive_broker_event(session.voice_session_id, q)
    # No exception raised; evidence failures are explicit.
    assert failing.calls, "expected failed evidence writes to be attempted"
    assert all(c[0] in ("record", "quote", "audit") for c in failing.calls)
    assert failing.evidence_status() == EvidenceWriteStatus.PERSISTENCE_FAILED.value
    # Provider interaction was NOT retried due to DB failure: the scripted
    # event stream was consumed exactly once to a firm quote.
    session_after = venv.engine.get(session.voice_session_id)
    assert session_after.quote_pending_normalization is True
    assert venv.transport.ended


async def test_quote_evidence_never_falsely_durable() -> None:
    env, sink = make_sink_env()
    # Force persistence failure on the quote path.
    class QuoteFailingSink(EvidenceServiceSink):
        def record_quote(self, intake_session_id, quote):
            return self._failure(ValueError("db down"))

    failing = QuoteFailingSink(env.service)
    result = failing.record_quote(
        "intake-1",
        QuoteObservation(
            quote_id="", intake_session_id="intake-1", attempt_id="att-1", registry_id="mock-insurer",
            observed_at=dt.datetime.now(dt.timezone.utc), annual_premium=Decimal("100.00"),
            firm_vs_estimate="firm", content_hash="", idempotency_key="", created_at=dt.datetime.now(dt.timezone.utc),
        ),
    )
    assert result.status is EvidenceWriteStatus.PERSISTENCE_FAILED
    assert result.durable is False


# ---------------------------------------------------------------------------
# Concurrency (§14): multiple attempts/routes, deterministic attempt-local order
# ---------------------------------------------------------------------------


async def test_concurrent_route_writes_have_deterministic_sequences() -> None:
    env, sink = make_sink_env()

    async def write_route(route_id: str, attempt_id: str, n: int) -> list[int]:
        for i in range(n):
            sink.record(
                "intake-1",
                EvidenceDraft(
                    event_type=EvidenceEventType.PAGE_OBSERVED,
                    payload=PageObservationEvidence(page_signature=f"{route_id}-{i}"),
                    plan_id="plan-1", planned_route_id=route_id, registry_id=route_id,
                    distinct_rate_source_id=f"RS-{route_id}", attempt_id=attempt_id,
                ),
            )
        records = await env.service.list_by_attempt("intake-1", attempt_id)
        return [r.sequence for r in records]

    results = await asyncio.gather(
        write_route("route-a", "att-a", 5),
        write_route("route-b", "att-b", 5),
        write_route("route-c", "att-c", 5),
    )
    for seqs in results:
        assert seqs == [1, 2, 3, 4, 5]  # attempt-local monotonic, deterministic


# ---------------------------------------------------------------------------
# Dynamic fields (§30) + safe field-interaction builder (§18)
# ---------------------------------------------------------------------------


def test_field_interaction_builder_never_accepts_values() -> None:
    draft = field_interaction_draft(
        "intake-1",
        source_channel=SourceChannel.VOICE,
        plan_id=None, planned_route_id="mock-insurer", registry_id="mock-insurer",
        distinct_rate_source_id="RS-MOCK-INSURER", attempt_id="att-1",
        source_session_id="vs-1",
        canonical_path="product_data.vehicles[0].use.annual_kilometres",
        transformation="collection_length",
        interaction_type="filled",
        success=True,
    )
    blob = draft.payload.model_dump_json()
    for marker in SENSITIVE_MARKERS:
        assert marker not in blob
    assert "annual_kilometres" in blob  # canonical path, not a value


async def test_dynamic_field_emits_generic_canonical_path(tmp_path: Path) -> None:
    from app.models.voice import BrokerQuestionKind
    from voice_helpers import field_question

    env, sink = make_sink_env()
    venv = make_voice_env(tmp_path, evidence_sink=sink)
    session = prepare_and_disclose(venv)
    # A canonical path that EvidenceService never hardcodes: disclosed JIT by
    # the voice engine, recorded generically with NO EvidenceService change.
    known_path = "product_data.vehicles[0].identity.model_year"
    venv.engine.receive_broker_event(
        session.voice_session_id,
        field_question(known_path, kind=BrokerQuestionKind.CANONICAL_FIELD),
    )
    records = await env.service.list_by_intake(venv.session_id)
    interactions = [
        r for r in records if r.event_type is EvidenceEventType.FIELD_INTERACTION_OBSERVED
    ]
    assert interactions, "expected an automatic field-interaction evidence record"
    assert any(r.payload.canonical_path == known_path for r in interactions)


# ---------------------------------------------------------------------------
# Route isolation on failure (§29): one route's failure doesn't block another
# ---------------------------------------------------------------------------


async def test_evidence_failure_is_route_local(tmp_path: Path) -> None:
    failing = FailingEvidenceSink()
    env, sink = make_sink_env()

    # Route A uses the failing sink; Route B uses the healthy sink.
    venv_a = make_voice_env(
        tmp_path, events=scripted_happy_path_questions(), evidence_sink=failing, registry_id="route-a"
    )
    venv_b = make_voice_env(
        tmp_path, events=scripted_happy_path_questions(), evidence_sink=sink, registry_id="route-b"
    )
    sa = prepare_and_disclose(venv_a)
    sb = prepare_and_disclose(venv_b)
    while True:
        q = venv_a.transport.receive_event(sa.voice_session_id)
        if q is None:
            break
        venv_a.engine.receive_broker_event(sa.voice_session_id, q)
    while True:
        q = venv_b.transport.receive_event(sb.voice_session_id)
        if q is None:
            break
        venv_b.engine.receive_broker_event(sb.voice_session_id, q)

    # Route B persisted independently despite Route A's persistence failures.
    records_b = await env.service.list_by_intake(venv_b.session_id)
    assert records_b, "Route B evidence must persist despite Route A failures"
    assert failing.evidence_status() == EvidenceWriteStatus.PERSISTENCE_FAILED.value
