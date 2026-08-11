"""Trusted just-in-time value source for the browser executor (Issue #7).

Privacy architecture (issue section 7): the Browser Agent identifies a
canonical path; the executor retrieves the value JUST-IN-TIME from the vault
via ``IntakeEngine.get_field_value`` immediately before filling the destination
control; the value exists only in a local trusted variable and is discarded
after the fill. It is NEVER placed in BrowserSession, BrowserObservation,
LangGraph state, LangSmith metadata, or logs.

The value source only ever surfaces PRESENCE booleans (``known``) plus the
single just-in-time scalar, and delegates missing-field requests back to
Issue #5 (``request_fields`` with source_context="browser_agent").
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

from ..models.intake.session import FieldRequestOutcome
from ..services.intake.engine import IntakeEngine

BROWSER_SOURCE_CONTEXT = "browser_agent"


@runtime_checkable
class BrowserValueSource(Protocol):
    """Safe value surface the executor depends on (never raw profiles)."""

    def profile_id(self, session_id: str) -> Optional[str]:
        ...

    def known(self, session_id: str, canonical_path: str) -> bool:
        ...

    def get(self, session_id: str, canonical_path: str) -> Any:
        """Just-in-time single scalar value (call immediately before fill)."""

    def collection_length(self, session_id: str, canonical_path: str) -> int:
        """Just-in-time length of a canonical collection (derived counts).

        Used by bindings with ``transform: collection_length`` so count fields
        (e.g. "number of vehicles") are derived from the canonical collection
        and can never drift from its actual length.
        """

    def request(self, session_id: str, paths: list[str]) -> list[FieldRequestOutcome]:
        ...

    def has_route_consent(self, session_id: str, registry_id: str) -> bool:
        ...

    def route_disclosure_covers(self, session_id: str, registry_id: str, canonical_path: str) -> bool:
        ...

    def has_collection_consent(self, session_id: str) -> bool:
        ...

    def field_gate(self, session_id: str, canonical_path: str) -> str:
        ...


class IntakeValueSource:
    """BrowserValueSource over the Issue #5 IntakeEngine."""

    def __init__(self, engine: IntakeEngine) -> None:
        self._engine = engine

    def profile_id(self, session_id: str) -> Optional[str]:
        session = self._engine.get_session(session_id)
        return session.profile_id

    def known(self, session_id: str, canonical_path: str) -> bool:
        return self._engine.field_presence(session_id, [canonical_path]).get(canonical_path, False)

    def get(self, session_id: str, canonical_path: str) -> Any:
        """Retrieve exactly one scalar value just-in-time (never cached)."""
        pid = self.profile_id(session_id)
        if pid is None:
            return None
        return self._engine.get_field_value(pid, canonical_path)

    def collection_length(self, session_id: str, canonical_path: str) -> int:
        """Derived count: just-in-time length of a canonical collection."""
        pid = self.profile_id(session_id)
        if pid is None:
            return 0
        return self._engine.get_collection_length(pid, canonical_path)

    def request(self, session_id: str, paths: list[str]) -> list[FieldRequestOutcome]:
        return self._engine.request_fields(session_id, paths, BROWSER_SOURCE_CONTEXT)

    def has_route_consent(self, session_id: str, registry_id: str) -> bool:
        return self._engine.has_route_consent(session_id, registry_id)

    def route_disclosure_covers(self, session_id: str, registry_id: str, canonical_path: str) -> bool:
        return self._engine.route_disclosure_covers(session_id, registry_id, canonical_path)

    def has_collection_consent(self, session_id: str) -> bool:
        return self._engine.has_collection_consent(self._engine.get_session(session_id))

    def field_gate(self, session_id: str, canonical_path: str) -> str:
        return self._engine.field_gate(session_id, canonical_path)
