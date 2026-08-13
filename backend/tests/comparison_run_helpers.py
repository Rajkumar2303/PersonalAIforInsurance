"""Shared helpers for Issue #13 comparison-run tests.

Builds a hermetic multi-provider mock env (registry + planner + browser manager
+ recovery + evidence + normalization + comparison + run service) over the
local mock quote site. No real insurers, no LLM, no LangSmith, no applicant
data.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.browser.manager import BrowserManager
from app.browser.mock_site import MockQuoteSite, build_scenario_config
from app.browser.session import BrowserSessionManager
from app.models.insurance.enums import InsuranceType
from app.services.comparison import QuoteComparisonService
from app.services.comparison_run import ComparisonRunService
from app.services.evidence.repository import InMemoryEvidenceRepository
from app.services.evidence.service import EvidenceService
from app.services.intake.catalog import IntakeFieldCatalog
from app.services.intake.consent import ConsentService
from app.services.intake.engine import IntakeEngine
from app.services.intake.session_store import InMemorySessionStore
from app.services.intake.vault import InMemoryProfileVault
from app.services.market_registry import MarketRegistryService
from app.services.normalization.repository import InMemoryNormalizationRepository
from app.services.normalization.service import QuoteNormalizationService
from app.services.route_planner.planner import IntakeProfileSource, RoutePlanner
from app.services.route_planner.requirements import RequirementResolver
from app.services.deduplication import RateSourceDeduplicationService
from app.services.recovery.engine import RecoveryEngine

from browser_helpers import StubRouteConfigLoader, mock_entry
from intake_helpers import standard_fields, write_catalog
from personas import make_standard_auto_profile
from route_planner_helpers import (
    DEFAULT_REQUIREMENTS,
    complete_starter,
    rate_source,
    write_rate_sources,
    write_registry,
    write_requirements,
)

# route tuples: (registry_id, display_name, scenario, distinct_rate_source_id, distribution)
DEMO_MULTI_ROUTES = [
    ("mock-insurer", "Mock Provider A", "quote", "RS-MOCK-INSURER", "direct"),
    ("mock-insurer-broker", "Mock Broker (Aggregator A)", "quote", "RS-MOCK-INSURER", "broker"),
    ("mock-provider-b", "Mock Provider B", "quote", "RS-MOCK-B", "direct"),
    ("mock-provider-c", "Mock Provider C", "captcha", "RS-MOCK-C", "direct"),
    ("mock-provider-d", "Mock Provider D", "quote-estimate", "RS-MOCK-D", "direct"),
]


@dataclass
class ComparisonRunEnv:
    engine: IntakeEngine
    planner: RoutePlanner
    manager: BrowserSessionManager
    recovery: RecoveryEngine
    evidence: EvidenceService
    normalization: QuoteNormalizationService
    run_service: ComparisonRunService
    session_id: str
    site: MockQuoteSite
    browser_manager: BrowserManager
    comparison: QuoteComparisonService
    registry: MarketRegistryService


def make_comparison_run_env(
    tmp_path: Path,
    site: MockQuoteSite,
    *,
    routes: Optional[list[tuple]] = None,
    max_concurrency: int = 4,
    registry_ids: Optional[list[str]] = None,
    no_config_registry_ids: Optional[list[str]] = None,
    verified_registry_ids: Optional[list[str]] = None,
    route_timeout_seconds: Optional[float] = None,
    run_timeout_seconds: Optional[float] = None,
) -> ComparisonRunEnv:
    """Build a multi-provider comparison-run environment over the mock site.

    ``no_config_registry_ids`` registers a route WITHOUT a browser route config
    (so ``manager.create`` raises -> the route fails locally).
    ``verified_registry_ids`` marks those mock routes as VERIFIED so a LIVE-mode
    comparison run (which requires verified routes + the live gate) can execute
    them hermetically against the local mock site.
    ``route_timeout_seconds`` / ``run_timeout_seconds`` override the Issue #14
    safety timeouts (small values let tests prove a stuck route can't hang the
    run).
    """
    routes = routes or DEMO_MULTI_ROUTES
    registry_ids = registry_ids or [r[0] for r in routes]
    no_config_registry_ids = no_config_registry_ids or []
    verified_registry_ids = verified_registry_ids or []

    catalog = IntakeFieldCatalog(catalog_dir=write_catalog(tmp_path, standard_fields()))
    registry_dir = write_registry(
        tmp_path,
        [
            mock_entry(
                site,
                registry_id=rid,
                brand_or_program=display,
                distribution_type=dist,
                distinct_rate_source_id=rsid,
                **({"status": "verified", "last_verified_at": "2026-08-12T00:00:00+00:00"} if rid in verified_registry_ids else {}),
            )
            for rid, display, _scen, rsid, dist in routes
        ],
    )
    registry = MarketRegistryService(registry_dir=registry_dir)
    # One rate-source record per DISTINCT source id (aggregating its registry
    # ids) - the dedup loader rejects duplicate distinct_rate_source_id rows.
    from collections import defaultdict

    source_to_registries: dict[str, list[str]] = defaultdict(list)
    for rid, _d, _s, rsid, _x in routes:
        source_to_registries[rsid].append(rid)
    demo_rate_sources = [
        rate_source(rsid, related_registry_ids=regs)
        for rsid, regs in source_to_registries.items()
    ]
    write_rate_sources(tmp_path, demo_rate_sources)
    vault = InMemoryProfileVault()
    sessions = InMemorySessionStore()
    consent = ConsentService()
    engine = IntakeEngine(catalog=catalog, vault=vault, sessions=sessions, consent=consent, registry=registry)

    per_route = {rid: ["product_data.vehicles[0].use.annual_kilometres"] for rid in registry_ids}
    requirements = RequirementResolver(
        requirements_dir=write_requirements(tmp_path, DEFAULT_REQUIREMENTS, per_route)
    )
    dedup = RateSourceDeduplicationService(
        registry_service=registry, rate_sources_dir=write_rate_sources(tmp_path, demo_rate_sources)
    )
    planner = RoutePlanner(
        registry=registry,
        dedup=dedup,
        requirements=requirements,
        profile_source=IntakeProfileSource(engine),
    )

    session, _gate = engine.create_session(InsuranceType.AUTO)
    sid = session.session_id
    complete_starter(engine, sid)
    vault.update(engine.get_session(sid).profile_id, make_standard_auto_profile())
    for rid in registry_ids:
        engine.grant_route_consent(sid, rid, [], True)

    configs = {
        rid: build_scenario_config(rid, site, scen)
        for rid, _d, scen, _r, _x in routes
        if rid not in no_config_registry_ids
    }
    loader = StubRouteConfigLoader(configs)
    browser_manager = BrowserManager(headless=True)
    manager = BrowserSessionManager(
        engine=engine, planner=planner, registry=registry,
        config_loader=loader, browser=browser_manager, headless=True,
    )
    recovery = RecoveryEngine()

    evidence = EvidenceService(InMemoryEvidenceRepository())
    normalization = QuoteNormalizationService(evidence, InMemoryNormalizationRepository())
    comparison = QuoteComparisonService()
    run_service = ComparisonRunService(
        planner=planner,
        manager=manager,
        recovery=recovery,
        intake=engine,
        evidence=evidence,
        normalization=normalization,
        comparison=comparison,
        max_concurrency=max_concurrency,
        route_timeout_seconds=route_timeout_seconds,
        run_timeout_seconds=run_timeout_seconds,
    )
    return ComparisonRunEnv(
        engine=engine,
        planner=planner,
        manager=manager,
        recovery=recovery,
        evidence=evidence,
        normalization=normalization,
        run_service=run_service,
        session_id=sid,
        site=site,
        browser_manager=browser_manager,
        comparison=comparison,
        registry=registry,
    )


async def await_run(env: ComparisonRunEnv, run_id: str, timeout: float = 180.0) -> object:
    """Poll a run until terminal (or timeout)."""
    import asyncio
    import datetime as dt

    deadline = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=timeout)
    while True:
        run = env.run_service.get_run(env.session_id, run_id)
        if run.status in ("completed", "completed_with_partial_results", "failed"):
            return run
        if dt.datetime.now(dt.timezone.utc) > deadline:
            raise TimeoutError("comparison run timed out")
        await asyncio.sleep(0.2)
