"""Pure conversion adapters: existing Issue #5/#7/#8/#9 domain objects ->
safe evidence drafts (Issue #10, Prompt 1).

These are PURE functions (no I/O, no repository) that map browser / voice /
recovery / route-plan / consent observations onto the typed ``EvidenceDraft``
contract the ``EvidenceService`` persists. They never emit terminal
``quoted_comparable`` / ``quoted_non_comparable`` (Issues #11/#12 own those),
never carry applicant values, and keep raw quote references private
(``reference_present`` + opaque ``private_reference_handle`` only).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Optional

from ...models.browser.observation import (
    BrowserObservation,
    BrowserObservationType,
    RawQuoteObservation,
)
from ...models.evidence import (
    AttemptEvidence,
    BarrierEvidence,
    CheckpointEvidence,
    ConsentEvidence,
    EvidenceEventType,
    FieldInteractionEvidence,
    FieldRequirementEvidence,
    PageObservationEvidence,
    QuoteObservation,
    QuoteObservationEvidence,
    RecoveryEvidence,
    RoutePlanEvidence,
    SafeMetadataEvidence,
    VoiceObservationEvidence,
)
from ...models.recovery import RecoveryDecision, SourceChannel
from ...models.route_planner import RoutePlan
from ...models.voice import VoiceObservationType


class EvidenceDraft:
    """A not-yet-persisted evidence record spec (safe metadata only)."""

    __slots__ = (
        "event_type",
        "payload",
        "source_channel",
        "plan_id",
        "planned_route_id",
        "registry_id",
        "distinct_rate_source_id",
        "attempt_id",
        "parent_attempt_id",
        "source_session_id",
        "page_signature",
        "safe_url",
        "observation_type",
        "reason_code",
        "quote_observation_id",
        "registry_snapshot_ref",
        "config_version",
        "attachments",
        "evidence_source",
        "observed_at",
        "idempotency_scope",
    )

    def __init__(
        self,
        *,
        event_type: EvidenceEventType,
        payload,
        source_channel: SourceChannel = SourceChannel.MANUAL,
        plan_id: Optional[str] = None,
        planned_route_id: Optional[str] = None,
        registry_id: Optional[str] = None,
        distinct_rate_source_id: Optional[str] = None,
        attempt_id: Optional[str] = None,
        parent_attempt_id: Optional[str] = None,
        source_session_id: Optional[str] = None,
        page_signature: Optional[str] = None,
        safe_url: Optional[str] = None,
        observation_type: Optional[str] = None,
        reason_code: Optional[str] = None,
        quote_observation_id: Optional[str] = None,
        registry_snapshot_ref: Optional[str] = None,
        config_version: Optional[int] = None,
        attachments=None,
        evidence_source: str = "evidence_service",
        observed_at: Optional[dt.datetime] = None,
        idempotency_scope: Optional[str] = None,
    ) -> None:
        self.event_type = event_type
        self.payload = payload
        self.source_channel = source_channel
        self.plan_id = plan_id
        self.planned_route_id = planned_route_id
        self.registry_id = registry_id
        self.distinct_rate_source_id = distinct_rate_source_id
        self.attempt_id = attempt_id
        self.parent_attempt_id = parent_attempt_id
        self.source_session_id = source_session_id
        self.page_signature = page_signature
        self.safe_url = safe_url
        self.observation_type = observation_type
        self.reason_code = reason_code
        self.quote_observation_id = quote_observation_id
        self.registry_snapshot_ref = registry_snapshot_ref
        self.config_version = config_version
        self.attachments = attachments or []
        self.evidence_source = evidence_source
        self.observed_at = observed_at
        self.idempotency_scope = idempotency_scope


# ---------------------------------------------------------------------------
# Browser observation mapping (Issue #7 objects -> evidence)
# ---------------------------------------------------------------------------


def browser_event_type(observation_type: BrowserObservationType) -> EvidenceEventType:
    """Map a browser observation kind to the provider-independent event type."""
    mapping: dict[BrowserObservationType, EvidenceEventType] = {
        BrowserObservationType.PAGE_LOADED: EvidenceEventType.PAGE_OBSERVED,
        BrowserObservationType.FIELDS_FILLED: EvidenceEventType.FIELD_INTERACTION_OBSERVED,
        BrowserObservationType.NEEDS_FIELD: EvidenceEventType.FIELD_REQUIREMENT_OBSERVED,
        BrowserObservationType.NEEDS_CONSENT: EvidenceEventType.CHECKPOINT_OBSERVED,
        BrowserObservationType.HUMAN_CHECKPOINT: EvidenceEventType.CHECKPOINT_OBSERVED,
        BrowserObservationType.ACCESS_CONTROL_DETECTED: EvidenceEventType.BLOCKING_ACCESS_CONTROL_OBSERVED,
        BrowserObservationType.UNKNOWN_EXTERNAL_FIELD: EvidenceEventType.FIELD_REQUIREMENT_OBSERVED,
        BrowserObservationType.QUOTE_DETECTED: EvidenceEventType.BROWSER_QUOTE_OBSERVED,
        BrowserObservationType.CALLBACK_DETECTED: EvidenceEventType.CALLBACK_OBSERVED,
        BrowserObservationType.MANUAL_CONTACT_DETECTED: EvidenceEventType.HUMAN_HANDOFF_REQUIRED,
        BrowserObservationType.TECHNICAL_ERROR: EvidenceEventType.UNAVAILABLE_OBSERVED,
        BrowserObservationType.ROUTE_CHANGED: EvidenceEventType.PAGE_OBSERVED,
        BrowserObservationType.COMPLETE_WITHOUT_QUOTE: EvidenceEventType.UNAVAILABLE_OBSERVED,
        BrowserObservationType.UNSUPPORTED_PAGE: EvidenceEventType.UNAVAILABLE_OBSERVED,
        BrowserObservationType.VALUE_NOT_SUPPORTED: EvidenceEventType.FIELD_INTERACTION_OBSERVED,
        BrowserObservationType.VALIDATION_ERROR: EvidenceEventType.VALIDATION_OBSERVED,
        BrowserObservationType.AMBIGUOUS_FIELD: EvidenceEventType.VALIDATION_OBSERVED,
        BrowserObservationType.AMBIGUOUS_ACTION: EvidenceEventType.VALIDATION_OBSERVED,
    }
    return mapping.get(observation_type, EvidenceEventType.PAGE_OBSERVED)


def _browser_payload(observation: BrowserObservation):
    ot = observation.observation_type
    if ot is BrowserObservationType.PAGE_LOADED or ot is BrowserObservationType.ROUTE_CHANGED:
        return PageObservationEvidence(
            page_signature=observation.page_signature,
            safe_url=observation.url,
            controls_count=len(observation.fields),
            interactives_count=len(observation.unknown_field_observations)
            + len(observation.unknown_external_fields),
            bot_protection_present=bool(observation.checkpoint),
        )
    if ot is BrowserObservationType.ACCESS_CONTROL_DETECTED:
        return BarrierEvidence(
            barrier_kind="access_control",
            bot_protection_present=True,
            access_control_detected=True,
            visible_challenge=bool(observation.checkpoint and observation.checkpoint.requires_human),
            message=observation.message,
        )
    if ot is BrowserObservationType.NEEDS_CONSENT or ot is BrowserObservationType.HUMAN_CHECKPOINT:
        return CheckpointEvidence(
            checkpoint_type=(
                "consent"
                if ot is BrowserObservationType.NEEDS_CONSENT
                else (observation.checkpoint.checkpoint_type if observation.checkpoint else "human")
            ),
            automation_decision="escalate",
            must_not_automate=bool(observation.checkpoint and observation.checkpoint.must_not_automate),
            requires_human=bool(not observation.checkpoint or observation.checkpoint.requires_human),
        )
    if ot is BrowserObservationType.CALLBACK_DETECTED:
        return CheckpointEvidence(
            checkpoint_type="callback",
            automation_decision="escalate",
            requires_human=True,
        )
    if ot is BrowserObservationType.MANUAL_CONTACT_DETECTED:
        return CheckpointEvidence(
            checkpoint_type="manual_contact",
            automation_decision="escalate",
            requires_human=True,
        )
    if ot in (
        BrowserObservationType.NEEDS_FIELD,
        BrowserObservationType.UNKNOWN_EXTERNAL_FIELD,
    ):
        return FieldRequirementEvidence(
            canonical_path=(
                observation.missing_field_paths[0] if observation.missing_field_paths else None
            ),
            required=True,
            external_field_id=(
                observation.unknown_external_fields[0]
                if observation.unknown_external_fields
                else None
            ),
            page_signature=observation.page_signature,
        )
    if ot is BrowserObservationType.FIELDS_FILLED or ot is BrowserObservationType.VALUE_NOT_SUPPORTED:
        return FieldInteractionEvidence(
            canonical_path=None,
            interaction_type=(
                "filled" if ot is BrowserObservationType.FIELDS_FILLED else "unsupported_value"
            ),
            success=ot is BrowserObservationType.FIELDS_FILLED,
            page_signature=observation.page_signature,
        )
    if ot is BrowserObservationType.QUOTE_DETECTED:
        raw = observation.quote.raw if observation.quote else None
        return QuoteObservationEvidence(
            provider=raw.registry_id if raw else observation.page_signature,
            annual_premium=_quote_amount(raw.annual_amount_decimal, raw.annual_amount_parsed) if raw else None,
            monthly_premium=_quote_amount(raw.monthly_amount_decimal, raw.monthly_amount_parsed) if raw else None,
            currency=raw.currency if raw else None,
            firm_vs_estimate="firm" if (raw and raw.is_firm_quote) else "estimate",
            reference_present=bool(raw and raw.reference_present),
            private_reference_handle=raw.private_reference_handle if raw else None,
            coverage_raw_present=bool(raw and raw.coverage_observations),
            quote_pending_normalization=True,
        )
    if ot is BrowserObservationType.TECHNICAL_ERROR:
        return BarrierEvidence(barrier_kind="technical_error", message=observation.message)
    if ot in (
        BrowserObservationType.VALIDATION_ERROR,
        BrowserObservationType.AMBIGUOUS_FIELD,
        BrowserObservationType.AMBIGUOUS_ACTION,
    ):
        return SafeMetadataEvidence(
            safe_metadata={"reason_code": ot.value}
        )
    # COMPLETE_WITHOUT_QUOTE / UNSUPPORTED_PAGE
    return SafeMetadataEvidence(safe_metadata={"reason_code": ot.value})


def browser_draft_from_observation(
    intake_session_id: str,
    observation: BrowserObservation,
    *,
    browser_session_id: str,
    plan_id: str,
    planned_route_id: str,
    registry_id: str,
    distinct_rate_source_id: str,
    attempt_id: str,
    parent_attempt_id: Optional[str] = None,
    observed_at: Optional[dt.datetime] = None,
) -> EvidenceDraft:
    """Convert one Issue #7 browser observation into an evidence draft."""
    event_type = browser_event_type(observation.observation_type)
    # Firm-vs-estimate is preserved from page evidence at the EVENT level too,
    # so an estimate never masquerades as a firm quote event.
    if observation.observation_type is BrowserObservationType.QUOTE_DETECTED:
        raw = observation.quote.raw if observation.quote else None
        event_type = (
            EvidenceEventType.BROWSER_QUOTE_OBSERVED
            if (raw and raw.is_firm_quote)
            else EvidenceEventType.BROWSER_ESTIMATE_OBSERVED
        )
    return EvidenceDraft(
        event_type=event_type,
        payload=_browser_payload(observation),
        source_channel=SourceChannel.BROWSER,
        plan_id=plan_id,
        planned_route_id=planned_route_id,
        registry_id=registry_id,
        distinct_rate_source_id=distinct_rate_source_id,
        attempt_id=attempt_id,
        parent_attempt_id=parent_attempt_id,
        source_session_id=browser_session_id,
        page_signature=observation.page_signature,
        safe_url=observation.url,
        observation_type=observation.observation_type.value,
        observed_at=_browser_observed_at(observation, observed_at),
    )


