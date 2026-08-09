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
    """Async context manager around a headless Playwright Chromium browser."""

    def __init__(self, headless: bool = True, executable_path: str | None = None) -> None:
        self.headless = headless
        self.executable_path = executable_path
        self._playwright: Any | None = None
        self._browser: Any | None = None

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

    async def stop(self) -> None:
        """Close the browser and the Playwright driver."""
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
