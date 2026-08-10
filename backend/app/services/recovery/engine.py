"""Recovery engine core (Issue #8).

Deterministic, bounded, explainable recovery/recovery-orchestration layer.

- It CHOOSES the ``RecoveryAction``; it NEVER executes browser retries, phone
  calls, or user answers (browser = Issue #7, voice = Issue #9, intake = #5).
- Retries/failovers are bounded by the data-driven ``RecoveryPolicy``.
- Pauses are distinct from failures and consume NO attempt budget.
- Safety boundaries (CAPTCHA/bot/consent-denied/prohibited) are never retried
  or bypassed.
- No LLM; no insurer-specific branching; no applicant PII in any model.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Optional, Protocol, runtime_checkable

from ...browser.route_identity import registry_id_for_planned_route
from ...models.recovery import (
    AttemptLifecycleStatus,
    AttemptRecord,
    ExecutionObservation,
    RecoveryAction,
    RecoveryDecideRequest,
    RecoveryDecision,
    RecoveryPolicy,
    RecoveryReasonCode,
    Retryability,
    RouteOutcomeStatus,
    SourceChannel,
)
from ...services.route_planner import get_route_planner
from .attempt_store import AttemptStore, InMemoryAttemptStore, TransitionError
from .classification import ClassifiedObservation, classify_observation
from .policy import RecoveryPolicyLoader

# Terminal-ish actions that are safe to return as-is from a classified spec.
_TERMINAL_ACTIONS = {
    RecoveryAction.MANUAL_HANDOFF.value,
    RecoveryAction.STOP_TERMINAL.value,
    RecoveryAction.PREPARE_VOICE_HANDOFF.value,
}


@runtime_checkable
class RecoveryRouteSource(Protocol):
    """Source of ready alternative routes for one rate source (Issue #6)."""

    def alternatives_for(
        self,
        *,
        plan_id: Optional[str],
        registry_id: str,
        distinct_rate_source_id: Optional[str],
        intake_session_id: Optional[str],
    ) -> list[str]:
        ...


class PlannerRouteSource:
    """Alternatives from the real Issue #6 ``RoutePlan`` (data-driven).

    Consumes the planner's primary/alternative relationships automatically, so
    adding/removing/reordering alternatives in Issue #6 data changes recovery
    behavior without any ``RecoveryEngine`` modification.
    """

    def __init__(self, planner: Optional[Any] = None) -> None:
        self._planner = planner or get_route_planner()

    def alternatives_for(
        self,
        *,
        plan_id: Optional[str],
        registry_id: str,
        distinct_rate_source_id: Optional[str],
        intake_session_id: Optional[str],
    ) -> list[str]:
        if not intake_session_id:
            return []
        try:
            plan = self._planner.plan(intake_session_id)
        except Exception:
            return []
        rs_id = distinct_rate_source_id
        if not rs_id:
            for route in plan.routes:
                if route.registry_id == registry_id:
                    rs_id = route.distinct_rate_source_id
                    break
        if not rs_id:
            return []
        return sorted(
            route.registry_id
            for route in plan.routes
            if route.registry_id != registry_id
            and route.distinct_rate_source_id == rs_id
            and route.is_alternative
            and route.is_ready
        )

    def blocked_alternatives(
        self,
        *,
        plan_id: Optional[str],
        registry_id: str,
        distinct_rate_source_id: Optional[str],
        intake_session_id: Optional[str],
    ) -> list[tuple[str, list[str]]]:
        """Non-ready alternatives and WHY (blocker kinds), for a conservative
        pause/request instead of blindly executing an invalid route."""
        if not intake_session_id:
            return []
        try:
            plan = self._planner.plan(intake_session_id)
        except Exception:
            return []
        rs_id = distinct_rate_source_id
        if not rs_id:
            for route in plan.routes:
                if route.registry_id == registry_id:
                    rs_id = route.distinct_rate_source_id
                    break
        if not rs_id:
            return []
        blocked: list[tuple[str, list[str]]] = []
        for route in plan.routes:
            if (
                route.registry_id != registry_id
                and route.distinct_rate_source_id == rs_id
                and route.is_alternative
                and not route.is_ready
            ):
                reasons = sorted({b.kind.value for b in route.blockers})
                blocked.append((route.registry_id, reasons))
        return blocked


@runtime_checkable
class RecoveryConsentSource(Protocol):
    """Current Issue #5 route-disclosure consent state (never stale copies)."""

    def route_consent_state(self, intake_session_id: Optional[str], registry_id: str) -> Optional[str]:
        """Return ``granted`` | ``denied`` | ``undecided`` | ``None``."""
        ...


class IntakeConsentSource:
    """Consent source over the real Issue #5 ``IntakeEngine``."""

    def __init__(self, engine: Optional[Any] = None) -> None:
        from ...services.intake import get_intake_engine

        self._engine = engine or get_intake_engine()

    def route_consent_state(self, intake_session_id: Optional[str], registry_id: str) -> Optional[str]:
        if not intake_session_id:
            return None
        try:
            return self._engine.route_consent_state(intake_session_id, registry_id)
        except Exception:
            return None


class RecoveryEngine:
    """Deterministic recovery decision engine."""

    def __init__(
        self,
        store: Optional[AttemptStore] = None,
        policy: Optional[RecoveryPolicy] = None,
        route_source: Optional[RecoveryRouteSource] = None,
        policy_loader: Optional[RecoveryPolicyLoader] = None,
        consent_source: Optional[RecoveryConsentSource] = None,
    ) -> None:
        self._store: AttemptStore = store or InMemoryAttemptStore()
        self._policy = policy if policy is not None else (policy_loader or RecoveryPolicyLoader()).load()
        self._route_source = route_source or PlannerRouteSource()
        self._consent_source = consent_source

    # --- lifecycle -----------------------------------------------------

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
    ) -> AttemptRecord:
        resolved_registry = registry_id or registry_id_for_planned_route(planned_route_id)
        attempt = AttemptRecord(
            attempt_id=uuid.uuid4().hex,
            plan_id=plan_id,
            planned_route_id=planned_route_id,
            registry_id=resolved_registry,
            distinct_rate_source_id=distinct_rate_source_id,
            attempt_number=self._store.next_attempt_number(plan_id, distinct_rate_source_id),
            channel=channel,
            started_at=dt.datetime.now(dt.timezone.utc),
            lifecycle_status=AttemptLifecycleStatus.RUNNING,
            parent_attempt_id=parent_attempt_id,
            alternative_of_attempt_id=alternative_of_attempt_id,
            policy_version=policy_version,
            plan_version=plan_version,
        )
        self._store.save(attempt)
        return attempt

    def get_attempt(self, attempt_id: str) -> Optional[AttemptRecord]:
        return self._store.get(attempt_id)

    def _resolve_current(self, request: RecoveryDecideRequest) -> Optional[AttemptRecord]:
        """Reuse an explicit or latest attempt; ``None`` when none exists yet.

        Idempotency/terminal-immutability: an explicitly referenced attempt is
        returned even when terminal (so duplicate submissions do not silently
        create a new attempt); otherwise the most recent non-terminal attempt
        wins, and a terminal-only history yields the latest terminal attempt
        (no auto-begin on duplicate processing).
        """
        if request.attempt_id:
            return self._store.get(request.attempt_id)
        registry_id = request.registry_id or registry_id_for_planned_route(request.planned_route_id)
        latest = self._store.list_by_route(request.plan_id, registry_id)
        latest.sort(key=lambda a: (a.started_at, a.attempt_id), reverse=True)
        if not latest:
            return None
        for attempt in latest:
            if attempt.lifecycle_status in {
                AttemptLifecycleStatus.PENDING,
                AttemptLifecycleStatus.RUNNING,
                AttemptLifecycleStatus.PAUSED,
                AttemptLifecycleStatus.RECOVERABLE,
            }:
                return attempt
        return latest[0]  # terminal-only history -> idempotent terminal handling

    # --- public helpers (used by the LangGraph workflow / API) -----------

    def resolve_current_attempt(self, request: RecoveryDecideRequest) -> AttemptRecord:
        """Return the attempt an observation should apply to (creating one if needed)."""
        attempt = self._resolve_current(request)
        if attempt is None:
            return self.begin_attempt(
                plan_id=request.plan_id,
                planned_route_id=request.planned_route_id,
                registry_id=request.registry_id or registry_id_for_planned_route(request.planned_route_id),
                distinct_rate_source_id=request.distinct_rate_source_id,
                channel=request.source_channel,
                policy_version=self.policy_for(request).version,
                plan_version=request.plan_version,
            )
        return attempt

    def route_attempts_used(self, plan_id: Optional[str], registry_id: str) -> int:
        """Non-pending attempts on this route (pauses never consume budget)."""
        return len(
            [a for a in self._store.list_by_route(plan_id, registry_id)
             if a.lifecycle_status is not AttemptLifecycleStatus.PENDING]
        )

    def rate_source_attempts_used(self, plan_id: Optional[str], distinct_rate_source_id: Optional[str]) -> int:
        """Non-pending attempts across all routes sharing one rate source."""
        if not distinct_rate_source_id:
            return 0
        return len(
            [a for a in self._store.list_by_rate_source(plan_id, distinct_rate_source_id)
             if a.lifecycle_status is not AttemptLifecycleStatus.PENDING]
        )

    def policy_for(self, request: RecoveryDecideRequest) -> RecoveryPolicy:
        """Resolve the effective policy (request override or configured default)."""
        return request.policy or self._policy

    # --- decision ------------------------------------------------------

    def decide(
        self,
        request: RecoveryDecideRequest,
        current_attempt: Optional[AttemptRecord] = None,
    ) -> RecoveryDecision:
        policy = request.policy or self._policy
        attempt = current_attempt or self._resolve_current(request)
        if attempt is None:
            attempt = self.begin_attempt(
                plan_id=request.plan_id,
                planned_route_id=request.planned_route_id,
                registry_id=request.registry_id or registry_id_for_planned_route(request.planned_route_id),
                distinct_rate_source_id=request.distinct_rate_source_id,
                channel=request.source_channel,
            )
        execution = ExecutionObservation(
            source_channel=request.source_channel,
            observation_type=request.observation_type,
            reason=request.reason,
            safe_context=dict(request.safe_context or {}),
        )
        classified = classify_observation(execution, policy)
        registry_id = request.registry_id or attempt.registry_id or registry_id_for_planned_route(request.planned_route_id)
        plan_id = request.plan_id or attempt.plan_id
        rs_id = request.distinct_rate_source_id or attempt.distinct_rate_source_id

        route_used = len([a for a in self._store.list_by_route(plan_id, registry_id)
                          if a.lifecycle_status is not AttemptLifecycleStatus.PENDING])
        rs_used = len([a for a in self._store.list_by_rate_source(plan_id, rs_id)
                       if a.lifecycle_status is not AttemptLifecycleStatus.PENDING]) if rs_id else route_used
        plan_used = len([a for a in self._store.list_by_plan(plan_id)
                         if a.lifecycle_status is not AttemptLifecycleStatus.PENDING]) if plan_id else route_used

        ctx: dict[str, Any] = dict(request.safe_context or {})
        ctx.update({
            "route_attempts_used": route_used,
            "rate_source_attempts_used": rs_used,
            "plan_attempts_used": plan_used,
            "max_attempts_per_route": policy.max_attempts_per_route,
            "max_attempts_per_rate_source": policy.max_attempts_per_rate_source,
            "policy_version": policy.version,
        })
        policy_version = policy.version
        plan_version = request.plan_version or attempt.plan_version

        def build(
            lifecycle: AttemptLifecycleStatus,
            action: RecoveryAction,
            *,
            retry_allowed: bool = False,
            terminal_status: Optional[RouteOutcomeStatus] = None,
            alternative_route_id: Optional[str] = None,
            attempts_used: Optional[int] = None,
            attempts_remaining: Optional[int] = None,
            reason_codes: Optional[list[str]] = None,
        ) -> RecoveryDecision:
            return RecoveryDecision(
                decision_id=uuid.uuid4().hex,
                attempt_id=attempt.attempt_id,
                plan_id=plan_id,
                planned_route_id=request.planned_route_id,
                registry_id=registry_id,
                distinct_rate_source_id=rs_id,
                lifecycle_status=lifecycle,
                recommended_action=action,
                reason_codes=reason_codes if reason_codes is not None else list(classified.reason_codes),
                retry_allowed=retry_allowed,
                attempts_used=attempts_used if attempts_used is not None else route_used,
                attempts_remaining=attempts_remaining if attempts_remaining is not None else max(0, policy.max_attempts_per_route - route_used),
                alternative_route_id=alternative_route_id,
                terminal_status=terminal_status,
                quote_pending_normalization=classified.quote_pending_normalization,
                policy_version=policy_version,
                plan_version=plan_version,
                safe_context=ctx,
                decided_at=dt.datetime.now(dt.timezone.utc),
            )

        # Pauses and in-progress observations are NOT terminal and consume NO budget.
        if classified.lifecycle_hint in (AttemptLifecycleStatus.PAUSED, AttemptLifecycleStatus.RUNNING):
            return build(
                classified.lifecycle_hint,
                RecoveryAction(classified.action_hint),
                attempts_used=route_used,
                attempts_remaining=max(0, policy.max_attempts_per_route - route_used),
            )

        # Deterministic same-route retry (only explicitly retryable + in budget).
        # Budgets: per route, per rate source, and per plan all bound retries so
        # "Route A retry -> Route B retry -> ..." can never loop past the caps.
        if classified.retryability is Retryability.RETRYABLE:
            gate = self._consent_gate(request, registry_id, policy, build, route_used, rs_used)
            if gate is not None:
                return gate  # consent revoked/undecided -> no retry
            if (
                route_used < policy.max_attempts_per_route
                and (rs_id is None or rs_used < policy.max_attempts_per_rate_source)
                and (plan_id is None or plan_used < policy.max_attempts_per_plan)
            ):
                return build(
                    AttemptLifecycleStatus.RECOVERABLE,
                    RecoveryAction.RETRY_SAME_ROUTE,
                    retry_allowed=True,
                    attempts_used=route_used,
                    attempts_remaining=max(0, policy.max_attempts_per_route - route_used),
                )
            return self._failover_or_terminal(
                build, request, attempt, classified, policy, registry_id, rs_id,
                plan_id, rs_used, plan_used, reason_codes=list(classified.reason_codes),
                budget_exhausted=True,
            )

        # Quote observation: stop retrying; comparability is NEVER classified
        # here (Issues #11/#12). terminal_status stays None pending normalization.
        if classified.quote_pending_normalization:
            return build(
                AttemptLifecycleStatus.TERMINAL,
                RecoveryAction.STOP_TERMINAL,
                attempts_used=route_used,
                attempts_remaining=max(0, policy.max_attempts_per_rate_source - rs_used),
            )

        # NON_RETRYABLE / UNKNOWN with an unambiguous terminal status.
        if classified.terminal_status is not None:
            return build(
                AttemptLifecycleStatus.TERMINAL,
                RecoveryAction(self._terminal_action(classified)),
                terminal_status=RouteOutcomeStatus(classified.terminal_status),
                attempts_used=route_used,
                attempts_remaining=max(0, policy.max_attempts_per_rate_source - rs_used),
            )

        # Terminal safety/consent stop with NO coverage status (e.g. consent
        # denied = route exclusion, never ineligible and never unresolved).
        if (
            classified.terminal_status is None
            and classified.fallback_terminal_status is None
            and not classified.failover_eligible
        ):
            return build(
                AttemptLifecycleStatus.TERMINAL,
                RecoveryAction(self._terminal_action(classified)),
                attempts_used=route_used,
                attempts_remaining=max(0, policy.max_attempts_per_rate_source - rs_used),
            )

        # Terminal without an unambiguous status -> failover (if eligible) or fallback.
        return self._failover_or_terminal(
            build, request, attempt, classified, policy, registry_id, rs_id,
            plan_id, rs_used, plan_used, reason_codes=list(classified.reason_codes),
            budget_exhausted=False,
        )

    def _failover_or_terminal(
        self,
        build: Any,
        request: RecoveryDecideRequest,
        attempt: AttemptRecord,
        classified: ClassifiedObservation,
        policy: RecoveryPolicy,
        registry_id: str,
        rs_id: Optional[str],
        plan_id: Optional[str],
        rs_used: int,
        plan_used: int,
        *,
        reason_codes: list[str],
        budget_exhausted: bool,
    ) -> RecoveryDecision:
        if (
            classified.failover_eligible
            and policy.alternative_route_after_exhaustion
            and (rs_id is None or rs_used < policy.max_attempts_per_rate_source)
            and (plan_id is None or plan_used < policy.max_attempts_per_plan)
        ):
            alternative = self._pick_alternative(request, registry_id, rs_id, plan_id)
            if alternative:
                return build(
                    AttemptLifecycleStatus.RECOVERABLE,
                    RecoveryAction.USE_ALTERNATIVE_ROUTE,
                    retry_allowed=True,
                    alternative_route_id=alternative,
                    attempts_used=rs_used,
                    attempts_remaining=max(0, policy.max_attempts_per_rate_source - rs_used),
                    reason_codes=[*reason_codes, RecoveryReasonCode.ALTERNATE_ROUTE_AVAILABLE.value],
                )
            # No READY alternative - but a non-ready alternative may simply need
            # a field/consent from the user: pause/request, do NOT execute an
            # invalid route or blindly go terminal.
            blocked = self._blocked_alternative_reasons(request, registry_id, rs_id, plan_id)
            pause_codes: list[str] = []
            for _alt_id, reasons in blocked:
                if "consent_required" in reasons and RecoveryReasonCode.CONSENT_REQUIRED.value not in pause_codes:
                    pause_codes.append(RecoveryReasonCode.CONSENT_REQUIRED.value)
                if any(r in reasons for r in ("missing_field", "human_required")) and \
                        RecoveryReasonCode.MISSING_FIELD.value not in pause_codes:
                    pause_codes.append(RecoveryReasonCode.MISSING_FIELD.value)
            if pause_codes:
                return build(
                    AttemptLifecycleStatus.PAUSED,
                    RecoveryAction.RESUME_AFTER_USER_INPUT,
                    retry_allowed=False,
                    attempts_used=rs_used,
                    attempts_remaining=max(0, policy.max_attempts_per_rate_source - rs_used),
                    reason_codes=[*reason_codes, *pause_codes],
                )
        terminal_status_value = (
            classified.terminal_status
            or classified.fallback_terminal_status
            or RouteOutcomeStatus.UNRESOLVED.value
        )
        terminal_reasons = list(reason_codes)
        if budget_exhausted:
            terminal_reasons.append(RecoveryReasonCode.RETRY_BUDGET_EXHAUSTED.value)
            terminal_reasons.append(RecoveryReasonCode.NO_ALTERNATIVE_ROUTE.value)
        return build(
            AttemptLifecycleStatus.TERMINAL,
            RecoveryAction.STOP_TERMINAL,
            terminal_status=RouteOutcomeStatus(terminal_status_value),
            attempts_used=rs_used,
            attempts_remaining=max(0, policy.max_attempts_per_rate_source - rs_used),
            reason_codes=terminal_reasons,
        )

    def _pick_alternative(
        self,
        request: RecoveryDecideRequest,
        registry_id: str,
        rs_id: Optional[str],
        plan_id: Optional[str],
    ) -> Optional[str]:
        candidates = self._route_source.alternatives_for(
            plan_id=plan_id,
            registry_id=registry_id,
            distinct_rate_source_id=rs_id,
            intake_session_id=request.intake_session_id,
        )
        # Never re-pick a route already attempted (works even without plan_id).
        source = self._store.list_by_plan(plan_id) if plan_id else self._store.list_all()
        attempted = {
            a.registry_id for a in source
            if a.registry_id and a.lifecycle_status is not AttemptLifecycleStatus.PENDING
        }
        for candidate in candidates:
            if candidate not in attempted:
                return candidate
        return None

    def _blocked_alternative_reasons(
        self,
        request: RecoveryDecideRequest,
        registry_id: str,
        rs_id: Optional[str],
        plan_id: Optional[str],
    ) -> list[tuple[str, list[str]]]:
        """Non-ready alternatives and their blocker kinds (Issue #6 data)."""
        if not hasattr(self._route_source, "blocked_alternatives"):
            return []
        return self._route_source.blocked_alternatives(  # type: ignore[attr-defined]
            plan_id=plan_id,
            registry_id=registry_id,
            distinct_rate_source_id=rs_id,
            intake_session_id=request.intake_session_id,
        )

    @staticmethod
    def _terminal_action(classified: ClassifiedObservation) -> str:
        if classified.action_hint in _TERMINAL_ACTIONS:
            return classified.action_hint
        return RecoveryAction.STOP_TERMINAL.value

    def _consent_gate(
        self,
        request: RecoveryDecideRequest,
        registry_id: str,
        policy: RecoveryPolicy,
        build: Any,
        route_used: int,
        rs_used: int,
    ) -> Optional[RecoveryDecision]:
        """Respect CURRENT Issue #5 consent before re-executing a route.

        Consent is never copied into attempt metadata - it is checked live so a
        revocation between attempts prevents a retry. ``denied`` -> terminal
        stop (never ineligible); ``undecided`` -> pause for consent.
        """
        if self._consent_source is None:
            return None
        state = self._consent_source.route_consent_state(request.intake_session_id, registry_id)
        if state == "granted":
            return None
        if state == "denied":
            return build(
                AttemptLifecycleStatus.TERMINAL,
                RecoveryAction.STOP_TERMINAL,
                reason_codes=[RecoveryReasonCode.CONSENT_DENIED.value],
                attempts_used=route_used,
                attempts_remaining=max(0, policy.max_attempts_per_rate_source - rs_used),
            )
        return build(
            AttemptLifecycleStatus.PAUSED,
            RecoveryAction.RESUME_AFTER_USER_INPUT,
            reason_codes=[RecoveryReasonCode.CONSENT_REQUIRED.value],
            attempts_used=route_used,
            attempts_remaining=max(0, policy.max_attempts_per_route - route_used),
        )

    # --- attempt recording (with terminal immutability) -----------------

    @staticmethod
    def _observation_key(request: RecoveryDecideRequest) -> str:
        return f"{request.observation_type}|{request.reason or ''}"

    def _idempotent_decision(
        self,
        request: RecoveryDecideRequest,
        attempt: AttemptRecord,
        policy: Optional[RecoveryPolicy] = None,
    ) -> RecoveryDecision:
        """Reflect the attempt's CURRENT state - no mutation, no budget change.

        Used for terminal reprocessing, stale observations, and duplicates.
        """
        policy = policy or self.policy_for(request)
        registry_id = attempt.registry_id or request.registry_id
        plan_id = request.plan_id or attempt.plan_id
        rs_id = request.distinct_rate_source_id or attempt.distinct_rate_source_id
        route_used = self.route_attempts_used(plan_id, registry_id)
        rs_used = self.rate_source_attempts_used(plan_id, rs_id)
        return RecoveryDecision(
            decision_id=uuid.uuid4().hex,
            attempt_id=attempt.attempt_id,
            plan_id=plan_id,
            planned_route_id=request.planned_route_id or attempt.planned_route_id or attempt.registry_id,
            registry_id=registry_id,
            distinct_rate_source_id=rs_id,
            lifecycle_status=attempt.lifecycle_status,
            recommended_action=attempt.recovery_action or RecoveryAction.NO_ACTION,
            reason_codes=list(attempt.reason_codes),
            retry_allowed=False,
            attempts_used=route_used,
            attempts_remaining=max(0, policy.max_attempts_per_route - route_used),
            terminal_status=attempt.terminal_status,
            quote_pending_normalization=attempt.quote_pending_normalization,
            policy_version=attempt.policy_version,
            plan_version=attempt.plan_version,
            safe_context=dict(request.safe_context or {}),
            decided_at=dt.datetime.now(dt.timezone.utc),
        )

    def record_observation(
        self,
        request: RecoveryDecideRequest,
        current_attempt: Optional[AttemptRecord] = None,
    ) -> RecoveryDecision:
        """Record an observation: decide + apply lifecycle to the attempt.

        Hardening guarantees:
        - terminal immutability (no silent mutation, no second terminal).
        - per-attempt duplicate guard (same observation key -> idempotent).
        - stale/out-of-order guard via ``observation_sequence``.
        - browser-session-lost-while-paused -> explicit close + bounded restart.
        - every mutation bumps ``revision`` (store-level).
        """
        attempt = current_attempt or self._resolve_current(request)
        policy = self.policy_for(request)

        # Plan/registry relationship changed mid-recovery: record the outcome on
        # a NEW attempt so existing attempt history is never rewritten.
        if request.observation_type == "route_invalid":
            invalid_attempt = self.begin_attempt(
                plan_id=request.plan_id,
                planned_route_id=request.planned_route_id,
                registry_id=request.registry_id or registry_id_for_planned_route(request.planned_route_id),
                distinct_rate_source_id=request.distinct_rate_source_id,
                channel=request.source_channel,
                policy_version=policy.version,
                plan_version=request.plan_version,
            )
            invalid_decision = self.decide(request, invalid_attempt)
            self._store.update(
                invalid_attempt.attempt_id,
                lifecycle_status=AttemptLifecycleStatus.TERMINAL,
                observation_type=request.observation_type,
                reason_codes=invalid_decision.reason_codes,
                recovery_action=invalid_decision.recommended_action,
                terminal_status=invalid_decision.terminal_status,
                ended_at=dt.datetime.now(dt.timezone.utc),
                last_observation_key=self._observation_key(request),
                last_observation_sequence=request.observation_sequence,
            )
            return invalid_decision

        if attempt is None:
            attempt = self.begin_attempt(
                plan_id=request.plan_id,
                planned_route_id=request.planned_route_id,
                registry_id=request.registry_id or registry_id_for_planned_route(request.planned_route_id),
                distinct_rate_source_id=request.distinct_rate_source_id,
                channel=request.source_channel,
                policy_version=policy.version,
                plan_version=request.plan_version,
            )
        now = dt.datetime.now(dt.timezone.utc)
        obs_key = self._observation_key(request)

        # Stale / out-of-order observation: older sequence -> safe idempotent.
        if (
            request.observation_sequence is not None
            and attempt.last_observation_sequence is not None
            and request.observation_sequence <= attempt.last_observation_sequence
        ):
            return self._idempotent_decision(request, attempt, policy)

        # Duplicate observation on the same attempt -> safe idempotent.
        if attempt.last_observation_key is not None and attempt.last_observation_key == obs_key:
            return self._idempotent_decision(request, attempt, policy)

        # Terminal immutability: no mutation, no new attempt, no double budget.
        if attempt.lifecycle_status is AttemptLifecycleStatus.TERMINAL:
            return self._idempotent_decision(request, attempt, policy)

        decision = self.decide(request, attempt)

        stamp = {
            "last_observation_key": obs_key,
            "last_observation_sequence": request.observation_sequence,
            "policy_version": decision.policy_version,
            "plan_version": decision.plan_version,
        }

        # Paused attempt + retry/failover = the browser session was lost while
        # paused (cannot resume the same session). Close the paused attempt
        # (explicit, evidence-backed) and return the bounded retry/failover
        # decision; attempt numbering continues on a fresh attempt.
        if (
            attempt.lifecycle_status is AttemptLifecycleStatus.PAUSED
            and decision.lifecycle_status is AttemptLifecycleStatus.RECOVERABLE
        ):
            try:
                self._store.update(
                    attempt.attempt_id,
                    lifecycle_status=AttemptLifecycleStatus.TERMINAL,
                    observation_type=request.observation_type,
                    reason_codes=[RecoveryReasonCode.RESUME_SESSION_UNAVAILABLE.value, *decision.reason_codes],
                    recovery_action=decision.recommended_action,
                    ended_at=now,
                    **stamp,
                )
            except TransitionError:  # already terminal - idempotent
                pass
            return decision.model_copy(
                update={"reason_codes": [RecoveryReasonCode.RESUME_SESSION_UNAVAILABLE.value, *decision.reason_codes]}
            )

        if decision.lifecycle_status is AttemptLifecycleStatus.PAUSED:
            self._store.update(
                attempt.attempt_id,
                lifecycle_status=AttemptLifecycleStatus.PAUSED,
                observation_type=request.observation_type,
                reason_codes=decision.reason_codes,
                recovery_action=decision.recommended_action,
                **stamp,
            )
        elif decision.lifecycle_status is AttemptLifecycleStatus.RUNNING:
            # In-progress observation (page_loaded/fields_filled) - resume of
            # the same attempt; pauses/recoverable consume no new budget.
            self._store.update(
                attempt.attempt_id,
                lifecycle_status=AttemptLifecycleStatus.RUNNING,
                observation_type=request.observation_type,
                reason_codes=decision.reason_codes,
                recovery_action=decision.recommended_action,
                **stamp,
            )
        elif decision.lifecycle_status is AttemptLifecycleStatus.TERMINAL:
            self._store.update(
                attempt.attempt_id,
                lifecycle_status=AttemptLifecycleStatus.TERMINAL,
                observation_type=request.observation_type,
                reason_codes=decision.reason_codes,
                recovery_action=decision.recommended_action,
                terminal_status=decision.terminal_status,
                quote_pending_normalization=decision.quote_pending_normalization,
                ended_at=now,
                **stamp,
            )
        elif decision.recommended_action is RecoveryAction.USE_ALTERNATIVE_ROUTE:
            self._store.update(
                attempt.attempt_id,
                lifecycle_status=AttemptLifecycleStatus.TERMINAL,
                observation_type=request.observation_type,
                reason_codes=decision.reason_codes,
                recovery_action=decision.recommended_action,
                ended_at=now,
                **stamp,
            )
        else:  # RECOVERABLE (retry_same_route)
            self._store.update(
                attempt.attempt_id,
                lifecycle_status=AttemptLifecycleStatus.RECOVERABLE,
                observation_type=request.observation_type,
                reason_codes=decision.reason_codes,
                recovery_action=decision.recommended_action,
                **stamp,
            )
        return decision

    def enrich_terminal(
        self,
        attempt_id: str,
        *,
        terminal_status: Optional[RouteOutcomeStatus] = None,
        reason_codes: Optional[list[str]] = None,
        note: Optional[str] = None,
    ) -> AttemptRecord:
        """EXPLICIT terminal enrichment (e.g. Issue #11/#12 later finalize
        quote comparability). Never a silent overwrite: requires an explicit call
        and keeps the lifecycle terminal."""
        attempt = self._store.get(attempt_id)
        if attempt is None:
            raise KeyError(attempt_id)
        changes: dict[str, object] = {}
        if terminal_status is not None:
            changes["terminal_status"] = terminal_status
        if reason_codes is not None:
            changes["reason_codes"] = list(dict.fromkeys([*attempt.reason_codes, *reason_codes]))
        if note is not None:
            changes["notes"] = note
        if not changes:
            return attempt
        return self._store.update(attempt_id, allow_terminal_mutation=True, **changes)

    # --- coverage-helper: unused duplicate alternative -------------------

    def classify_unused_alternative(
        self,
        *,
        plan_id: Optional[str],
        registry_id: str,
        distinct_rate_source_id: Optional[str],
    ) -> RecoveryDecision:
        """Represent a confirmed duplicate alternative that never needs execution
        as ``duplicate_rate_source`` in coverage accounting. An alternative that
        WAS executed after failover keeps its own execution outcome instead."""
        attempt = self.begin_attempt(
            plan_id=plan_id,
            planned_route_id=registry_id,
            registry_id=registry_id,
            distinct_rate_source_id=distinct_rate_source_id,
        )
        decision = RecoveryDecision(
            decision_id=uuid.uuid4().hex,
            attempt_id=attempt.attempt_id,
            plan_id=plan_id,
            planned_route_id=registry_id,
            registry_id=registry_id,
            distinct_rate_source_id=distinct_rate_source_id,
            lifecycle_status=AttemptLifecycleStatus.TERMINAL,
            recommended_action=RecoveryAction.STOP_TERMINAL,
            reason_codes=[RecoveryReasonCode.DUPLICATE_RATE_SOURCE.value],
            retry_allowed=False,
            attempts_used=attempt.attempt_number,
            attempts_remaining=0,
            terminal_status=RouteOutcomeStatus.DUPLICATE_RATE_SOURCE,
            safe_context={"duplicate_alternative": True, "executed": False},
            decided_at=dt.datetime.now(dt.timezone.utc),
        )
        self._store.update(
            attempt.attempt_id,
            lifecycle_status=AttemptLifecycleStatus.TERMINAL,
            recovery_action=RecoveryAction.STOP_TERMINAL,
            reason_codes=decision.reason_codes,
            terminal_status=RouteOutcomeStatus.DUPLICATE_RATE_SOURCE,
            ended_at=dt.datetime.now(dt.timezone.utc),
        )
        return decision

    def list_attempts(self, plan_id: Optional[str] = None) -> list[AttemptRecord]:
        if plan_id:
            return self._store.list_by_plan(plan_id)
        return self._store.list_all()
