"""Issue #8.5 Smoke #1b - quote-detector hardening regression tests.

Proves a bare "$" anywhere in page body is NEVER enough to declare a quote:

- intro/marketing page containing "$"  -> quote_detected = False
- actual synthetic quote result         -> quote_detected = True (firm)
- estimate result                       -> quote_detected = True, NOT firm

All against the local mock site (hermetic, no internet). Uses the raw route
config (not merged) so it also covers the empty-patterns worst case that
caused the Smoke #1 false positive.
"""

from __future__ import annotations

from app.browser.detect import PageDetector
from app.browser.manager import BrowserManager
from app.demo.mock_quote_site import build_mock_route_config
from app.models.browser.config import BrowserRouteConfig, QuoteDetectionConfig


async def _detect(site, path: str, config) -> object:
    browser = BrowserManager(headless=True)
    await browser.start()
    try:
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(site.url(path), wait_until="domcontentloaded")
        await page.wait_for_timeout(150)
        return await PageDetector().quote_detected(page, config)
    finally:
        await browser.stop()


async def test_marketing_dollar_page_is_not_a_quote(mock_site) -> None:
    cfg = build_mock_route_config(start_url=mock_site.url("/marketing"))
    result = await _detect(mock_site, "/marketing", cfg)
    assert result is None


async def test_marketing_dollar_page_not_a_quote_with_empty_config(mock_site) -> None:
    # Worst case: no heading/premium patterns configured at all. The old
    # `or not heading_patterns` fallback wrongly declared a quote here.
    cfg = BrowserRouteConfig(
        registry_id="x",
        start_url=mock_site.url("/marketing"),
        quote_detection=QuoteDetectionConfig(),
    )
    result = await _detect(mock_site, "/marketing", cfg)
    assert result is None


async def test_quote_result_detected_and_firm(mock_site) -> None:
    cfg = build_mock_route_config(start_url=mock_site.url("/page-d"))
    result = await _detect(mock_site, "/page-d?variant=quote", cfg)
    assert result is not None
    assert result.annual_amount_parsed == 1234.56
    assert result.is_firm_quote is True


async def test_estimate_result_detected_but_not_firm(mock_site) -> None:
    cfg = build_mock_route_config(start_url=mock_site.url("/quote?variant=estimate"))
    result = await _detect(mock_site, "/quote?variant=estimate", cfg)
    assert result is not None
    assert result.annual_amount_parsed == 900.0
    assert result.is_firm_quote is False
