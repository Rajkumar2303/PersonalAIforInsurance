"""Voice transport boundary (Issue #9, Prompt 1).

The transport is where real audio/hardware would eventually live. Prompt 1
implements a ``MockVoiceTransport`` (records SAFE renderings only) and a
``ScriptedBrokerSimulator`` so the whole voice layer is fully hermetic and
deterministic - NO real phone calls, NO LLM, NO transcription.

PRIVACY: the transport only ever receives SAFE values to speak. The engine
retrieves applicant values JIT, hands them to ``speak()``, and immediately
clears the transport's ``last_spoken`` - the value never persists in a
session, decision, graph state, or trace.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

from ...models.voice import BrokerQuestion, VoiceDecision, VoiceSession


@runtime_checkable
class VoiceTransport(Protocol):
    """Boundary between the deterministic engine and the phone medium."""

    def start_session(self, session: VoiceSession) -> None: ...
    def speak(self, session_id: str, text: str, path: Optional[str] = None) -> None: ...
    def receive_event(self, session_id: str) -> Optional[BrokerQuestion]: ...
    def pause(self, session_id: str, reason: str) -> None: ...
    def resume(self, session_id: str) -> None: ...
    def transfer_to_human(self, session_id: str, context: str) -> None: ...
    def end_session(self, session_id: str, reason: str) -> None: ...


class MockVoiceTransport:
    """Hermetic in-memory transport used by Prompt 1/2 and all tests.

    Privacy model (Prompt 2): the engine passes the value text to ``speak()``
    and then calls ``discard_last_spoken()`` so the JIT value is discarded
    immediately after use - only SAFE metadata (the canonical path, spoken
    counts) is retained. A raw transcript is retained ONLY when
    ``retain_transcript=True`` (test-only verification of the JIT flow); the
    privacy scans use the default (no transcript).
    """

    def __init__(
        self,
        simulator: Optional["ScriptedBrokerSimulator"] = None,
        *,
        retain_transcript: bool = False,
    ) -> None:
        self._simulator = simulator
        self._retain_transcript = retain_transcript
        self._spoken: list[str] = []
        self._spoken_paths: list[Optional[str]] = []
        self._events: list[BrokerQuestion] = []
        self._started: set[str] = set()
        self._ended: list[tuple[str, str]] = []
        self._transferred: list[tuple[str, str]] = []
        self._paused: list[tuple[str, str]] = []
        self._resumed: list[str] = []
        self.last_spoken: Optional[str] = None
        self.last_spoken_path: Optional[str] = None
        self.spoken_count: int = 0

    # -- voice transport protocol --------------------------------------

    def start_session(self, session: VoiceSession) -> None:
        self._started.add(session.voice_session_id)

    def speak(self, session_id: str, text: str, path: Optional[str] = None) -> None:
        self.last_spoken = text
        self.last_spoken_path = path
        self.spoken_count += 1
        if self._retain_transcript:
            self._spoken.append(text)
        self._spoken_paths.append(path)

    def receive_event(self, session_id: str) -> Optional[BrokerQuestion]:
        if self._events:
            return self._events.pop(0)
        if self._simulator is not None:
            return self._simulator.next_event(session_id)
        return None

    def pause(self, session_id: str, reason: str) -> None:
        self._paused.append((session_id, reason))

    def resume(self, session_id: str) -> None:
        self._resumed.append(session_id)

    def transfer_to_human(self, session_id: str, context: str) -> None:
        self._transferred.append((session_id, context))

    def end_session(self, session_id: str, reason: str) -> None:
        self._ended.append((session_id, reason))

    # -- test helpers --------------------------------------------------

    def discard_last_spoken(self) -> None:
        """Discard the transient value after it has been spoken (Prompt 2)."""
        self.last_spoken = None

    def clear_last_spoken(self) -> None:
        self.discard_last_spoken()

    def push_event(self, question: BrokerQuestion) -> None:
        self._events.append(question)

    @property
    def spoken(self) -> list[str]:
        return list(self._spoken)

    @property
    def spoken_paths(self) -> list[Optional[str]]:
        return list(self._spoken_paths)

    @property
    def ended(self) -> list[tuple[str, str]]:
        return list(self._ended)

    @property
    def transferred(self) -> list[tuple[str, str]]:
        return list(self._transferred)

    @property
    def started(self) -> set[str]:
        return set(self._started)


class ScriptedBrokerSimulator:
    """Deterministic broker script: a fixed list of BrokerQuestion events.

    Supports out-of-order / no-answer scenarios by returning ``None``
    (``BROKER_UNAVAILABLE``-style) when the script is exhausted, and can be
    marked unreachable to simulate a phone that never answers.
    """

    def __init__(self, events: Optional[list[BrokerQuestion]] = None, *, unreachable: bool = False) -> None:
        self._events: list[BrokerQuestion] = list(events or [])
        self._index = 0
        self.unreachable = unreachable

    def next_event(self, session_id: str) -> Optional[BrokerQuestion]:
        if self.unreachable:
            return None
        if self._index >= len(self._events):
            return None
        event = self._events[self._index]
        self._index += 1
        return event

    def reset(self) -> None:
        self._index = 0

    @property
    def remaining(self) -> int:
        return max(0, len(self._events) - self._index)

    @property
    def answered(self) -> int:
        return self._index


def safe_render(value: Any, canonical_path: str) -> str:
    """Render a JIT value into a SPOKEN sentence for the transport.

    The value is included because the automation genuinely speaks it to the
    broker (like a human agent). It is TRANSIENT: it lives only in the
    transport boundary (``last_spoken``), is never stored in a session,
    decision, graph state, or trace, and can be cleared by the transport.
    Collection paths render a count.
    """
    if canonical_path in ("product_data.vehicles", "product_data.drivers"):
        return f"The count for {canonical_path} is {value}."
    return f"The value for {canonical_path} is {value}."
