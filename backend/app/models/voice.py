"""Voice / phone context handoff models (Issue #9, Prompt 1).

A provider-agnostic, deterministic voice/phone handoff layer. NO real calls,
NO LLM, NO transcription/recording, NO second applicant-information store.
The engine answers broker questions JIT from the Issue #5 intake vault (via
``IntakeValueSource``) and pushes every outcome through the Issue #8
``RecoveryEngine`` as an ``ExecutionObservation`` with
``source_channel=VOICE``.

PRIVACY RULE: every model here carries SAFE metadata only (ids, canonical
paths, status strings, counts, public route metadata). Applicant values are
retrieved JIT, spoken through the transport boundary, then discarded - they
are never cached in a session, decision, graph state, or trace.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Optional, TypedDict

from pydantic import ConfigDict, Field

from .insurance.base import SensitiveBaseModel
from .recovery import SourceChannel

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class VoiceLifecycleStatus(StrEnum):
    """Voice-session lifecycle (distinct from Issue #8 AttemptLifecycleStatus).

    ``prepared`` -> ``awaiting_disclosure`` -> ``active`` -> paused / human ->
    ``completed`` | ``terminated``.
    """

    PREPARED = "prepared"
    AWAITING_DISCLOSURE = "awaiting_disclosure"
    ACTIVE = "active"
    PAUSED_FOR_APPLICANT = "paused_for_applicant"
    PAUSED_FOR_CONSENT = "paused_for_consent"
    AWAITING_HUMAN = "awaiting_human"
    COMPLETED = "completed"
    TERMINATED = "terminated"


class DisclosureStatus(StrEnum):
    """Automation-disclosure state. Disclosure is mandatory BEFORE any
    substantive value is spoken to the broker."""

    NOT_DISCLOSED = "not_disclosed"
    DISCLOSED = "disclosed"
    REFUSED = "refused"


class RecordingConsentStatus(StrEnum):
    """Recording consent (Prompt 1 never records; the status is explicit and
    defaults to ``not_requested`` so the applicant can never be silently
    recorded)."""

    NOT_REQUESTED = "not_requested"
    GRANTED = "granted"
    DENIED = "denied"


class TranscriptionConsentStatus(StrEnum):
    """Transcription consent (Prompt 1 never transcribes; same explicit rule)."""

    NOT_REQUESTED = "not_requested"
    GRANTED = "granted"
    DENIED = "denied"


class VoiceResponseAction(StrEnum):
    """What the voice layer should do next (safe, deterministic)."""

    SPEAK_DISCLOSURE = "speak_disclosure"
    DISCLOSE_VALUE = "disclose_value"
    REQUEST_CONSENT = "request_consent"
    PAUSE_FOR_APPLICANT = "pause_for_applicant"
    TRANSFER_TO_APPLICANT = "transfer_to_applicant"
    TRANSFER_TO_HUMAN = "transfer_to_human"
    ACKNOWLEDGE = "acknowledge"
    END_QUOTE = "end_quote"
    END_TERMINAL = "end_terminal"
    MANUAL_HANDOFF = "manual_handoff"
    CALLBACK_SCHEDULED = "callback_scheduled"


class VoiceRouteStatus(StrEnum):
    """Safe route-local status for the frontend / future orchestrator (Issue #9,
    Prompt 2). One stable contract derived deterministically from a
    ``VoiceSession``'s lifecycle + terminal status - never applicant values.

    These are the states the dashboard/orchestrator will need later; they map
    onto Issue #8 terminal semantics where applicable (never duplicating
    ``RouteOutcomeStatus``): ``callback_scheduled`` ~ ``callback_required``,
    ``estimate_only`` and ``manual_handoff`` reuse the same names.
    """

    PREPARED = "prepared"
    RUNNING = "running"
    PAUSED_MISSING_INFORMATION = "paused_missing_information"
    APPLICANT_REQUIRED = "applicant_required"
    MANUAL_HANDOFF = "manual_handoff"
    CALLBACK_SCHEDULED = "callback_scheduled"
    QUOTE_PENDING_NORMALIZATION = "quote_pending_normalization"
    ESTIMATE_ONLY = "estimate_only"
    COMPLETED = "completed"
    FAILED = "failed"


class BrokerQuestionKind(StrEnum):
    """Structured classification of one broker statement/question.

    The automation NEVER answers identity, declaration, advice, or
    applicant-required items - those are always transferred to the applicant
    or a human. Unknown items pause for manual mapping (never guessed).
    """

    CANONICAL_FIELD = "canonical_field"
    COLLECTION_LENGTH = "collection_length"
    HOUSEHOLD_DRIVER = "household_driver"
    IDENTITY_CHECKPOINT = "identity_checkpoint"
    CONSENT_EXPANSION = "consent_expansion"
    DECLARATION = "declaration"
    ADVICE_REQUEST = "advice_request"
    CALLBACK_REQUEST = "callback_request"
    QUOTE_DISCLOSURE = "quote_disclosure"
    ESTIMATE_DISCLOSURE = "estimate_disclosure"
    INELIGIBILITY = "ineligibility"
    AFFINITY_RESTRICTION = "affinity_restriction"
    SPECIALTY_ONLY = "specialty_only"
    NOT_CURRENTLY_WRITING = "not_currently_writing"
    APPLICANT_REQUIRED = "applicant_required"
    MANUAL_REVIEW = "manual_review"
    BROKER_UNAVAILABLE = "broker_unavailable"
    COMPLETED_WITHOUT_QUOTE = "completed_without_quote"
    UNKNOWN = "unknown"


class VoiceObservationType(StrEnum):
    """Voice observation types emitted to the Issue #8 recovery engine.

    Several of these are already rows in the recovery classification table
    (``explicit_ineligible``, ``affinity_restricted``, ``specialty_only``,
    ``not_currently_writing``, ``technical_error``, ``completed_without_quote``)
    and are reused as-is; the voice-specific ones are added there by Issue #9.
    """

    PHONE_QUOTE_OBSERVED = "phone_quote_observed"
    PHONE_ESTIMATE_OBSERVED = "phone_estimate_observed"
    CALLBACK_SCHEDULED = "callback_scheduled"
    BROKER_REQUIRES_FIELD = "broker_requires_field"
    APPLICANT_REQUIRED = "applicant_required"
    MANUAL_REVIEW_REQUIRED = "manual_review_required"
    EXPLICIT_INELIGIBLE = "explicit_ineligible"
    AFFINITY_RESTRICTED = "affinity_restricted"
    SPECIALTY_ONLY = "specialty_only"
    NOT_CURRENTLY_WRITING = "not_currently_writing"
    PHONE_UNREACHABLE = "phone_unreachable"
    TECHNICAL_ERROR = "technical_error"
    COMPLETED_WITHOUT_QUOTE = "completed_without_quote"
    UNKNOWN_BROKER_QUESTION = "unknown_broker_question"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class BrokerQuestion(SensitiveBaseModel):
    """One structured broker question/statement (already interpreted).

    ``raw_safe_text`` is a normalized, GENERIC wording (e.g. "postal code") -
    never applicant data, never raw transcript audio/text with PII.
    """

    model_config = ConfigDict(extra="forbid")

    kind: BrokerQuestionKind = BrokerQuestionKind.CANONICAL_FIELD
    canonical_path: Optional[str] = None
    raw_safe_text: Optional[str] = None
    is_identity_checkpoint: bool = False
    is_advice_request: bool = False
    is_household_driver: bool = False
    requires_applicant: bool = False
    mapping_confidence: float = 1.0
    safe_context: dict[str, Any] = Field(default_factory=dict)


class VoiceSession(SensitiveBaseModel):
    """A prepared/active voice session - SAFE METADATA ONLY.

    Never stores applicant values. ``provider_phone_route`` is the PUBLIC
    provider phone published in the Ontario market registry (safe market
    metadata per Issue #6) - never the applicant's phone.
    """

    model_config = ConfigDict(extra="forbid")

    voice_session_id: str
    intake_session_id: str
    registry_id: str
    distinct_rate_source_id: Optional[str] = None
    planned_route_id: Optional[str] = None
    source_attempt_id: Optional[str] = None
    recovery_attempt_id: Optional[str] = None  # this voice session's own Issue #8 attempt
    source_channel: SourceChannel = SourceChannel.VOICE
    target_channel: SourceChannel = SourceChannel.PHONE
    lifecycle_status: str = VoiceLifecycleStatus.PREPARED
    disclosure_status: str = DisclosureStatus.NOT_DISCLOSED
    recording_consent: str = RecordingConsentStatus.NOT_REQUESTED
    transcription_consent: str = TranscriptionConsentStatus.NOT_REQUESTED
    pending_field_paths: list[str] = Field(default_factory=list)
    pending_checkpoint: Optional[str] = None
    callback_reason: Optional[str] = None
    provider_phone_route: Optional[str] = None  # PUBLIC provider phone (safe)
    authorized_canonical_paths: list[str] = Field(default_factory=list)
    reference_present: bool = False
    private_reference_handle: Optional[str] = None  # sha256 hexdigest[:16]
    terminal_status: Optional[str] = None
    attempt_history: list[dict[str, Any]] = Field(default_factory=list)
    # Prompt 2: unattended-execution counters + route-local status (safe).
    automated_answers: int = 0
    applicant_interruptions: int = 0
    quote_pending_normalization: bool = False
    route_status: str = VoiceRouteStatus.PREPARED
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class VoiceDecision(SensitiveBaseModel):
    """What the voice layer should do next (safe - never the value itself)."""

    model_config = ConfigDict(extra="forbid")

    voice_session_id: str
    lifecycle_status: str
    disclosure_status: str
    action: str
    canonical_path: Optional[str] = None
    value_present: bool = False
    checkpoint_kind: Optional[str] = None
    reason_code: Optional[str] = None
    requires_consent_expansion: bool = False
    recording_consent: str = RecordingConsentStatus.NOT_REQUESTED
    transcription_consent: str = TranscriptionConsentStatus.NOT_REQUESTED
    safe_reference_handle: Optional[str] = None
    message: Optional[str] = None  # generic, safe explanation


class PhoneHandoffContext(SensitiveBaseModel):
    """Safe input describing a phone/voice handoff (ids + route metadata).

    ``provider_phone_route`` and ``callback_reason`` are public route metadata;
    authorized/missing canonical paths describe WHICH Issue #5 fields may be
    disclosed - never the values.
    """

    model_config = ConfigDict(extra="forbid")

    intake_session_id: str
    registry_id: str
    distinct_rate_source_id: Optional[str] = None
    planned_route_id: Optional[str] = None
    source_attempt_id: Optional[str] = None
    source_channel: SourceChannel = SourceChannel.BROWSER
    target_channel: SourceChannel = SourceChannel.PHONE
    callback_reason: Optional[str] = None
    provider_phone_route: Optional[str] = None  # PUBLIC provider phone (safe)
    authorized_canonical_paths: list[str] = Field(default_factory=list)
    missing_canonical_paths: list[str] = Field(default_factory=list)
    reference_present: bool = False
    private_reference_handle: Optional[str] = None  # sha256 hexdigest[:16]
    recovery_reason_codes: list[str] = Field(default_factory=list)
    route_consent_state: Optional[str] = None  # "granted" | "undecided" | "denied"


class VoiceRouteSummary(SensitiveBaseModel):
    """Safe per-route summary for the future orchestrator / frontend.

    Enumerates every route session for an intake session with its route-local
    status and batchable pending fields, so a missing field on one route never
    blocks another (Prompt 2). No applicant values.
    """

    model_config = ConfigDict(extra="forbid")

    voice_session_id: str
    registry_id: str
    distinct_rate_source_id: Optional[str] = None
    planned_route_id: Optional[str] = None
    recovery_attempt_id: Optional[str] = None
    lifecycle_status: str
    route_status: str
    terminal_status: Optional[str] = None
    quote_pending_normalization: bool = False
    pending_field_paths: list[str] = Field(default_factory=list)
    automated_answers: int = 0
    applicant_interruptions: int = 0


# ---------------------------------------------------------------------------
# Safe-context allowlist + sanitizer (mirrors Issue #8 recovery rules)
# ---------------------------------------------------------------------------

# Only these keys may flow into LangSmith-traced state / API requests.
_VOICE_SAFE_CONTEXT_KEYS: frozenset[str] = frozenset(
    {
        "voice_session_id",
        "registry_id",
        "distinct_rate_source_id",
        "planned_route_id",
        "canonical_path",
        "missing_field_paths",
        "pending_field_paths",
        "checkpoint_kind",
        "reason_code",
        "quote_present",
        "is_firm_quote",
        "reference_present",
        "estimate_only",
        "private_reference_handle",
        "consent_state",
        "recording_consent",
        "transcription_consent",
        "route_type",
        "error_type",
    }
)


def sanitize_voice_context(ctx: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Return only allowlisted safe metadata from a caller-provided context.

    Anything else (e.g. a nested raw answer) is dropped so traces can never
    accidentally include applicant values.
    """
    if not ctx:
        return {}
    return {k: v for k, v in ctx.items() if k in _VOICE_SAFE_CONTEXT_KEYS}


# Human-checkpoint kinds whose route status is ``applicant_required`` (the
# automation must NOT answer; the applicant decides). Everything else under
# ``awaiting_human`` is ``manual_handoff`` (manual mapping / review).
_APPLICANT_REQUIRED_CHECKPOINTS: frozenset[str] = frozenset(
    {
        "identity_checkpoint",
        "declaration",
        "advice_request",
        "applicant_required",
        "consent_attestation",
        "consent_expansion",
        "household_driver",
    }
)


def derive_voice_route_status(session: VoiceSession) -> VoiceRouteStatus:
    """Deterministically derive the route-local status from a voice session.

    Terminal status wins (callback / estimate / manual / failed), then quote
    pending normalization, then lifecycle. Used to expose one stable safe state
    to the frontend / future orchestrator without duplicating Issue #8 enums.
    """
    terminal = session.terminal_status
    if terminal == "callback_required":
        return VoiceRouteStatus.CALLBACK_SCHEDULED
    if terminal == "estimate_only":
        return VoiceRouteStatus.ESTIMATE_ONLY
    if terminal == "manual_handoff":
        return VoiceRouteStatus.MANUAL_HANDOFF
    if terminal is not None:
        return VoiceRouteStatus.FAILED
    if session.quote_pending_normalization:
        return VoiceRouteStatus.QUOTE_PENDING_NORMALIZATION
    lifecycle = session.lifecycle_status
    if lifecycle == VoiceLifecycleStatus.COMPLETED:
        return VoiceRouteStatus.COMPLETED
    if lifecycle == VoiceLifecycleStatus.TERMINATED:
        return VoiceRouteStatus.FAILED
    if lifecycle in (VoiceLifecycleStatus.PAUSED_FOR_APPLICANT, VoiceLifecycleStatus.PAUSED_FOR_CONSENT):
        return VoiceRouteStatus.PAUSED_MISSING_INFORMATION
    if lifecycle == VoiceLifecycleStatus.AWAITING_HUMAN:
        if session.pending_checkpoint in _APPLICANT_REQUIRED_CHECKPOINTS:
            return VoiceRouteStatus.APPLICANT_REQUIRED
        return VoiceRouteStatus.MANUAL_HANDOFF
    if lifecycle in (VoiceLifecycleStatus.AWAITING_DISCLOSURE, VoiceLifecycleStatus.ACTIVE):
        return VoiceRouteStatus.RUNNING
    return VoiceRouteStatus.PREPARED


class VoiceWorkflowState(TypedDict, total=False):
    """LangGraph state for the voice workflow - SAFE METADATA ONLY.

    Never contains applicant values; values flow JIT through the transport
    boundary inside the engine and are discarded.
    """

    entry: str
    request_id: Optional[str]
    voice_session_id: Optional[str]
    intake_session_id: Optional[str]
    registry_id: Optional[str]
    distinct_rate_source_id: Optional[str]
    planned_route_id: Optional[str]
    source_attempt_id: Optional[str]
    lifecycle_status: Optional[str]
    disclosure_status: Optional[str]
    action: Optional[str]
    canonical_path: Optional[str]
    checkpoint_kind: Optional[str]
    observation_type: Optional[str]
    reason: Optional[str]
    workflow_stage: Optional[str]
    workflow_status: Optional[str]
    safe_context: Optional[dict[str, Any]]

    # Voice-workflow specific inputs (all safe, structured models/dicts).
    handoff_context: Optional[Any]  # PhoneHandoffContext (safe metadata)
    disclosure_granted: Optional[bool]
    broker_question: Optional[dict[str, Any]]  # BrokerQuestion (safe fields)
    broker_question_kind: Optional[str]
    value_present: Optional[bool]
    route_status: Optional[str]

    # Issue #8 recovery observation outputs (safe metadata).
    recovery_recommended_action: Optional[str]
    recovery_lifecycle_status: Optional[str]
    recovery_terminal_status: Optional[str]
    quote_pending_normalization: Optional[bool]


from typing import TypedDict  # noqa: E402  (kept after class defs for clarity)