def _browser_observed_at(observation: BrowserObservation, fallback: Optional[dt.datetime]):
    raw = observation.quote.raw if observation.quote else None
    if raw and raw.observed_at:
        return raw.observed_at
    return fallback


def quote_from_browser_observation(
    intake_session_id: str,
    observation: BrowserObservation,
    *,
    plan_id: str,
    planned_route_id: str,
    registry_id: str,
    distinct_rate_source_id: str,
    attempt_id: str,
    parent_attempt_id: Optional[str] = None,
) -> Optional[QuoteObservation]:
    """Build a typed QuoteObservation from a browser QUOTE_DETECTED observation.

    Returns None when no quote was observed. Firm-vs-estimate is preserved from
    page evidence; raw references stay private (opaque handle only).
    """
    if observation.quote is None or not observation.quote.quote_present:
        return None
    raw: RawQuoteObservation = observation.quote.raw
    now = dt.datetime.now(dt.timezone.utc)
    return QuoteObservation(
        quote_id="",
        intake_session_id=intake_session_id,
        attempt_id=attempt_id,
        parent_attempt_id=parent_attempt_id,
        plan_id=plan_id,
        planned_route_id=planned_route_id,
        registry_id=registry_id,
        distinct_rate_source_id=distinct_rate_source_id,
        presented_carrier=None,
        observed_at=raw.observed_at or now,
        annual_premium=_quote_amount(raw.annual_amount_decimal, raw.annual_amount_parsed),
        monthly_premium=_quote_amount(raw.monthly_amount_decimal, raw.monthly_amount_parsed),
        currency=raw.currency,
        firm_vs_estimate="firm" if raw.is_firm_quote else "estimate",
        reference_present=raw.reference_present,
        private_reference_handle=raw.private_reference_handle,
        coverage_raw_present=bool(raw.coverage_observations),
        quote_pending_normalization=True,
        content_hash="",
        idempotency_key="",
        created_at=now,
    )


