"""Issue #10 - lineage & evidence-flow tests (SQLite-backed, hermetic).

Covers the cross-issue evidence scenarios: callback->voice continuation,
browser quotes, estimate stays estimate, aggregator multi-result quotes,
CAPTCHA->blocked, consent grant->revoke, integrity mutation, idempotency,
stable timelines, and private reference handling.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from app.models.evidence import (
    AuditEventName,
    EvidenceEventType,
    QuoteObservation,
)
from app.models.recovery import (
    RecoveryAction,
    RecoveryDecision,
    AttemptLifecycleStatus,
)
from app.services.evidence.ingest import EvidenceDraft
from app.models.evidence import PageObservationEvidence

from evidence_helpers import (
    SENSITIVE_MARKERS,
    QUOTE_REFERENCE,
    access_control_observation,
    make_sqlite_evidence_env,
    page_observation,
    quote_observation,
)


@pytest.fixture
async def env(tmp_path: Path):
    return await make_sqlite_evidence_env(tmp_path)


async def _recovery_decision(attempt_id: str, terminal_status: str) -> RecoveryDecision:
    return RecoveryDecision(
        decision_id=f"dec-{attempt_id}",
        attempt_id=attempt_id,
        plan_id="plan-1",
        planned_route_id="mock-insurer",
        registry_id="mock-insurer",
        distinct_rate_source_id="RS-MOCK-INSURER",
        lifecycle_status=AttemptLifecycleStatus.TERMINAL,
        recommended_action=RecoveryAction.STOP_TERMINAL,
        reason_codes=["captcha_or_bot_control"] if terminal_status == "blocked" else [],
        retry_allowed=False,
        terminal_status=terminal_status,
        policy_version="v1",
        plan_version="v1",
        safe_context={"reason_code": terminal_status},
        decided_at=dt.datetime(2026, 1, 1, 13, 0, tzinfo=dt.timezone.utc),
    )


# ---------------------------------------------------------------------------
# §47 - callback -> voice continuation keeps both attempts visible + linked
# ---------------------------------------------------------------------------


async def test_callback_to_voice_continuation_lineage(env) -> None:
    browser_attempt = "att-browser"
    voice_attempt = "att-voice"

    # Browser leg: callback observed, no quote.
    cb = await env.service.record_browser_observation(
        "intake-1",
        BrowserObservation_callback(),
        plan_id="plan-1",
        planned_route_id="mock-insurer",
        registry_id="mock-insurer",
        distinct_rate_source_id="RS-MOCK-INSURER",
        attempt_id=browser_attempt,
        browser_session_id="bs-1",
    )
    assert cb.event_type is EvidenceEventType.CALLBACK_OBSERVED

    # Voice continuation leg: same route, OWN attempt, parent = browser attempt.
    vq = await env.service.record_voice_observation(
        "intake-1",
        voice_session_id="vs-1",
        observation_type="phone_quote_observed",
        plan_id="plan-1",
        planned_route_id="mock-insurer",
        registry_id="mock-insurer",
        distinct_rate_source_id="RS-MOCK-INSURER",
        attempt_id=voice_attempt,
        parent_attempt_id=browser_attempt,
        route_status="quote_pending_normalization",
    )
    assert vq.event_type is EvidenceEventType.VOICE_QUOTE_OBSERVED
    assert vq.parent_attempt_id == browser_attempt

    # Quote row on the VOICE attempt, linked back to the browser attempt.
    quote = await env.service.record_voice_quote(
        "intake-1",
        voice_session_id="vs-1",
        plan_id="plan-1",
        planned_route_id="mock-insurer",
        registry_id="mock-insurer",
        distinct_rate_source_id="RS-MOCK-INSURER",
        attempt_id=voice_attempt,
        parent_attempt_id=browser_attempt,
        annual_premium=Decimal("1800.00"),
        firm_vs_estimate="firm",
        observed_at=dt.datetime(2026, 1, 1, 14, 0, tzinfo=dt.timezone.utc),
    )
    assert quote.parent_attempt_id == browser_attempt

    # Browser attempt is immutable: NO quote event ever attached to it.
    browser_rows = await env.service.list_by_attempt("intake-1", browser_attempt)
    assert all(r.event_type is not EvidenceEventType.VOICE_QUOTE_OBSERVED for r in browser_rows)
    assert all(r.event_type is not EvidenceEventType.BROWSER_QUOTE_OBSERVED for r in browser_rows)
    assert any(r.event_type is EvidenceEventType.CALLBACK_OBSERVED for r in browser_rows)
    # Voice attempt carries its own observations.
    voice_rows = await env.service.list_by_attempt("intake-1", voice_attempt)
    assert any(r.event_type is EvidenceEventType.VOICE_QUOTE_OBSERVED for r in voice_rows)


def BrowserObservation_callback():
    from app.models.browser.observation import BrowserObservation, BrowserObservationType

    return BrowserObservation(
        observation_type=BrowserObservationType.CALLBACK_DETECTED,
        page_signature="mock:auto:quote:callback",
        url="http://127.0.0.1:8765/callback",
        message="callback offered",
    )


# ---------------------------------------------------------------------------
# §48 - browser quote -> typed quote row (Decimal, sanitized URL, private ref)
# ---------------------------------------------------------------------------


async def test_browser_quote_lineage(env) -> None:
    obs = quote_observation(annual=1234.56, monthly=102.88, firm=True)
    record = await env.service.record_browser_observation(
        "intake-1", obs, plan_id="plan-1", planned_route_id="mock-insurer",
        registry_id="mock-insurer", distinct_rate_source_id="RS-MOCK-INSURER",
        attempt_id="att-1", browser_session_id="bs-1",
    )
    quote = await env.service.record_browser_quote(
        "intake-1", obs, plan_id="plan-1", planned_route_id="mock-insurer",
        registry_id="mock-insurer", distinct_rate_source_id="RS-MOCK-INSURER",
        attempt_id="att-1",
    )
    assert record.event_type is EvidenceEventType.BROWSER_QUOTE_OBSERVED
    assert quote.annual_premium == Decimal("1234.56")
    assert quote.monthly_premium == Decimal("102.88")
    assert quote.firm_vs_estimate == "firm"
    assert quote.private_reference_handle == "opaque-ref-hash"
    assert quote.reference_present is True
    # Sanitized URL on the record never carries the query/token.
    assert record.safe_url == "127.0.0.1:8765/quote-page"
    # Raw reference value never appears anywhere in evidence.
    for marker in [QUOTE_REFERENCE]:
        assert marker not in record.model_dump_json()
        assert marker not in quote.model_dump_json()


# ---------------------------------------------------------------------------
# §49 - estimate stays estimate (never upgraded)
# ---------------------------------------------------------------------------


async def test_estimate_stays_estimate(env) -> None:
    obs = quote_observation(annual=900.0, monthly=75.0, firm=False)
    record = await env.service.record_browser_observation(
        "intake-1", obs, plan_id="plan-1", planned_route_id="mock-insurer",
        registry_id="mock-insurer", distinct_rate_source_id="RS-MOCK-INSURER",
        attempt_id="att-1", browser_session_id="bs-1",
    )
    quote = await env.service.record_browser_quote(
        "intake-1", obs, plan_id="plan-1", planned_route_id="mock-insurer",
        registry_id="mock-insurer", distinct_rate_source_id="RS-MOCK-INSURER",
        attempt_id="att-1",
    )
    assert record.event_type is EvidenceEventType.BROWSER_ESTIMATE_OBSERVED
    assert quote.firm_vs_estimate == "estimate"
    assert quote.quote_pending_normalization is True
    # The estimate was never upgraded to a firm quote.
    quotes = await env.service.list_quote_observations("intake-1", "att-1")
    assert all(q.firm_vs_estimate == "estimate" for q in quotes)


# ---------------------------------------------------------------------------
# §50 - aggregator: multiple results under ONE attempt
# ---------------------------------------------------------------------------


async def test_aggregator_multiple_quotes_single_attempt(env) -> None:
    now = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc)
    carriers = [("AggCarrier A", Decimal("1100.00")), ("AggCarrier B", Decimal("1250.00")), ("AggCarrier C", Decimal("980.00"))]
    for i, (carrier, annual) in enumerate(carriers):
        q = QuoteObservation(
            quote_id="",
            intake_session_id="intake-1",
            attempt_id="att-agg",
            plan_id="plan-1",
            planned_route_id="mock-aggregator",
            registry_id="mock-aggregator",
            distinct_rate_source_id="RS-MOCK-AGGREGATOR",
            aggregator_registry_id="mock-aggregator",
            presented_carrier=carrier,
            observed_at=now,
            annual_premium=annual,
            monthly_premium=None,
            currency="CAD",
            firm_vs_estimate="firm",
            reference_present=True,
            private_reference_handle=f"agg-ref-{i}",
            coverage_raw_present=False,
            quote_pending_normalization=True,
            content_hash="",
            idempotency_key="",
            created_at=now,
        )
        saved = await env.service.record_quote_observation("intake-1", q)
        assert saved.quote_id

    quotes = await env.service.list_quote_observations("intake-1", "att-agg")
    assert len(quotes) == 3
    assert {q.presented_carrier for q in quotes} == {"AggCarrier A", "AggCarrier B", "AggCarrier C"}
    assert all(q.aggregator_registry_id == "mock-aggregator" for q in quotes)
    assert all(q.attempt_id == "att-agg" for q in quotes)


# ---------------------------------------------------------------------------
# §51 - CAPTCHA / access control -> blocked (no quote ever claimed)
# ---------------------------------------------------------------------------


async def test_captcha_to_blocked_lineage(env) -> None:
    barrier = await env.service.record_browser_observation(
        "intake-1", access_control_observation(), plan_id="plan-1",
        planned_route_id="mock-insurer", registry_id="mock-insurer",
        distinct_rate_source_id="RS-MOCK-INSURER", attempt_id="att-1", browser_session_id="bs-1",
    )
    decision = await env.service.record_recovery_decision(
        "intake-1", await _recovery_decision("att-1", "blocked")
    )
    assert barrier.event_type is EvidenceEventType.BLOCKING_ACCESS_CONTROL_OBSERVED
    assert barrier.payload.barrier_kind == "access_control"
    assert barrier.payload.access_control_detected is True
    assert barrier.payload.bot_protection_present is True
    assert decision.event_type is EvidenceEventType.RECOVERY_DECISION
    assert decision.payload.terminal_status == "blocked"
    # No quote observations ever recorded for this blocked attempt.
    assert await env.service.list_quote_observations("intake-1", "att-1") == []
    # Terminal status was never set to a comparable status.
    assert decision.payload.terminal_status not in {"quoted_comparable", "quoted_non_comparable"}


# ---------------------------------------------------------------------------
# §53 - consent grant -> revoke preserved append-only
# ---------------------------------------------------------------------------


async def test_consent_grant_then_revoke(env) -> None:
    paths = ["applicant.identity.legal_name", "applicant.address.postal_code"]
    granted = await env.service.record_consent(
        "intake-1", plan_id="plan-1", planned_route_id="mock-insurer",
        registry_id="mock-insurer", scope="quote", canonical_paths=paths, state="granted",
    )
    revoked = await env.service.record_consent(
        "intake-1", plan_id="plan-1", planned_route_id="mock-insurer",
        registry_id="mock-insurer", scope="quote", canonical_paths=paths, state="revoked",
    )
    assert granted.event_type is EvidenceEventType.CONSENT_EVENT
    assert granted.payload.state == "granted"
    assert revoked.payload.state == "revoked"
    assert granted.evidence_id != revoked.evidence_id  # both preserved

    rows = await env.service.list_by_intake("intake-1")
    consent_rows = [r for r in rows if r.event_type is EvidenceEventType.CONSENT_EVENT]
    assert len(consent_rows) == 2
    # Redelivered grant collapses (idempotent), revoke stays separate.
    again = await env.service.record_consent(
        "intake-1", plan_id="plan-1", planned_route_id="mock-insurer",
        registry_id="mock-insurer", scope="quote", canonical_paths=paths, state="granted",
    )
    assert again.evidence_id == granted.evidence_id
    rows = await env.service.list_by_intake("intake-1")
    consent_rows = [r for r in rows if r.event_type is EvidenceEventType.CONSENT_EVENT]
    assert len(consent_rows) == 2


# ---------------------------------------------------------------------------
# §54 - integrity mutation detected at the DB level
# ---------------------------------------------------------------------------


async def test_integrity_mutation_detected(env) -> None:
    r = await env.service.record_browser_observation(
        "intake-1", page_observation(), plan_id="plan-1", planned_route_id="mock-insurer",
        registry_id="mock-insurer", distinct_rate_source_id="RS-MOCK-INSURER",
        attempt_id="att-1", browser_session_id="bs-1",
    )
    assert await env.service.verify_integrity("intake-1", r.evidence_id)
    from sqlalchemy import text

    async with env.repo.begin() as conn:
        await conn.execute(
            text("UPDATE evidence_records SET registry_id = :r WHERE evidence_id = :id"),
            {"r": "tampered", "id": r.evidence_id},
        )
    assert await env.service.verify_integrity("intake-1", r.evidence_id) is False


# ---------------------------------------------------------------------------
# §55 - idempotent redelivery collapses to one logical record
# ---------------------------------------------------------------------------


async def test_redelivery_is_idempotent(env) -> None:
    obs = page_observation()
    r1 = await env.service.record_browser_observation(
        "intake-1", obs, plan_id="plan-1", planned_route_id="mock-insurer",
        registry_id="mock-insurer", distinct_rate_source_id="RS-MOCK-INSURER",
        attempt_id="att-1", browser_session_id="bs-1",
    )
    r2 = await env.service.record_browser_observation(
        "intake-1", obs, plan_id="plan-1", planned_route_id="mock-insurer",
        registry_id="mock-insurer", distinct_rate_source_id="RS-MOCK-INSURER",
        attempt_id="att-1", browser_session_id="bs-1",
    )
    assert r1.evidence_id == r2.evidence_id
    assert len(await env.service.list_by_attempt("intake-1", "att-1")) == 1
    assert await env.service.verify_integrity("intake-1", r1.evidence_id)


# ---------------------------------------------------------------------------
# §56 - stable, monotonic per-attempt timeline
# ---------------------------------------------------------------------------


async def test_stable_monotonic_timeline(env) -> None:
    events = [
        EvidenceEventType.ATTEMPT_STARTED,
        EvidenceEventType.PAGE_OBSERVED,
        EvidenceEventType.FIELD_REQUIREMENT_OBSERVED,
        EvidenceEventType.FIELD_INTERACTION_OBSERVED,
        EvidenceEventType.BROWSER_QUOTE_OBSERVED,
        EvidenceEventType.ATTEMPT_COMPLETED,
    ]
    for ev in events:
        await env.service.append(
            "intake-1",
            EvidenceDraft(
                event_type=ev,
                payload=PageObservationEvidence(page_signature=f"s-{ev.value}"),
                **{
                    "plan_id": "plan-1", "planned_route_id": "mock-insurer",
                    "registry_id": "mock-insurer", "distinct_rate_source_id": "RS-MOCK-INSURER",
                    "attempt_id": "att-1",
                },
            ),
        )
    rows = await env.service.list_by_attempt("intake-1", "att-1")
    assert [r.sequence for r in rows] == list(range(1, len(events) + 1))
    assert [r.event_type for r in rows] == events
    # Timeline is stable across repeated reads.
    again = await env.service.list_by_attempt("intake-1", "att-1")
    assert [r.sequence for r in again] == [r.sequence for r in rows]


# ---------------------------------------------------------------------------
# §46 - raw reference never leaves the private boundary
# ---------------------------------------------------------------------------


async def test_raw_reference_never_persisted(env) -> None:
    obs = quote_observation(reference_present=True, private_handle="opaque-ref-hash")
    await env.service.record_browser_observation(
        "intake-1", obs, plan_id="plan-1", planned_route_id="mock-insurer",
        registry_id="mock-insurer", distinct_rate_source_id="RS-MOCK-INSURER",
        attempt_id="att-1", browser_session_id="bs-1",
    )
    await env.service.record_browser_quote(
        "intake-1", obs, plan_id="plan-1", planned_route_id="mock-insurer",
        registry_id="mock-insurer", distinct_rate_source_id="RS-MOCK-INSURER",
        attempt_id="att-1",
    )
    await env.service.record_audit_event(
        "intake-1", event_name=AuditEventName.EXPORT_CREATED, actor="system"
    )
    export = await env.service.export("intake-1")
    blob = export.model_dump_json()
    for marker in SENSITIVE_MARKERS + [QUOTE_REFERENCE]:
        assert marker not in blob
    # The opaque handle IS present (it is not the raw reference).
    assert "opaque-ref-hash" in blob
