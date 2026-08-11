"""Shared helpers for Issue #7 browser tests.

All browser tests run against the LOCAL mock quote site (no internet, no real
insurer websites, no LLM, no LangSmith uploads). A ``BrowserEnv`` wires a REAL
``IntakeEngine`` (synthetic catalog) + real ``RoutePlanner`` (synthetic
registry/rate-source/requirements) + a ``BrowserSessionManager`` whose route
config points at the mock site.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.browser.manager import BrowserManager
from app.browser.mock_site import (
    MOCK_REGISTRY_ID,
    MockQuoteSite,
    build_mock_route_config,
    build_scenario_config,
)
from app.browser.session import BrowserSessionManager
from app.models.browser.config import BrowserRouteConfig
from app.models.insurance.enums import InsuranceType
from app.services.intake.catalog import IntakeFieldCatalog
from app.services.intake.consent import ConsentService
from app.services.intake.engine import IntakeEngine
from app.services.intake.session_store import InMemorySessionStore
from app.services.intake.vault import InMemoryProfileVault
from app.services.market_registry import MarketRegistryService
from app.services.route_planner.planner import IntakeProfileSource, RoutePlanner
from app.services.route_planner.requirements import RequirementResolver
from app.services.deduplication import RateSourceDeduplicationService
from intake_helpers import SYNTHETIC_DOB, standard_fields, write_catalog
from personas import make_standard_auto_profile
from route_planner_helpers import (
    DEFAULT_REQUIREMENTS,
    complete_starter,
    rate_source,
    write_rate_sources,
    write_registry,
    write_requirements,
)


class StubRouteConfigLoader:
    """Returns pre-built route configs (no filesystem needed)."""

    def __init__(self, configs: dict[str, BrowserRouteConfig]) -> None:
        self._configs = configs

    def load(self, registry_id: str) -> BrowserRouteConfig:
        if registry_id not in self._configs:
            raise RuntimeError(f"no browser route config for {registry_id!r}")
        return self._configs[registry_id]

    def load_or_none(self, registry_id: str) -> Optional[BrowserRouteConfig]:
        return self._configs.get(registry_id)


def mock_entry(site: MockQuoteSite, registry_id: str = MOCK_REGISTRY_ID, **overrides: object) -> dict:
    """One synthetic AUTO registry record pointing at the mock quote site."""
    base: dict = {
        "registry_id": registry_id,
        "product_type": "auto",
        "brand_or_program": "Mock Insurer",
        "distribution_type": "direct",
        "product_scope": "standard_PPA",
        "status": "discovered",
        "quote_url": site.url("/page-a"),
        "distinct_rate_source_id": f"RS-{registry_id.upper()}",
        "active": True,
    }
    base.update(overrides)
    return base


@dataclass
class BrowserEnv:
    engine: IntakeEngine
    planner: RoutePlanner
    manager: BrowserSessionManager
    vault: InMemoryProfileVault
    registry: MarketRegistryService
    session_id: str
    site: MockQuoteSite
    config_loader: StubRouteConfigLoader
    browser_manager: BrowserManager


def make_browser_env(
    tmp_path: Path,
    site: MockQuoteSite,
    *,
    persona=None,
    per_route_reqs: Optional[dict[str, list[str]]] = None,
    scenario: Optional[str] = None,
    entry_overrides: Optional[dict[str, object]] = None,
    grant_consent: bool = True,
    route_config: Optional[BrowserRouteConfig] = None,
    registry_id: str = MOCK_REGISTRY_ID,
    consent_paths: Optional[list[str]] = None,
    headless: bool = True,
    slow_mo: int = 0,
    evidence_sink=None,
    recovery=None,
) -> BrowserEnv:
    """Build a hermetic browser environment (real engine + planner + manager).

    - persona: synthetic profile injected into the vault (default standard).
    - per_route_reqs: extra required canonical paths for the mock route.
    - scenario: route config start_url points at the given mock scenario page.
    - grant_consent: when True, route-disclosure consent is granted up front.
    - route_config: explicit route config (overrides scenario/default).
    - registry_id: registry id for the mock route (e.g. a second synthetic route).
    - consent_paths: when given (and grant_consent=True), the disclosure is
      scoped to exactly these canonical paths (for consent-expansion tests).
    - headless: headless Chromium (default True, hermetic tests). Pass False for
      a headful visual demo.
    - slow_mo: DEV/DEMO ONLY Playwright per-action delay in ms (default 0).
    - evidence_sink / recovery: automatic evidence emission wiring (Issue #10).
    """
    rate_source_id = f"RS-{registry_id.upper()}"
    catalog = IntakeFieldCatalog(catalog_dir=write_catalog(tmp_path, standard_fields()))
    registry_dir = write_registry(tmp_path, [mock_entry(site, registry_id=registry_id, **(entry_overrides or {}))])
    registry = MarketRegistryService(registry_dir=registry_dir)
    write_rate_sources(
        tmp_path, [rate_source(rate_source_id, related_registry_ids=[registry_id])]
    )
    vault = InMemoryProfileVault()
    sessions = InMemorySessionStore()
    consent = ConsentService()
    engine = IntakeEngine(
        catalog=catalog,
        vault=vault,
        sessions=sessions,
        consent=consent,
        registry=registry,
    )
    requirements = RequirementResolver(
        requirements_dir=write_requirements(
            tmp_path, DEFAULT_REQUIREMENTS, per_route_reqs or {}
        )
    )
    dedup = RateSourceDeduplicationService(
        registry_service=registry,
        rate_sources_dir=write_rate_sources(
            tmp_path, [rate_source(rate_source_id, related_registry_ids=[registry_id])]
        ),
    )
    planner = RoutePlanner(
        registry=registry,
        dedup=dedup,
        requirements=requirements,
        profile_source=IntakeProfileSource(engine),
    )

    session, gate = engine.create_session(InsuranceType.AUTO)
    sid = session.session_id
    complete_starter(engine, sid)
    resolved_persona = persona if persona is not None else make_standard_auto_profile()
    intake = engine.get_session(sid)
    vault.update(intake.profile_id, resolved_persona)
    if grant_consent:
        if consent_paths is not None:
            engine.grant_route_consent(sid, registry_id, consent_paths, True)
        else:
            engine.grant_route_consent(sid, registry_id, [], True)

    if route_config is not None:
        config = route_config
    elif scenario is not None:
        config = build_scenario_config(registry_id, site, scenario)
    else:
        config = build_mock_route_config(registry_id, start_url=site.url("/page-a"))
    loader = StubRouteConfigLoader({registry_id: config})

    browser_manager = BrowserManager(headless=headless, slow_mo=slow_mo)
    manager = BrowserSessionManager(
        engine=engine,
        planner=planner,
        registry=registry,
        config_loader=loader,
        browser=browser_manager,
        headless=headless,
        evidence_sink=evidence_sink,
        recovery=recovery,
    )
    return BrowserEnv(
        engine=engine,
        planner=planner,
        manager=manager,
        vault=vault,
        registry=registry,
        session_id=sid,
        site=site,
        config_loader=loader,
        browser_manager=browser_manager,
    )


def progressive_persona_with_dob():
    """Progressive profile with DOB present but annual kilometres missing."""
    return make_standard_auto_profile(annual_kilometres=None)
