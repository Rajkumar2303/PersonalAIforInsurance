"""Evidence, audit & trace models (Issue #10, Prompt 1).

The durable evidence/audit layer answers: "for every provider we attempted,
what happened, when, through which route, based on what observed evidence,
and how did we reach the resulting status?"

PRIVACY / BOUNDARY RULES (all models are ``SensitiveBaseModel``):
- SAFE METADATA ONLY: ids, canonical paths, counts, page signatures, sanitized
  URLs, typed safe payloads. NEVER applicant values, NEVER raw quote
  references (only ``reference_present`` + an opaque ``private_reference_handle``),
  NEVER raw screenshots/audio/transcripts, NEVER a full ``InsuranceProfile``.
- Evidence is append-oriented: historical facts are preserved; corrections
  create new records (never mutate old ones).
- ``quoted_comparable`` / ``quoted_non_comparable`` are NEVER assigned here
  (Issues #11/#12 own comparability).
- Quote/estimate amounts are stored precisely as ``Decimal`` (never float).

Payloads are TYPED (a discriminated union), never an unrestricted JSON
``dict[str, Any]`` dumping ground for PII. Unknown/extended events pass through
the privacy-safe ``SafeMetadataEvidence`` allowlist only.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import ConfigDict, Field, TypeAdapter, field_validator

from .insurance.base import SensitiveBaseModel
from .recovery import SourceChannel

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class EvidenceEventType(StrEnum):
    """Typed, provider-independent evidence event categories.

    Mapped from existing Issue #7/#8/#9 observation types where possible; never
    insurer-specific.
    """

    ROUTE_PLANNED = "route_planned"
    ATTEMPT_STARTED = "attempt_started"
    ATTEMPT_RESUMED = "attempt_resumed"
    PAGE_OBSERVED = "page_observed"
    FIELD_REQUIREMENT_OBSERVED = "field_requirement_observed"
    FIELD_INTERACTION_OBSERVED = "field_interaction_observed"
    CHECKPOINT_OBSERVED = "checkpoint_observed"
    BOT_PROTECTION_OBSERVED = "bot_protection_observed"
    BLOCKING_ACCESS_CONTROL_OBSERVED = "blocking_access_control_observed"
    VALIDATION_OBSERVED = "validation_observed"
    CALLBACK_OBSERVED = "callback_observed"
    BROWSER_QUOTE_OBSERVED = "browser_quote_observed"
    BROWSER_ESTIMATE_OBSERVED = "browser_estimate_observed"
    VOICE_SESSION_STARTED = "voice_session_started"
    VOICE_CHECKPOINT_OBSERVED = "voice_checkpoint_observed"
    VOICE_QUOTE_OBSERVED = "voice_quote_observed"
    VOICE_ESTIMATE_OBSERVED = "voice_estimate_observed"
    EXPLICIT_INELIGIBILITY_OBSERVED = "explicit_ineligibility_observed"
    AFFINITY_RESTRICTED_OBSERVED = "affinity_restricted_observed"
    SPECIALTY_ONLY_OBSERVED = "specialty_only_observed"
    NOT_CURRENTLY_WRITING_OBSERVED = "not_currently_writing_observed"
    UNAVAILABLE_OBSERVED = "unavailable_observed"
    RECOVERY_DECISION = "recovery_decision"
    CONSENT_EVENT = "consent_event"
    HUMAN_HANDOFF_REQUIRED = "human_handoff_required"
    ATTEMPT_COMPLETED = "attempt_completed"
    ATTEMPT_FAILED = "attempt_failed"
    QUOTE_OBSERVATION = "quote_observation"


class AuditEventName(StrEnum):
    """Operational audit event names (distinct from business evidence)."""

    CONSENT_GRANTED = "consent_granted"
    CONSENT_REVOKED = "consent_revoked"
    ROUTE_PLAN_CREATED = "route_plan_created"
    ATTEMPT_STARTED = "attempt_started"
    ATTEMPT_RESUMED = "attempt_resumed"
    HUMAN_CHECKPOINT = "human_checkpoint"
    RECOVERY_DECISION = "recovery_decision"
    ATTEMPT_TERMINALIZED = "attempt_terminalized"
    EVIDENCE_WRITE_FAILED = "evidence_write_failed"
    EXPORT_CREATED = "export_created"


# Allowlist for generic safe-metadata extension keys (never applicant values).
_EVIDENCE_SAFE_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "page_signature",
        "checkpoint_type",
        "error_type",
        "reason_code",
        "barrier_kind",
        "visible_challenge",
        "service_hours_restricted",
        "temporary_service_unavailable",
        "route_status",
        "config_version",
        "registry_snapshot_ref",
    }
)


# ---------------------------------------------------------------------------
# Typed evidence payloads (discriminated union on ``kind``)
# ---------------------------------------------------------------------------


class EvidencePayloadBase(SensitiveBaseModel):
    """Base for every typed safe evidence payload."""

    model_config = ConfigDict(extra="forbid")

    kind: str


class PageObservationEvidence(EvidencePayloadBase):
    """Safe snapshot of one inspected page (no values, no DOM)."""

    kind: Literal["page_observation"] = "page_observation"
    page_signature: Optional[str] = None
    safe_url: Optional[str] = None  # sanitized (no query/fragment/tokens)
    controls_count: int = 0
    interactives_count: int = 0
    bot_protection_present: bool = False  # passive badge, NOT a blocker
    access_control_detected: bool = False
    visible_challenge: bool = False


class BarrierEvidence(EvidencePayloadBase):
    """A blocking barrier (bot/access-control/validation/auth) - keeps the
    generic distinction from the Sonnet discovery (bot_protection_present vs
    access_control_detected) separate; never collapses them."""

    kind: Literal["barrier"] = "barrier"
    barrier_kind: str  # bot_protection | access_control | validation | authentication
    bot_protection_present: bool = False
    access_control_detected: bool = False
    visible_challenge: bool = False
    reason_code: Optional[str] = None  # safe reason enum value
    message: Optional[str] = None  # safe text only


class FieldRequirementEvidence(EvidencePayloadBase):
    """A provider asked for a canonical field (path only, NEVER a value)."""

    kind: Literal["field_requirement"] = "field_requirement"
    canonical_path: Optional[str] = None
    required: bool = True
    external_field_id: Optional[str] = None  # safe provider-side id only
    page_signature: Optional[str] = None


class FieldInteractionEvidence(EvidencePayloadBase):
    """A safe field interaction (path + transformation + outcome, NEVER value)."""

    kind: Literal["field_interaction"] = "field_interaction"
    canonical_path: Optional[str] = None
    transformation: Optional[str] = None  # e.g. "collection_length"
    interaction_type: str  # filled | requested | observed_missing | consented | revealed
    success: bool = True
    page_signature: Optional[str] = None
    # Browser-action logging extension: action category + status so per-action
    # navigate/fill/select/click/pause/extract events are preserved redacted.
    action: str = "fill"  # navigate | fill | select | click | pause | extract
    status: str = "success"  # success | failure | paused | blocked | skipped


class CheckpointEvidence(EvidencePayloadBase):
    """A human checkpoint - decision category + type, NEVER a DOM/text dump."""

    kind: Literal["checkpoint"] = "checkpoint"
    checkpoint_type: str
    automation_decision: str  # automate | escalate | prohibited
    must_not_automate: bool = False
    requires_human: bool = True


class QuoteObservationEvidence(EvidencePayloadBase):
    """A structured quote/estimate observation (precise Decimal amounts).

    Firm-vs-estimate preserved; estimate is never upgraded to firm here.
    Raw references stay private (only reference_present + opaque handle).
    ``coverage_observations``/``discount_observations`` carry SAFE provider-
    presentable label segments (never DOM, never PII) so Issue #11 can map raw
    wording onto the canonical coverage ledger.
    """

    kind: Literal["quote_observation"] = "quote_observation"
    provider: Optional[str] = None  # registry_id
    presented_carrier: Optional[str] = None  # safe carrier/brand label
    annual_premium: Optional[Decimal] = None
    monthly_premium: Optional[Decimal] = None
    currency: Optional[str] = None
    firm_vs_estimate: str = "firm"  # "firm" | "estimate"
    reference_present: bool = False
    private_reference_handle: Optional[str] = None  # opaque, hashed
    coverage_raw_present: bool = False
    coverage_observations: list[str] = Field(default_factory=list)  # safe labels
    discount_observations: list[str] = Field(default_factory=list)  # safe labels
    quote_pending_normalization: bool = False


class VoiceObservationEvidence(EvidencePayloadBase):
    """A safe voice observation (kind/path/statuses; NO recording/transcript)."""

    kind: Literal["voice_observation"] = "voice_observation"
    voice_observation_type: Optional[str] = None
    canonical_path: Optional[str] = None
    checkpoint_kind: Optional[str] = None
    route_status: Optional[str] = None
    lifecycle_status: Optional[str] = None
    recording_consent: str = "not_requested"
    transcription_consent: str = "not_requested"


class RecoveryEvidence(EvidencePayloadBase):
    """What Issue #8 DECIDED (records it; never duplicates decision logic)."""

    kind: Literal["recovery"] = "recovery"
    observation_type: Optional[str] = None
    execution_result_kind: Optional[str] = None
    lifecycle_status: Optional[str] = None
    recommended_action: Optional[str] = None
    retryability: Optional[str] = None
    reason_codes: list[str] = Field(default_factory=list)
    terminal_status: Optional[str] = None
    quote_pending_normalization: bool = False
    policy_version: Optional[str] = None
    retry_allowed: bool = False


