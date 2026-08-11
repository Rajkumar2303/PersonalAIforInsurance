"""Voice / phone context handoff API (Issue #9, Prompt 1).

Minimal, safe interface for the deterministic voice layer.

- ``POST /api/v1/voice/handoffs``: prepare a voice session from a safe
  ``PhoneHandoffContext``.
- ``GET  /api/v1/voice/sessions/{id}``: current session (safe metadata).
- ``POST /api/v1/voice/sessions/{id}/disclosure``: automation disclosure.
- ``POST /api/v1/voice/sessions/{id}/events``: one structured broker question.
- ``POST /api/v1/voice/sessions/{id}/resume``: resume after Issue #5 answered.
- ``POST /api/v1/voice/sessions/{id}/human-handoff``: explicit human escalation.
- ``POST /api/v1/voice/sessions/{id}/observations``: push a voice observation to
  the Issue #8 recovery engine (returns the authoritative RecoveryDecision).

Requests carry ids + structured safe metadata; responses carry safe voice /
recovery metadata only - never applicant values.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ConfigDict, Field

from ..core.config import Settings, get_settings
from ..core.logging import clear_log_context, set_log_context
from ..core.tracing import run_config
from ..graph.voice_workflow import WORKFLOW_NAME
from ..models.insurance.base import SensitiveBaseModel
from ..models.recovery import RecoveryDecision
from ..models.voice import (
    BrokerQuestion,
    PhoneHandoffContext,
    VoiceDecision,
    VoiceRouteSummary,
    VoiceSession,
    sanitize_voice_context,
)
from ..services.voice import (
    VoiceEngine,
    VoiceSessionNotFoundError,
    get_voice_engine,
)

router = APIRouter(prefix="/api/v1", tags=["voice"])

logger = logging.getLogger(__name__)


class VoiceDisclosureRequest(SensitiveBaseModel):
    """Automation-disclosure confirmation (safe)."""

    model_config = ConfigDict(extra="forbid")

    granted: bool = True


class VoiceHumanHandoffRequest(SensitiveBaseModel):
    """Explicit human escalation (safe)."""

    model_config = ConfigDict(extra="forbid")

    reason: str
    checkpoint_kind: Optional[str] = None


class VoiceObservationRequest(SensitiveBaseModel):
    """Generic voice observation for the Issue #8 recovery engine (safe)."""

    model_config = ConfigDict(extra="forbid")

    observation_type: str
    reason: Optional[str] = None
    safe_context: dict[str, Any] = Field(default_factory=dict)


def _engine_dep() -> VoiceEngine:
    return get_voice_engine()


@router.post(
    "/voice/handoffs",
    response_model=VoiceSession,
    summary="Prepare a voice session from a safe phone-handoff context",
)
async def prepare_handoff(
    payload: PhoneHandoffContext,
    engine: VoiceEngine = Depends(_engine_dep),
    settings: Settings = Depends(get_settings),
) -> VoiceSession:
    request_id = uuid.uuid4().hex
    set_log_context(request_id=request_id, workflow=WORKFLOW_NAME, workflow_stage="prepare_handoff")
    try:
        run_config(
            settings,
            request_id=request_id,
            workflow=WORKFLOW_NAME,
            workflow_stage="prepare_handoff",
            extra_metadata={
                "registry_id": payload.registry_id,
                "distinct_rate_source_id": payload.distinct_rate_source_id,
                "target_channel": payload.target_channel.value,
            },
        )
        return engine.prepare_handoff(payload)
    finally:
        clear_log_context()


@router.get(
    "/voice/sessions/{voice_session_id}",
    response_model=VoiceSession,
    summary="Get one voice session (safe metadata)",
)
async def get_session(
    voice_session_id: str,
    engine: VoiceEngine = Depends(_engine_dep),
) -> VoiceSession:
    try:
        return engine.get(voice_session_id)
    except VoiceSessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="voice session not found") from exc


@router.get(
    "/voice/summaries",
    response_model=list[VoiceRouteSummary],
    summary="Safe per-route voice summaries for one intake session (orchestrator)",
)
async def list_summaries(
    intake_session_id: str,
    engine: VoiceEngine = Depends(_engine_dep),
) -> list[VoiceRouteSummary]:
    return engine.route_summaries(intake_session_id)


