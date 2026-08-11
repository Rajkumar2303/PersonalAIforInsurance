"""Deterministic voice/phone handoff engine (Issue #9, Prompt 1).

The engine is the single authority for the voice layer. It is fully
deterministic (NO LLM, NO real calls) and:

- prepares a ``VoiceSession`` from a ``PhoneHandoffContext``;
- enforces automation disclosure BEFORE any substantive interaction;
- answers broker questions JIT via the Issue #5 ``IntakeValueSource`` (never
  a second applicant-information store) - values are spoken through the
  transport boundary and discarded;
- pauses for the applicant (Issue #5 ``request_fields``) or for consent
  (Issue #5 route-disclosure / household-driver consent) when a field is
  missing;
- NEVER answers identity, declaration, advice, or applicant-required items -
  those are transferred to the applicant or a human;
- pushes every outcome to the Issue #8 ``RecoveryEngine`` as an
  ``ExecutionObservation`` with ``source_channel=VOICE`` so terminal-status,
  retry, failover, and handoff decisions stay with Issue #8 (never duplicated
  here) - and NEVER assigns ``quoted_comparable`` / ``quoted_non_comparable``;
- never records, never transcribes (explicit consent statuses only).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Protocol, runtime_checkable

from ...browser.value_provider import BrowserValueSource, IntakeValueSource
from ...models.recovery import (
    AttemptRecord,
    RecoveryDecideRequest,
    RecoveryDecision,
    RouteOutcomeStatus,
    SourceChannel,
)
from ...models.voice import (
    BrokerQuestion,
    BrokerQuestionKind,
    DisclosureStatus,
    PhoneHandoffContext,
    RecordingConsentStatus,
    TranscriptionConsentStatus,
    VoiceDecision,
    VoiceLifecycleStatus,
    VoiceObservationType,
    VoiceResponseAction,
    VoiceRouteStatus,
    VoiceRouteSummary,
    VoiceSession,
    derive_voice_route_status,
    sanitize_voice_context,
)
from ..intake import get_intake_engine
from ..recovery import RecoveryEngine, get_recovery_engine
from ..evidence.ingest import (
    field_interaction_draft,
    voice_checkpoint_draft,
    voice_draft,
    voice_quote,
    voice_session_started_draft,
)
from ..evidence.sink import EvidenceSink, NoopEvidenceSink
from .question_interpreter import BrokerQuestionInterpreter, DeterministicBrokerQuestionInterpreter
from .session_store import VoiceSessionNotFoundError, VoiceSessionStore
from .transport import MockVoiceTransport, VoiceTransport, safe_render

logger = logging.getLogger(__name__)

VOICE_SOURCE_CONTEXT = "voice_agent"


@runtime_checkable
class RecoverySource(Protocol):
    """Minimal Issue #8 surface the voice engine depends on."""

    def record_observation(
        self,
        request: RecoveryDecideRequest,
        current_attempt: Optional[AttemptRecord] = None,
    ) -> RecoveryDecision: ...

    def begin_attempt(
        self,
        *,
        plan_id: Optional[str] = None,
        planned_route_id: str,
        registry_id: Optional[str] = None,
        distinct_rate_source_id: Optional[str] = None,
        channel: SourceChannel = SourceChannel.BROWSER,
        parent_attempt_id: Optional[str] = None,
        alternative_of_attempt_id: Optional[str] = None,
        policy_version: Optional[str] = None,
        plan_version: Optional[str] = None,
    ) -> AttemptRecord: ...


class VoiceValueSource(IntakeValueSource):
    """Issue #5 ``IntakeValueSource`` with a voice source-context label.

    Reuses the existing JIT value surface as-is (no second applicant store);
    only the provenance label differs from the browser agent.
    """

    def request(self, session_id: str, paths: list[str]) -> list[Any]:
        return self._engine.request_fields(session_id, paths, VOICE_SOURCE_CONTEXT)


# Human-boundary checkpoint kinds the automation NEVER answers directly.
_APPLICANT_BOUNDARY_KINDS: frozenset[BrokerQuestionKind] = frozenset(
    {
        BrokerQuestionKind.IDENTITY_CHECKPOINT,
        BrokerQuestionKind.DECLARATION,
        BrokerQuestionKind.ADVICE_REQUEST,
        BrokerQuestionKind.APPLICANT_REQUIRED,
    }
)

# Observation type per human-boundary kind (Issue #8 visibility).
_BOUNDARY_OBSERVATION: dict[BrokerQuestionKind, str] = {
    BrokerQuestionKind.IDENTITY_CHECKPOINT: VoiceObservationType.APPLICANT_REQUIRED,
    BrokerQuestionKind.DECLARATION: VoiceObservationType.APPLICANT_REQUIRED,
    BrokerQuestionKind.ADVICE_REQUEST: VoiceObservationType.APPLICANT_REQUIRED,
    BrokerQuestionKind.APPLICANT_REQUIRED: VoiceObservationType.APPLICANT_REQUIRED,
    BrokerQuestionKind.MANUAL_REVIEW: VoiceObservationType.MANUAL_REVIEW_REQUIRED,
}

