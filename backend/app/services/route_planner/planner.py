"""Deterministic route planner (Issue #6).

Builds a per-route, evidence-first plan for one intake session:

- product-aware AUTO routing (other products -> not-applicable plan),
- market registry integration (AUTO + active entries),
- Issue #4 dedup integration (confirmed duplicates grouped; possible/unresolved
  stay visible - never suppressed),
- per-route readiness with MULTIPLE simultaneous blockers,
- data-driven requirement resolver + Issue #5 missing-field integration,
- consent-state integration (route-disclosure consent),
- route channels (online/phone/callback/broker),
- deterministic ranking.

The planner receives only PRESENCE booleans from the profile source (never raw
values), so the plan carries canonical paths + public market data only - no
applicant PII. No LLM. No browser/voice execution (Issue #7+).
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Optional, Protocol, runtime_checkable

from ...models.dedup import ReasonCode
from ...models.insurance.enums import InsuranceType
from ...models.registry import MarketRegistryEntry, MarketRequirement, ProductScope
from ...models.route_planner import (
    PlannedRoute,
    RouteBlocker,
    RouteBlockerKind,
    RouteChannel,
    RouteChannelKind,
    RoutePlan,
    RoutePlanSummary,
)
from ..deduplication import RateSourceDeduplicationService
from ..intake.engine import IntakeEngine
from ..intake import get_intake_engine
from ..market_registry import MarketRegistryService
from .requirements import RequirementResolver

logger = logging.getLogger(__name__)


@runtime_checkable
class RoutePlannerProfileSource(Protocol):
    """Safe profile surface the planner depends on (never raw values)."""

    def get_session(self, session_id: str) -> Any:
        ...

    def check_supported(self, session: Any) -> bool:
        ...

    def field_presence(self, session_id: str, paths: list[str]) -> dict[str, bool]:
        ...

    def has_route_consent(self, session_id: str, registry_id: str) -> bool:
        ...

    def field_gate(self, session_id: str, canonical_path: str) -> str:
        ...

    def request_fields(self, session_id: str, paths: list[str], source_context: Optional[str] = None) -> list[Any]:
        ...


class IntakeProfileSource:
    """Adapter exposing only the safe planner surface over ``IntakeEngine``."""

    def __init__(self, engine: Optional[IntakeEngine] = None) -> None:
        self._engine = engine or get_intake_engine()

    def get_session(self, session_id: str) -> Any:
        return self._engine.get_session(session_id)

    def check_supported(self, session: Any) -> bool:
        return self._engine.check_supported(session)

    def field_presence(self, session_id: str, paths: list[str]) -> dict[str, bool]:
        return self._engine.field_presence(session_id, paths)

    def has_route_consent(self, session_id: str, registry_id: str) -> bool:
        return self._engine.has_route_consent(session_id, registry_id)

    def field_gate(self, session_id: str, canonical_path: str) -> str:
        return self._engine.field_gate(session_id, canonical_path)

    def request_fields(self, session_id: str, paths: list[str], source_context: Optional[str] = None) -> list[Any]:
        return self._engine.request_fields(session_id, paths, source_context)


class RoutePlanner:
    """Deterministic, data-driven route planning service."""

    def __init__(
        self,
        registry: Optional[MarketRegistryService] = None,
        dedup: Optional[RateSourceDeduplicationService] = None,
        requirements: Optional[RequirementResolver] = None,
        profile_source: Optional[RoutePlannerProfileSource] = None,
    ) -> None:
        self._registry = registry or MarketRegistryService()
        self._dedup = dedup or RateSourceDeduplicationService(registry_service=self._registry)
        self._requirements = requirements or RequirementResolver()
        self._profile_source = profile_source or IntakeProfileSource()

    # --- public API ----------------------------------------------------

    def plan(self, session_id: str) -> RoutePlan:
        """Build the deterministic route plan for a session."""
        session = self._profile_source.get_session(session_id)
        now = dt.datetime.now(dt.timezone.utc)
        if session.insurance_type is not InsuranceType.AUTO:
            return RoutePlan(
                session_id=session_id,
                insurance_type=session.insurance_type,
                generated_at=now,
            )

        entries = {
            e.registry_id: e
            for e in self._registry.list_markets()
            if e.product_type is InsuranceType.AUTO and e.active
        }
        view = self._dedup.deduplicated_registry_view()

        routes: list[PlannedRoute] = []
        unresolved_count = 0
        possible_count = 0
        alternative_count = 0
        for row in view:
            confirmed = row.deduplication_status.value == "duplicate_confirmed" and len(row.group_members) > 1
            if confirmed:
                # Confirmed duplicate group -> primary + alternatives, all visible.
                members = [member_id for member_id in row.group_members if member_id in entries]
                if not members:
                    continue
                for index, member_id in enumerate(members):
                    routes.append(
                        self._plan_route(
                            session_id,
                            entries[member_id],
                            distinct_rate_source_id=row.distinct_rate_source_id,
                            deduplication_status=row.deduplication_status.value,
                            group_members=row.group_members,
                            is_alternative=index > 0,
                        )
                    )
                    if index > 0:
                        alternative_count += 1
                continue
            entry = entries.get(row.registry_id)
            if entry is None:
                continue  # inactive / non-AUTO representative
            routes.append(
                self._plan_route(
                    session_id,
                    entry,
                    distinct_rate_source_id=row.distinct_rate_source_id,
                    deduplication_status=row.deduplication_status.value,
                    group_members=row.group_members,
                    is_alternative=False,
                )
            )
            if row.distinct_rate_source_id is None:
                unresolved_count += 1
                if self._is_possible_duplicate(entry):
                    possible_count += 1

        routes = self._rank(routes)
        missing_paths = sorted(
            {
                b.canonical_path
                for route in routes
                for b in route.blockers
                if b.kind is RouteBlockerKind.MISSING_FIELD and b.canonical_path
            }
        )
        ready = sum(1 for route in routes if route.is_ready)
        summary = RoutePlanSummary(
            raw_registry_count=len(entries),
            planned_route_count=len(routes),
            ready_count=ready,
            blocked_count=len(routes) - ready,
            confirmed_duplicate_groups=len(self._dedup.get_duplicate_groups()),
            alternative_route_count=alternative_count,
            unresolved_rate_sources=unresolved_count,
            possible_duplicate_routes=possible_count,
            missing_field_paths_count=len(missing_paths),
        )
        return RoutePlan(
            session_id=session_id,
            insurance_type=InsuranceType.AUTO,
            routes=routes,
            required_missing_paths=missing_paths,
            summary=summary,
            generated_at=now,
        )

    def request_missing_fields(self, session_id: str, source_context: str = "route_planner") -> list[Any]:
        """Issue #5 integration: ask for the union of missing required paths.

        The intake engine asks each genuinely-missing field once (ask-once
        semantics preserved). Returns the FieldRequestOutcomes.
        """
        plan = self.plan(session_id)
        if not plan.required_missing_paths:
            return []
        return self._profile_source.request_fields(session_id, plan.required_missing_paths, source_context)

    # --- per-route planning ---------------------------------------------

    def _plan_route(
        self,
        session_id: str,
        entry: MarketRegistryEntry,
        *,
        distinct_rate_source_id: Optional[str],
        deduplication_status: str,
        group_members: list[str],
        is_alternative: bool,
    ) -> PlannedRoute:
        required = self._requirements.requirements_for(entry)
        presence = self._profile_source.field_presence(session_id, sorted(required))
        blockers: list[RouteBlocker] = []

        for path in sorted(required):
            if not presence.get(path, False):
                blockers.append(
                    RouteBlocker(
                        kind=RouteBlockerKind.MISSING_FIELD,
                        canonical_path=path,
                        reason="required field not provided yet",
                    )
                )
                if self._profile_source.field_gate(session_id, path) == "household_consent_required":
                    blockers.append(
                        RouteBlocker(
                            kind=RouteBlockerKind.HUMAN_REQUIRED,
                            canonical_path=path,
                            reason="household-driver consent required to collect this field",
                        )
                    )

        if not self._profile_source.has_route_consent(session_id, entry.registry_id):
            blockers.append(
                RouteBlocker(
                    kind=RouteBlockerKind.CONSENT_REQUIRED,
                    reason="route-disclosure consent not granted",
                )
            )

        for requirement in entry.requirements:
            if requirement is MarketRequirement.MEMBERSHIP:
                blockers.append(
                    RouteBlocker(kind=RouteBlockerKind.AFFINITY_RESTRICTED, reason="membership/affinity required")
                )
            elif requirement is MarketRequirement.CALLBACK:
                blockers.append(
                    RouteBlocker(kind=RouteBlockerKind.CALLBACK_REQUIRED, reason="callbacks required; no instant online quote")
                )
            elif requirement is MarketRequirement.HUMAN:
                blockers.append(
                    RouteBlocker(kind=RouteBlockerKind.HUMAN_REQUIRED, reason="human interaction required")
                )

        if entry.product_scope not in (ProductScope.STANDARD_PPA, ProductScope.UNKNOWN):
            blockers.append(
                RouteBlocker(
                    kind=RouteBlockerKind.SPECIALTY_ONLY,
                    reason=f"route targets {entry.product_scope.value} risk",
                )
            )

        source_resolved = distinct_rate_source_id is not None
        if not source_resolved:
            blockers.append(
                RouteBlocker(
                    kind=RouteBlockerKind.RATE_SOURCE_UNRESOLVED,
                    reason="no verified distinct rate source",
                )
            )

        is_ready = not blockers
        return PlannedRoute(
            registry_id=entry.registry_id,
            brand_or_program=entry.brand_or_program,
            legal_underwriter=entry.legal_underwriter,
            insurer_group=entry.insurer_group,
            distribution_type=entry.distribution_type.value,
            product_scope=entry.product_scope.value,
            distinct_rate_source_id=distinct_rate_source_id,
            deduplication_status=deduplication_status,
            group_members=group_members,
            channels=self._channels_for(entry),
            requirements=sorted(required),
            blockers=blockers,
            is_ready=is_ready,
            is_alternative=is_alternative,
            route_status="ready" if is_ready else "blocked",
        )

    def _channels_for(self, entry: MarketRegistryEntry) -> list[RouteChannel]:
        channels: list[RouteChannel] = []
        if entry.quote_url:
            channels.append(RouteChannel(kind=RouteChannelKind.ONLINE, label="Online quote", value=entry.quote_url))
        if entry.public_phone_route:
            channels.append(RouteChannel(kind=RouteChannelKind.PHONE, label="Phone", value=entry.public_phone_route))
        if entry.callback_route:
            channels.append(RouteChannel(kind=RouteChannelKind.CALLBACK, label="Callback", value=entry.callback_route))
        if entry.licensed_intermediary:
            channels.append(RouteChannel(kind=RouteChannelKind.BROKER, label="Broker", value=entry.licensed_intermediary))
        if MarketRequirement.HUMAN in entry.requirements:
            channels.append(RouteChannel(kind=RouteChannelKind.HUMAN, label="Human-assisted", value="human"))
        if not channels:
            channels.append(RouteChannel(kind=RouteChannelKind.DISCOVERY_ONLY, label="Discovery only", value=None))
        return channels

    def _is_possible_duplicate(self, entry: MarketRegistryEntry) -> bool:
        for candidate in self._dedup.find_duplicate_candidates(entry.registry_id):
            if candidate.reason_code in (
                ReasonCode.SAME_UNDERWRITER_POSSIBLE_DUPLICATE,
                ReasonCode.SAME_GROUP_ONLY_INSUFFICIENT,
            ):
                return True
        return False

    @staticmethod
    def _rank(routes: list[PlannedRoute]) -> list[PlannedRoute]:
        """Deterministic ranking: ready first, verified-source before
        unresolved, fewer blockers first, then alphabetical by brand/id."""
        def key(route: PlannedRoute) -> tuple[object, ...]:
            source_resolved = route.distinct_rate_source_id is not None
            return (
                route.is_ready is False,
                source_resolved is False,
                len(route.blockers),
                route.brand_or_program.lower(),
                route.registry_id,
            )

        ranked = sorted(routes, key=key)
        return [
            route.model_copy(update={"rank": index})
            for index, route in enumerate(ranked, start=1)
        ]

    def trace_metadata(self) -> dict[str, object]:
        """Safe, non-sensitive planner metadata (counts only)."""
        return {**self._requirements.trace_metadata()}
