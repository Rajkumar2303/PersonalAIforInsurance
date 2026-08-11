"""Phone-handoff context builders (Issue #9, Prompt 1).

Deterministic helpers that assemble a safe ``PhoneHandoffContext`` from:
- an Issue #8 ``RecoveryDecision`` recommending ``prepare_voice_handoff``
  (browser callback observed, e.g. Sonnet), or
- a phone/callback ``PlannedRoute`` from the Issue #6 route planner.

Only ids, public route metadata, canonical paths, and safe flags are carried.
Never applicant values, never the applicant's phone number.
"""

from __future__ import annotations

from typing import Optional

from ...browser.route_identity import planned_route_id_for_registry
from ...models.recovery import RecoveryDecision, RouteOutcomeStatus
from ...models.route_planner import PlannedRoute, RouteChannelKind
from ...models.voice import PhoneHandoffContext, SourceChannel


def _provider_phone_from_route(route: PlannedRoute) -> Optional[str]:
    """Public provider phone/callback from a planned route (safe metadata)."""
    for channel in route.channels:
        if channel.kind in (RouteChannelKind.PHONE, RouteChannelKind.CALLBACK, RouteChannelKind.BROKER):
            if channel.value:
                return channel.value
    return None


def _callback_reason(decision: Optional[RecoveryDecision]) -> Optional[str]:
    if decision is None:
        return None
    reason_codes = decision.reason_codes or []
    if decision.terminal_status is RouteOutcomeStatus.CALLBACK_REQUIRED:
        return "callback_required"
    if decision.recommended_action.value == "prepare_voice_handoff":
        return "callback_required"
    if reason_codes:
        return ";".join(reason_codes[:3])
    return None


def handoff_context_from_recovery(
    *,
    decision: RecoveryDecision,
    intake_session_id: str,
    registry_id: str,
    distinct_rate_source_id: Optional[str] = None,
    planned_route_id: Optional[str] = None,
    provider_phone_route: Optional[str] = None,
    authorized_canonical_paths: Optional[list[str]] = None,
    missing_canonical_paths: Optional[list[str]] = None,
    reference_present: bool = False,
    private_reference_handle: Optional[str] = None,
) -> PhoneHandoffContext:
    """Build a handoff context from an Issue #8 recovery decision that chose
    ``prepare_voice_handoff`` (browser callback observed)."""
    return PhoneHandoffContext(
        intake_session_id=intake_session_id,
        registry_id=registry_id,
        distinct_rate_source_id=distinct_rate_source_id,
        planned_route_id=planned_route_id or decision.planned_route_id,
        source_attempt_id=decision.attempt_id,
        source_channel=SourceChannel.BROWSER,
        target_channel=SourceChannel.PHONE,
        callback_reason=_callback_reason(decision),
        provider_phone_route=provider_phone_route,
        authorized_canonical_paths=list(authorized_canonical_paths or []),
        missing_canonical_paths=list(missing_canonical_paths or []),
        reference_present=reference_present or bool(decision.safe_context.get("reference_present")),
        private_reference_handle=private_reference_handle
        or decision.safe_context.get("private_reference_handle"),
        recovery_reason_codes=list(decision.reason_codes or []),
        route_consent_state=decision.safe_context.get("consent_state"),
    )


def handoff_context_from_phone_route(
    *,
    route: PlannedRoute,
    intake_session_id: str,
    registry_id: str,
    source_attempt_id: Optional[str] = None,
    reference_present: bool = False,
    private_reference_handle: Optional[str] = None,
) -> PhoneHandoffContext:
    """Build a handoff context for a phone/callback/broker-only planned route.

    ``planned_route_id`` uses the Issue #7 route-identity compat shim (today
    it equals the registry_id); ``requirements`` are canonical-path strings.
    """
    return PhoneHandoffContext(
        intake_session_id=intake_session_id,
        registry_id=registry_id or route.registry_id,
        distinct_rate_source_id=route.distinct_rate_source_id,
        planned_route_id=planned_route_id_for_registry(route.registry_id),
        source_attempt_id=source_attempt_id,
        source_channel=SourceChannel.BROWSER,
        target_channel=SourceChannel.PHONE,
        callback_reason=None,
        provider_phone_route=_provider_phone_from_route(route),
        authorized_canonical_paths=[],
        missing_canonical_paths=list(route.requirements or []),
        reference_present=reference_present,
        private_reference_handle=private_reference_handle,
        recovery_reason_codes=[],
        route_consent_state=None,
    )