@router.post(
    "/voice/sessions/{voice_session_id}/disclosure",
    response_model=VoiceSession,
    summary="Confirm automation disclosure before any substantive interaction",
)
async def disclose_automation(
    voice_session_id: str,
    payload: VoiceDisclosureRequest,
    engine: VoiceEngine = Depends(_engine_dep),
) -> VoiceSession:
    try:
        return engine.disclose_automation(voice_session_id, granted=payload.granted)
    except VoiceSessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="voice session not found") from exc


@router.post(
    "/voice/sessions/{voice_session_id}/events",
    response_model=VoiceDecision,
    summary="Process one structured broker question",
)
async def receive_event(
    voice_session_id: str,
    payload: BrokerQuestion,
    engine: VoiceEngine = Depends(_engine_dep),
    settings: Settings = Depends(get_settings),
) -> VoiceDecision:
    request_id = uuid.uuid4().hex
    set_log_context(request_id=request_id, workflow=WORKFLOW_NAME, workflow_stage="receive_event")
    try:
        run_config(
            settings,
            request_id=request_id,
            workflow=WORKFLOW_NAME,
            workflow_stage="receive_event",
            extra_metadata={
                "voice_session_id": voice_session_id,
                "broker_question_kind": payload.kind.value,
                "canonical_path": payload.canonical_path,
            },
        )
        return engine.receive_broker_event(voice_session_id, payload)
    except VoiceSessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="voice session not found") from exc
    finally:
        clear_log_context()


@router.post(
    "/voice/sessions/{voice_session_id}/resume",
    response_model=VoiceDecision,
    summary="Resume after Issue #5 answered a paused field/consent",
)
async def resume_session(
    voice_session_id: str,
    engine: VoiceEngine = Depends(_engine_dep),
) -> VoiceDecision:
    try:
        return engine.resume(voice_session_id)
    except VoiceSessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="voice session not found") from exc


@router.post(
    "/voice/sessions/{voice_session_id}/pause",
    response_model=VoiceSession,
    summary="Route-local pause (only this route pauses; idempotent)",
)
async def pause_session(
    voice_session_id: str,
    engine: VoiceEngine = Depends(_engine_dep),
) -> VoiceSession:
    try:
        return engine.pause(voice_session_id)
    except VoiceSessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="voice session not found") from exc


@router.post(
    "/voice/sessions/{voice_session_id}/human-handoff",
    response_model=VoiceSession,
    summary="Explicitly escalate to a human",
)
async def human_handoff(
    voice_session_id: str,
    payload: VoiceHumanHandoffRequest,
    engine: VoiceEngine = Depends(_engine_dep),
) -> VoiceSession:
    try:
        return engine.transfer_to_human(
            voice_session_id,
            reason=payload.reason,
            checkpoint_kind=payload.checkpoint_kind,
        )
    except VoiceSessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="voice session not found") from exc


@router.post(
    "/voice/sessions/{voice_session_id}/observations",
    response_model=RecoveryDecision,
    summary="Push a voice observation to the Issue #8 recovery engine",
)
async def emit_observation(
    voice_session_id: str,
    payload: VoiceObservationRequest,
    engine: VoiceEngine = Depends(_engine_dep),
    settings: Settings = Depends(get_settings),
) -> RecoveryDecision:
    request_id = uuid.uuid4().hex
    set_log_context(request_id=request_id, workflow=WORKFLOW_NAME, workflow_stage="emit_observation")
    try:
        safe_ctx = sanitize_voice_context(payload.safe_context)
        run_config(
            settings,
            request_id=request_id,
            workflow=WORKFLOW_NAME,
            workflow_stage="emit_observation",
            extra_metadata={
                "voice_session_id": voice_session_id,
                "observation_type": payload.observation_type,
            },
        )
        return engine.emit_observation(
            voice_session_id,
            payload.observation_type,
            reason=payload.reason,
            extra_safe_context=safe_ctx,
        )
    except VoiceSessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="voice session not found") from exc
    finally:
        clear_log_context()
