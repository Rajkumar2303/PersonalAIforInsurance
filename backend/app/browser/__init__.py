"""Browser automation layer (Issue #1 foundation; Issue #7 execution).

- ``BrowserManager``: Playwright lifecycle (context isolation per session).
- ``BrowserExecutor``: deterministic, data-driven, observation-first steps.
- ``BrowserSessionManager``: session lifecycle + route-start validation.
- ``BrowserRouteConfigLoader``: data-driven route configs.
- Mock quote site for hermetic local testing (no internet).

Safety: no CAPTCHA bypass, no bot-control evasion, no authentication bypass,
no false data, no declaration/signature/payment/purchase automation, and no
applicant values in session state, logs, or traces.
"""

from __future__ import annotations

from typing import Optional

from ..core.config import get_settings
from ..services.intake import get_intake_engine
from ..services.market_registry import MarketRegistryService
from ..services.route_planner import get_route_planner
from .config import BrowserRouteConfigLoader
from .executor import BrowserExecutor
from .manager import BrowserManager, BrowserRuntimeError
from .session import BrowserSessionManager

__all__ = [
    "BrowserManager",
    "BrowserRuntimeError",
    "BrowserExecutor",
    "BrowserSessionManager",
    "BrowserRouteConfigLoader",
    "get_browser_manager",
]

_manager: Optional[BrowserSessionManager] = None


def get_browser_manager() -> BrowserSessionManager:
    """Cached default browser session manager (real engine + planner + registry)."""
    global _manager
    if _manager is None:
        settings = get_settings()
        _manager = BrowserSessionManager(
            engine=get_intake_engine(),
            planner=get_route_planner(),
            registry=MarketRegistryService(),
            config_loader=BrowserRouteConfigLoader(),
            browser=BrowserManager(headless=settings.browser_headless),
            headless=settings.browser_headless,
        )
    return _manager