class ConsentEvidence(EvidencePayloadBase):
    """Proof of consent state at disclosure time (paths only, no values)."""

    kind: Literal["consent"] = "consent"
    consent_receipt_id: Optional[str] = None
    scope: Optional[str] = None
    canonical_paths: list[str] = Field(default_factory=list)
    state: str  # granted | revoked | undecided
    route_registry_id: Optional[str] = None


class RoutePlanEvidence(EvidencePayloadBase):
    """Safe summary of a route plan creation."""

    kind: Literal["route_plan"] = "route_plan"
    insurance_type: Optional[str] = None
    planned_route_count: int = 0
    ready_count: int = 0
    blocked_count: int = 0


class AttemptEvidence(EvidencePayloadBase):
    """Safe metadata for attempt_started / completed / failed events."""

    kind: Literal["attempt"] = "attempt"
    channel: Optional[str] = None
    attempt_number: int = 1
    lifecycle_status: Optional[str] = None
    policy_version: Optional[str] = None
    plan_version: Optional[str] = None


class SafeMetadataEvidence(EvidencePayloadBase):
    """Generic extension payload - allowlisted safe keys only (never values)."""

    kind: Literal["safe_metadata"] = "safe_metadata"
    safe_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("safe_metadata")
    @classmethod
    def _enforce_allowlist(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Reject any key outside the safe-metadata allowlist (never values)."""
        disallowed = sorted(set(value) - _EVIDENCE_SAFE_METADATA_KEYS)
        if disallowed:
            raise ValueError(
                f"unsafe metadata keys not allowed in evidence payload: {disallowed}"
            )
        return value


EvidencePayload = Annotated[
    Union[
        PageObservationEvidence,
        BarrierEvidence,
        FieldRequirementEvidence,
        FieldInteractionEvidence,
        CheckpointEvidence,
        QuoteObservationEvidence,
        VoiceObservationEvidence,
        RecoveryEvidence,
        ConsentEvidence,
        RoutePlanEvidence,
        AttemptEvidence,
        SafeMetadataEvidence,
    ],
    Field(discriminator="kind"),
]

EVIDENCE_PAYLOAD_ADAPTER: TypeAdapter[EvidencePayload] = TypeAdapter(EvidencePayload)


def validate_evidence_payload(data: Any) -> EvidencePayload:
    """Validate/parse a payload dict into a typed safe payload."""
    return EVIDENCE_PAYLOAD_ADAPTER.validate_python(data)


# ---------------------------------------------------------------------------
# Attachment metadata (metadata only - NEVER raw screenshot/audio bytes)
# ---------------------------------------------------------------------------


class EvidenceAttachmentMetadata(SensitiveBaseModel):
    """Safe metadata reference to an optional evidence attachment.

    ``screenshots_enabled`` defaults to False; raw bytes are never stored in
    the DB. Tests use synthetic attachments only.
    """

    model_config = ConfigDict(extra="forbid")

    attachment_id: str
    kind: str = "document"  # screenshot | document | export | other
    content_type: Optional[str] = None
    size_bytes: int = 0
    sha256: Optional[str] = None  # hash of the external artifact
    redacted: bool = True
    safe_reference: Optional[str] = None  # opaque external reference
    created_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))


# ---------------------------------------------------------------------------
# Core evidence record
# ---------------------------------------------------------------------------


class EvidenceRecord(SensitiveBaseModel):
    """One append-only, typed evidence record (SAFE metadata only)."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    event_type: EvidenceEventType
    observed_at: dt.datetime
    created_at: dt.datetime
    sequence: int = 1  # attempt-local monotonic ordering
    intake_session_id: Optional[str] = None  # ownership / retention scope
    plan_id: Optional[str] = None
    planned_route_id: Optional[str] = None
    registry_id: Optional[str] = None
    distinct_rate_source_id: Optional[str] = None
    attempt_id: Optional[str] = None
    parent_attempt_id: Optional[str] = None
    source_channel: SourceChannel = SourceChannel.MANUAL
    source_session_id: Optional[str] = None  # browser or voice session id
    page_signature: Optional[str] = None
    safe_url: Optional[str] = None  # sanitized (no query/fragment/tokens)
    observation_type: Optional[str] = None  # e.g. BrowserObservationType value
    reason_code: Optional[str] = None  # safe reason enum value
    evidence_source: str = "evidence_service"
    payload_version: int = 1
    payload: EvidencePayload
    content_hash: str
    idempotency_key: str
    quote_observation_id: Optional[str] = None
    registry_snapshot_ref: Optional[str] = None  # registry version lineage
    config_version: Optional[int] = None  # browser adapter config lineage
    attachments: list[EvidenceAttachmentMetadata] = Field(default_factory=list)


class QuoteObservation(SensitiveBaseModel):
    """One typed quote/estimate result (one-to-many from an attempt)."""

    model_config = ConfigDict(extra="forbid")

    quote_id: str
    intake_session_id: str
    attempt_id: Optional[str] = None
    parent_attempt_id: Optional[str] = None
    plan_id: Optional[str] = None
    planned_route_id: Optional[str] = None
    registry_id: Optional[str] = None
    distinct_rate_source_id: Optional[str] = None
    aggregator_registry_id: Optional[str] = None  # aggregator route (multi-result)
    presented_carrier: Optional[str] = None  # presented carrier/brand
    observed_at: dt.datetime
    annual_premium: Optional[Decimal] = None
    monthly_premium: Optional[Decimal] = None
    currency: Optional[str] = None
    firm_vs_estimate: str = "firm"  # firm | estimate
    reference_present: bool = False
    private_reference_handle: Optional[str] = None  # opaque, hashed
    coverage_raw_present: bool = False
    coverage_observations: list[str] = Field(default_factory=list)  # safe labels
    discount_observations: list[str] = Field(default_factory=list)  # safe labels
    quote_pending_normalization: bool = False
    sequence: int = 1
    content_hash: str
    idempotency_key: str
    created_at: dt.datetime


class AuditEvent(SensitiveBaseModel):
    """One operational audit event (privacy-safe metadata only)."""

    model_config = ConfigDict(extra="forbid")

    audit_id: str
    intake_session_id: Optional[str] = None
    event_name: AuditEventName
    occurred_at: dt.datetime
    actor: str = "system"  # system | applicant | human
    safe_metadata: dict[str, Any] = Field(default_factory=dict)  # allowlisted
    content_hash: str
    idempotency_key: str


# ---------------------------------------------------------------------------
# API-safe view models (never expose ORM/internal/persisted blobs)
# ---------------------------------------------------------------------------


class EvidenceRecordView(SensitiveBaseModel):
    """Safe API projection of an evidence record."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    event_type: str
    observed_at: dt.datetime
    sequence: int
    intake_session_id: Optional[str] = None
    plan_id: Optional[str] = None
    planned_route_id: Optional[str] = None
    registry_id: Optional[str] = None
    distinct_rate_source_id: Optional[str] = None
    attempt_id: Optional[str] = None
    parent_attempt_id: Optional[str] = None
    source_channel: str
    source_session_id: Optional[str] = None
    page_signature: Optional[str] = None
    safe_url: Optional[str] = None
    observation_type: Optional[str] = None
    reason_code: Optional[str] = None
    evidence_source: str
    payload_version: int
    payload_kind: str
    payload: dict[str, Any]
    content_hash: str


class QuoteObservationView(SensitiveBaseModel):
    """Safe API projection of a quote observation."""

    model_config = ConfigDict(extra="forbid")

    quote_id: str
    attempt_id: Optional[str] = None
    parent_attempt_id: Optional[str] = None
    registry_id: Optional[str] = None
    distinct_rate_source_id: Optional[str] = None
    aggregator_registry_id: Optional[str] = None
    presented_carrier: Optional[str] = None
    observed_at: dt.datetime
    annual_premium: Optional[str] = None  # Decimal serialized as string
    monthly_premium: Optional[str] = None
    currency: Optional[str] = None
    firm_vs_estimate: str
    reference_present: bool
    coverage_raw_present: bool
    coverage_observations: list[str] = Field(default_factory=list)
    discount_observations: list[str] = Field(default_factory=list)
    quote_pending_normalization: bool
    sequence: int
    content_hash: str


class AuditEventView(SensitiveBaseModel):
    """Safe API projection of an audit event."""

    model_config = ConfigDict(extra="forbid")

    audit_id: str
    intake_session_id: Optional[str] = None
    event_name: str
    occurred_at: dt.datetime
    actor: str
    safe_metadata: dict[str, Any]
    content_hash: str


class EvidenceExportView(SensitiveBaseModel):
    """Safe, PII-free JSON export for future submission/demo use."""

    model_config = ConfigDict(extra="forbid")

    intake_session_id: str
    exported_at: dt.datetime
    evidence_count: int
    quote_count: int
    audit_event_count: int
    distinct_plans: list[str] = Field(default_factory=list)
    distinct_routes: list[str] = Field(default_factory=list)
    distinct_attempts: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRecordView] = Field(default_factory=list)
    quotes: list[QuoteObservationView] = Field(default_factory=list)
    audit_events: list[AuditEventView] = Field(default_factory=list)


# Safe-metadata allowlist helper (used by EvidenceService for extensions).
def sanitize_evidence_safe_metadata(ctx: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Return only allowlisted safe metadata keys (never applicant values)."""
    if not ctx:
        return {}
    return {k: v for k, v in ctx.items() if k in _EVIDENCE_SAFE_METADATA_KEYS}
