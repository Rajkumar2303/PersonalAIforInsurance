"""Issue #10 - evidence model tests: typed payloads, safe serialization, views."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.models.evidence import (
    AuditEvent,
    AuditEventName,
    BarrierEvidence,
    EvidenceEventType,
    EvidenceRecord,
    EvidenceRecordView,
    QuoteObservation,
    QuoteObservationView,
    SafeMetadataEvidence,
    sanitize_evidence_safe_metadata,
    validate_evidence_payload,
)
from app.models.recovery import SourceChannel
from evidence_helpers import SENSITIVE_MARKERS


def _record(event_type=EvidenceEventType.PAGE_OBSERVED, **overrides) -> EvidenceRecord:
    now = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    return EvidenceRecord(
        evidence_id="ev-1",
        event_type=event_type,
        observed_at=now,
        created_at=now,
        sequence=1,
        intake_session_id="intake-1",
        plan_id="plan-1",
        planned_route_id="mock-insurer",
        registry_id="mock-insurer",
        distinct_rate_source_id="RS-MOCK-INSURER",
        attempt_id="att-1",
        source_channel=SourceChannel.BROWSER,
        source_session_id="bs-1",
        page_signature="mock:auto:quote:page-a",
        safe_url="127.0.0.1:8765/page-a",
        evidence_source="evidence_service",
        payload=validate_evidence_payload(
            {"kind": "page_observation", "page_signature": "mock:auto:quote:page-a", "safe_url": "127.0.0.1:8765/page-a"}
        ),
        content_hash="a" * 64,
        idempotency_key="page_observed|intake-1|att-1|mock-insurer|digest",
        **overrides,
    )


def test_evidence_record_never_contains_sensitive_markers() -> None:
    data = _record().model_dump_json()
    for marker in SENSITIVE_MARKERS:
        assert marker not in data


def test_evidence_payload_discriminated_union_roundtrip() -> None:
    payload = validate_evidence_payload(
        {
            "kind": "barrier",
            "barrier_kind": "access_control",
            "bot_protection_present": True,
            "access_control_detected": True,
            "visible_challenge": True,
            "reason_code": "blocked",
        }
    )
    assert isinstance(payload, BarrierEvidence)
    assert payload.access_control_detected is True
    data = payload.model_dump(mode="json")
    again = validate_evidence_payload(data)
    assert isinstance(again, BarrierEvidence)
    assert again.barrier_kind == "access_control"


def test_evidence_payload_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        validate_evidence_payload({"kind": "not_a_real_kind"})


def test_evidence_payload_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        validate_evidence_payload(
            {"kind": "barrier", "barrier_kind": "access_control", "applicant_postal_code": "M5V 1A1"}
        )


def test_quote_observation_preserves_decimal_money() -> None:
    now = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    q = QuoteObservation(
        quote_id="q-1",
        intake_session_id="intake-1",
        attempt_id="att-1",
        registry_id="mock-insurer",
        observed_at=now,
        annual_premium=Decimal("1234.56"),
        monthly_premium=Decimal("120.00"),
        currency="CAD",
        firm_vs_estimate="firm",
        reference_present=True,
        private_reference_handle="opaque-ref-hash",
        content_hash="b" * 64,
        idempotency_key="quote|intake-1|att-1|mock-insurer|digest",
        created_at=now,
    )
    assert q.annual_premium == Decimal("1234.56")
    view = QuoteObservationView(
        quote_id=q.quote_id,
        attempt_id=q.attempt_id,
        registry_id=q.registry_id,
        observed_at=q.observed_at,
        annual_premium=str(q.annual_premium),
        monthly_premium=str(q.monthly_premium),
        currency=q.currency,
        firm_vs_estimate=q.firm_vs_estimate,
        reference_present=q.reference_present,
        coverage_raw_present=q.coverage_raw_present,
        quote_pending_normalization=q.quote_pending_normalization,
        sequence=1,
        content_hash=q.content_hash,
    )
    assert isinstance(view.annual_premium, str)
    for marker in SENSITIVE_MARKERS:
        assert marker not in view.model_dump_json()


def test_evidence_record_view_is_safe_projection() -> None:
    record = _record()
    view = EvidenceRecordView(
        evidence_id=record.evidence_id,
        event_type=record.event_type.value,
        observed_at=record.observed_at,
        sequence=record.sequence,
        intake_session_id=record.intake_session_id,
        plan_id=record.plan_id,
        planned_route_id=record.planned_route_id,
        registry_id=record.registry_id,
        distinct_rate_source_id=record.distinct_rate_source_id,
        attempt_id=record.attempt_id,
        parent_attempt_id=None,
        source_channel=record.source_channel.value,
        source_session_id=record.source_session_id,
        page_signature=record.page_signature,
        safe_url=record.safe_url,
        observation_type=None,
        reason_code=None,
        evidence_source=record.evidence_source,
        payload_version=1,
        payload_kind=record.payload.kind,
        payload=record.payload.model_dump(mode="json"),
        content_hash=record.content_hash,
    )
    for marker in SENSITIVE_MARKERS:
        assert marker not in view.model_dump_json()


def test_audit_event_serialization() -> None:
    now = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    event = AuditEvent(
        audit_id="a-1",
        intake_session_id="intake-1",
        event_name=AuditEventName.CONSENT_GRANTED,
        occurred_at=now,
        actor="applicant",
        safe_metadata={"scope": "quote"},
        content_hash="c" * 64,
        idempotency_key="audit|intake-1|consent_granted|digest",
    )
    data = event.model_dump_json()
    for marker in SENSITIVE_MARKERS:
        assert marker not in data
    assert '"consent_granted"' in data


def test_sanitize_evidence_safe_metadata_allowlists_keys() -> None:
    ctx = {
        "page_signature": "sig",
        "reason_code": "blocked",
        "checkpoint_type": "consent",
        "config_version": 1,
        "applicant_postal_code": "M5V 1A1",
        "legal_name": "Test Applicant",
        "claim_details": "rear-ended",
    }
    safe = sanitize_evidence_safe_metadata(ctx)
    assert "page_signature" in safe and "reason_code" in safe and "config_version" in safe
    assert "applicant_postal_code" not in safe
    assert "legal_name" not in safe
    assert "claim_details" not in safe


def test_safe_metadata_payload_rejects_sensitive_free_keys() -> None:
    with pytest.raises(ValidationError):
        SafeMetadataEvidence(safe_metadata={"applicant_licence": "T0000-00000-00000"})


def test_evidence_event_type_never_assigns_comparable_statuses() -> None:
    values = {e.value for e in EvidenceEventType}
    assert "quoted_comparable" not in values
    assert "quoted_non_comparable" not in values