# ---------------------------------------------------------------------------
# Voice observation mapping (Issue #9 objects -> evidence)
# ---------------------------------------------------------------------------


def voice_event_type(observation_type: str) -> EvidenceEventType:
    """Map a voice observation kind to the provider-independent event type."""
    try:
        ot = VoiceObservationType(observation_type)
    except ValueError:
        return EvidenceEventType.VOICE_CHECKPOINT_OBSERVED
    mapping = {
        VoiceObservationType.PHONE_QUOTE_OBSERVED: EvidenceEventType.VOICE_QUOTE_OBSERVED,
        VoiceObservationType.PHONE_ESTIMATE_OBSERVED: EvidenceEventType.VOICE_ESTIMATE_OBSERVED,
        VoiceObservationType.CALLBACK_SCHEDULED: EvidenceEventType.CALLBACK_OBSERVED,
        VoiceObservationType.BROKER_REQUIRES_FIELD: EvidenceEventType.FIELD_REQUIREMENT_OBSERVED,
        VoiceObservationType.APPLICANT_REQUIRED: EvidenceEventType.CHECKPOINT_OBSERVED,
        VoiceObservationType.MANUAL_REVIEW_REQUIRED: EvidenceEventType.HUMAN_HANDOFF_REQUIRED,
        VoiceObservationType.EXPLICIT_INELIGIBLE: EvidenceEventType.EXPLICIT_INELIGIBILITY_OBSERVED,
        VoiceObservationType.AFFINITY_RESTRICTED: EvidenceEventType.AFFINITY_RESTRICTED_OBSERVED,
        VoiceObservationType.SPECIALTY_ONLY: EvidenceEventType.SPECIALTY_ONLY_OBSERVED,
        VoiceObservationType.NOT_CURRENTLY_WRITING: EvidenceEventType.NOT_CURRENTLY_WRITING_OBSERVED,
        VoiceObservationType.PHONE_UNREACHABLE: EvidenceEventType.UNAVAILABLE_OBSERVED,
        VoiceObservationType.TECHNICAL_ERROR: EvidenceEventType.UNAVAILABLE_OBSERVED,
        VoiceObservationType.COMPLETED_WITHOUT_QUOTE: EvidenceEventType.UNAVAILABLE_OBSERVED,
        VoiceObservationType.UNKNOWN_BROKER_QUESTION: EvidenceEventType.CHECKPOINT_OBSERVED,
    }
    return mapping.get(ot, EvidenceEventType.VOICE_CHECKPOINT_OBSERVED)


