"""Evidence repository abstraction (Issue #10, Prompt 1).

``EvidenceRepository`` is a narrow persistence surface. Two implementations:
- ``InMemoryEvidenceRepository``: hermetic, deterministic (default + unit tests).
- ``SqlAlchemyEvidenceRepository`` (``app/services/evidence/persistence.py``):
  PostgreSQL (asyncpg) in production, SQLite (aiosqlite) in hermetic tests.

Ownership boundary: every read is scoped by ``intake_session_id`` so a caller
can never enumerate evidence belonging to another session.

Idempotency: appends are deduplicated by ``idempotency_key`` (a unique
constraint / in-memory key) so a retried delivery yields ONE logical record.
"""

from __future__ import annotations

import datetime as dt
import threading
import uuid
from typing import Optional, Protocol, runtime_checkable

from ...models.evidence import AuditEvent, EvidenceRecord, QuoteObservation


@runtime_checkable
class EvidenceRepository(Protocol):
    """Persistence surface the ``EvidenceService`` depends on."""

    async def append(self, record: EvidenceRecord) -> EvidenceRecord: ...
    async def append_many(self, records: list[EvidenceRecord]) -> list[EvidenceRecord]: ...
    async def get(self, intake_session_id: str, evidence_id: str) -> Optional[EvidenceRecord]: ...
    async def list_by_attempt(self, intake_session_id: str, attempt_id: str) -> list[EvidenceRecord]: ...
    async def list_by_route(self, intake_session_id: str, planned_route_id: str) -> list[EvidenceRecord]: ...
    async def list_by_plan(self, intake_session_id: str, plan_id: str) -> list[EvidenceRecord]: ...
    async def list_by_intake(self, intake_session_id: str) -> list[EvidenceRecord]: ...
    async def list_quote_observations(
        self, intake_session_id: str, attempt_id: Optional[str] = None
    ) -> list[QuoteObservation]: ...
    async def save_quote_observation(
        self, intake_session_id: str, quote: QuoteObservation
    ) -> QuoteObservation: ...
    async def append_audit_event(self, event: AuditEvent) -> AuditEvent: ...
    async def list_audit_events(self, intake_session_id: str) -> list[AuditEvent]: ...
    async def verify_integrity(self, intake_session_id: str, evidence_id: str) -> bool: ...
    async def delete_by_intake_session(self, intake_session_id: str) -> int: ...


def _ordered(records: list[EvidenceRecord]) -> list[EvidenceRecord]:
    """Stable deterministic ordering: sequence, then created_at, then id."""
    return sorted(
        records,
        key=lambda r: (r.sequence, r.created_at.isoformat(), r.evidence_id),
    )


