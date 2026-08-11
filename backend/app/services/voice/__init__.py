"""Voice / phone context handoff services (Issue #9, Prompt 1).

Provider-agnostic, deterministic voice layer: transport, broker-question
interpreter, session store, handoff-context builders, and the ``VoiceEngine``
(single authority). NO real calls, NO LLM, NO recording/transcription, and NO
second applicant-information store - values are read JIT from the Issue #5
intake vault and every outcome flows through the Issue #8 recovery engine.
"""

from __future__ import annotations

from typing import Optional

from ...browser.value_provider import IntakeValueSource
from ...models.voice import PhoneHandoffContext, VoiceDecision, VoiceSession, sanitize_voice_context
from ..intake import get_intake_engine
from .engine import RecoverySource, VoiceEngine, VoiceValueSource
from .handoff import (
    handoff_context_from_phone_route,
    handoff_context_from_recovery,
)
from .question_interpreter import (
    BrokerQuestionInterpreter,
    DeterministicBrokerQuestionInterpreter,
)
from .session_store import (
    InMemoryVoiceSessionStore,
    VoiceSessionNotFoundError,
    VoiceSessionStore,
)
from .transport import MockVoiceTransport, ScriptedBrokerSimulator, VoiceTransport, safe_render

__all__ = [
    "VoiceEngine",
    "VoiceSession",
    "VoiceDecision",
    "PhoneHandoffContext",
    "VoiceTransport",
    "MockVoiceTransport",
    "ScriptedBrokerSimulator",
    "VoiceSessionStore",
    "InMemoryVoiceSessionStore",
    "VoiceSessionNotFoundError",
    "BrokerQuestionInterpreter",
    "DeterministicBrokerQuestionInterpreter",
    "RecoverySource",
    "VoiceValueSource",
    "handoff_context_from_recovery",
    "handoff_context_from_phone_route",
    "sanitize_voice_context",
    "safe_render",
    "get_voice_engine",
]

_engine: Optional[VoiceEngine] = None


def get_voice_engine() -> VoiceEngine:
    """Cached default voice engine (hermetic transport + real intake/recovery)."""
    global _engine
    if _engine is None:
        _engine = VoiceEngine(
            store=InMemoryVoiceSessionStore(),
            values=VoiceValueSource(get_intake_engine()),
            interpreter=DeterministicBrokerQuestionInterpreter(),
            transport=MockVoiceTransport(),
        )
    return _engine
