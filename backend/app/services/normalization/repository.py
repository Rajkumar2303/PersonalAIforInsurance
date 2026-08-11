"""Normalization repository (Issue #11, Prompt 1).

Protocol + in-memory (hermetic default) + SQLAlchemy (Postgres prod / SQLite
tests) implementations. Persists ``NormalizedQuote`` objects plus typed
coverage-ledger rows. Idempotency is enforced on
``(source_quote_observation_id, normalization_rule_version)`` so re-running
normalization for the same source+rule returns the SAME normalized quote.

Raw Issue #10 ``QuoteObservation`` rows are never mutated here.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any, Optional, Protocol

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from ...db.base import Base
from ...models.normalization import (
    CoverageItemState,
    CoverageLedger,
    CoverageLedgerItem,
    CoverageProvenance,
    NormalizationStatus,
    NormalizedQuote,
    PremiumNormalized,
    UnmappedCoverageObservation,
    validate_coverage_value,
)
from ...models.recovery import SourceChannel

# ---------------------------------------------------------------------------
# ORM tables
# ---------------------------------------------------------------------------


class NormalizedQuoteORM(Base):
    __tablename__ = "normalized_quotes"
    __table_args__ = (
        UniqueConstraint(
            "source_quote_observation_id",
            "normalization_rule_version",
            name="uq_normalized_source_rule",
        ),
    )

    normalized_quote_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    intake_session_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    plan_id: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    planned_route_id: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    registry_id: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    distinct_rate_source_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    aggregator_registry_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    presented_carrier: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    attempt_id: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    parent_attempt_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    source_quote_observation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_channel: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    firm_vs_estimate: Mapped[str] = mapped_column(String(16), nullable=False, default="firm")
    premium: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    unmapped_coverage: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    source_evidence_record_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    normalization_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    normalization_rule_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1")
    normalized_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class NormalizedCoverageItemORM(Base):
    __tablename__ = "normalized_coverage_items"

    coverage_item_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    normalized_quote_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("normalized_quotes.normalized_quote_id", ondelete="CASCADE"), index=True
    )
    intake_session_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    item_key: Mapped[str] = mapped_column(String(48), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    value: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    provenance: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    raw_labels: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    source_evidence_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


# ---------------------------------------------------------------------------
# ORM <-> domain conversions
# ---------------------------------------------------------------------------


def _decimal_or_none(value: Any) -> Optional[Decimal]:
    return Decimal(str(value)) if value is not None else None


def _premium_to_dict(premium: PremiumNormalized) -> dict[str, Any]:
    data = premium.model_dump(mode="json")
    return data


def _dict_to_premium(data: dict[str, Any]) -> PremiumNormalized:
    return PremiumNormalized.model_validate(data)


def _quote_to_orm(quote: NormalizedQuote) -> NormalizedQuoteORM:
    return NormalizedQuoteORM(
        normalized_quote_id=quote.normalized_quote_id,
        intake_session_id=quote.intake_session_id,
        plan_id=quote.plan_id,
        planned_route_id=quote.planned_route_id,
        registry_id=quote.registry_id,
        distinct_rate_source_id=quote.distinct_rate_source_id,
        aggregator_registry_id=quote.aggregator_registry_id,
        presented_carrier=quote.presented_carrier,
        attempt_id=quote.attempt_id,
        parent_attempt_id=quote.parent_attempt_id,
        source_quote_observation_id=quote.source_quote_observation_id,
        source_channel=quote.source_channel.value,
        firm_vs_estimate=quote.firm_vs_estimate,
        premium=_premium_to_dict(quote.premium),
        unmapped_coverage=[
            item.model_dump(mode="json") for item in quote.coverage_ledger.unmapped_coverage
        ],
        source_evidence_record_ids=list(quote.source_evidence_record_ids),
        normalization_status=quote.normalization_status.value,
        normalization_rule_version=quote.normalization_rule_version,
        normalized_at=quote.normalized_at,
        content_hash=quote.content_hash,
        idempotency_key=quote.idempotency_key,
        created_at=quote.created_at,
    )


def _quote_to_coverage_items(quote: NormalizedQuote) -> list[NormalizedCoverageItemORM]:
    rows: list[NormalizedCoverageItemORM] = []
    for index, item in enumerate(quote.coverage_ledger.ordered_items(), start=1):
        rows.append(
            NormalizedCoverageItemORM(
                coverage_item_id=uuid.uuid4().hex,
                normalized_quote_id=quote.normalized_quote_id,
                intake_session_id=quote.intake_session_id,
                item_key=item.item_key.value,
                state=item.state.value,
                value=item.value.model_dump(mode="json") if item.value else None,
                provenance=item.provenance.value,
                raw_labels=list(item.raw_labels),
                source_evidence_ids=list(item.source_evidence_ids),
                sequence=index,
            )
        )
    return rows


def _orm_to_quote(row: NormalizedQuoteORM, items: list[NormalizedCoverageItemORM]) -> NormalizedQuote:
    ledger = CoverageLedger()
    for item in sorted(items, key=lambda r: (r.sequence, r.coverage_item_id)):
        value = validate_coverage_value(item.value) if item.value else None
        ledger.set_item(
            CoverageLedgerItem(
                item_key=item.item_key,  # type: ignore[arg-type]
                state=CoverageItemState(item.state),
                value=value,
                provenance=CoverageProvenance(item.provenance),
                raw_labels=list(item.raw_labels or []),
                source_evidence_ids=list(item.source_evidence_ids or []),
            )
        )
    ledger.unmapped_coverage = [
        UnmappedCoverageObservation.model_validate(u) for u in (row.unmapped_coverage or [])
    ]
    return NormalizedQuote(
        normalized_quote_id=row.normalized_quote_id,
        intake_session_id=row.intake_session_id,
        plan_id=row.plan_id,
        planned_route_id=row.planned_route_id,
        registry_id=row.registry_id,
        distinct_rate_source_id=row.distinct_rate_source_id,
        aggregator_registry_id=row.aggregator_registry_id,
        presented_carrier=row.presented_carrier,
        attempt_id=row.attempt_id,
        parent_attempt_id=row.parent_attempt_id,
        source_quote_observation_id=row.source_quote_observation_id,
        source_channel=SourceChannel(row.source_channel),
        firm_vs_estimate=row.firm_vs_estimate,
        premium=_dict_to_premium(row.premium),
        coverage_ledger=ledger,
        normalization_status=NormalizationStatus(row.normalization_status),
        normalization_rule_version=row.normalization_rule_version,
        normalized_at=row.normalized_at,
        source_evidence_record_ids=list(row.source_evidence_record_ids or []),
        content_hash=row.content_hash,
        idempotency_key=row.idempotency_key,
        created_at=row.created_at,
    )


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class NormalizationRepository(Protocol):
    async def save_normalized_quote(self, quote: NormalizedQuote) -> NormalizedQuote: ...

    async def get(
        self, intake_session_id: str, normalized_quote_id: str
    ) -> Optional[NormalizedQuote]: ...

    async def list_by_intake(self, intake_session_id: str) -> list[NormalizedQuote]: ...

    async def list_by_plan(
        self, intake_session_id: str, plan_id: str
    ) -> list[NormalizedQuote]: ...

    async def list_by_route(
        self, intake_session_id: str, planned_route_id: str
    ) -> list[NormalizedQuote]: ...

    async def list_by_attempt(
        self, intake_session_id: str, attempt_id: str
    ) -> list[NormalizedQuote]: ...

    async def verify_integrity(
        self, intake_session_id: str, normalized_quote_id: str
    ) -> bool: ...

    async def delete_by_intake_session(self, intake_session_id: str) -> int: ...


class InMemoryNormalizationRepository:
    """Thread-safe in-memory normalization repository (hermetic default)."""

    def __init__(self) -> None:
        self._quotes: dict[str, NormalizedQuote] = {}
        self._by_key: dict[str, str] = {}

    def _key(self, quote: NormalizedQuote) -> str:
        return f"{quote.source_quote_observation_id}::{quote.normalization_rule_version}"

    async def save_normalized_quote(self, quote: NormalizedQuote) -> NormalizedQuote:
        key = self._key(quote)
        existing_id = self._by_key.get(key)
        if existing_id is not None:
            return self._quotes[existing_id]
        stored = quote.model_copy(update={"normalized_quote_id": quote.normalized_quote_id or uuid.uuid4().hex})
        self._quotes[stored.normalized_quote_id] = stored
        self._by_key[key] = stored.normalized_quote_id
        return stored

    async def get(
        self, intake_session_id: str, normalized_quote_id: str
    ) -> Optional[NormalizedQuote]:
        quote = self._quotes.get(normalized_quote_id)
        if quote is None or quote.intake_session_id != intake_session_id:
            return None
        return quote

    async def list_by_intake(self, intake_session_id: str) -> list[NormalizedQuote]:
        rows = [q for q in self._quotes.values() if q.intake_session_id == intake_session_id]
        return sorted(rows, key=lambda q: (q.normalized_at.isoformat(), q.normalized_quote_id))

    async def list_by_plan(
        self, intake_session_id: str, plan_id: str
    ) -> list[NormalizedQuote]:
        rows = [
            q for q in self._quotes.values()
            if q.intake_session_id == intake_session_id and q.plan_id == plan_id
        ]
        return sorted(rows, key=lambda q: (q.normalized_at.isoformat(), q.normalized_quote_id))

    async def list_by_route(
        self, intake_session_id: str, planned_route_id: str
    ) -> list[NormalizedQuote]:
        rows = [
            q for q in self._quotes.values()
            if q.intake_session_id == intake_session_id and q.planned_route_id == planned_route_id
        ]
        return sorted(rows, key=lambda q: (q.normalized_at.isoformat(), q.normalized_quote_id))

    async def list_by_attempt(
        self, intake_session_id: str, attempt_id: str
    ) -> list[NormalizedQuote]:
        rows = [
            q for q in self._quotes.values()
            if q.intake_session_id == intake_session_id and q.attempt_id == attempt_id
        ]
        return sorted(rows, key=lambda q: (q.normalized_at.isoformat(), q.normalized_quote_id))

    async def verify_integrity(
        self, intake_session_id: str, normalized_quote_id: str
    ) -> bool:
        quote = await self.get(intake_session_id, normalized_quote_id)
        if quote is None:
            return False
        from .service import normalized_quote_content_hash

        return normalized_quote_content_hash(quote) == quote.content_hash

    async def delete_by_intake_session(self, intake_session_id: str) -> int:
        removed = [qid for qid, q in self._quotes.items() if q.intake_session_id == intake_session_id]
        for qid in removed:
            self._quotes.pop(qid, None)
        self._by_key = {k: v for k, v in self._by_key.items() if v not in removed}
        return len(removed)


class SqlAlchemyNormalizationRepository:
    """Async SQLAlchemy normalization repository (Postgres prod / SQLite tests)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def save_normalized_quote(self, quote: NormalizedQuote) -> NormalizedQuote:
        async with AsyncSession(self._engine) as session:
            existing = (
                await session.execute(
                    select(NormalizedQuoteORM).where(
                        NormalizedQuoteORM.source_quote_observation_id
                        == quote.source_quote_observation_id,
                        NormalizedQuoteORM.normalization_rule_version
                        == quote.normalization_rule_version,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                items = (
                    await session.execute(
                        select(NormalizedCoverageItemORM).where(
                            NormalizedCoverageItemORM.normalized_quote_id
                            == existing.normalized_quote_id
                        )
                    )
                ).scalars().all()
                return _orm_to_quote(existing, list(items))

            session.add(_quote_to_orm(quote))
            session.add_all(_quote_to_coverage_items(quote))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = (
                    await session.execute(
                        select(NormalizedQuoteORM).where(
                            NormalizedQuoteORM.source_quote_observation_id
                            == quote.source_quote_observation_id,
                            NormalizedQuoteORM.normalization_rule_version
                            == quote.normalization_rule_version,
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    items = (
                        await session.execute(
                            select(NormalizedCoverageItemORM).where(
                                NormalizedCoverageItemORM.normalized_quote_id
                                == existing.normalized_quote_id
                            )
                        )
                    ).scalars().all()
                    return _orm_to_quote(existing, list(items))
                raise
            return quote

    async def _load(self, row: NormalizedQuoteORM) -> NormalizedQuote:
        async with AsyncSession(self._engine) as session:
            items = (
                await session.execute(
                    select(NormalizedCoverageItemORM).where(
                        NormalizedCoverageItemORM.normalized_quote_id == row.normalized_quote_id
                    )
                )
            ).scalars().all()
        return _orm_to_quote(row, list(items))

    async def get(
        self, intake_session_id: str, normalized_quote_id: str
    ) -> Optional[NormalizedQuote]:
        async with AsyncSession(self._engine) as session:
            row = (
                await session.execute(
                    select(NormalizedQuoteORM).where(
                        NormalizedQuoteORM.normalized_quote_id == normalized_quote_id,
                        NormalizedQuoteORM.intake_session_id == intake_session_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
        return await self._load(row)

    async def _list(self, *filters) -> list[NormalizedQuote]:
        async with AsyncSession(self._engine) as session:
            rows = (
                await session.execute(select(NormalizedQuoteORM).where(*filters))
            ).scalars().all()
        return [await self._load(r) for r in rows]

    async def list_by_intake(self, intake_session_id: str) -> list[NormalizedQuote]:
        return await self._list(NormalizedQuoteORM.intake_session_id == intake_session_id)

    async def list_by_plan(
        self, intake_session_id: str, plan_id: str
    ) -> list[NormalizedQuote]:
        return await self._list(
            NormalizedQuoteORM.intake_session_id == intake_session_id,
            NormalizedQuoteORM.plan_id == plan_id,
        )

    async def list_by_route(
        self, intake_session_id: str, planned_route_id: str
    ) -> list[NormalizedQuote]:
        return await self._list(
            NormalizedQuoteORM.intake_session_id == intake_session_id,
            NormalizedQuoteORM.planned_route_id == planned_route_id,
        )

    async def list_by_attempt(
        self, intake_session_id: str, attempt_id: str
    ) -> list[NormalizedQuote]:
        return await self._list(
            NormalizedQuoteORM.intake_session_id == intake_session_id,
            NormalizedQuoteORM.attempt_id == attempt_id,
        )

    async def verify_integrity(
        self, intake_session_id: str, normalized_quote_id: str
    ) -> bool:
        quote = await self.get(intake_session_id, normalized_quote_id)
        if quote is None:
            return False
        from .service import normalized_quote_content_hash

        return normalized_quote_content_hash(quote) == quote.content_hash

    async def delete_by_intake_session(self, intake_session_id: str) -> int:
        async with AsyncSession(self._engine) as session:
            rows = (
                await session.execute(
                    select(NormalizedQuoteORM).where(
                        NormalizedQuoteORM.intake_session_id == intake_session_id
                    )
                )
            ).scalars().all()
            quote_ids = [r.normalized_quote_id for r in rows]
            if quote_ids:
                await session.execute(
                    NormalizedCoverageItemORM.__table__.delete().where(
                        NormalizedCoverageItemORM.normalized_quote_id.in_(quote_ids)
                    )
                )
                await session.execute(
                    NormalizedQuoteORM.__table__.delete().where(
                        NormalizedQuoteORM.normalized_quote_id.in_(quote_ids)
                    )
                )
            await session.commit()
            return len(quote_ids)