def voice_draft(
    intake_session_id: str,
    *,
    voice_session_id: str,
    observation_type: str,
    plan_id: str,
    planned_route_id: str,
    registry_id: str,
    distinct_rate_source_id: str,
    attempt_id: str,
    parent_attempt_id: Optional[str] = None,
    canonical_path: Optional[str] = None,
    checkpoint_kind: Optional[str] = None,
    route_status: Optional[str] = None,
    lifecycle_status: Optional[str] = None,
    recording_consent: str = "not_requested",
    transcription_consent: str = "not_requested",
    observed_at: Optional[dt.datetime] = None,
) -> EvidenceDraft:
    """Convert one Issue #9 voice observation into an evidence draft."""
    event_type = voice_event_type(observation_type)
    return EvidenceDraft(
        event_type=event_type,
        payload=VoiceObservationEvidence(
            voice_observation_type=observation_type,
            canonical_path=canonical_path,
            checkpoint_kind=checkpoint_kind,
            route_status=route_status,
            lifecycle_status=lifecycle_status,
            recording_consent=recording_consent,
            transcription_consent=transcription_consent,
        ),
        source_channel=SourceChannel.VOICE,
        plan_id=plan_id,
        planned_route_id=planned_route_id,
        registry_id=registry_id,
        distinct_rate_source_id=distinct_rate_source_id,
        attempt_id=attempt_id,
        parent_attempt_id=parent_attempt_id,
        source_session_id=voice_session_id,
        observation_type=observation_type,
        observed_at=observed_at,
    )


