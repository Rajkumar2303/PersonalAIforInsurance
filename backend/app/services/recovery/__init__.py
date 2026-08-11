"""Recovery services (Issue #8): deterministic terminal-status & recovery engine.

Exposes the ``RecoveryEngine`` (decision core), the in-memory ``AttemptStore``
(replaceable by Issue #10 evidence persistence), the data-driven
``RecoveryPolicyLoader``, and the deterministic ``classification`` mapping.
"""

from __future__ import annotations

from typing import Optional

from .attempt_store import AttemptStore, InMemoryAttemptStore
from .classification import (
    ClassifiedObservation,
    browser_observation_to_execution,
    classify_observation,
    sanitize_recovery_context,
)
from .engine import (
    IntakeConsentSource,
    PlannerRouteSource,
    RecoveryConsentSource,
    RecoveryEngine,
    RecoveryRouteSource,
)
from .policy import RecoveryPolicyLoader

__all__ = [
    "AttemptStore",
    "InMemoryAttemptStore",
    "RecoveryPolicyLoader",
    "RecoveryEngine",
    "RecoveryRouteSource",
    "PlannerRouteSource",
    "RecoveryConsentSource",
    "IntakeConsentSource",
    "ClassifiedObservation",
    "classify_observation",
    "browser_observation_to_execution",
    "sanitize_recovery_context",
    "get_recovery_engine",
]

_engine: Optional[RecoveryEngine] = None


def get_recovery_engine() -> RecoveryEngine:
    """Cached default recovery engine (real store + data policy + planner source).

    Automatically records decisions/transitions as evidence via the shared sink.
    """
    global _engine
    if _engine is None:
        from ..evidence import get_evidence_sink

        _engine = RecoveryEngine(evidence_sink=get_evidence_sink())
    return _engine
