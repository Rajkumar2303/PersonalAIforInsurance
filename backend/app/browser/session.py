"""Browser session manager (Issue #7).

Lifecycle: create (route validation + mode/live gate) -> run -> pause -> resume
-> close/cleanup. In-memory active Playwright sessions (hackathon-appropriate);
one isolated browser context per session. No distributed browser infra.

``create()`` is browser-free (pure validation returning a session or a
structured refusal); the browser is launched lazily on the first run.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any, Optional

from ..core.config import get_settings
from ..models.browser.config import BrowserRouteConfig
from ..models.browser.session import (
    BrowserExecutionMode,
    BrowserRefusalReason,
    BrowserSession,
    BrowserSessionStatus,
    BrowserStartRefusal,
    BrowserStepResult,
    LiveExecutionGate,
)
from ..models.insurance.enums import InsuranceType
from ..services.intake.engine import IntakeEngine, SessionNotFoundError
from ..services.market_registry import MarketRegistryService
from ..services.route_planner import RoutePlanner
from .config import BrowserRouteConfigLoader
from .executor import BrowserExecutor
from .manager import BrowserManager
from .route_identity import registry_id_for_planned_route
from .value_provider import IntakeValueSource

logger = logging.getLogger(__name__)

_AUTOMATION_PROHIBITED_HINTS = (
    "no automation",
    "do not automate",
    "automation prohibited",
    "automation not permitted",
    "not permitted",
    "manual only",
    "no bots",
    "against terms",
)


class BrowserSessionNotFoundError(KeyError):
    """Raised when an unknown browser_session_id is used."""


class InMemoryBrowserSessionStore:
    """Ephemeral, process-lifetime browser session store."""

    def __init__(self) -> None:
        self._sessions: dict[str, BrowserSession] = {}

    def save(self, session: BrowserSession) -> None:
        self._sessions[session.browser_session_id] = session

    def get(self, session_id: str) -> Optional[BrowserSession]:
        return self._sessions.get(session_id)

    def delete(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def list(self) -> list[BrowserSession]:
        return sorted(self._sessions.values(), key=lambda s: s.started_at)


def live_privacy_context_kwargs() -> dict[str, Any]:
    """LIVE-mode browser-context privacy defaults.

    Video recording, Playwright tracing, automatic screenshots, HAR capture and
    network-body logging are all DISABLED because they may capture PII.
    """
    return {"no_viewport": True}


class BrowserSessionManager:
    """Owns browser sessions, contexts, and the executor."""

    def __init__(
        self,
        engine: IntakeEngine,
        planner: RoutePlanner,
        registry: Optional[MarketRegistryService] = None,
        config_loader: Optional[BrowserRouteConfigLoader] = None,
        browser: Optional[BrowserManager] = None,
        value_source: Optional[IntakeValueSource] = None,
        executor: Optional[BrowserExecutor] = None,
        store: Optional[InMemoryBrowserSessionStore] = None,
        headless: Optional[bool] = None,
    ) -> None:
        self._engine = engine
        self._planner = planner
        self._registry = registry or MarketRegistryService()
        self._config_loader = config_loader or BrowserRouteConfigLoader()
        settings = get_settings()
        self._browser = browser or BrowserManager(
            headless=settings.browser_headless if headless is None else headless,
            slow_mo=settings.browser_slow_mo_ms,
        )
        self._values = value_source or IntakeValueSource(engine)
        self._executor = executor or BrowserExecutor(self._values)
        self._store = store or InMemoryBrowserSessionStore()
        self._contexts: dict[str, Any] = {}  # session_id -> context
        self._pages: dict[str, Any] = {}  # session_id -> page
        self._last_results: dict[str, BrowserStepResult] = {}  # session_id -> last step

    # --- create / lifecycle -----------------------------------------

    def create(
        self,
        intake_session_id: str,
        planned_route_id: str,
        execution_mode: BrowserExecutionMode = BrowserExecutionMode.SANDBOX,
        live_gate: Optional[LiveExecutionGate] = None,
        plan_id: Optional[str] = None,
    ) -> BrowserSession | BrowserStartRefusal:
        refusal = self._validate_start(intake_session_id, planned_route_id, execution_mode, live_gate)
        if refusal is not None:
            return refusal
        intake = self._engine.get_session(intake_session_id)
        registry_id = registry_id_for_planned_route(planned_route_id)
        now = dt.datetime.now(dt.timezone.utc)
        session = BrowserSession(
            browser_session_id=uuid.uuid4().hex,
            plan_id=plan_id,
            planned_route_id=planned_route_id,
            registry_id=registry_id,
            profile_id=intake.profile_id,
            intake_session_id=intake_session_id,
            execution_mode=execution_mode,
            status=BrowserSessionStatus.CREATED,
            started_at=now,
            updated_at=now,
        )
        self._store.save(session)
        logger.info(
            "browser session created",
            extra={"workflow": "browser_session", "workflow_stage": "create", "status": "ok",
                   "browser_session_id": session.browser_session_id, "registry_id": registry_id,
                   "execution_mode": execution_mode.value},
        )
        return session

    def get(self, session_id: str) -> BrowserSession:
        session = self._store.get(session_id)
        if session is None:
            raise BrowserSessionNotFoundError(session_id)
        return session

    def list(self) -> list[BrowserSession]:
        return self._store.list()

    async def start_session(self, session_id: str) -> BrowserStepResult:
        """First run: navigate to the start URL, then run the first step."""
        session = self.get(session_id)
        if session.status is BrowserSessionStatus.CLOSED:
            raise BrowserSessionNotFoundError("session closed")
        page = await self._ensure_page(session)
        registry_id = session.registry_id or ""
        entry = self._registry.get_by_registry_id(registry_id)
        config = self._config_for(registry_id)
        start_url = config.start_url or (entry.quote_url if entry else None)
        if not start_url:
            result = self._executor._technical_error(session, "no verified quote URL for this route")
            self._store.save(session)
            return result
        result = await self._executor.start(page, session, config, start_url)
        self._last_results[session_id] = result
        self._store.save(session)
        return result

    async def step_session(self, session_id: str) -> BrowserStepResult:
        """Advance one step on the current page (used by resume/loop)."""
        session = self.get(session_id)
        if session.status is BrowserSessionStatus.CLOSED:
            raise BrowserSessionNotFoundError("session closed")
        page = self._pages.get(session_id)
        if page is None:
            return await self.start_session(session_id)
        config = self._config_for(session.registry_id or "")
        result = await self._executor.advance(page, session, config)
        self._last_results[session_id] = result
        self._store.save(session)
        return result

    async def close(self, session_id: str) -> BrowserSession:
        session = self.get(session_id)
        context = self._contexts.pop(session_id, None)
        if context is not None:
            await self._browser.close_context(context)
        self._pages.pop(session_id, None)
        self._last_results.pop(session_id, None)
        session = session.model_copy(update={"status": BrowserSessionStatus.CLOSED,
                                             "updated_at": dt.datetime.now(dt.timezone.utc)})
        self._store.save(session)
        logger.info(
            "browser session closed",
            extra={"workflow": "browser_session", "workflow_stage": "close", "status": "ok",
                   "browser_session_id": session_id},
        )
        return session

    def last_result(self, session_id: str) -> Optional[BrowserStepResult]:
        """Return the most recent BrowserStepResult for a session, if any."""
        return self._last_results.get(session_id)

    async def cleanup_abandoned(self, now: Optional[dt.datetime] = None) -> int:
        """Close sessions idle longer than the configured timeout (best-effort)."""
        now = now or dt.datetime.now(dt.timezone.utc)
        timeout = get_settings().browser_idle_timeout_seconds
        closed = 0
        for session in self.list():
            if session.status in (BrowserSessionStatus.RUNNING, BrowserSessionStatus.CREATED):
                if (now - session.updated_at).total_seconds() > timeout:
                    await self.close(session.browser_session_id)
                    closed += 1
        return closed

    # --- internals --------------------------------------------------

    def _validate_start(
        self,
        intake_session_id: str,
        planned_route_id: str,
        execution_mode: BrowserExecutionMode,
        live_gate: Optional[LiveExecutionGate],
    ) -> Optional[BrowserStartRefusal]:
        now = dt.datetime.now(dt.timezone.utc)
        try:
            intake = self._engine.get_session(intake_session_id)
        except SessionNotFoundError:
            return BrowserStartRefusal(intake_session_id=intake_session_id,
                                       planned_route_id=planned_route_id,
                                       reason=BrowserRefusalReason.UNKNOWN_SESSION,
                                       detail="intake session not found", refused_at=now)
        if not self._engine.check_supported(intake):
            return BrowserStartRefusal(intake_session_id=intake_session_id,
                                       planned_route_id=planned_route_id,
                                       reason=BrowserRefusalReason.NOT_AUTO,
                                       detail="browser execution is AUTO-only", refused_at=now)

        registry_id = registry_id_for_planned_route(planned_route_id)
        entry = self._registry.get_by_registry_id(registry_id)
        if entry is None:
            return BrowserStartRefusal(intake_session_id=intake_session_id, planned_route_id=planned_route_id,
                                       registry_id=registry_id, reason=BrowserRefusalReason.ROUTE_NOT_FOUND,
                                       detail="registry route not found", refused_at=now)

        plan = self._planner.plan(intake_session_id)
        route = next((r for r in plan.routes if r.registry_id == registry_id), None)
        if route is None:
            return BrowserStartRefusal(intake_session_id=intake_session_id, planned_route_id=planned_route_id,
                                       registry_id=registry_id, reason=BrowserRefusalReason.ROUTE_NOT_FOUND,
                                       detail="route not present in the route plan", refused_at=now)
        if not any(channel.kind.value == "online" for channel in route.channels):
            return BrowserStartRefusal(intake_session_id=intake_session_id, planned_route_id=planned_route_id,
                                       registry_id=registry_id, reason=BrowserRefusalReason.NON_WEB_CHANNEL,
                                       detail="route has no online/web quote channel", refused_at=now)

        # Consent gate precedes readiness: the applicant's explicit route
        # decision (grant/deny) should surface specifically, not hidden behind
        # the generic not-ready blocker.
        consent_state = self._engine.route_consent_state(intake_session_id, registry_id)
        if consent_state == "denied":
            return BrowserStartRefusal(intake_session_id=intake_session_id, planned_route_id=planned_route_id,
                                       registry_id=registry_id, reason=BrowserRefusalReason.ROUTE_EXCLUDED,
                                       detail="route was excluded by the applicant", refused_at=now)
        if consent_state != "granted":
            return BrowserStartRefusal(intake_session_id=intake_session_id, planned_route_id=planned_route_id,
                                       registry_id=registry_id, reason=BrowserRefusalReason.CONSENT_MISSING,
                                       detail="route-disclosure consent not granted", refused_at=now)

        if not route.is_ready:
            blockers = ", ".join(sorted({b.kind.value for b in route.blockers}))
            return BrowserStartRefusal(intake_session_id=intake_session_id, planned_route_id=planned_route_id,
                                       registry_id=registry_id, reason=BrowserRefusalReason.ROUTE_NOT_READY,
                                       detail=f"route not ready; blockers: {blockers}", refused_at=now)

        if execution_mode is BrowserExecutionMode.LIVE:
            return self._validate_live(intake_session_id, planned_route_id, registry_id, entry, live_gate, now)
        return None

    def _validate_live(
        self,
        intake_session_id: str,
        planned_route_id: str,
        registry_id: str,
        entry: Any,
        live_gate: Optional[LiveExecutionGate],
        now: dt.datetime,
    ) -> Optional[BrowserStartRefusal]:
        settings = get_settings()
        if settings.browser_live_gate_required and (live_gate is None or not live_gate.satisfied):
            return BrowserStartRefusal(intake_session_id=intake_session_id, planned_route_id=planned_route_id,
                                       registry_id=registry_id, reason=BrowserRefusalReason.LIVE_GATE_REQUIRED,
                                       detail="live execution requires personal_use_confirmed + accurate_information_attested",
                                       refused_at=now)
        if entry.status.value != "verified":
            return BrowserStartRefusal(intake_session_id=intake_session_id, planned_route_id=planned_route_id,
                                       registry_id=registry_id, reason=BrowserRefusalReason.NO_VERIFIED_ROUTE,
                                       detail="no manually verified live web route exists", refused_at=now)
        if not entry.quote_url:
            return BrowserStartRefusal(intake_session_id=intake_session_id, planned_route_id=planned_route_id,
                                       registry_id=registry_id, reason=BrowserRefusalReason.NO_VERIFIED_ROUTE,
                                       detail="verified route has no quote URL", refused_at=now)
        if entry.automation_notes and self._automation_prohibited(entry.automation_notes):
            return BrowserStartRefusal(intake_session_id=intake_session_id, planned_route_id=planned_route_id,
                                       registry_id=registry_id, reason=BrowserRefusalReason.AUTOMATION_NOT_PERMITTED,
                                       detail="route terms prohibit automation", refused_at=now)
        if self._config_loader.load_or_none(registry_id) is None:
            return BrowserStartRefusal(intake_session_id=intake_session_id, planned_route_id=planned_route_id,
                                       registry_id=registry_id, reason=BrowserRefusalReason.NO_VERIFIED_ROUTE,
                                       detail="no browser route config for the verified route", refused_at=now)
        return None

    @staticmethod
    def _automation_prohibited(notes: str) -> bool:
        lowered = notes.lower()
        return any(hint in lowered for hint in _AUTOMATION_PROHIBITED_HINTS)

    def _config_for(self, registry_id: str) -> BrowserRouteConfig:
        config = self._config_loader.load_or_none(registry_id)
        if config is None:
            config = BrowserRouteConfig(registry_id=registry_id)
        return self._executor.merged_config(config)

    async def _ensure_page(self, session: BrowserSession) -> Any:
        existing = self._pages.get(session.browser_session_id)
        if existing is not None:
            return existing
        await self._browser.start()
        kwargs: dict[str, Any] = {}
        if session.execution_mode is BrowserExecutionMode.LIVE:
            kwargs = live_privacy_context_kwargs()  # no video/trace/screenshots/HAR
        context = await self._browser.new_context(**kwargs)
        page = await context.new_page()
        if session.execution_mode is BrowserExecutionMode.SANDBOX:
            # Hermetic guarantee: never allow requests to non-localhost hosts
            # during sandbox/test execution.
            await self._block_external_requests(page)
        self._contexts[session.browser_session_id] = context
        self._pages[session.browser_session_id] = page
        return page

    @staticmethod
    async def _block_external_requests(page: Any) -> None:
        from urllib.parse import urlsplit

        async def _route(route: Any) -> None:
            host = (urlsplit(route.request.url).hostname or "").lower()
            if host in ("127.0.0.1", "localhost"):
                await route.continue_()
            else:
                await route.abort()

        await page.route("**/*", _route)