def voice_quote(
    intake_session_id: str,
    *,
    voice_session_id: str,
    plan_id: str,
    planned_route_id: str,
    registry_id: str,
    distinct_rate_source_id: str,
    attempt_id: str,
    parent_attempt_id: Optional[str] = None,
    annual_premium: Optional[Decimal] = None,
    monthly_premium: Optional[Decimal] = None,
    currency: Optional[str] = None,
    firm_vs_estimate: str = "firm",
    reference_present: bool = False,
    private_reference_handle: Optional[str] = None,
    coverage_raw_present: bool = False,
    observed_at: Optional[dt.datetime] = None,
) -> QuoteObservation:
    """Build a typed QuoteObservation from a phone/voice quote result."""
    now = observed_at or dt.datetime.now(dt.timezone.utc)
    return QuoteObservation(
        quote_id="",
        intake_session_id=intake_session_id,
        attempt_id=attempt_id,
        parent_attempt_id=parent_attempt_id,
        plan_id=plan_id,
        planned_route_id=planned_route_id,
        registry_id=registry_id,
        distinct_rate_source_id=distinct_rate_source_id,
        presented_carrier=None,
        observed_at=now,
        annual_premium=annual_premium,
        monthly_premium=monthly_premium,
        currency=currency,
        firm_vs_estimate=firm_vs_estimate,
        reference_present=reference_present,
        private_reference_handle=private_reference_handle,
        coverage_raw_present=coverage_raw_present,
        quote_pending_normalization=True,
        content_hash="",
        idempotency_key="",
        created_at=now,
    )


# ---------------------------------------------------------------------------
# Voice session / checkpoint / completion + generic safe field interactions
# ---------------------------------------------------------------------------


def voice_session_started_draft(
    intake_session_id: str,
    *,
    voice_session_id: str,
    plan_id: Optional[str],
    planned_route_id: str,
    registry_id: str,
    distinct_rate_source_id: Optional[str],
    attempt_id: Optional[str],
    parent_attempt_id: Optional[str] = None,
    disclosure_status: str = "not_disclosed",
    lifecycle_status: str = "prepared",
    observed_at: Optional[dt.datetime] = None,
) -> EvidenceDraft:
    """VOICE_SESSION_STARTED: a voice session was prepared (safe metadata)."""
    return EvidenceDraft(
        event_type=EvidenceEventType.VOICE_SESSION_STARTED,
        payload=VoiceObservationEvidence(
            voice_observation_type="voice_session_started",
            checkpoint_kind=None,
            route_status=None,
            lifecycle_status=lifecycle_status,
            recording_consent="not_requested",
            transcription_consent="not_requested",
        ),
        source_channel=SourceChannel.VOICE,
        plan_id=plan_id,
        planned_route_id=planned_route_id,
        registry_id=registry_id,
        distinct_rate_source_id=distinct_rate_source_id,
        attempt_id=attempt_id,
        parent_attempt_id=parent_attempt_id,
        source_session_id=voice_session_id,
        observation_type="voice_session_started",
        observed_at=observed_at,
        evidence_source="voice_engine",
    )


