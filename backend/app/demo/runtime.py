"""Mode-scoped demo runtime (Issue #8.5 integration checkpoint).

Builds a fully-isolated set of planner/browser/recovery services over the
``backend/data/demo`` overlay. This runtime is used ONLY for
``execution_mode=mock``. LIVE execution always uses the real singletons
(``get_route_planner`` / ``get_browser_manager`` / ``get_recovery_engine``)
over the real market registry, so synthetic mock entries can never affect:

- real market counts / ``/markets`` reporting
- dedup metrics / ``/rate-sources``
- registry reporting / market coverage
- live execution (mock routes are never ``verified``, so the Issue #7 live
  gate refuses them, and live mode never loads this overlay)

The shared intake engine (catalog + vault + consent) is reused in both modes:
intake is mode-independent. Production/application code never imports from
``tests/``; tests construct a ``DemoRuntime`` over a temp overlay.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from ..browser.config import BrowserRouteConfigLoader
from ..browser.manager import BrowserManager
from ..browser.session import BrowserSessionManager
from ..core.config import BACKEND_ROOT, get_settings
from ..services.deduplication import RateSourceDeduplicationService
from ..services.intake import get_intake_engine
from ..services.intake.engine import IntakeEngine
from ..services.market_registry import MarketRegistryService
from ..services.recovery import IntakeConsentSource, PlannerRouteSource, RecoveryEngine
from ..services.route_planner.planner import IntakeProfileSource, RoutePlanner
from ..services.route_planner.requirements import RequirementResolver
from .mock_quote_site import MockQuoteSite

logger = logging.getLogger(__name__)


def default_demo_dir() -> Path:
    """Resolve the demo data overlay directory (CWD-independent)."""
    settings = get_settings()
    if settings.demo_data_dir:
        return Path(settings.demo_data_dir)
    return BACKEND_ROOT / "data" / "demo"


class DemoRuntime:
    """Isolated mock-mode services over the demo data overlay."""

    def __init__(
        self,
        demo_dir: Optional[Path] = None,
        mock_site: Optional[MockQuoteSite] = None,
        headless: Optional[bool] = None,
        slow_mo: Optional[int] = None,
    ) -> None:
        self._demo_dir = Path(demo_dir) if demo_dir else default_demo_dir()
        self._mock_site = mock_site
        settings = get_settings()
        self._headless = settings.browser_headless if headless is None else headless
        self._slow_mo = settings.browser_slow_mo_ms if slow_mo is None else slow_mo
        self._intake: Optional[IntakeEngine] = None
        self._registry: Optional[MarketRegistryService] = None
        self._dedup: Optional[RateSourceDeduplicationService] = None
        self._requirements: Optional[RequirementResolver] = None
        self._planner: Optional[RoutePlanner] = None
        self._config_loader: Optional[BrowserRouteConfigLoader] = None
        self._browser_manager: Optional[BrowserManager] = None
        self._manager: Optional[BrowserSessionManager] = None
        self._recovery: Optional[RecoveryEngine] = None

    # --- data paths ---------------------------------------------------

    @property
    def demo_dir(self) -> Path:
        return self._demo_dir

    @property
    def registry_dir(self) -> Path:
        return self._demo_dir / "market_registry"

    @property
    def requirements_dir(self) -> Path:
        return self._demo_dir / "routes"

    @property
    def rate_sources_dir(self) -> Path:
        return self._demo_dir / "rate_sources"

    @property
    def browser_config_dir(self) -> Path:
        return self._demo_dir / "browser" / "routes"

    # --- mock site ----------------------------------------------------

    @property
    def mock_site(self) -> Optional[MockQuoteSite]:
        return self._mock_site

    def start_mock_site(self) -> MockQuoteSite:
        """Start (or return the already-started) localhost mock quote site.

        Binds localhost only, enabled only through explicit dev/mock config,
        never a live destination, and shut down with the app lifespan.
        """
        if self._mock_site is None:
            settings = get_settings()
            if not settings.mock_site_enabled:
                raise RuntimeError("mock site is disabled (mock_site_enabled=false)")
            self._mock_site = MockQuoteSite(
                host=settings.mock_site_host, port=settings.mock_site_port
            ).start()
            logger.info(
                "mock quote site started",
                extra={
                    "workflow": "demo_runtime",
                    "workflow_stage": "start_mock_site",
                    "status": "ok",
                    "url": self._mock_site.base_url,
                },
            )
        return self._mock_site

    async def shutdown(self) -> None:
        """Stop the browser manager and the mock site (app-lifespan clean)."""
        if self._browser_manager is not None and self._browser_manager.is_running:
            try:
                await self._browser_manager.stop()
            except Exception:  # pragma: no cover - best-effort shutdown
                logger.warning("demo browser shutdown failed", extra={"status": "error"})
        if self._mock_site is not None:
            try:
                self._mock_site.stop()
            finally:
                self._mock_site = None

    # --- mode-scoped services (lazy) ----------------------------------

    @property
    def intake(self) -> IntakeEngine:
        """Intake engine sharing the real sessions/vault/consent/catalog but
        resolving route lookups against the DEMO registry (mock consent +
        disclosure). Never enumerates markets, so real counts are unaffected.
        """
        if self._intake is None:
            self._intake = get_intake_engine().with_registry(self.registry)
        return self._intake

    @property
    def registry(self) -> MarketRegistryService:
        if self._registry is None:
            self._registry = MarketRegistryService(registry_dir=self.registry_dir)
        return self._registry

    @property
    def dedup(self) -> RateSourceDeduplicationService:
        if self._dedup is None:
            self._dedup = RateSourceDeduplicationService(
                registry_service=self.registry, rate_sources_dir=self.rate_sources_dir
            )
        return self._dedup

    @property
    def requirements(self) -> RequirementResolver:
        if self._requirements is None:
            self._requirements = RequirementResolver(requirements_dir=self.requirements_dir)
        return self._requirements

    @property
    def planner(self) -> RoutePlanner:
        if self._planner is None:
            self._planner = RoutePlanner(
                registry=self.registry,
                dedup=self.dedup,
                requirements=self.requirements,
                profile_source=IntakeProfileSource(self.intake),
            )
        return self._planner

    @property
    def config_loader(self) -> BrowserRouteConfigLoader:
        if self._config_loader is None:
            self._config_loader = BrowserRouteConfigLoader(config_dir=self.browser_config_dir)
        return self._config_loader

    @property
    def browser_manager(self) -> BrowserManager:
        if self._browser_manager is None:
            self._browser_manager = BrowserManager(headless=self._headless, slow_mo=self._slow_mo)
        return self._browser_manager

    @property
    def manager(self) -> BrowserSessionManager:
        if self._manager is None:
            self._manager = BrowserSessionManager(
                engine=self.intake,
                planner=self.planner,
                registry=self.registry,
                config_loader=self.config_loader,
                browser=self.browser_manager,
                headless=self._headless,
            )
        return self._manager

    @property
    def recovery(self) -> RecoveryEngine:
        """Recovery engine wired to the DEMO planner + demo consent source.

        Keeping this separate from the real singleton means mock failover
        consults the demo overlay and never mutates real attempt history.
        """
        if self._recovery is None:
            self._recovery = RecoveryEngine(
                route_source=PlannerRouteSource(self.planner),
                consent_source=IntakeConsentSource(self.intake),
            )
        return self._recovery


_runtime: Optional[DemoRuntime] = None


def get_demo_runtime() -> DemoRuntime:
    """Cached default demo runtime (data-driven demo overlay)."""
    global _runtime
    if _runtime is None:
        _runtime = DemoRuntime()
    return _runtime


def reset_demo_runtime() -> None:
    """Drop the cached singleton (test isolation)."""
    global _runtime
    _runtime = None