# Terminal statement -> voice observation + recovery terminal status.
_TERMINAL_STATEMENTS: dict[
    BrokerQuestionKind, tuple[str, Optional[str]]
] = {
    BrokerQuestionKind.INELIGIBILITY: (
        VoiceObservationType.EXPLICIT_INELIGIBLE, RouteOutcomeStatus.INELIGIBLE,
    ),
    BrokerQuestionKind.AFFINITY_RESTRICTION: (
        VoiceObservationType.AFFINITY_RESTRICTED, RouteOutcomeStatus.AFFINITY_RESTRICTED,
    ),
    BrokerQuestionKind.SPECIALTY_ONLY: (
        VoiceObservationType.SPECIALTY_ONLY, RouteOutcomeStatus.SPECIALTY_ONLY,
    ),
    BrokerQuestionKind.NOT_CURRENTLY_WRITING: (
        VoiceObservationType.NOT_CURRENTLY_WRITING, RouteOutcomeStatus.NOT_CURRENTLY_WRITING,
    ),
}

# Actions that interrupt the applicant (Prompt 2 unattended-execution counter).
# Everything else is automated. A known question answered JIT is
# ``disclose_value``; a route-local pause never blocks other routes.
_INTERRUPT_ACTIONS: frozenset[str] = frozenset(
    {
        VoiceResponseAction.PAUSE_FOR_APPLICANT,
        VoiceResponseAction.REQUEST_CONSENT,
        VoiceResponseAction.TRANSFER_TO_APPLICANT,
        VoiceResponseAction.TRANSFER_TO_HUMAN,
        VoiceResponseAction.MANUAL_HANDOFF,
    }
)


