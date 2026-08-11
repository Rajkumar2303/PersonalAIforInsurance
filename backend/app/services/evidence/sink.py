"""EvidenceSink: narrow, synchronous evidence-persistence abstraction (Issue #10,
Prompt 2).

Engines (RecoveryEngine, VoiceEngine, BrowserSessionManager, IntakeEngine) MUST
NOT instantiate SQLAlchemy sessions, Postgres clients, or EvidenceRepository
implementations. They depend on this small sink protocol instead.

- ``NoopEvidenceSink`` — safe default: evidence is intentionally disabled.
- ``EvidenceServiceSink`` — synchronous bridge to the async ``EvidenceService``.
  It runs the service coroutine on a DEDICATED background event loop so it is
  safe to call from both synchronous engines and asynchronous managers, with or
  without a running event loop in the caller's thread.

Persistence-failure policy:
- ``record*`` NEVER raises and NEVER triggers a provider retry. A failed write
  returns ``EvidenceWriteResult(status=persistence_failed)`` so the failure is
  explicit (evidence is never silently lost) while provider execution continues
  normally (a DB failure must never cause duplicate submissions to insurers).
- Quote-result evidence returns the REAL status so a quote is never falsely
  reported as durably recorded when persistence failed.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Optional, Protocol, runtime_checkable

from ...models.evidence import AuditEventName
from .ingest import EvidenceDraft
from .service import EvidenceService

logger = logging.getLogger(__name__)


class EvidenceWriteStatus(StrEnum):
    DURABLE = "durable"
    PERSISTENCE_FAILED = "persistence_failed"
    DISABLED = "disabled"


@dataclass(frozen=True)
class EvidenceWriteResult:
    """Outcome of one automatic evidence write (never raises on failure)."""

    status: EvidenceWriteStatus
    record_id: Optional[str] = None
    error_category: Optional[str] = None  # safe (e.g. "OperationalError"), never PII

    @property
    def durable(self) -> bool:
        return self.status is EvidenceWriteStatus.DURABLE


@runtime_checkable
class EvidenceSink(Protocol):
    """Synchronous evidence-write surface engines depend on."""

    @property
    def enabled(self) -> bool: ...

    def record(self, intake_session_id: str, draft: EvidenceDraft) -> EvidenceWriteResult: ...
    def record_quote(
        self, intake_session_id: str, quote: Any
    ) -> EvidenceWriteResult: ...
    def record_audit(
        self,
        intake_session_id: str,
        *,
        event_name: AuditEventName,
        actor: str = "system",
        safe_metadata: Optional[dict[str, Any]] = None,
    ) -> EvidenceWriteResult: ...

    def evidence_status(self) -> str:
        """'durable' | 'persistence_failed' | 'disabled' (safe health metadata)."""
        ...


class NoopEvidenceSink:
    """Evidence persistence intentionally disabled (safe default)."""

    @property
    def enabled(self) -> bool:
        return False

    def record(self, intake_session_id: str, draft: EvidenceDraft) -> EvidenceWriteResult:
        return EvidenceWriteResult(EvidenceWriteStatus.DISABLED)

    def record_quote(self, intake_session_id: str, quote: Any) -> EvidenceWriteResult:
        return EvidenceWriteResult(EvidenceWriteStatus.DISABLED)

    def record_audit(
        self,
        intake_session_id: str,
        *,
        event_name: AuditEventName,
        actor: str = "system",
        safe_metadata: Optional[dict[str, Any]] = None,
    ) -> EvidenceWriteResult:
        return EvidenceWriteResult(EvidenceWriteStatus.DISABLED)

    def evidence_status(self) -> str:
        return EvidenceWriteStatus.DISABLED.value


class EvidenceServiceSink:
    """Synchronous, durable sink over the async EvidenceService.

    Uses a dedicated background event loop + thread so writes are immediate and
    deterministic from any caller context. The underlying repository is
    thread-safe (in-memory uses a lock; SQLAlchemy engines pool per-operation).
    """

    def __init__(self, service: EvidenceService, *, timeout_seconds: float = 10.0) -> None:
        self._service = service
        self._timeout = timeout_seconds
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._last_status = EvidenceWriteStatus.DURABLE.value

    @property
    def enabled(self) -> bool:
        return True

    def evidence_status(self) -> str:
        return self._last_status

    # -- public write surface -------------------------------------------

    def record(self, intake_session_id: str, draft: EvidenceDraft) -> EvidenceWriteResult:
        try:
            record = self._run(self._service.append(intake_session_id, draft))
        except Exception as exc:  # noqa: BLE001 - never propagate to engines
            return self._failure(exc)
        self._last_status = EvidenceWriteStatus.DURABLE.value
        return EvidenceWriteResult(EvidenceWriteStatus.DURABLE, record_id=record.evidence_id)

    def record_quote(self, intake_session_id: str, quote: Any) -> EvidenceWriteResult:
        try:
            saved = self._run(self._service.record_quote_observation(intake_session_id, quote))
        except Exception as exc:  # noqa: BLE001
            return self._failure(exc)
        self._last_status = EvidenceWriteStatus.DURABLE.value
        return EvidenceWriteResult(EvidenceWriteStatus.DURABLE, record_id=saved.quote_id)

    def record_audit(
        self,
        intake_session_id: str,
        *,
        event_name: AuditEventName,
        actor: str = "system",
        safe_metadata: Optional[dict[str, Any]] = None,
    ) -> EvidenceWriteResult:
        try:
            event = self._run(
                self._service.record_audit_event(
                    intake_session_id,
                    event_name=event_name,
                    actor=actor,
                    safe_metadata=safe_metadata,
                )
            )
        except Exception as exc:  # noqa: BLE001
            return self._failure(exc)
        self._last_status = EvidenceWriteStatus.DURABLE.value
        return EvidenceWriteResult(EvidenceWriteStatus.DURABLE, record_id=event.audit_id)

    # -- internals ------------------------------------------------------

    def _run(self, coro):
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=self._timeout)

    def _failure(self, exc: Exception) -> EvidenceWriteResult:
        self._last_status = EvidenceWriteStatus.PERSISTENCE_FAILED.value
        category = type(exc).__name__
        logger.warning(
            "evidence persistence failed (provider execution unaffected)",
            extra={"workflow": "evidence", "workflow_stage": "write",
                   "status": "error", "error_type": category, "sensitive": False},
        )
        # Never include exception args (may carry PII/SQL dumps) in the result.
        return EvidenceWriteResult(
            EvidenceWriteStatus.PERSISTENCE_FAILED, error_category=category
        )

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None or self._loop.is_closed():
                loop = asyncio.new_event_loop()
                thread = threading.Thread(
                    target=loop.run_forever, daemon=True, name="evidence-sink"
                )
                thread.start()
                self._loop, self._thread = loop, thread
            return self._loop

    def close(self) -> None:
        """Stop the background loop (best-effort; used by app shutdown)."""
        with self._lock:
            loop, thread = self._loop, self._thread
            self._loop, self._thread = None, None
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)