def voice_checkpoint_draft(
    intake_session_id: str,
    *,
    voice_session_id: str,
    plan_id: Optional[str],
    planned_route_id: str,
    registry_id: str,
    distinct_rate_source_id: Optional[str],
    attempt_id: Optional[str],
    parent_attempt_id: Optional[str] = None,
    checkpoint_kind: str,
    lifecycle_status: Optional[str] = None,
    recording_consent: str = "not_requested",
    transcription_consent: str = "not_requested",
    observed_at: Optional[dt.datetime] = None,
) -> EvidenceDraft:
    """VOICE_CHECKPOINT_OBSERVED: disclosure / consent / human checkpoint."""
    return EvidenceDraft(
        event_type=EvidenceEventType.VOICE_CHECKPOINT_OBSERVED,
        payload=VoiceObservationEvidence(
            voice_observation_type="voice_checkpoint_observed",
            checkpoint_kind=checkpoint_kind,
            route_status=None,
            lifecycle_status=lifecycle_status,
            recording_consent=recording_consent,
            transcription_consent=transcription_consent,
        ),
        source_channel=SourceChannel.VOICE,
        plan_id=plan_id,
        planned_route_id=planned_route_id,
        registry_id=registry_id,
        distinct_rate_source_id=distinct_rate_source_id,
        attempt_id=attempt_id,
        parent_attempt_id=parent_attempt_id,
        source_session_id=voice_session_id,
        observation_type="voice_checkpoint_observed",
        observed_at=observed_at,
        evidence_source="voice_engine",
    )


def field_interaction_draft(
    intake_session_id: str,
    *,
    source_channel: SourceChannel,
    plan_id: Optional[str],
    planned_route_id: str,
    registry_id: str,
    distinct_rate_source_id: Optional[str],
    attempt_id: Optional[str],
    parent_attempt_id: Optional[str] = None,
    source_session_id: Optional[str] = None,
    canonical_path: Optional[str] = None,
    transformation: Optional[str] = None,
    interaction_type: str = "filled",
    success: bool = True,
    page_signature: Optional[str] = None,
    observed_at: Optional[dt.datetime] = None,
) -> EvidenceDraft:
    """Safe automatic field evidence: canonical PATH + operation + outcome only.

    Callers supply ``canonical_path`` / ``transformation`` / ``interaction_type``
    / ``success`` and NEVER a value. Synthetic sensitive values can never cross
    this builder because there is no value field.
    """
    return EvidenceDraft(
        event_type=EvidenceEventType.FIELD_INTERACTION_OBSERVED,
        payload=FieldInteractionEvidence(
            canonical_path=canonical_path,
            transformation=transformation,
            interaction_type=interaction_type,
            success=success,
            page_signature=page_signature,
        ),
        source_channel=source_channel,
        plan_id=plan_id,
        planned_route_id=planned_route_id,
        registry_id=registry_id,
        distinct_rate_source_id=distinct_rate_source_id,
        attempt_id=attempt_id,
        parent_attempt_id=parent_attempt_id,
        source_session_id=source_session_id,
        observation_type="field_interaction",
        observed_at=observed_at,
    )


# ---------------------------------------------------------------------------
# Recovery / route plan / consent / attempt mapping
# ---------------------------------------------------------------------------


