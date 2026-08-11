"""Issue #10 - EvidenceService tests (in-memory repo): append/idempotency/
ordering/integrity/audit/retention/ownership + privacy-safe metadata."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.models.evidence import (
    AuditEventName,
    EvidenceEventType,
)
from app.models.recovery import RecoveryAction, RecoveryDecision, AttemptLifecycleStatus
from app.models.route_planner import InsuranceType, PlannedRoute, RoutePlan, RoutePlanSummary
from app.services.evidence.ingest import EvidenceDraft
from app.services.evidence.repository import InMemoryEvidenceRepository
from app.services.evidence.service import EvidenceService
from app.models.evidence import PageObservationEvidence, BarrierEvidence

from evidence_helpers import (
    SENSITIVE_MARKERS,
    EvidenceEnv,
    access_control_observation,
    make_evidence_env,
    page_observation,
    quote_observation,
)


# ---------------------------------------------------------------------------
# Core append / ordering / idempotency
# ---------------------------------------------------------------------------


async def test_append_assigns_attempt_local_sequence() -> None:
    env = make_evidence_env()
    d1 = EvidenceDraft(
        event_type=EvidenceEventType.ATTEMPT_STARTED,
        payload=PageObservationEvidence(page_signature="s1"),
        **env.ids(attempt_id="att-1"),
    )
    d2 = EvidenceDraft(
        event_type=EvidenceEventType.PAGE_OBSERVED,
        payload=PageObservationEvidence(page_signature="s2"),
        **env.ids(attempt_id="att-1"),
    )
    r1 = await env.service.append("intake-1", d1)
    r2 = await env.service.append("intake-1", d2)
    assert r1.sequence == 1
    assert r2.sequence == 2
    rows = await env.service.list_by_attempt("intake-1", "att-1")
    assert [r.sequence for r in rows] == [1, 2]


async def test_sequences_are_independent_per_attempt() -> None:
    env = make_evidence_env()
    await env.service.append(
        "intake-1",
        EvidenceDraft(event_type=EvidenceEventType.ATTEMPT_STARTED, payload=PageObservationEvidence(), **env.ids(attempt_id="att-a")),
    )
    await env.service.append(
        "intake-1",
        EvidenceDraft(event_type=EvidenceEventType.ATTEMPT_STARTED, payload=PageObservationEvidence(), **env.ids(attempt_id="att-b")),
    )
    rows_a = await env.service.list_by_attempt("intake-1", "att-a")
    rows_b = await env.service.list_by_attempt("intake-1", "att-b")
    assert rows_a[0].sequence == 1
    assert rows_b[0].sequence == 1


async def test_append_is_idempotent_by_content() -> None:
    env = make_evidence_env()
    d = EvidenceDraft(
        event_type=EvidenceEventType.PAGE_OBSERVED,
        payload=PageObservationEvidence(page_signature="sig-x"),
        **env.ids(attempt_id="att-1"),
    )
    r1 = await env.service.append("intake-1", d)
    r2 = await env.service.append("intake-1", d)
    assert r1.evidence_id == r2.evidence_id
    assert (await env.service.list_by_attempt("intake-1", "att-1")).__len__() == 1


async def test_different_content_is_not_deduplicated() -> None:
    env = make_evidence_env()
    d1 = EvidenceDraft(event_type=EvidenceEventType.PAGE_OBSERVED, payload=PageObservationEvidence(page_signature="a"), **env.ids(attempt_id="att-1"))
    d2 = EvidenceDraft(event_type=EvidenceEventType.PAGE_OBSERVED, payload=PageObservationEvidence(page_signature="b"), **env.ids(attempt_id="att-1"))
    r1 = await env.service.append("intake-1", d1)
    r2 = await env.service.append("intake-1", d2)
    assert r1.evidence_id != r2.evidence_id
    assert r2.sequence == 2


async def test_integrity_verifies_and_detects_mutation() -> None:
    env = make_evidence_env()
    record = await env.service.record_browser_observation(
        "intake-1", page_observation(), **env.ids(), browser_session_id="bs-1"
    )
    assert await env.service.verify_integrity("intake-1", record.evidence_id) is True
    # Mutate the stored payload directly (simulates tampering).
    env.repo._records[record.evidence_id] = record.model_copy(
        update={"safe_url": "evil.example.com/leak?token=SECRET"}
    )
    assert await env.service.verify_integrity("intake-1", record.evidence_id) is False


async def test_integrity_false_for_unknown_record() -> None:
    env = make_evidence_env()
    assert await env.service.verify_integrity("intake-1", "does-not-exist") is False


# ---------------------------------------------------------------------------
# Ownership boundary
# ---------------------------------------------------------------------------


async def test_reads_are_scoped_by_intake_session() -> None:
    env = make_evidence_env()
    await env.service.record_browser_observation(
        "intake-1", page_observation(), **env.ids(), browser_session_id="bs-1"
    )
    own = (await env.service.list_by_intake("intake-1"))[0]
    assert await env.service.get("intake-2", own.evidence_id) is None
    assert await env.service.list_by_intake("intake-2") == []
    assert await env.service.list_by_attempt("intake-2", "att-100") == []
    assert await env.service.list_by_route("intake-2", "mock-insurer") == []
    assert await env.service.list_by_plan("intake-2", "plan-1") == []


async def test_delete_by_intake_session_is_scoped() -> None:
    env = make_evidence_env()
    await env.service.record_browser_observation(
        "intake-1", page_observation(), **env.ids(), browser_session_id="bs-1"
    )
    await env.service.record_browser_observation(
        "intake-2", page_observation(), **env.ids(), browser_session_id="bs-2"
    )
    removed = await env.service.delete_by_intake_session("intake-1")
    assert removed >= 1
    assert await env.service.list_by_intake("intake-1") == []
    assert len(await env.service.list_by_intake("intake-2")) == 1


# ---------------------------------------------------------------------------
# Audit events
# ---------------------------------------------------------------------------


async def test_audit_event_idempotent_and_safe_metadata_allowlisted() -> None:
    env = make_evidence_env()
    e1 = await env.service.record_audit_event(
        "intake-1",
        event_name=AuditEventName.CONSENT_GRANTED,
        actor="applicant",
        safe_metadata={
            "scope": "quote",
            "reason_code": "ok",
            "applicant_postal_code": "M5V 1A1",
            "legal_name": "Test Applicant",
        },
    )
    assert e1.content_hash
    assert "applicant_postal_code" not in e1.safe_metadata
    assert "legal_name" not in e1.safe_metadata
    assert e1.safe_metadata["reason_code"] == "ok"
    e2 = await env.service.record_audit_event(
        "intake-1",
        event_name=AuditEventName.CONSENT_GRANTED,
        actor="applicant",
        safe_metadata={"scope": "quote", "reason_code": "ok"},
    )
    assert e1.audit_id == e2.audit_id  # idempotent
    events = await env.service.list_audit_events("intake-1")
    assert len(events) == 1


async def test_audit_events_serialize_without_sensitive_markers() -> None:
    env = make_evidence_env()
    event = await env.service.record_audit_event(
        "intake-1",
        event_name=AuditEventName.HUMAN_CHECKPOINT,
        actor="human",
        safe_metadata={"checkpoint_type": "identity"},
    )
    data = event.model_dump_json()
    for marker in SENSITIVE_MARKERS:
        assert marker not in data


# ---------------------------------------------------------------------------
# Quote observations
# ---------------------------------------------------------------------------


async def test_quote_observation_decimal_roundtrip_and_idempotency() -> None:
    env = make_evidence_env()
    q = await env.service.record_voice_quote(
        "intake-1",
        voice_session_id="vs-1",
        plan_id="plan-1",
        planned_route_id="mock-insurer",
        registry_id="mock-insurer",
        distinct_rate_source_id="RS-MOCK-INSURER",
        attempt_id="att-100",
        annual_premium=Decimal("1234.56"),
        monthly_premium=Decimal("120.00"),
        currency="CAD",
        firm_vs_estimate="firm",
        reference_present=True,
        private_reference_handle="opaque-ref-hash",
    )
    assert q.annual_premium == Decimal("1234.56")
    assert q.content_hash
    quotes = await env.service.list_quote_observations("intake-1", "att-100")
    assert len(quotes) == 1
    assert quotes[0].annual_premium == Decimal("1234.56")
    assert quotes[0].firm_vs_estimate == "firm"
    assert quotes[0].private_reference_handle == "opaque-ref-hash"


async def test_quote_observation_idempotent() -> None:
    env = make_evidence_env()
    observed = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc)
    kwargs = {
        "voice_session_id": "vs-1",
        "attempt_id": "att-100",
        "annual_premium": Decimal("100.00"),
        "firm_vs_estimate": "estimate",
        "observed_at": observed,
    }
    kwargs.update({k: v for k, v in env.ids().items() if k != "attempt_id"})
    q1 = await env.service.record_voice_quote("intake-1", **kwargs)
    q2 = await env.service.record_voice_quote("intake-1", **kwargs)
    assert q1.quote_id == q2.quote_id
    assert len(await env.service.list_quote_observations("intake-1", "att-100")) == 1


# ---------------------------------------------------------------------------
# High-level adapters
# ---------------------------------------------------------------------------


async def test_record_browser_observation_maps_page_and_barrier() -> None:
    env = make_evidence_env()
    r_page = await env.service.record_browser_observation(
        "intake-1", page_observation(), **env.ids(), browser_session_id="bs-1"
    )
    assert r_page.event_type == EvidenceEventType.PAGE_OBSERVED
    assert r_page.source_channel.value == "browser"
    assert r_page.safe_url == "127.0.0.1:8765/page-a"
    r_block = await env.service.record_browser_observation(
        "intake-1", access_control_observation(), **env.ids(), browser_session_id="bs-1"
    )
    assert r_block.event_type == EvidenceEventType.BLOCKING_ACCESS_CONTROL_OBSERVED
    assert r_block.payload.barrier_kind == "access_control"
    assert r_block.payload.access_control_detected is True


async def test_record_browser_quote_maps_event_and_quote_row() -> None:
    env = make_evidence_env()
    obs = quote_observation()
    record = await env.service.record_browser_observation(
        "intake-1", obs, **env.ids(), browser_session_id="bs-1"
    )
    assert record.event_type == EvidenceEventType.BROWSER_QUOTE_OBSERVED
    assert record.payload.firm_vs_estimate == "firm"
    assert record.payload.annual_premium == Decimal("1234.56")
    assert record.payload.private_reference_handle == "opaque-ref-hash"
    quote = await env.service.record_browser_quote("intake-1", obs, **env.ids())
    assert quote is not None
    assert quote.annual_premium == Decimal("1234.56")


async def test_estimate_observation_stays_estimate() -> None:
    env = make_evidence_env()
    obs = quote_observation(firm=False, annual=900.0)
    record = await env.service.record_browser_observation(
        "intake-1", obs, **env.ids(), browser_session_id="bs-1"
    )
    assert record.event_type == EvidenceEventType.BROWSER_ESTIMATE_OBSERVED
    assert record.payload.firm_vs_estimate == "estimate"
    assert record.payload.quote_pending_normalization is True


async def test_record_voice_observation_maps_to_event() -> None:
    env = make_evidence_env()
    r = await env.service.record_voice_observation(
        "intake-1",
        voice_session_id="vs-1",
        observation_type="phone_quote_observed",
        plan_id="plan-1",
        planned_route_id="mock-insurer",
        registry_id="mock-insurer",
        distinct_rate_source_id="RS-MOCK-INSURER",
        attempt_id="att-100",
        route_status="quote_pending_normalization",
    )
    assert r.event_type == EvidenceEventType.VOICE_QUOTE_OBSERVED
    assert r.payload.voice_observation_type == "phone_quote_observed"
    assert r.payload.route_status == "quote_pending_normalization"


async def test_record_recovery_decision_records_but_never_redecides() -> None:
    env = make_evidence_env()
    decision = RecoveryDecision(
        decision_id="dec-1",
        attempt_id="att-100",
        plan_id="plan-1",
        planned_route_id="mock-insurer",
        registry_id="mock-insurer",
        distinct_rate_source_id="RS-MOCK-INSURER",
        lifecycle_status=AttemptLifecycleStatus.TERMINAL,
        recommended_action=RecoveryAction.STOP_TERMINAL,
        reason_codes=["blocked"],
        retry_allowed=False,
        terminal_status="blocked",
        policy_version="v1",
        plan_version="v1",
        safe_context={"reason_code": "blocked"},
        decided_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
    )
    record = await env.service.record_recovery_decision("intake-1", decision)
    assert record.event_type == EvidenceEventType.RECOVERY_DECISION
    assert record.payload.recommended_action == "stop_terminal"
    assert record.payload.terminal_status == "blocked"
    assert record.payload.quote_pending_normalization is False


async def test_record_route_planned_safe_counts() -> None:
    env = make_evidence_env()
    now = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    plan = RoutePlan(
        session_id="plan-1",
        insurance_type=InsuranceType.AUTO,
        routes=[
            PlannedRoute(
                registry_id="mock-insurer", brand_or_program="Mock",
                distribution_type="direct", product_scope="standard_PPA",
                deduplication_status="primary", route_status="ready", is_ready=True, rank=1,
            ),
            PlannedRoute(
                registry_id="mock-b", brand_or_program="Mock B",
                distribution_type="direct", product_scope="standard_PPA",
                deduplication_status="primary", route_status="blocked", is_ready=False, rank=2,
            ),
        ],
        required_missing_paths=["applicant.address.city"],
        summary=RoutePlanSummary(),
        generated_at=now,
    )
    record = await env.service.record_route_planned("intake-1", plan)
    assert record.event_type == EvidenceEventType.ROUTE_PLANNED
    assert record.payload.planned_route_count == 2
    assert record.payload.ready_count == 1
    assert record.payload.blocked_count == 1
    assert record.plan_id == "plan-1"


async def test_record_consent_state_paths_only() -> None:
    env = make_evidence_env()
    record = await env.service.record_consent(
        "intake-1",
        plan_id="plan-1",
        planned_route_id="mock-insurer",
        registry_id="mock-insurer",
        scope="quote",
        canonical_paths=["applicant.identity.legal_name"],
        state="granted",
    )
    assert record.event_type == EvidenceEventType.CONSENT_EVENT
    assert record.payload.canonical_paths == ["applicant.identity.legal_name"]
    assert record.payload.state == "granted"
    for marker in SENSITIVE_MARKERS:
        assert marker not in record.model_dump_json()


async def test_record_attempt_lifecycle() -> None:
    env = make_evidence_env()
    started = await env.service.record_attempt(
        "intake-1",
        event_type=EvidenceEventType.ATTEMPT_STARTED,
        channel="browser",
        attempt_number=1,
        lifecycle_status="running",
        **env.ids(),
    )
    assert started.event_type == EvidenceEventType.ATTEMPT_STARTED
    assert started.payload.channel == "browser"
    assert started.payload.attempt_number == 1


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


async def test_export_rolls_up_safe_view() -> None:
    env = make_evidence_env()
    await env.service.record_browser_observation(
        "intake-1", page_observation(), **env.ids(), browser_session_id="bs-1"
    )
    await env.service.record_browser_quote(
        "intake-1", quote_observation(), **env.ids()
    )
    await env.service.record_audit_event(
        "intake-1", event_name=AuditEventName.ATTEMPT_STARTED, actor="system"
    )
    view = await env.service.export("intake-1")
    assert view.evidence_count >= 1
    assert view.quote_count == 1
    assert view.audit_event_count == 1
    assert "plan-1" in view.distinct_plans
    assert "mock-insurer" in view.distinct_routes
    assert "att-100" in view.distinct_attempts
    data = view.model_dump_json()
    for marker in SENSITIVE_MARKERS:
        assert marker not in data
