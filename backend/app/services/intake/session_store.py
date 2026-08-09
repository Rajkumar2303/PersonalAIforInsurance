"""In-memory intake session store (Issue #5).

Ephemeral, process-lifetime storage for ``IntakeSession`` records. Sessions
carry safe metadata only (no raw answers). Persistence of sessions is future
work - Issue #5 keeps everything in memory behind this small interface.
"""

from __future__ import annotations

import logging
from typing import Optional

from ...models.intake.session import IntakeSession

logger = logging.getLogger(__name__)


class InMemorySessionStore:
    """Thread-safe-enough ephemeral session store for dev/tests."""

    def __init__(self) -> None:
        self._sessions: dict[str, IntakeSession] = {}

    def save(self, session: IntakeSession) -> IntakeSession:
        self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> Optional[IntakeSession]:
        return self._sessions.get(session_id)

    def delete(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
