"""Intake services (Issue #5): catalog, vault, consent, sessions, engine."""

from __future__ import annotations

from typing import Optional

from .catalog import IntakeFieldCatalog
from .consent import ConsentService
from .engine import IntakeEngine
from .session_store import InMemorySessionStore
from .vault import build_profile_vault

__all__ = [
    "IntakeFieldCatalog",
    "ConsentService",
    "IntakeEngine",
    "InMemorySessionStore",
    "build_profile_vault",
    "get_intake_catalog",
    "get_intake_engine",
]

_catalog: Optional[IntakeFieldCatalog] = None
_engine: Optional[IntakeEngine] = None


def get_intake_catalog() -> IntakeFieldCatalog:
    """Cached default catalog (data-driven)."""
    global _catalog
    if _catalog is None:
        _catalog = IntakeFieldCatalog()
    return _catalog


def get_intake_engine() -> IntakeEngine:
    """Cached default engine used by the API layer (ephemeral in-memory by
    default; encrypted-at-rest when INTAKE_VAULT_KEY is configured)."""
    global _engine
    if _engine is None:
        _engine = IntakeEngine(
            catalog=get_intake_catalog(),
            vault=build_profile_vault(),
            sessions=InMemorySessionStore(),
            consent=ConsentService(),
        )
    return _engine