class VoiceEngine:
    """Deterministic voice/phone handoff engine (single authority)."""

    def __init__(
        self,
        *,
        store: VoiceSessionStore,
        values: BrowserValueSource,
        interpreter: Optional[BrokerQuestionInterpreter] = None,
        transport: Optional[VoiceTransport] = None,
        recovery: Optional[RecoverySource] = None,
        evidence_sink: Optional[EvidenceSink] = None,
    ) -> None:
        self._store = store
        self._values = values
        self._interpreter = interpreter or DeterministicBrokerQuestionInterpreter()
        self._transport = transport or MockVoiceTransport()
        self._recovery = recovery or get_recovery_engine()
        self._sink: EvidenceSink = evidence_sink or NoopEvidenceSink()

    def _emit(self, intake_session_id: str, draft) -> None:
        """Automatic voice evidence write (no-op when disabled/failing)."""
        if self._sink.enabled and intake_session_id:
            self._sink.record(intake_session_id, draft)

    def _emit_quote(self, intake_session_id: str, quote) -> None:
        if self._sink.enabled and intake_session_id:
            self._sink.record_quote(intake_session_id, quote)

    # -- session lifecycle ----------------------------------------------

    def get(self, voice_session_id: str) -> VoiceSession:
        return self._store.get(voice_session_id)

    def prepare_handoff(self, context: PhoneHandoffContext) -> VoiceSession:
        """Create a PREPARED voice session from a safe handoff context."""
        now = datetime.now(timezone.utc)
        session = VoiceSession(
            voice_session_id=uuid.uuid4().hex,
            intake_session_id=context.intake_session_id,
            registry_id=context.registry_id,
            distinct_rate_source_id=context.distinct_rate_source_id,
            planned_route_id=context.planned_route_id,
            source_attempt_id=context.source_attempt_id,
            source_channel=SourceChannel.VOICE,
            target_channel=context.target_channel,
            lifecycle_status=VoiceLifecycleStatus.PREPARED,
            disclosure_status=DisclosureStatus.NOT_DISCLOSED,
            pending_field_paths=[],
            callback_reason=context.callback_reason,
            provider_phone_route=context.provider_phone_route,
            authorized_canonical_paths=list(context.authorized_canonical_paths),
            reference_present=context.reference_present,
            private_reference_handle=context.private_reference_handle,
            created_at=now,
            updated_at=now,
        )
        self._transport.start_session(session)
        self._ensure_recovery_attempt(session)
        self._store.save(session)
        self._emit(
            session.intake_session_id or "",
            voice_session_started_draft(
                session.intake_session_id or "",
                voice_session_id=session.voice_session_id,
                plan_id=None,
                planned_route_id=session.planned_route_id or session.registry_id,
                registry_id=session.registry_id,
                distinct_rate_source_id=session.distinct_rate_source_id,
                attempt_id=session.recovery_attempt_id,
                parent_attempt_id=session.source_attempt_id,
                disclosure_status=session.disclosure_status,
                lifecycle_status=session.lifecycle_status,
                observed_at=session.created_at,
            ),
        )
        logger.info(
            "voice handoff prepared",
            extra={"voice_session_id": session.voice_session_id, "registry_id": session.registry_id},
        )
        return session

    def _emit_disclosure(self, session: VoiceSession, granted: bool) -> None:
        self._emit(
            session.intake_session_id or "",
            voice_checkpoint_draft(
                session.intake_session_id or "",
                voice_session_id=session.voice_session_id,
                plan_id=None,
                planned_route_id=session.planned_route_id or session.registry_id,
                registry_id=session.registry_id,
                distinct_rate_source_id=session.distinct_rate_source_id,
                attempt_id=session.recovery_attempt_id,
                parent_attempt_id=session.source_attempt_id,
                checkpoint_kind="automation_disclosure",
                lifecycle_status="disclosed" if granted else "refused",
            ),
        )

    def disclose_automation(self, voice_session_id: str, *, granted: bool = True) -> VoiceSession:
        """Mandatory automation disclosure before ANY substantive interaction."""
        session = self._store.get(voice_session_id)
        session.disclosure_status = (
            DisclosureStatus.DISCLOSED if granted else DisclosureStatus.REFUSED
        )
        if granted and session.lifecycle_status == VoiceLifecycleStatus.PREPARED:
            session.lifecycle_status = VoiceLifecycleStatus.AWAITING_DISCLOSURE
        if not granted:
            session.lifecycle_status = VoiceLifecycleStatus.TERMINATED
        self._touch(session)
        self._emit_disclosure(session, granted)
        logger.info(
            "automation disclosure %s",
            session.disclosure_status,
            extra={"voice_session_id": session.voice_session_id},
        )
        return session

    # -- core interaction -----------------------------------------------

    def receive_broker_event(self, voice_session_id: str, question: BrokerQuestion) -> VoiceDecision:
        """Process one structured broker question; returns a safe VoiceDecision."""
        session = self._store.get(voice_session_id)

        # Automation-disclosure gate: nothing substantive before disclosure.
        # A refused disclosure is terminal regardless of lifecycle.
        if session.disclosure_status == DisclosureStatus.REFUSED:
            return self._decision(
                session,
                action=VoiceResponseAction.END_TERMINAL,
                message="automation disclosure refused; ending session",
            )
        if session.lifecycle_status in (
            VoiceLifecycleStatus.COMPLETED,
            VoiceLifecycleStatus.TERMINATED,
        ):
            return self._decision(
                session,
                action=VoiceResponseAction.ACKNOWLEDGE,
                message="voice session already ended",
            )
        if session.disclosure_status != DisclosureStatus.DISCLOSED:
            return self._decision(
                session,
                lifecycle=VoiceLifecycleStatus.AWAITING_DISCLOSURE,
                action=VoiceResponseAction.SPEAK_DISCLOSURE,
                message="automation disclosure required before any substantive interaction",
            )
        if session.lifecycle_status == VoiceLifecycleStatus.AWAITING_DISCLOSURE:
            session.lifecycle_status = VoiceLifecycleStatus.ACTIVE
            self._touch(session)

        # Human-boundary kinds: the automation NEVER answers.
        if question.kind in _APPLICANT_BOUNDARY_KINDS or question.kind == BrokerQuestionKind.MANUAL_REVIEW:
            return self._applicant_boundary(session, question)

        # Consent expansion / household driver.
        if question.kind in (BrokerQuestionKind.CONSENT_EXPANSION, BrokerQuestionKind.HOUSEHOLD_DRIVER):
            return self._consent_gate(session, question)

        # Callback request.
        if question.kind == BrokerQuestionKind.CALLBACK_REQUEST:
            return self._callback(session, question)

        # Quote / estimate.
        if question.kind == BrokerQuestionKind.QUOTE_DISCLOSURE:
            return self._quote_or_estimate(session, question, estimate=False)
        if question.kind == BrokerQuestionKind.ESTIMATE_DISCLOSURE:
            return self._quote_or_estimate(session, question, estimate=True)

        # Explicit terminal statements.
        if question.kind in _TERMINAL_STATEMENTS:
            return self._terminal_statement(session, question)

        if question.kind == BrokerQuestionKind.BROKER_UNAVAILABLE:
            return self._broker_unavailable(session)

        if question.kind == BrokerQuestionKind.COMPLETED_WITHOUT_QUOTE:
            return self._completed_without_quote(session)

        # Unknown question: never guess.
        if question.kind == BrokerQuestionKind.UNKNOWN:
            return self._unknown_question(session, question)

        # Canonical field / collection length.
        return self._field_flow(session, question)

    def resume(self, voice_session_id: str) -> VoiceDecision:
        """Resume after Issue #5 resolved a paused field/consent.

        Re-checks CURRENT consent (a revocation between pause and resume is
        honoured) and only then discloses a value JIT.
        """
        session = self._store.get(voice_session_id)

        if session.pending_checkpoint and session.lifecycle_status == VoiceLifecycleStatus.AWAITING_HUMAN:
            return self._decision(
                session,
                action=VoiceResponseAction.TRANSFER_TO_APPLICANT,
                checkpoint_kind=session.pending_checkpoint,
                message="human checkpoint still pending",
            )

        if session.lifecycle_status == VoiceLifecycleStatus.PAUSED_FOR_CONSENT:
            # Re-check route-disclosure consent live (revocation honoured).
            for path in session.pending_field_paths:
                if not self._values.route_disclosure_covers(
                    session.intake_session_id, session.registry_id, path
                ):
                    return self._decision(
                        session,
                        lifecycle=VoiceLifecycleStatus.PAUSED_FOR_CONSENT,
                        action=VoiceResponseAction.REQUEST_CONSENT,
                        canonical_path=path,
                        requires_consent_expansion=True,
                        message="route-disclosure consent still required",
                    )
            # Consent present; continue to the pending-field disclosure below.
            session.lifecycle_status = VoiceLifecycleStatus.ACTIVE
            self._touch(session)

        # Consent confirmed and/or applicant answered: disclose the first
        # pending field JIT, or stay paused if it is still missing.
        if session.lifecycle_status in (
            VoiceLifecycleStatus.PAUSED_FOR_APPLICANT,
            VoiceLifecycleStatus.ACTIVE,
        ):
            if session.pending_field_paths:
                path = session.pending_field_paths[0]
                if not self._values.known(session.intake_session_id, path):
                    # Still missing - stay paused; Issue #5 drives the applicant.
                    return self._decision(
                        session,
                        lifecycle=VoiceLifecycleStatus.PAUSED_FOR_APPLICANT,
                        action=VoiceResponseAction.PAUSE_FOR_APPLICANT,
                        canonical_path=path,
                        message="field still missing; waiting for applicant",
                    )
                self._speak_value(session, path)
                session.pending_field_paths = []
                session.lifecycle_status = VoiceLifecycleStatus.ACTIVE
                self._touch(session)
                return self._decision(
                    session,
                    action=VoiceResponseAction.DISCLOSE_VALUE,
                    canonical_path=path,
                    value_present=True,
                    message="value disclosed to broker",
                )
            session.lifecycle_status = VoiceLifecycleStatus.ACTIVE
            self._touch(session)
            return self._decision(
                session,
                action=VoiceResponseAction.ACKNOWLEDGE,
                message="no pending fields",
            )

        return self._decision(session, action=VoiceResponseAction.ACKNOWLEDGE,
                              message="session not paused")

    def transfer_to_human(
        self,
        voice_session_id: str,
        *,
        reason: str,
        checkpoint_kind: Optional[str] = None,
    ) -> VoiceSession:
        """Escalate to a human (identity, declaration, advice, unknown)."""
        session = self._store.get(voice_session_id)
        session.lifecycle_status = VoiceLifecycleStatus.AWAITING_HUMAN
        session.pending_checkpoint = checkpoint_kind or reason
        self._transport.transfer_to_human(voice_session_id, reason)
        self._touch(session)
        return session

    def pause(self, voice_session_id: str, *, reason: str = "route_local_pause") -> VoiceSession:
        """Route-local pause (Prompt 2). Only this route/session pauses; other
        routes keep running. Idempotent: pausing an already-paused or terminal
        session is a safe no-op that returns the current state."""
        session = self._store.get(voice_session_id)
        if session.lifecycle_status in (
            VoiceLifecycleStatus.PREPARED,
            VoiceLifecycleStatus.AWAITING_DISCLOSURE,
            VoiceLifecycleStatus.ACTIVE,
            VoiceLifecycleStatus.AWAITING_HUMAN,
        ):
            session.lifecycle_status = VoiceLifecycleStatus.PAUSED_FOR_APPLICANT
            self._transport.pause(voice_session_id, reason)
        self._touch(session)
        return session

    def route_summaries(self, intake_session_id: str) -> list[VoiceRouteSummary]:
        """Safe per-route summaries for the future orchestrator (Prompt 2).

        Enumerates every route session for an intake session with its
        route-local status and batchable pending fields, so a missing field or
        applicant-required state on one route never blocks another.
        """
        return [self._route_summary(s) for s in self._store.list_by_intake(intake_session_id)]

    def _route_summary(self, session: VoiceSession) -> VoiceRouteSummary:
        return VoiceRouteSummary(
            voice_session_id=session.voice_session_id,
            registry_id=session.registry_id,
            distinct_rate_source_id=session.distinct_rate_source_id,
            planned_route_id=session.planned_route_id,
            recovery_attempt_id=session.recovery_attempt_id,
            lifecycle_status=session.lifecycle_status,
            route_status=derive_voice_route_status(session),
            terminal_status=session.terminal_status,
            quote_pending_normalization=session.quote_pending_normalization,
            pending_field_paths=list(session.pending_field_paths),
            automated_answers=session.automated_answers,
            applicant_interruptions=session.applicant_interruptions,
        )

    def end_session(self, voice_session_id: str, *, status: str, reason: str) -> VoiceSession:
        """End a voice session (completed or terminated)."""
        session = self._store.get(voice_session_id)
        session.lifecycle_status = status
        self._transport.end_session(voice_session_id, reason)
        self._touch(session)
        self._emit(
            session.intake_session_id or "",
            voice_checkpoint_draft(
                session.intake_session_id or "",
                voice_session_id=session.voice_session_id,
                plan_id=None,
                planned_route_id=session.planned_route_id or session.registry_id,
                registry_id=session.registry_id,
                distinct_rate_source_id=session.distinct_rate_source_id,
                attempt_id=session.recovery_attempt_id,
                parent_attempt_id=session.source_attempt_id,
                checkpoint_kind="session_end",
                lifecycle_status=status,
            ),
        )
        return session

    def emit_observation(
        self,
        voice_session_id: str,
        observation_type: str,
        *,
        reason: Optional[str] = None,
        extra_safe_context: Optional[dict[str, Any]] = None,
    ) -> RecoveryDecision:
        """Push a voice observation to the Issue #8 recovery engine.

        Issue #8 remains the terminal-status / retry / failover / handoff
        authority. This never assigns quoted_comparable / quoted_non_comparable.
        """
        session = self._store.get(voice_session_id)
        ctx: dict[str, Any] = {
            "voice_session_id": session.voice_session_id,
            "registry_id": session.registry_id,
            "distinct_rate_source_id": session.distinct_rate_source_id,
            "route_type": "phone",
        }
        if session.reference_present:
            ctx["reference_present"] = True
        if session.private_reference_handle:
            ctx["private_reference_handle"] = session.private_reference_handle
        if extra_safe_context:
            ctx.update(extra_safe_context)
        ctx = sanitize_voice_context(ctx)

        request = RecoveryDecideRequest(
            plan_id=None,
            attempt_id=session.recovery_attempt_id,
            planned_route_id=session.planned_route_id or session.registry_id,
            registry_id=session.registry_id,
            distinct_rate_source_id=session.distinct_rate_source_id,
            intake_session_id=session.intake_session_id,
            source_channel=SourceChannel.VOICE,
            observation_type=observation_type,
            reason=reason,
            safe_context=ctx,
        )
        decision = self._recovery.record_observation(request, current_attempt=None)
        self._emit_voice_observation(session, observation_type, reason)
        logger.info(
            "voice observation emitted",
            extra={
                "voice_session_id": session.voice_session_id,
                "observation_type": observation_type,
                "recommended_action": decision.recommended_action.value,
                "lifecycle_status": decision.lifecycle_status.value,
            },
        )
        return decision

    def _emit_voice_observation(
        self, session: VoiceSession, observation_type: str, reason: Optional[str]
    ) -> None:
        """Automatic safe voice observation evidence (paths/statuses only)."""
        if not self._sink.enabled:
            return
        draft = voice_draft(
            session.intake_session_id or "",
            voice_session_id=session.voice_session_id,
            observation_type=observation_type,
            plan_id=None,
            planned_route_id=session.planned_route_id or session.registry_id,
            registry_id=session.registry_id,
            distinct_rate_source_id=session.distinct_rate_source_id,
            attempt_id=session.recovery_attempt_id,
            parent_attempt_id=session.source_attempt_id,
            route_status=session.route_status or None,
            lifecycle_status=session.lifecycle_status,
            recording_consent="not_requested",
            transcription_consent="not_requested",
            observed_at=datetime.now(timezone.utc),
        )
        self._sink.record(session.intake_session_id or "", draft)

    def _emit_voice_quote(self, session: VoiceSession, *, estimate: bool) -> None:
        """Automatic quote/estimate observation row (firm-vs-estimate preserved)."""
        if not self._sink.enabled:
            return
        quote = voice_quote(
            session.intake_session_id or "",
            voice_session_id=session.voice_session_id,
            plan_id=None,
            planned_route_id=session.planned_route_id or session.registry_id,
            registry_id=session.registry_id,
            distinct_rate_source_id=session.distinct_rate_source_id,
            attempt_id=session.recovery_attempt_id,
            parent_attempt_id=session.source_attempt_id,
            annual_premium=None,  # no STT amount capture; firm-vs-estimate + ref only
            monthly_premium=None,
            currency=None,
            firm_vs_estimate="estimate" if estimate else "firm",
            reference_present=session.reference_present,
            private_reference_handle=session.private_reference_handle,
            coverage_raw_present=False,
            observed_at=datetime.now(timezone.utc),
        )
        self._sink.record_quote(session.intake_session_id or "", quote)

    # -- internal handlers ----------------------------------------------

    def _applicant_boundary(self, session: VoiceSession, question: BrokerQuestion) -> VoiceDecision:
        observation = _BOUNDARY_OBSERVATION.get(
            question.kind, VoiceObservationType.APPLICANT_REQUIRED
        )
        self.emit_observation(
            session.voice_session_id,
            observation,
            reason=f"human boundary: {question.kind.value}",
        )
        session.lifecycle_status = VoiceLifecycleStatus.AWAITING_HUMAN
        session.pending_checkpoint = question.kind.value
        self._transport.transfer_to_human(
            session.voice_session_id,
            f"applicant boundary: {question.kind.value}",
        )
        self._touch(session)
        action = (
            VoiceResponseAction.TRANSFER_TO_HUMAN
            if question.kind == BrokerQuestionKind.MANUAL_REVIEW
            else VoiceResponseAction.TRANSFER_TO_APPLICANT
        )
        return self._decision(
            session,
            action=action,
            checkpoint_kind=question.kind.value,
            message="transferred to applicant/human - automation never answers this",
        )

    def _consent_gate(self, session: VoiceSession, question: BrokerQuestion) -> VoiceDecision:
        path = question.canonical_path
        if path:
            covered = self._values.route_disclosure_covers(
                session.intake_session_id, session.registry_id, path
            )
        else:
            covered = self._values.has_route_consent(session.intake_session_id, session.registry_id)
        if covered:
            session.lifecycle_status = VoiceLifecycleStatus.ACTIVE
            self._touch(session)
            return self._decision(
                session,
                action=VoiceResponseAction.ACKNOWLEDGE,
                canonical_path=path,
                message="consent covers the requested disclosure",
            )
        session.lifecycle_status = VoiceLifecycleStatus.PAUSED_FOR_CONSENT
        session.pending_checkpoint = question.kind.value
        self._accumulate_pending_path(session, path)
        self._touch(session)
        return self._decision(
            session,
            lifecycle=VoiceLifecycleStatus.PAUSED_FOR_CONSENT,
            action=VoiceResponseAction.REQUEST_CONSENT,
            canonical_path=path,
            requires_consent_expansion=True,
            checkpoint_kind=question.kind.value,
            message="consent expansion required before disclosing to broker",
        )

    def _callback(self, session: VoiceSession, question: BrokerQuestion) -> VoiceDecision:
        decision = self.emit_observation(
            session.voice_session_id,
            VoiceObservationType.CALLBACK_SCHEDULED,
            reason="broker requested a callback",
        )
        session.lifecycle_status = VoiceLifecycleStatus.COMPLETED
        session.terminal_status = (
            decision.terminal_status.value if decision.terminal_status else None
        )
        self._finish(session, "callback scheduled")
        return self._decision(
            session,
            lifecycle=VoiceLifecycleStatus.COMPLETED,
            action=VoiceResponseAction.CALLBACK_SCHEDULED,
            reason_code=decision.recommended_action.value,
            message="callback scheduled; voice flow complete",
        )

    def _quote_or_estimate(
        self, session: VoiceSession, question: BrokerQuestion, *, estimate: bool
    ) -> VoiceDecision:
        otype = (
            VoiceObservationType.PHONE_ESTIMATE_OBSERVED
            if estimate
            else VoiceObservationType.PHONE_QUOTE_OBSERVED
        )
        decision = self.emit_observation(
            session.voice_session_id,
            otype,
            reason="broker disclosed a quote" if not estimate else "broker disclosed an estimate",
            extra_safe_context={
                "is_firm_quote": not estimate,
                "estimate_only": estimate,
                "quote_present": True,
            },
        )
        self._emit_voice_quote(session, estimate=estimate)
        session.lifecycle_status = VoiceLifecycleStatus.COMPLETED
        session.terminal_status = (
            decision.terminal_status.value if decision.terminal_status else None
        )
        session.reference_present = bool(decision.safe_context.get("reference_present"))
        session.quote_pending_normalization = bool(decision.quote_pending_normalization)
        self._finish(session, "quote observed" if not estimate else "estimate observed")
        return self._decision(
            session,
            lifecycle=VoiceLifecycleStatus.COMPLETED,
            action=VoiceResponseAction.END_QUOTE,
            value_present=False,
            reason_code=decision.recommended_action.value,
            safe_reference_handle=session.private_reference_handle,
            message="estimate disclosed" if estimate else "quote disclosed; pending normalization (Issue #11/#12)",
        )

    def _terminal_statement(self, session: VoiceSession, question: BrokerQuestion) -> VoiceDecision:
        otype, _status = _TERMINAL_STATEMENTS[question.kind]
        decision = self.emit_observation(
            session.voice_session_id,
            otype,
            reason=f"explicit terminal statement: {question.kind.value}",
        )
        session.lifecycle_status = VoiceLifecycleStatus.COMPLETED
        session.terminal_status = (
            decision.terminal_status.value if decision.terminal_status else None
        )
        self._finish(session, "terminal statement")
        return self._decision(
            session,
            lifecycle=VoiceLifecycleStatus.COMPLETED,
            action=VoiceResponseAction.END_TERMINAL,
            reason_code=decision.recommended_action.value,
            message="explicit terminal statement from broker",
        )

    def _broker_unavailable(self, session: VoiceSession) -> VoiceDecision:
        decision = self.emit_observation(
            session.voice_session_id,
            VoiceObservationType.PHONE_UNREACHABLE,
            reason="broker unavailable / no answer / disconnected",
        )
        session.lifecycle_status = VoiceLifecycleStatus.TERMINATED
        session.terminal_status = (
            decision.terminal_status.value if decision.terminal_status else None
        )
        self._finish(session, "broker unavailable")
        action = (
            VoiceResponseAction.MANUAL_HANDOFF
            if decision.recommended_action.value == "manual_handoff"
            else VoiceResponseAction.END_TERMINAL
        )
        return self._decision(
            session,
            lifecycle=VoiceLifecycleStatus.TERMINATED,
            action=action,
            reason_code=decision.recommended_action.value,
            message="broker unreachable; Issue #8 decides retry/failover/handoff",
        )

    def _completed_without_quote(self, session: VoiceSession) -> VoiceDecision:
        decision = self.emit_observation(
            session.voice_session_id,
            VoiceObservationType.COMPLETED_WITHOUT_QUOTE,
            reason="broker call completed without a quote",
        )
        session.lifecycle_status = VoiceLifecycleStatus.COMPLETED
        session.terminal_status = (
            decision.terminal_status.value if decision.terminal_status else None
        )
        self._finish(session, "completed without quote")
        return self._decision(
            session,
            lifecycle=VoiceLifecycleStatus.COMPLETED,
            action=VoiceResponseAction.END_TERMINAL,
            reason_code=decision.recommended_action.value,
            message="call completed without a quote",
        )

    def _unknown_question(self, session: VoiceSession, question: BrokerQuestion) -> VoiceDecision:
        self.emit_observation(
            session.voice_session_id,
            VoiceObservationType.UNKNOWN_BROKER_QUESTION,
            reason="unknown broker question; manual mapping required",
        )
        session.lifecycle_status = VoiceLifecycleStatus.AWAITING_HUMAN
        session.pending_checkpoint = "manual_mapping"
        self._transport.transfer_to_human(
            session.voice_session_id, "unknown broker question requires mapping"
        )
        self._touch(session)
        return self._decision(
            session,
            lifecycle=VoiceLifecycleStatus.AWAITING_HUMAN,
            action=VoiceResponseAction.MANUAL_HANDOFF,
            message="unknown broker question; never guessed - manual mapping required",
        )

    def _field_flow(self, session: VoiceSession, question: BrokerQuestion) -> VoiceDecision:
        path = question.canonical_path
        if not path:
            return self._unknown_question(session, question)

        # Route-disclosure consent must cover this field (Issue #5).
        if not self._values.route_disclosure_covers(session.intake_session_id, session.registry_id, path):
            session.lifecycle_status = VoiceLifecycleStatus.PAUSED_FOR_CONSENT
            session.pending_checkpoint = BrokerQuestionKind.CONSENT_EXPANSION.value
            self._accumulate_pending_path(session, path)
            self._touch(session)
            return self._decision(
                session,
                lifecycle=VoiceLifecycleStatus.PAUSED_FOR_CONSENT,
                action=VoiceResponseAction.REQUEST_CONSENT,
                canonical_path=path,
                requires_consent_expansion=True,
                checkpoint_kind=BrokerQuestionKind.CONSENT_EXPANSION.value,
                message="route-disclosure consent required for this field",
            )

        # Known -> speak JIT (never cached).
        if self._values.known(session.intake_session_id, path):
            self._speak_value(session, path)
            session.lifecycle_status = VoiceLifecycleStatus.ACTIVE
            self._touch(session)
            return self._decision(
                session,
                action=VoiceResponseAction.DISCLOSE_VALUE,
                canonical_path=path,
                value_present=True,
                message="value disclosed to broker (JIT, not stored)",
            )

        # Missing -> request via Issue #5, pause for the applicant.
        outcomes = self._values.request(session.intake_session_id, [path])
        outcome = outcomes[0] if outcomes else None
        if outcome is not None and outcome.state.value == "unsupported":
            return self._unknown_question(session, question)
        if outcome is not None and (outcome.consent_required or outcome.human_checkpoint_required):
            session.lifecycle_status = VoiceLifecycleStatus.PAUSED_FOR_CONSENT
            session.pending_checkpoint = outcome.checkpoint_kind or "consent_attestation"
            self._accumulate_pending_path(session, path)
            self._touch(session)
            return self._decision(
                session,
                lifecycle=VoiceLifecycleStatus.PAUSED_FOR_CONSENT,
                action=VoiceResponseAction.REQUEST_CONSENT,
                canonical_path=path,
                checkpoint_kind=outcome.checkpoint_kind or "consent_attestation",
                message="applicant action/consent required before disclosing",
            )
        session.lifecycle_status = VoiceLifecycleStatus.PAUSED_FOR_APPLICANT
        self._accumulate_pending_path(session, path)
        self._touch(session)
        return self._decision(
            session,
            lifecycle=VoiceLifecycleStatus.PAUSED_FOR_APPLICANT,
            action=VoiceResponseAction.PAUSE_FOR_APPLICANT,
            canonical_path=path,
            message="field missing - asked via Issue #5; resume when answered",
        )

    def _accumulate_pending_path(self, session: VoiceSession, path: str) -> None:
        """Batch missing canonical paths (Prompt 2): append, never overwrite."""
        if path and path not in session.pending_field_paths:
            session.pending_field_paths = [*session.pending_field_paths, path]

    # -- helpers --------------------------------------------------------

    def _speak_value(self, session: VoiceSession, path: str) -> None:
        """JIT retrieve a scalar/collection value, speak it, then discard.

        The value lives only in the transport boundary and is discarded
        immediately after speaking (Prompt 2 privacy hardening). Evidence
        records the canonical PATH + operation, never the value.
        """
        is_collection = path in ("product_data.vehicles", "product_data.drivers")
        if is_collection:
            value = self._values.collection_length(session.intake_session_id, path)
        else:
            value = self._values.get(session.intake_session_id, path)
        self._transport.speak(session.voice_session_id, safe_render(value, path), path=path)
        self._transport.discard_last_spoken()
        if self._sink.enabled:
            self._emit(
                session.intake_session_id or "",
                field_interaction_draft(
                    session.intake_session_id or "",
                    source_channel=SourceChannel.VOICE,
                    plan_id=None,
                    planned_route_id=session.planned_route_id or session.registry_id,
                    registry_id=session.registry_id,
                    distinct_rate_source_id=session.distinct_rate_source_id,
                    attempt_id=session.recovery_attempt_id,
                    parent_attempt_id=session.source_attempt_id,
                    source_session_id=session.voice_session_id,
                    canonical_path=path,
                    transformation="collection_length" if is_collection else None,
                    interaction_type="filled",
                    success=True,
                ),
            )

    def _decision(
        self,
        session: VoiceSession,
        *,
        action: str,
        lifecycle: Optional[str] = None,
        message: Optional[str] = None,
        canonical_path: Optional[str] = None,
        value_present: bool = False,
        checkpoint_kind: Optional[str] = None,
        reason_code: Optional[str] = None,
        requires_consent_expansion: bool = False,
        safe_reference_handle: Optional[str] = None,
    ) -> VoiceDecision:
        # Prompt 2 unattended-execution counters (safe metadata).
        if action in _INTERRUPT_ACTIONS:
            session.applicant_interruptions += 1
        elif action == VoiceResponseAction.DISCLOSE_VALUE:
            session.automated_answers += 1
        self._touch(session)
        return VoiceDecision(
            voice_session_id=session.voice_session_id,
            lifecycle_status=lifecycle or session.lifecycle_status,
            disclosure_status=session.disclosure_status,
            action=action,
            canonical_path=canonical_path,
            value_present=value_present,
            checkpoint_kind=checkpoint_kind,
            reason_code=reason_code,
            requires_consent_expansion=requires_consent_expansion,
            recording_consent=RecordingConsentStatus.NOT_REQUESTED,
            transcription_consent=TranscriptionConsentStatus.NOT_REQUESTED,
            safe_reference_handle=safe_reference_handle,
            message=message,
        )

    def _touch(self, session: VoiceSession) -> VoiceSession:
        session.updated_at = datetime.now(timezone.utc)
        session.route_status = derive_voice_route_status(session)
        return self._store.save(session)

    def _ensure_recovery_attempt(self, session: VoiceSession) -> None:
        """Give every voice session its OWN Issue #8 attempt identity.

        A voice continuation of a browser route (e.g. callback_required) gets a
        NEW attempt linked to the source browser attempt via ``parent_attempt_id``
        so it may progress independently (to quote_pending_normalization / an
        estimate / its own failure) WITHOUT mutating the immutable browser
        terminal attempt. Falls back to implicit resolution when the recovery
        source has no ``begin_attempt`` (never crashes).
        """
        if session.recovery_attempt_id is not None:
            return
        begin = getattr(self._recovery, "begin_attempt", None)
        if begin is None:  # pragma: no cover - defaults are RecoveryEngine
            return
        attempt = begin(
            plan_id=None,
            planned_route_id=session.planned_route_id or session.registry_id,
            registry_id=session.registry_id,
            distinct_rate_source_id=session.distinct_rate_source_id,
            channel=SourceChannel.VOICE,
            parent_attempt_id=session.source_attempt_id,
            intake_session_id=session.intake_session_id,
        )
        session.recovery_attempt_id = attempt.attempt_id

    def _finish(self, session: VoiceSession, reason: str) -> None:
        """Notify the transport that the route-local call is complete."""
        self._transport.end_session(session.voice_session_id, reason)
        self._touch(session)
