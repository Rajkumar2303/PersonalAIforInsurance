"""Voice session store (Issue #9, Prompt 1).

In-memory store for safe ``VoiceSession`` records (ids + status metadata only,
never applicant values). Replaceable by Issue #10 persistence later without
touching the engine.
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from ...models.voice import VoiceSession


class VoiceSessionNotFoundError(KeyError):
    """Raised when a voice session does not exist."""


@runtime_checkable
class VoiceSessionStore(Protocol):
    def save(self, session: VoiceSession) -> VoiceSession: ...
    def get(self, voice_session_id: str) -> VoiceSession: ...
    def list_all(self) -> list[VoiceSession]: ...
    def list_by_intake(self, intake_session_id: str) -> list[VoiceSession]: ...
    def list_by_registry(self, registry_id: str) -> list[VoiceSession]: ...
    def delete(self, voice_session_id: str) -> None: ...


class InMemoryVoiceSessionStore:
    """Hermetic in-memory implementation for Prompt 1 and tests."""

    def __init__(self) -> None:
        self._sessions: dict[str, VoiceSession] = {}

    def save(self, session: VoiceSession) -> VoiceSession:
        self._sessions[session.voice_session_id] = session
        return session

    def get(self, voice_session_id: str) -> VoiceSession:
        try:
            return self._sessions[voice_session_id]
        except KeyError:
            raise VoiceSessionNotFoundError(voice_session_id) from None

    def list_all(self) -> list[VoiceSession]:
        return list(self._sessions.values())

    def list_by_intake(self, intake_session_id: str) -> list[VoiceSession]:
        return [s for s in self._sessions.values() if s.intake_session_id == intake_session_id]

    def list_by_registry(self, registry_id: str) -> list[VoiceSession]:
        return [s for s in self._sessions.values() if s.registry_id == registry_id]

    def delete(self, voice_session_id: str) -> None:
        self._sessions.pop(voice_session_id, None)
