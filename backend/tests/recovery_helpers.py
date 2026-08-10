"""Shared helpers for Issue #8 recovery tests (hermetic, synthetic).

Wires a REAL IntakeEngine + RoutePlanner (synthetic registry/rate-source data)
with a ``RecoveryEngine`` whose route source consumes Issue #6 primary/
alternative relationships. All observations are synthetic; no browser/LLM/real
insurer/external API involvement.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.models.insurance.enums import InsuranceType
from app.models.recovery import RecoveryDecideRequest, RecoveryPolicy
from app.services.recovery.attempt_store import InMemoryAttemptStore
from app.services.recovery.engine import IntakeConsentSource, PlannerRouteSource, RecoveryEngine
from app.services.recovery.policy import RecoveryPolicyLoader
from route_planner_helpers import (
    DEFAULT_REQUIREMENTS,
    complete_starter,
    entry,
    make_integration_env,
    rate_source,
)


class StubRouteSource:
    """Deterministic alternatives for focused engine tests (no planner needed)."""

    def __init__(self, alternatives: Optional[dict[str, list[str]]] = None) -> None:
        self._alternatives = alternatives or {}

    def alternatives_for(
        self, *, plan_id, registry_id, distinct_rate_source_id, intake_session_id
    ) -> list[str]:
        return list(self._alternatives.get(registry_id, []))


@dataclass
class RecoveryEnv:
    engine: RecoveryEngine
    store: InMemoryAttemptStore
    planner: object = None
    intake_engine: object = None
    session_id: str = ""


def make_recovery_env(
    tmp_path: Path,
    *,
    policy: Optional[RecoveryPolicy] = None,
    policy_dir: Optional[Path] = None,
    route_source=None,
    entries: Optional[list[dict]] = None,
    rate_sources: Optional[list[dict]] = None,
) -> RecoveryEnv:
    """Build a hermetic recovery environment (real engine + planner + store)."""
    entries = entries or [entry("route-a", distinct_rate_source_id="RS-A")]
    rate_sources = rate_sources or [rate_source("RS-A", related_registry_ids=["route-a"])]
    intake_engine, planner = make_integration_env(
        tmp_path,
        entries,
        rate_sources,
        default_reqs=DEFAULT_REQUIREMENTS,
        per_route_reqs={},
    )
    session, _gate = intake_engine.create_session(InsuranceType.AUTO)
    complete_starter(intake_engine, session.session_id)
    for e in entries:
        intake_engine.grant_route_consent(session.session_id, e["registry_id"], [], True)

    store = InMemoryAttemptStore()
    loader = RecoveryPolicyLoader(policy_dir=policy_dir or (tmp_path / "recovery"))
    engine = RecoveryEngine(
        store=store,
        policy=policy if policy is not None else loader.load(),
        route_source=route_source or PlannerRouteSource(planner),
        consent_source=IntakeConsentSource(intake_engine),
    )
    return RecoveryEnv(
        engine=engine,
        store=store,
        planner=planner,
        intake_engine=intake_engine,
        session_id=session.session_id,
    )


def req(
    env: RecoveryEnv,
    observation_type: str,
    *,
    registry_id: str = "route-a",
    plan_id: str = "plan-1",
    distinct_rate_source_id: str = "RS-A",
    reason: Optional[str] = None,
    ctx: Optional[dict] = None,
    attempt_id: Optional[str] = None,
    source_channel: str = "browser",
    observation_sequence: Optional[int] = None,
    plan_version: Optional[str] = None,
    **extra: object,
) -> RecoveryDecideRequest:
    """Build a safe recovery decide request against a recovery env."""
    return RecoveryDecideRequest(
        attempt_id=attempt_id,
        plan_id=plan_id,
        planned_route_id=registry_id,
        registry_id=registry_id,
        distinct_rate_source_id=distinct_rate_source_id,
        intake_session_id=env.session_id,
        source_channel=source_channel,
        observation_type=observation_type,
        reason=reason,
        observation_sequence=observation_sequence,
        plan_version=plan_version,
        safe_context=dict(ctx or {}),
        **extra,
    )


def revoke_route_consent(env: RecoveryEnv, registry_id: str) -> None:
    """Revoke the active route-disclosure consent for a route (test helper).

    Makes Issue #5 consent live-change so the recovery consent gate and the
    route planner both observe the revocation on the next decision.
    """
    consent = env.intake_engine._consent  # type: ignore[attr-defined]
    for receipt in consent.for_session(env.session_id):
        if getattr(receipt, "route_registry_id", None) == registry_id:
            consent.revoke(receipt.consent_id)


def standard_rs_entries() -> tuple[list[dict], list[dict]]:
    """Primary route-a + ready alternative route-b sharing RS-TEST-001."""
    entries = [
        entry("route-a", distinct_rate_source_id="RS-TEST-001", quote_url="https://mock.example/route-a"),
        entry("route-b", distinct_rate_source_id="RS-TEST-001", quote_url="https://mock.example/route-b"),
    ]
    rate_sources = [
        rate_source(
            "RS-TEST-001",
            deduplication_status="duplicate_confirmed",
            related_registry_ids=["route-a", "route-b"],
        )
    ]
    return entries, rate_sources