class InMemoryEvidenceRepository:
    """Ephemeral, process-lifetime evidence store (hermetic default/tests)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, EvidenceRecord] = {}
        self._by_key: dict[str, str] = {}  # idempotency_key -> evidence_id
        self._quotes: dict[str, QuoteObservation] = {}
        self._quotes_by_key: dict[str, str] = {}
        self._audit: dict[str, AuditEvent] = {}
        self._audit_by_key: dict[str, str] = {}
        self._sequences: dict[Optional[str], int] = {}

    # -- evidence ------------------------------------------------------

    async def append(self, record: EvidenceRecord) -> EvidenceRecord:
        with self._lock:
            existing = self._by_key.get(record.idempotency_key)
            if existing is not None:
                return self._records[existing]
            seq = self._sequences.get(record.attempt_id, 0) + 1
            self._sequences[record.attempt_id] = seq
            stored = record.model_copy(
                update={"evidence_id": record.evidence_id or uuid.uuid4().hex, "sequence": seq}
            )
            self._records[stored.evidence_id] = stored
            self._by_key[stored.idempotency_key] = stored.evidence_id
            return stored

    async def append_many(self, records: list[EvidenceRecord]) -> list[EvidenceRecord]:
        return [await self.append(r) for r in records]

    async def get(self, intake_session_id: str, evidence_id: str) -> Optional[EvidenceRecord]:
        record = self._records.get(evidence_id)
        if record is None or record.intake_session_id != intake_session_id:
            return None
        return record

    async def list_by_attempt(self, intake_session_id: str, attempt_id: str) -> list[EvidenceRecord]:
        rows = [
            r for r in self._records.values()
            if r.intake_session_id == intake_session_id and r.attempt_id == attempt_id
        ]
        return _ordered(rows)

    async def list_by_route(self, intake_session_id: str, planned_route_id: str) -> list[EvidenceRecord]:
        rows = [
            r for r in self._records.values()
            if r.intake_session_id == intake_session_id and r.planned_route_id == planned_route_id
        ]
        return _ordered(rows)

    async def list_by_plan(self, intake_session_id: str, plan_id: str) -> list[EvidenceRecord]:
        rows = [
            r for r in self._records.values()
            if r.intake_session_id == intake_session_id and r.plan_id == plan_id
        ]
        return _ordered(rows)

    async def list_by_intake(self, intake_session_id: str) -> list[EvidenceRecord]:
        rows = [r for r in self._records.values() if r.intake_session_id == intake_session_id]
        return _ordered(rows)

    # -- quotes --------------------------------------------------------

    async def save_quote_observation(
        self, intake_session_id: str, quote: QuoteObservation
    ) -> QuoteObservation:
        with self._lock:
            existing = self._quotes_by_key.get(quote.idempotency_key)
            if existing is not None:
                return self._quotes[existing]
            stored = quote.model_copy(
                update={"quote_id": quote.quote_id or uuid.uuid4().hex}
            )
            self._quotes[stored.quote_id] = stored
            self._quotes_by_key[stored.idempotency_key] = stored.quote_id
            return stored

    async def list_quote_observations(
        self, intake_session_id: str, attempt_id: Optional[str] = None
    ) -> list[QuoteObservation]:
        rows = [
            q for q in self._quotes.values()
            if q.intake_session_id == intake_session_id
            and (attempt_id is None or q.attempt_id == attempt_id)
        ]
        return sorted(rows, key=lambda q: (q.sequence, q.created_at.isoformat(), q.quote_id))

    # -- audit ---------------------------------------------------------

    async def append_audit_event(self, event: AuditEvent) -> AuditEvent:
        with self._lock:
            existing = self._audit_by_key.get(event.idempotency_key)
            if existing is not None:
                return self._audit[existing]
            self._audit[event.audit_id] = event
            self._audit_by_key[event.idempotency_key] = event.audit_id
            return event

    async def list_audit_events(self, intake_session_id: str) -> list[AuditEvent]:
        rows = [e for e in self._audit.values() if e.intake_session_id == intake_session_id]
        return sorted(rows, key=lambda e: (e.occurred_at.isoformat(), e.audit_id))

    # -- integrity / retention ----------------------------------------

    async def verify_integrity(self, intake_session_id: str, evidence_id: str) -> bool:
        record = await self.get(intake_session_id, evidence_id)
        if record is None:
            return False
        from .hashing import evidence_content_hash

        data = record.model_dump()
        return evidence_content_hash(data) == record.content_hash

    async def delete_by_intake_session(self, intake_session_id: str) -> int:
        with self._lock:
            removed = [
                eid for eid, r in self._records.items() if r.intake_session_id == intake_session_id
            ]
            for eid in removed:
                self._records.pop(eid, None)
            self._by_key = {
                k: v for k, v in self._by_key.items() if v not in removed
            }
            quotes = [
                qid for qid, q in self._quotes.items() if q.intake_session_id == intake_session_id
            ]
            for qid in quotes:
                self._quotes.pop(qid, None)
            self._quotes_by_key = {k: v for k, v in self._quotes_by_key.items() if v not in quotes}
            audit = [
                aid for aid, e in self._audit.items() if e.intake_session_id == intake_session_id
            ]
            for aid in audit:
                self._audit.pop(aid, None)
            self._audit_by_key = {k: v for k, v in self._audit_by_key.items() if v not in audit}
            return len(removed) + len(quotes) + len(audit)
