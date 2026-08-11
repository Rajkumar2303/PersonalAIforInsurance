"""EvidenceService: validate -> sanitize -> hash -> idempotency -> persist
(Issue #10, Prompt 1).

The service owns the durable-evidence invariants:
- Every record is built from a typed ``EvidenceDraft`` (never raw dicts).
- ``content_hash`` covers the protected semantic fields (repository-assigned
  ordering keys like sequence/created_at are excluded so hashing is stable).
- ``idempotency_key`` is deterministic from event type + ownership + payload
  digest, so redelivered appends collapse to one logical record.
- All reads are scoped by ``intake_session_id``.
- ``quoted_comparable`` / ``quoted_non_comparable`` are never assigned here.

Wiring is EXPLICIT for Prompt 1: engines/executors do not auto-emit; the demo
API and tests call these adapters directly. (Auto-emission is a later issue.)
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Optional

from ...models.browser.observation import BrowserObservation
from ...models.evidence import (
    AuditEvent,
    AuditEventName,
    AuditEventView,
    EvidenceEventType,
    EvidenceExportView,
    EvidenceRecord,
    EvidenceRecordView,
    QuoteObservation,
    QuoteObservationView,
    sanitize_evidence_safe_metadata,
)
from ...models.recovery import RecoveryDecision
from ...models.route_planner import RoutePlan
from .hashing import (
    QUOTE_HASH_FIELDS,
    audit_content_hash,
    canonical_json,
    evidence_content_hash,
    quote_content_hash,
    sha256_hex,
)
from .ingest import (
    EvidenceDraft,
    attempt_draft,
    browser_draft_from_observation,
    consent_draft,
    quote_from_browser_observation,
    recovery_draft_from_decision,
    route_plan_draft,
    voice_draft,
    voice_quote,
)
from .repository import EvidenceRepository
from .url_sanitizer import safe_url_only


class EvidenceService:
    """Domain service for the durable evidence / audit / trace store."""

    def __init__(self, repository: EvidenceRepository) -> None:
        self._repo = repository

    # ------------------------------------------------------------------
    # Core append
    # ------------------------------------------------------------------

    async def append(
        self, intake_session_id: str, draft: EvidenceDraft
    ) -> EvidenceRecord:
        observed_at = draft.observed_at or dt.datetime.now(dt.timezone.utc)
        created_at = dt.datetime.now(dt.timezone.utc)
        payload = draft.payload
        payload_dict = payload.model_dump(mode="json")

        # Deterministic idempotency key (event + ownership + payload digest).
        digest = sha256_hex(canonical_json(payload_dict))[:16]
        scope = draft.idempotency_scope or ""
        idem_parts = [
            draft.event_type.value,
            intake_session_id or "-",
            draft.attempt_id or "-",
            draft.registry_id or "-",
            digest,
        ]
        if scope:
            idem_parts.append(scope)
        idempotency_key = "|".join(idem_parts)

        # URLs are always sanitized at the boundary - never persist query/
        # fragment/userinfo regardless of what a caller passed in.
        safe_url = safe_url_only(draft.safe_url)

        record = EvidenceRecord(
            evidence_id=uuid.uuid4().hex,
            event_type=draft.event_type,
            observed_at=observed_at,
            created_at=created_at,
            sequence=1,  # repository assigns the authoritative sequence
            intake_session_id=intake_session_id,
            plan_id=draft.plan_id,
            planned_route_id=draft.planned_route_id,
            registry_id=draft.registry_id,
            distinct_rate_source_id=draft.distinct_rate_source_id,
            attempt_id=draft.attempt_id,
            parent_attempt_id=draft.parent_attempt_id,
            source_channel=draft.source_channel,
            source_session_id=draft.source_session_id,
            page_signature=draft.page_signature,
            safe_url=safe_url,
            observation_type=draft.observation_type,
            reason_code=draft.reason_code,
            evidence_source=draft.evidence_source,
            payload_version=getattr(payload, "payload_version", 1) or 1,
            payload=payload,
            content_hash="",  # computed below against the final field set
            idempotency_key=idempotency_key,
            quote_observation_id=draft.quote_observation_id,
            registry_snapshot_ref=draft.registry_snapshot_ref,
            config_version=draft.config_version,
            attachments=list(draft.attachments),
        )
        record = record.model_copy(
            update={"content_hash": evidence_content_hash(record.model_dump())}
        )
        return await self._repo.append(record)

    async def append_many(
        self, intake_session_id: str, drafts: list[EvidenceDraft]
    ) -> list[EvidenceRecord]:
        return [await self.append(intake_session_id, d) for d in drafts]

    # ------------------------------------------------------------------
    # High-level adapters (explicit wiring for Prompt 1)
    # ------------------------------------------------------------------

    async def record_browser_observation(
        self,
        intake_session_id: str,
        observation: BrowserObservation,
        *,
        browser_session_id: str,
        plan_id: str,
        planned_route_id: str,
        registry_id: str,
        distinct_rate_source_id: str,
        attempt_id: str,
        parent_attempt_id: Optional[str] = None,
    ) -> EvidenceRecord:
        draft = browser_draft_from_observation(
            intake_session_id,
            observation,
            browser_session_id=browser_session_id,
            plan_id=plan_id,
            planned_route_id=planned_route_id,
            registry_id=registry_id,
            distinct_rate_source_id=distinct_rate_source_id,
            attempt_id=attempt_id,
            parent_attempt_id=parent_attempt_id,
        )
        return await self.append(intake_session_id, draft)

    async def record_voice_observation(
        self,
        intake_session_id: str,
        *,
        voice_session_id: str,
        observation_type: str,
        plan_id: str,
        planned_route_id: str,
        registry_id: str,
        distinct_rate_source_id: str,
        attempt_id: str,
        parent_attempt_id: Optional[str] = None,
        canonical_path: Optional[str] = None,
        checkpoint_kind: Optional[str] = None,
        route_status: Optional[str] = None,
        lifecycle_status: Optional[str] = None,
        recording_consent: str = "not_requested",
        transcription_consent: str = "not_requested",
    ) -> EvidenceRecord:
        draft = voice_draft(
            intake_session_id,
            voice_session_id=voice_session_id,
            observation_type=observation_type,
            plan_id=plan_id,
            planned_route_id=planned_route_id,
            registry_id=registry_id,
            distinct_rate_source_id=distinct_rate_source_id,
            attempt_id=attempt_id,
            parent_attempt_id=parent_attempt_id,
            canonical_path=canonical_path,
            checkpoint_kind=checkpoint_kind,
            route_status=route_status,
            lifecycle_status=lifecycle_status,
            recording_consent=recording_consent,
            transcription_consent=transcription_consent,
        )
        return await self.append(intake_session_id, draft)

    async def record_recovery_decision(
        self, intake_session_id: str, decision: RecoveryDecision
    ) -> EvidenceRecord:
        return await self.append(intake_session_id, recovery_draft_from_decision(intake_session_id, decision))

    async def record_route_planned(
        self, intake_session_id: str, route_plan: RoutePlan
    ) -> EvidenceRecord:
        return await self.append(intake_session_id, route_plan_draft(intake_session_id, route_plan))

    async def record_consent(
        self,
        intake_session_id: str,
        *,
        plan_id: str,
        planned_route_id: str,
        registry_id: str,
        consent_receipt_id: Optional[str] = None,
        scope: str = "quote",
        canonical_paths: Optional[list[str]] = None,
        state: str = "granted",
    ) -> EvidenceRecord:
        draft = consent_draft(
            intake_session_id,
            plan_id=plan_id,
            planned_route_id=planned_route_id,
            registry_id=registry_id,
            consent_receipt_id=consent_receipt_id,
            scope=scope,
            canonical_paths=canonical_paths,
            state=state,
        )
        return await self.append(intake_session_id, draft)

    async def record_attempt(
        self,
        intake_session_id: str,
        *,
        event_type: EvidenceEventType,
        plan_id: str,
        planned_route_id: str,
        registry_id: str,
        distinct_rate_source_id: str,
        attempt_id: str,
        channel: str,
        attempt_number: int = 1,
        lifecycle_status: Optional[str] = None,
        policy_version: Optional[str] = None,
        plan_version: Optional[str] = None,
    ) -> EvidenceRecord:
        draft = attempt_draft(
            intake_session_id,
            event_type=event_type,
            plan_id=plan_id,
            planned_route_id=planned_route_id,
            registry_id=registry_id,
            distinct_rate_source_id=distinct_rate_source_id,
            attempt_id=attempt_id,
            channel=channel,
            attempt_number=attempt_number,
            lifecycle_status=lifecycle_status,
            policy_version=policy_version,
            plan_version=plan_version,
        )
        return await self.append(intake_session_id, draft)

    # ------------------------------------------------------------------
    # Quote observations (one-to-many from an attempt)
    # ------------------------------------------------------------------

    async def record_quote_observation(
        self, intake_session_id: str, quote: QuoteObservation
    ) -> QuoteObservation:
        """Persist a typed quote/estimate result (idempotent by key).

        The idempotency key is derived ONLY from the semantic protected fields
        (``QUOTE_HASH_FIELDS``), never from the operational quote_id/created_at,
        so a redelivered logical quote collapses to one row.
        """
        now = dt.datetime.now(dt.timezone.utc)
        digest_fields = {
            k: v for k, v in quote.model_dump().items() if k in QUOTE_HASH_FIELDS
        }
        digest = sha256_hex(canonical_json(digest_fields))[:16]
        idempotency_key = "|".join(
            [
                "quote",
                intake_session_id,
                quote.attempt_id or "-",
                quote.registry_id or "-",
                digest,
            ]
        )
        quote = quote.model_copy(
            update={
                "quote_id": quote.quote_id or uuid.uuid4().hex,
                "observed_at": quote.observed_at or now,
                "created_at": quote.created_at or now,
                "idempotency_key": idempotency_key,
                "content_hash": quote_content_hash(quote.model_dump()),
            }
        )
        return await self._repo.save_quote_observation(intake_session_id, quote)

    async def record_browser_quote(
        self,
        intake_session_id: str,
        observation: BrowserObservation,
        *,
        plan_id: str,
        planned_route_id: str,
        registry_id: str,
        distinct_rate_source_id: str,
        attempt_id: str,
        parent_attempt_id: Optional[str] = None,
    ) -> Optional[QuoteObservation]:
        quote = quote_from_browser_observation(
            intake_session_id,
            observation,
            plan_id=plan_id,
            planned_route_id=planned_route_id,
            registry_id=registry_id,
            distinct_rate_source_id=distinct_rate_source_id,
            attempt_id=attempt_id,
            parent_attempt_id=parent_attempt_id,
        )
        if quote is None:
            return None
        return await self.record_quote_observation(intake_session_id, quote)

    async def record_voice_quote(
        self,
        intake_session_id: str,
        *,
        voice_session_id: str,
        plan_id: str,
        planned_route_id: str,
        registry_id: str,
        distinct_rate_source_id: str,
        attempt_id: str,
        parent_attempt_id: Optional[str] = None,
        annual_premium: Optional[Any] = None,
        monthly_premium: Optional[Any] = None,
        currency: Optional[str] = None,
        firm_vs_estimate: str = "firm",
        reference_present: bool = False,
        private_reference_handle: Optional[str] = None,
        coverage_raw_present: bool = False,
        observed_at: Optional[dt.datetime] = None,
    ) -> QuoteObservation:
        from decimal import Decimal

        quote = voice_quote(
            intake_session_id,
            voice_session_id=voice_session_id,
            plan_id=plan_id,
            planned_route_id=planned_route_id,
            registry_id=registry_id,
            distinct_rate_source_id=distinct_rate_source_id,
            attempt_id=attempt_id,
            parent_attempt_id=parent_attempt_id,
            annual_premium=Decimal(str(annual_premium)) if annual_premium is not None else None,
            monthly_premium=Decimal(str(monthly_premium)) if monthly_premium is not None else None,
            currency=currency,
            firm_vs_estimate=firm_vs_estimate,
            reference_present=reference_present,
            private_reference_handle=private_reference_handle,
            coverage_raw_present=coverage_raw_present,
            observed_at=observed_at,
        )
        return await self.record_quote_observation(intake_session_id, quote)

    # ------------------------------------------------------------------
    # Audit events
    # ------------------------------------------------------------------

    async def record_audit_event(
        self,
        intake_session_id: str,
        *,
        event_name: AuditEventName,
        actor: str = "system",
        safe_metadata: Optional[dict[str, Any]] = None,
        occurred_at: Optional[dt.datetime] = None,
    ) -> AuditEvent:
        occurred = occurred_at or dt.datetime.now(dt.timezone.utc)
        meta = sanitize_evidence_safe_metadata(safe_metadata)
        event = AuditEvent(
            audit_id=uuid.uuid4().hex,
            intake_session_id=intake_session_id,
            event_name=event_name,
            occurred_at=occurred,
            actor=actor,
            safe_metadata=meta,
            content_hash="",
            idempotency_key="",
        )
        digest = sha256_hex(canonical_json({"name": event_name.value, "meta": meta}))[:16]
        event = event.model_copy(
            update={
                "content_hash": audit_content_hash(event.model_dump()),
                "idempotency_key": "|".join(["audit", intake_session_id, event_name.value, digest]),
            }
        )
        return await self._repo.append_audit_event(event)

    # ------------------------------------------------------------------
    # Reads (all scoped by intake_session_id)
    # ------------------------------------------------------------------

    async def get(
        self, intake_session_id: str, evidence_id: str
    ) -> Optional[EvidenceRecord]:
        return await self._repo.get(intake_session_id, evidence_id)

    async def list_by_attempt(
        self, intake_session_id: str, attempt_id: str
    ) -> list[EvidenceRecord]:
        return await self._repo.list_by_attempt(intake_session_id, attempt_id)

    async def list_by_route(
        self, intake_session_id: str, planned_route_id: str
    ) -> list[EvidenceRecord]:
        return await self._repo.list_by_route(intake_session_id, planned_route_id)

    async def list_by_plan(
        self, intake_session_id: str, plan_id: str
    ) -> list[EvidenceRecord]:
        return await self._repo.list_by_plan(intake_session_id, plan_id)

    async def list_by_intake(self, intake_session_id: str) -> list[EvidenceRecord]:
        return await self._repo.list_by_intake(intake_session_id)

    async def list_quote_observations(
        self, intake_session_id: str, attempt_id: Optional[str] = None
    ) -> list[QuoteObservation]:
        return await self._repo.list_quote_observations(intake_session_id, attempt_id)

    async def list_audit_events(self, intake_session_id: str) -> list[AuditEvent]:
        return await self._repo.list_audit_events(intake_session_id)

    # ------------------------------------------------------------------
    # Integrity / retention / export
    # ------------------------------------------------------------------

    async def verify_integrity(self, intake_session_id: str, evidence_id: str) -> bool:
        return await self._repo.verify_integrity(intake_session_id, evidence_id)

    async def delete_by_intake_session(self, intake_session_id: str) -> int:
        return await self._repo.delete_by_intake_session(intake_session_id)

    async def export(self, intake_session_id: str) -> EvidenceExportView:
        """Safe, PII-free export of everything recorded for one session."""
        records = await self._repo.list_by_intake(intake_session_id)
        quotes = await self._repo.list_quote_observations(intake_session_id)
        audit = await self._repo.list_audit_events(intake_session_id)
        return EvidenceExportView(
            intake_session_id=intake_session_id,
            exported_at=dt.datetime.now(dt.timezone.utc),
            evidence_count=len(records),
            quote_count=len(quotes),
            audit_event_count=len(audit),
            distinct_plans=sorted({r.plan_id for r in records if r.plan_id}),
            distinct_routes=sorted({r.planned_route_id for r in records if r.planned_route_id}),
            distinct_attempts=sorted({r.attempt_id for r in records if r.attempt_id}),
            evidence=[_record_view(r) for r in records],
            quotes=[_quote_view(q) for q in quotes],
            audit_events=[_audit_view(a) for a in audit],
        )


# ---------------------------------------------------------------------------
# View builders
# ---------------------------------------------------------------------------


def _record_view(record: EvidenceRecord) -> EvidenceRecordView:
    return EvidenceRecordView(
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
        parent_attempt_id=record.parent_attempt_id,
        source_channel=record.source_channel.value,
        source_session_id=record.source_session_id,
        page_signature=record.page_signature,
        safe_url=record.safe_url,
        observation_type=record.observation_type,
        reason_code=record.reason_code,
        evidence_source=record.evidence_source,
        payload_version=record.payload_version,
        payload_kind=record.payload.kind,
        payload=record.payload.model_dump(mode="json"),
        content_hash=record.content_hash,
    )


def _quote_view(quote: QuoteObservation) -> QuoteObservationView:
    return QuoteObservationView(
        quote_id=quote.quote_id,
        attempt_id=quote.attempt_id,
        parent_attempt_id=quote.parent_attempt_id,
        registry_id=quote.registry_id,
        distinct_rate_source_id=quote.distinct_rate_source_id,
        aggregator_registry_id=quote.aggregator_registry_id,
        presented_carrier=quote.presented_carrier,
        observed_at=quote.observed_at,
        annual_premium=str(quote.annual_premium) if quote.annual_premium is not None else None,
        monthly_premium=str(quote.monthly_premium) if quote.monthly_premium is not None else None,
        currency=quote.currency,
        firm_vs_estimate=quote.firm_vs_estimate,
        reference_present=quote.reference_present,
        coverage_raw_present=quote.coverage_raw_present,
        quote_pending_normalization=quote.quote_pending_normalization,
        sequence=quote.sequence,
        content_hash=quote.content_hash,
    )


def _audit_view(event: AuditEvent) -> AuditEventView:
    return AuditEventView(
        audit_id=event.audit_id,
        intake_session_id=event.intake_session_id,
        event_name=event.event_name.value,
        occurred_at=event.occurred_at,
        actor=event.actor,
        safe_metadata=event.safe_metadata,
        content_hash=event.content_hash,
    )
