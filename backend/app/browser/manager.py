"""Async-friendly Playwright browser lifecycle.

Issue 1: foundation only. This wrapper owns the browser process so future
milestones (quote retrieval) can reuse a single lifecycle with context
isolation, without bundling automation into domain logic.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class BrowserRuntimeError(RuntimeError):
    """Raised when the Playwright runtime is unavailable or fails to start."""


class BrowserManager:
    """Async context manager around a headless Playwright Chromium browser.

    Issue 1: foundation. Issue 7: adds per-session browser contexts so each
    route/session runs in isolation (never shared across routes/users) with
    LIVE-mode privacy defaults (no video/trace/screenshot/HAR/network bodies).
    """

    def __init__(self, headless: bool = True, executable_path: str | None = None) -> None:
        self.headless = headless
        self.executable_path = executable_path
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._contexts: list[Any] = []

    @property
    def is_running(self) -> bool:
        """True when a browser process is currently launched."""
        return self._browser is not None

    async def start(self) -> None:
        """Launch Chromium. Requires ``playwright install chromium``."""
        if self._browser is not None:
            return
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise BrowserRuntimeError(
                "Playwright is not installed. Run: pip install playwright "
                "&& playwright install chromium"
            ) from exc

        self._playwright = await async_playwright().start()
        launch_kwargs: dict[str, Any] = {"headless": self.headless}
        if self.executable_path:
            launch_kwargs["executable_path"] = self.executable_path
        self._browser = await self._playwright.chromium.launch(**launch_kwargs)
        logger.info(
            "browser started",
            extra={"workflow": "browser", "workflow_stage": "start", "headless": self.headless},
        )

    async def new_context(self, **kwargs: Any) -> Any:
        """Create an isolated browser context (one per route/session).

        LIVE-mode callers pass privacy defaults (no video/tracing/screenshots/
        HAR/network bodies) - see ``app/browser/session.py``.
        """
        if not self.is_running:
            await self.start()
        context = await self._browser.new_context(**kwargs)
        self._contexts.append(context)
        return context

    async def close_context(self, context: Any) -> None:
        """Close a context and forget it (idempotent)."""
        if context in self._contexts:
            self._contexts.remove(context)
        try:
            await context.close()
        except Exception:  # pragma: no cover - best-effort close
            pass

    async def stop(self) -> None:
        """Close all contexts, the browser, and the Playwright driver."""
        for context in list(self._contexts):
            await self.close_context(context)
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        logger.info("browser stopped", extra={"workflow": "browser", "workflow_stage": "stop"})

    async def __aenter__(self) -> "BrowserManager":
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()
