"""SAFE provider-page inspection for onboarding (no applicant data).

Reuses the project's existing deterministic browser primitives:
``BrowserManager`` + ``live_privacy_context_kwargs`` + ``PageInspector`` +
``PageDetector`` + ``GenericQuoteSiteAdapter``. It ONLY observes public pages:

- final URL + host validation
- page heading + page signatures
- visible form fields (safe metadata: labels, ids/names, types, options)
- interactive elements/buttons (safe metadata)
- privacy/cookie banner heuristics
- CAPTCHA / bot-protection / callback / quote detection

It NEVER fills a form, never clicks a submission/quote action, never sends
applicant data, and never dumps full DOM HTML.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlsplit

from ...browser.adapters import GenericQuoteSiteAdapter
from ...browser.detect import PageDetector
from ...browser.inspect import PageInspector
from ...browser.manager import BrowserManager
from ...browser.session import live_privacy_context_kwargs

logger = logging.getLogger(__name__)

_PRIVACY_TOKENS = ("cookies", "cookie", "privacy", "we use cookies", "accept all")
#: Bot-block phrase evidence that a provider page is NOT quote-accessible from
#: automation (Cloudflare/WAF challenges, "you have been blocked", etc.). Used
#: only to classify honestly - never to bypass.
_BOT_BLOCK_TOKENS = (
    "sorry, you have been blocked",
    "you have been blocked",
    "cloudflare",
    "access denied",
    "unusual traffic",
    "verify you are human",
    "blocked by",
    "we've detected unusual",
    "automated access is not permitted",
)
_FALLBACK_READY = (
    "button, a, input, select, textarea, [role='button'], [role='radio'], [role='option'], [role='checkbox']"
)


def is_bot_block_text(heading: Optional[str], body_text: str = "") -> bool:
    """Deterministic soft bot-block classification from page text (no PII)."""
    haystack = f"{heading or ''}\n{body_text or ''}".lower()
    return any(token in haystack for token in _BOT_BLOCK_TOKENS)


def _safe_url(url: str) -> str:
    """Host + path + fragment (never a query string - may carry data)."""
    parts = urlsplit(url or "")
    fragment = parts.fragment or ""
    path = parts.path or "/"
    return f"{parts.netloc}{path}" + (f"#{fragment}" if fragment else "")


def _sanitize_diagnostic(message: str) -> str:
    return re.sub(r"\s+", " ", message or "").strip()[:160]


@dataclass
class InspectionResult:
    """Safe, non-sensitive inspection outcome for one provider page."""

    registry_id: str
    start_url: str
    final_url: str
    final_host: str
    host_allowed: bool
    allowed_hosts: list[str]
    heading: Optional[str]
    page_signature_ids: list[str]
    fields: list[dict] = field(default_factory=list)  # safe metadata
    interactives: list[dict] = field(default_factory=list)  # safe metadata
    observed_buttons: list[str] = field(default_factory=list)
    privacy_banner_detected: bool = False
    access_control_detected: bool = False
    callback_detected: bool = False
    quote_detected: bool = False
    page_errors: list[str] = field(default_factory=list)
    console_msgs: list[str] = field(default_factory=list)
    hydration_timeout: bool = False
    inspected_at: str = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat())


async def _wait_ready(page: Any, max_wait_ms: int, poll_ms: int = 300) -> bool:
    waited = 0
    while waited < max_wait_ms:
        try:
            locator = page.locator(_FALLBACK_READY)
            if await locator.count() and await locator.first.is_visible():
                return True
        except Exception:
            pass
        await page.wait_for_timeout(poll_ms)
        waited += poll_ms
    return False


async def inspect_page(
    registry_id: str,
    start_url: str,
    *,
    headless: bool = True,
    slow_mo_ms: int = 0,
    settle_ms: int = 4000,
    max_wait_ms: int = 20000,
) -> InspectionResult:
    """Open a provider page and collect SAFE inspection metadata only."""
    allowed_hosts = [(urlsplit(start_url).netloc or "").lower()]
    browser = BrowserManager(headless=headless, slow_mo=slow_mo_ms)
    try:
        await browser.start()
        context = await browser.new_context(**live_privacy_context_kwargs())
        page = await context.new_page()

        page_errors: list[str] = []
        console_msgs: list[str] = []

        def _on_pageerror(exc: Any) -> None:
            page_errors.append(_sanitize_diagnostic(str(exc)))

        def _on_console(msg: Any) -> None:
            if msg.type in ("error", "warning"):
                console_msgs.append(_sanitize_diagnostic(msg.text))

        page.on("pageerror", _on_pageerror)
        page.on("console", _on_console)

        await page.goto(start_url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(settle_ms)

        final_url = page.url
        host = (urlsplit(final_url).netloc or "").lower()
        host_allowed = host == allowed_hosts[0] or host.endswith("." + allowed_hosts[0])
        # Deterministic generic detection defaults (same protective patterns
        # the executor uses), so barrier/quote detection is consistent.
        from ...models.browser.config import BrowserRouteConfig

        cfg = GenericQuoteSiteAdapter().merged_config(
            BrowserRouteConfig(registry_id=registry_id, allowed_hosts=allowed_hosts)
        )

        ready = await _wait_ready(page, max_wait_ms)
        inspector = PageInspector()
        page_obs = await inspector.inspect(page, 0)
        detector = PageDetector()
        sig = await detector.page_signature(page, page_obs, cfg) if ready else None
        access_control = await detector.access_control_detected(page, cfg)
        callback = await detector.callback_detected(page, cfg)
        quote = await detector.quote_detected(page, cfg) if ready else None

        # Privacy/cookie banner + soft bot-block heuristics (safe metadata only;
        # never auto-clicked here - routine privacy UI is a separate concern).
        body_text = ""
        privacy_banner = False
        try:
            body_text = (await page.locator("body").inner_text() or "").lower()[:8000]
            privacy_banner = any(token in body_text for token in _PRIVACY_TOKENS)
        except Exception:
            pass
        soft_block = is_bot_block_text(page_obs.heading, body_text)
        access_control = access_control or soft_block

        fields: list[dict] = []
        for f in page_obs.fields:
            fields.append({
                "label": f.label,
                "external_id": f.external_field_id,
                "control_type": f.control_type,
                "input_type": f.input_type,
                "required": f.required,
                "options": (f.options_labels or [])[:20],
            })
        interactives: list[dict] = []
        buttons: list[str] = []
        for el in page_obs.interactives:
            interactives.append({
                "role": el.role,
                "element_type": el.element_type,
                "accessible_name": el.accessible_name,
                "external_id": el.external_id,
            })
            if el.element_type in ("button", "a") and el.accessible_name:
                buttons.append(el.accessible_name[:80])

        return InspectionResult(
            registry_id=registry_id,
            start_url=start_url,
            final_url=_safe_url(final_url),
            final_host=host,
            host_allowed=host_allowed,
            allowed_hosts=allowed_hosts,
            heading=page_obs.heading,
            page_signature_ids=[sig.signature_id] if sig else [],
            fields=fields,
            interactives=interactives,
            observed_buttons=buttons,
            privacy_banner_detected=privacy_banner,
            access_control_detected=access_control,
            callback_detected=callback,
            quote_detected=quote is not None,
            page_errors=page_errors[:20],
            console_msgs=console_msgs[:20],
            hydration_timeout=not ready,
        )
    finally:
        try:
            await browser.stop()
        except Exception:  # pragma: no cover - best-effort shutdown
            pass


def print_report(report: dict) -> None:
    """Print a human-readable onboarding report (safe metadata only)."""
    print(json.dumps(report, indent=2, ensure_ascii=False))


async def follow_quote_links(
    inspection: InspectionResult,
    *,
    headless: bool = True,
    slow_mo_ms: int = 0,
    max_links: int = 3,
) -> list[dict]:
    """Deterministic same-host quote-link discovery (no clicks on live forms).

    Returns safe candidate destinations (URL + label) for human review. Not
    used to auto-approve anything.
    """
    candidates: list[dict] = []
    for el in inspection.interactives:
        if len(candidates) >= max_links:
            break
        name = (el.get("accessible_name") or "").strip()
        if not name:
            continue
        if any(token in name.lower() for token in ("get a quote", "get quote", "auto quote",
                                                   "start quote", "car insurance", "compare", "get started")):
            candidates.append({"label": name, "candidate": True})
    return candidates