def recovery_draft_from_decision(
    intake_session_id: str, decision: RecoveryDecision
) -> EvidenceDraft:
    """Record what Issue #8 DECIDED (records it; never re-decides)."""
    return EvidenceDraft(
        event_type=EvidenceEventType.RECOVERY_DECISION,
        payload=RecoveryEvidence(
            observation_type=None,
            lifecycle_status=decision.lifecycle_status.value,
            recommended_action=decision.recommended_action.value,
            reason_codes=list(decision.reason_codes),
            terminal_status=decision.terminal_status.value if decision.terminal_status else None,
            quote_pending_normalization=decision.quote_pending_normalization,
            policy_version=decision.policy_version,
            retry_allowed=decision.retry_allowed,
        ),
        source_channel=SourceChannel.MANUAL,
        plan_id=decision.plan_id,
        planned_route_id=decision.planned_route_id,
        registry_id=decision.registry_id,
        distinct_rate_source_id=decision.distinct_rate_source_id,
        attempt_id=decision.attempt_id,
        observed_at=decision.decided_at,
        evidence_source="recovery_engine",
    )


def route_plan_draft(intake_session_id: str, route_plan: RoutePlan) -> EvidenceDraft:
    """Safe summary of a route plan creation (counts + insurance type only)."""
    ready = sum(1 for r in route_plan.routes if r.is_ready)
    blocked = sum(1 for r in route_plan.routes if not r.is_ready)
    return EvidenceDraft(
        event_type=EvidenceEventType.ROUTE_PLANNED,
        payload=RoutePlanEvidence(
            insurance_type=route_plan.insurance_type.value,
            planned_route_count=len(route_plan.routes),
            ready_count=ready,
            blocked_count=blocked,
        ),
        source_channel=SourceChannel.MANUAL,
        plan_id=route_plan.session_id,
        observed_at=route_plan.generated_at,
        evidence_source="route_planner",
    )


def consent_draft(
    intake_session_id: str,
    *,
    plan_id: str,
    planned_route_id: str,
    registry_id: str,
    consent_receipt_id: Optional[str] = None,
    scope: str = "quote",
    canonical_paths: Optional[list[str]] = None,
    state: str = "granted",
    observed_at: Optional[dt.datetime] = None,
) -> EvidenceDraft:
    """Proof of consent state at disclosure time (paths only, no values)."""
    return EvidenceDraft(
        event_type=EvidenceEventType.CONSENT_EVENT,
        payload=ConsentEvidence(
            consent_receipt_id=consent_receipt_id,
            scope=scope,
            canonical_paths=list(canonical_paths or []),
            state=state,
            route_registry_id=registry_id,
        ),
        source_channel=SourceChannel.MANUAL,
        plan_id=plan_id,
        planned_route_id=planned_route_id,
        registry_id=registry_id,
        observed_at=observed_at,
        evidence_source="intake_consent",
    )


def attempt_draft(
    intake_session_id: str,
    *,
    event_type: EvidenceEventType,
    plan_id: str,
    planned_route_id: str,
    registry_id: str,
    distinct_rate_source_id: str,
    attempt_id: str,
    parent_attempt_id: Optional[str] = None,
    channel: str,
    attempt_number: int = 1,
    lifecycle_status: Optional[str] = None,
    policy_version: Optional[str] = None,
    plan_version: Optional[str] = None,
    observed_at: Optional[dt.datetime] = None,
) -> EvidenceDraft:
    """attempt_started / completed / failed lifecycle evidence."""
    return EvidenceDraft(
        event_type=event_type,
        payload=AttemptEvidence(
            channel=channel,
            attempt_number=attempt_number,
            lifecycle_status=lifecycle_status,
            policy_version=policy_version,
            plan_version=plan_version,
        ),
        source_channel=SourceChannel(channel),
        plan_id=plan_id,
        planned_route_id=planned_route_id,
        registry_id=registry_id,
        distinct_rate_source_id=distinct_rate_source_id,
        attempt_id=attempt_id,
        parent_attempt_id=parent_attempt_id,
        observed_at=observed_at,
        evidence_source="recovery_engine",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _quote_amount(decimal_value: Optional[Decimal], float_value: Optional[float]) -> Optional[Decimal]:
    """Prefer the exact Decimal parsed from the original text; fall back to the
    safe ``Decimal(str(float))`` boundary (never raw float money)."""
    if decimal_value is not None:
        return decimal_value
    return _float_to_decimal(float_value)


def _float_to_decimal(value: Optional[float]) -> Optional[Decimal]:
    """Convert a float (browser parse) to a precise Decimal via str()."""
    if value is None:
        return None
    return Decimal(str(value))
