"""Browser automation foundation (Issue 1: interface only).

No insurer websites are automated yet. The manager exposes a single
async Playwright lifecycle for future quote-retrieval milestones and
never bypasses CAPTCHAs, authentication, bot controls, or rate limits.
"""

from __future__ import annotations

from .manager import BrowserManager, BrowserRuntimeError

__all__ = ["BrowserManager", "BrowserRuntimeError"]
