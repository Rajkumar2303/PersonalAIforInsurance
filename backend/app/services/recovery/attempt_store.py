"""Attempt store (Issue #8).

A small persistence abstraction for attempt history. Issue #8 uses the
in-memory store; the interface is designed so Issue #10 (evidence/audit
persistence) can replace the implementation without changing the engine.
Attempt records carry SAFE metadata only - never applicant values.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional, Protocol, runtime_checkable

from ...models.recovery import AttemptLifecycleStatus, AttemptRecord


class TransitionError(RuntimeError):
    """Raised when an attempt lifecycle transition is not allowed."""


# Allowed lifecycle transitions. Self-transitions are allowed as idempotent
# no-ops. ``terminal`` is only reachable from running/paused/recoverable and can
# only be enriched (terminal -> terminal) with explicit permission.
_ALLOWED_TRANSITIONS: dict[AttemptLifecycleStatus, frozenset[AttemptLifecycleStatus]] = {
    AttemptLifecycleStatus.PENDING: frozenset(
        {AttemptLifecycleStatus.PENDING, AttemptLifecycleStatus.RUNNING}
    ),
    AttemptLifecycleStatus.RUNNING: frozenset(
        {
            AttemptLifecycleStatus.RUNNING,
            AttemptLifecycleStatus.PAUSED,
            AttemptLifecycleStatus.RECOVERABLE,
            AttemptLifecycleStatus.TERMINAL,
        }
    ),
    AttemptLifecycleStatus.PAUSED: frozenset(
        {AttemptLifecycleStatus.PAUSED, AttemptLifecycleStatus.RUNNING, AttemptLifecycleStatus.TERMINAL}
    ),
    AttemptLifecycleStatus.RECOVERABLE: frozenset(
        {AttemptLifecycleStatus.RECOVERABLE, AttemptLifecycleStatus.RUNNING, AttemptLifecycleStatus.TERMINAL}
    ),
    AttemptLifecycleStatus.TERMINAL: frozenset({AttemptLifecycleStatus.TERMINAL}),
}


@runtime_checkable
class AttemptStore(Protocol):
    """Persistence surface the ``RecoveryEngine`` depends on."""

    def save(self, attempt: AttemptRecord) -> None:
        ...

    def get(self, attempt_id: str) -> Optional[AttemptRecord]:
        ...

    def delete(self, attempt_id: str) -> None:
        ...

    def list_all(self) -> list[AttemptRecord]:
        ...

    def list_by_plan(self, plan_id: str) -> list[AttemptRecord]:
        ...

    def list_by_route(self, plan_id: Optional[str], registry_id: str) -> list[AttemptRecord]:
        ...

    def list_by_rate_source(self, plan_id: Optional[str], distinct_rate_source_id: str) -> list[AttemptRecord]:
        ...

    def next_attempt_number(self, plan_id: Optional[str], distinct_rate_source_id: Optional[str]) -> int:
        ...

    def update(self, attempt_id: str, *, allow_terminal_mutation: bool = False, **changes: object) -> AttemptRecord:
        ...


class InMemoryAttemptStore:
    """Deterministic in-memory attempt store (hermetic, Issue #8)."""

    def __init__(self) -> None:
        self._attempts: dict[str, AttemptRecord] = {}

    def save(self, attempt: AttemptRecord) -> None:
        self._attempts[attempt.attempt_id] = attempt

    def get(self, attempt_id: str) -> Optional[AttemptRecord]:
        return self._attempts.get(attempt_id)

    def delete(self, attempt_id: str) -> None:
        self._attempts.pop(attempt_id, None)

    def list_all(self) -> list[AttemptRecord]:
        return sorted(self._attempts.values(), key=lambda a: (a.started_at, a.attempt_id))

    def list_by_plan(self, plan_id: str) -> list[AttemptRecord]:
        return [a for a in self.list_all() if a.plan_id == plan_id]

    def list_by_route(self, plan_id: Optional[str], registry_id: str) -> list[AttemptRecord]:
        return [
            a for a in self.list_all()
            if a.registry_id == registry_id and (plan_id is None or a.plan_id == plan_id)
        ]

    def list_by_rate_source(self, plan_id: Optional[str], distinct_rate_source_id: str) -> list[AttemptRecord]:
        return [
            a for a in self.list_all()
            if a.distinct_rate_source_id == distinct_rate_source_id
            and (plan_id is None or a.plan_id == plan_id)
        ]

    def next_attempt_number(self, plan_id: Optional[str], distinct_rate_source_id: Optional[str]) -> int:
        """Per-(plan, rate source) sequence so alternatives share one budget."""
        if not distinct_rate_source_id:
            return 1
        numbers = [
            a.attempt_number
            for a in self.list_by_rate_source(plan_id, distinct_rate_source_id)
        ]
        return max(numbers) + 1 if numbers else 1

    def update(
        self,
        attempt_id: str,
        *,
        allow_terminal_mutation: bool = False,
        **changes: object,
    ) -> AttemptRecord:
        """Apply a validated update; terminal attempts are immutable by default.

        - Lifecycle transitions are validated against ``_ALLOWED_TRANSITIONS``.
        - Terminal attempts: immutable unless ``allow_terminal_mutation`` is
          explicitly set (enrichment only - the lifecycle must stay terminal).
        - Every successful mutation bumps ``revision`` (stale/ordering guard).
        """
        attempt = self._attempts.get(attempt_id)
        if attempt is None:
            raise KeyError(attempt_id)
        if attempt.lifecycle_status is AttemptLifecycleStatus.TERMINAL:
            if not allow_terminal_mutation:
                if changes.get("lifecycle_status") is not None:
                    raise TransitionError("terminal attempt is immutable")
                return attempt  # non-lifecycle changes silently ignored
            target = changes.get("lifecycle_status", AttemptLifecycleStatus.TERMINAL)
            if AttemptLifecycleStatus(target) is not AttemptLifecycleStatus.TERMINAL:
                raise TransitionError("terminal attempt may only be enriched, not transitioned")
        else:
            new_lifecycle = changes.get("lifecycle_status")
            if new_lifecycle is not None:
                target = AttemptLifecycleStatus(new_lifecycle)
                allowed = _ALLOWED_TRANSITIONS.get(attempt.lifecycle_status, frozenset())
                if target not in allowed:
                    raise TransitionError(
                        f"invalid attempt transition {attempt.lifecycle_status.value} -> {target.value}"
                    )
        updated = attempt.model_copy(update={**changes, "revision": attempt.revision + 1})  # type: ignore[arg-type]
        self._attempts[attempt_id] = updated
        return updated
