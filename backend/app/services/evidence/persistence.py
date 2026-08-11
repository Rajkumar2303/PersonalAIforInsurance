"""SQLAlchemy evidence persistence (Issue #10, Prompt 1).

PostgreSQL in production (asyncpg); SQLite (aiosqlite) for hermetic tests -
same dialect-portable models (JSON columns, String enums, Numeric money).

Business logic never writes SQL here; this repository only persists/loads the
domain ``EvidenceRecord`` / ``QuoteObservation`` / ``AuditEvent`` objects and
enforces idempotency via a unique ``idempotency_key``.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, DateTime, Integer, Numeric, String, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from ...db.base import Base
from ...models.evidence import (
    AuditEvent,
    EvidenceAttachmentMetadata,
    EvidenceRecord,
    QuoteObservation,
    validate_evidence_payload,
)

# ---------------------------------------------------------------------------
# ORM tables
# ---------------------------------------------------------------------------


class EvidenceRecordORM(Base):
    __tablename__ = "evidence_records"

    evidence_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    observed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    intake_session_id: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    plan_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    planned_route_id: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    registry_id: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    distinct_rate_source_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    attempt_id: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    parent_attempt_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    source_channel: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    source_session_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    page_signature: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    safe_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    observation_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    reason_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    evidence_source: Mapped[str] = mapped_column(String(64), nullable=False, default="evidence_service")
    payload_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    quote_observation_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    registry_snapshot_ref: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    config_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    attachments: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)


class QuoteObservationORM(Base):
    __tablename__ = "quote_observations"

    quote_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    intake_session_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    attempt_id: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    parent_attempt_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    plan_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    planned_route_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    registry_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    distinct_rate_source_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    aggregator_registry_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    presented_carrier: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    observed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    annual_premium: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    monthly_premium: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    currency: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    firm_vs_estimate: Mapped[str] = mapped_column(String(16), nullable=False, default="firm")
    reference_present: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    private_reference_handle: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    coverage_raw_present: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    coverage_observations: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    discount_observations: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    quote_pending_normalization: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuditEventORM(Base):
    __tablename__ = "audit_events"

    audit_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    intake_session_id: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    event_name: Mapped[str] = mapped_column(String(48), nullable=False)
    occurred_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor: Mapped[str] = mapped_column(String(16), nullable=False, default="system")
    safe_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)


# ---------------------------------------------------------------------------
# Domain <-> ORM conversion
# ---------------------------------------------------------------------------


def _record_to_orm(record: EvidenceRecord) -> EvidenceRecordORM:
    return EvidenceRecordORM(
        evidence_id=record.evidence_id,
        event_type=record.event_type.value,
        observed_at=record.observed_at,
        created_at=record.created_at,
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
        payload=record.payload.model_dump(mode="json"),
        content_hash=record.content_hash,
        idempotency_key=record.idempotency_key,
        quote_observation_id=record.quote_observation_id,
        registry_snapshot_ref=record.registry_snapshot_ref,
        config_version=record.config_version,
        attachments=[a.model_dump(mode="json") for a in record.attachments],
    )


def _orm_to_record(row: EvidenceRecordORM) -> EvidenceRecord:
    from ...models.recovery import SourceChannel
    from ...models.evidence import EvidenceEventType

    return EvidenceRecord(
        evidence_id=row.evidence_id,
        event_type=EvidenceEventType(row.event_type),
        observed_at=row.observed_at,
        created_at=row.created_at,
        sequence=row.sequence,
        intake_session_id=row.intake_session_id,
        plan_id=row.plan_id,
        planned_route_id=row.planned_route_id,
        registry_id=row.registry_id,
        distinct_rate_source_id=row.distinct_rate_source_id,
        attempt_id=row.attempt_id,
        parent_attempt_id=row.parent_attempt_id,
        source_channel=SourceChannel(row.source_channel),
        source_session_id=row.source_session_id,
        page_signature=row.page_signature,
        safe_url=row.safe_url,
        observation_type=row.observation_type,
        reason_code=row.reason_code,
        evidence_source=row.evidence_source,
        payload_version=row.payload_version,
        payload=validate_evidence_payload(row.payload),
        content_hash=row.content_hash,
        idempotency_key=row.idempotency_key,
        quote_observation_id=row.quote_observation_id,
        registry_snapshot_ref=row.registry_snapshot_ref,
        config_version=row.config_version,
        attachments=[
            EvidenceAttachmentMetadata(**a) for a in (row.attachments or [])
        ],
    )


def _quote_to_orm(quote: QuoteObservation) -> QuoteObservationORM:
    return QuoteObservationORM(
        quote_id=quote.quote_id,
        intake_session_id=quote.intake_session_id,
        attempt_id=quote.attempt_id,
        parent_attempt_id=quote.parent_attempt_id,
        plan_id=quote.plan_id,
        planned_route_id=quote.planned_route_id,
        registry_id=quote.registry_id,
        distinct_rate_source_id=quote.distinct_rate_source_id,
        aggregator_registry_id=quote.aggregator_registry_id,
        presented_carrier=quote.presented_carrier,
        observed_at=quote.observed_at,
        annual_premium=quote.annual_premium,
        monthly_premium=quote.monthly_premium,
        currency=quote.currency,
        firm_vs_estimate=quote.firm_vs_estimate,
        reference_present=quote.reference_present,
        private_reference_handle=quote.private_reference_handle,
        coverage_raw_present=quote.coverage_raw_present,
        coverage_observations=list(quote.coverage_observations),
        discount_observations=list(quote.discount_observations),
        quote_pending_normalization=quote.quote_pending_normalization,
        sequence=quote.sequence,
        content_hash=quote.content_hash,
        idempotency_key=quote.idempotency_key,
        created_at=quote.created_at,
    )


def _orm_to_quote(row: QuoteObservationORM) -> QuoteObservation:
    return QuoteObservation(
        quote_id=row.quote_id,
        intake_session_id=row.intake_session_id,
        attempt_id=row.attempt_id,
        parent_attempt_id=row.parent_attempt_id,
        plan_id=row.plan_id,
        planned_route_id=row.planned_route_id,
        registry_id=row.registry_id,
        distinct_rate_source_id=row.distinct_rate_source_id,
        aggregator_registry_id=row.aggregator_registry_id,
        presented_carrier=row.presented_carrier,
        observed_at=row.observed_at,
        annual_premium=row.annual_premium,
        monthly_premium=row.monthly_premium,
        currency=row.currency,
        firm_vs_estimate=row.firm_vs_estimate,
        reference_present=row.reference_present,
        private_reference_handle=row.private_reference_handle,
        coverage_raw_present=row.coverage_raw_present,
        coverage_observations=list(row.coverage_observations or []),
        discount_observations=list(row.discount_observations or []),
        quote_pending_normalization=row.quote_pending_normalization,
        sequence=row.sequence,
        content_hash=row.content_hash,
        idempotency_key=row.idempotency_key,
        created_at=row.created_at,
    )


def _audit_to_orm(event: AuditEvent) -> AuditEventORM:
    return AuditEventORM(
        audit_id=event.audit_id,
        intake_session_id=event.intake_session_id,
        event_name=event.event_name.value,
        occurred_at=event.occurred_at,
        actor=event.actor,
        safe_metadata=event.safe_metadata,
        content_hash=event.content_hash,
        idempotency_key=event.idempotency_key,
    )


def _orm_to_audit(row: AuditEventORM) -> AuditEvent:
    from ...models.evidence import AuditEventName

    return AuditEvent(
        audit_id=row.audit_id,
        intake_session_id=row.intake_session_id,
        event_name=AuditEventName(row.event_name),
        occurred_at=row.occurred_at,
        actor=row.actor,
        safe_metadata=row.safe_metadata,
        content_hash=row.content_hash,
        idempotency_key=row.idempotency_key,
    )


def _ordered_rows(rows: list) -> list:
    return sorted(rows, key=lambda r: (r.sequence, r.created_at.isoformat(), r.evidence_id))


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class SqlAlchemyEvidenceRepository:
    """Async SQLAlchemy evidence repository (Postgres prod / SQLite tests)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    # -- evidence ------------------------------------------------------

    async def append(self, record: EvidenceRecord) -> EvidenceRecord:
        async with AsyncSession(self._engine) as session:
            existing = (
                await session.execute(
                    select(EvidenceRecordORM).where(
                        EvidenceRecordORM.idempotency_key == record.idempotency_key
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return _orm_to_record(existing)
            max_seq = (
                await session.execute(
                    select(func.max(EvidenceRecordORM.sequence)).where(
                        EvidenceRecordORM.attempt_id == record.attempt_id
                    )
                )
            ).scalar()
            seq = int(max_seq or 0) + 1
            stored = record.model_copy(update={"sequence": seq})
            session.add(_record_to_orm(stored))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = (
                    await session.execute(
                        select(EvidenceRecordORM).where(
                            EvidenceRecordORM.idempotency_key == record.idempotency_key
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    return _orm_to_record(existing)
                raise
            return stored

    async def append_many(self, records: list[EvidenceRecord]) -> list[EvidenceRecord]:
        return [await self.append(r) for r in records]

    async def get(self, intake_session_id: str, evidence_id: str) -> Optional[EvidenceRecord]:
        async with AsyncSession(self._engine) as session:
            row = (
                await session.execute(
                    select(EvidenceRecordORM).where(
                        EvidenceRecordORM.evidence_id == evidence_id,
                        EvidenceRecordORM.intake_session_id == intake_session_id,
                    )
                )
            ).scalar_one_or_none()
            return _orm_to_record(row) if row else None

    async def list_by_attempt(self, intake_session_id: str, attempt_id: str) -> list[EvidenceRecord]:
        return await self._list(
            EvidenceRecordORM.attempt_id == attempt_id,
            EvidenceRecordORM.intake_session_id == intake_session_id,
        )

    async def list_by_route(self, intake_session_id: str, planned_route_id: str) -> list[EvidenceRecord]:
        return await self._list(
            EvidenceRecordORM.planned_route_id == planned_route_id,
            EvidenceRecordORM.intake_session_id == intake_session_id,
        )

    async def list_by_plan(self, intake_session_id: str, plan_id: str) -> list[EvidenceRecord]:
        return await self._list(
            EvidenceRecordORM.plan_id == plan_id,
            EvidenceRecordORM.intake_session_id == intake_session_id,
        )

    async def list_by_intake(self, intake_session_id: str) -> list[EvidenceRecord]:
        return await self._list(EvidenceRecordORM.intake_session_id == intake_session_id)

    async def _list(self, *filters) -> list[EvidenceRecord]:
        async with AsyncSession(self._engine) as session:
            rows = (await session.execute(select(EvidenceRecordORM).where(*filters))).scalars().all()
            return [_orm_to_record(r) for r in _ordered_rows(list(rows))]

    # -- quotes --------------------------------------------------------

    async def get_quote_observation(
        self, intake_session_id: str, quote_id: str
    ) -> Optional[QuoteObservation]:
        async with AsyncSession(self._engine) as session:
            row = (
                await session.execute(
                    select(QuoteObservationORM).where(
                        QuoteObservationORM.quote_id == quote_id,
                        QuoteObservationORM.intake_session_id == intake_session_id,
                    )
                )
            ).scalar_one_or_none()
            return _orm_to_quote(row) if row else None

    async def save_quote_observation(
        self, intake_session_id: str, quote: QuoteObservation
    ) -> QuoteObservation:
        async with AsyncSession(self._engine) as session:
            existing = (
                await session.execute(
                    select(QuoteObservationORM).where(
                        QuoteObservationORM.idempotency_key == quote.idempotency_key
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return _orm_to_quote(existing)
            session.add(_quote_to_orm(quote))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = (
                    await session.execute(
                        select(QuoteObservationORM).where(
                            QuoteObservationORM.idempotency_key == quote.idempotency_key
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    return _orm_to_quote(existing)
                raise
            return quote

    async def list_quote_observations(
        self, intake_session_id: str, attempt_id: Optional[str] = None
    ) -> list[QuoteObservation]:
        filters = [QuoteObservationORM.intake_session_id == intake_session_id]
        if attempt_id is not None:
            filters.append(QuoteObservationORM.attempt_id == attempt_id)
        async with AsyncSession(self._engine) as session:
            rows = (await session.execute(select(QuoteObservationORM).where(*filters))).scalars().all()
            return sorted(
                [_orm_to_quote(r) for r in rows],
                key=lambda q: (q.sequence, q.created_at.isoformat(), q.quote_id),
            )

    # -- audit ---------------------------------------------------------

    async def append_audit_event(self, event: AuditEvent) -> AuditEvent:
        async with AsyncSession(self._engine) as session:
            existing = (
                await session.execute(
                    select(AuditEventORM).where(AuditEventORM.idempotency_key == event.idempotency_key)
                )
            ).scalar_one_or_none()
            if existing is not None:
                return _orm_to_audit(existing)
            session.add(_audit_to_orm(event))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = (
                    await session.execute(
                        select(AuditEventORM).where(AuditEventORM.idempotency_key == event.idempotency_key)
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    return _orm_to_audit(existing)
                raise
            return event

    async def list_audit_events(self, intake_session_id: str) -> list[AuditEvent]:
        async with AsyncSession(self._engine) as session:
            rows = (
                await session.execute(
                    select(AuditEventORM).where(AuditEventORM.intake_session_id == intake_session_id)
                )
            ).scalars().all()
            return sorted(
                [_orm_to_audit(r) for r in rows],
                key=lambda e: (e.occurred_at.isoformat(), e.audit_id),
            )

    # -- integrity / retention ----------------------------------------

    async def verify_integrity(self, intake_session_id: str, evidence_id: str) -> bool:
        record = await self.get(intake_session_id, evidence_id)
        if record is None:
            return False
        from .hashing import evidence_content_hash

        return evidence_content_hash(record.model_dump()) == record.content_hash

    async def delete_by_intake_session(self, intake_session_id: str) -> int:
        async with AsyncSession(self._engine) as session:
            from sqlalchemy import delete

            ev = (await session.execute(
                delete(EvidenceRecordORM).where(EvidenceRecordORM.intake_session_id == intake_session_id)
            )).rowcount
            q = (await session.execute(
                delete(QuoteObservationORM).where(QuoteObservationORM.intake_session_id == intake_session_id)
            )).rowcount
            a = (await session.execute(
                delete(AuditEventORM).where(AuditEventORM.intake_session_id == intake_session_id)
            )).rowcount
            await session.commit()
            return int(ev or 0) + int(q or 0) + int(a or 0)
