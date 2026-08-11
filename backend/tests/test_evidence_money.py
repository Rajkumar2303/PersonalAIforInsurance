"""Issue #10, Prompt 2 - money / Decimal precision hardening tests (§16).

NO float money in persistence. The browser quote detector now parses an exact
Decimal from the ORIGINAL text; the ingest boundary prefers it and falls back
to the safe ``Decimal(str(float))`` conversion. Precision values (1234.56,
0.01, 99999.99) must round-trip exactly.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.browser.detect import PageDetector
from app.browser.manager import BrowserManager
from app.demo.mock_quote_site import build_mock_route_config
from app.models.browser.observation import (
    BrowserObservation,
    BrowserObservationType,
    BrowserQuoteObservation,
    RawQuoteObservation,
)
from app.services.evidence.ingest import (
    _float_to_decimal,
    quote_from_browser_observation,
)

PRECISION_VALUES = [("1234.56", "1234.56"), ("0.01", "0.01"), ("99999.99", "99999.99")]


@pytest.mark.parametrize("raw,expected", PRECISION_VALUES)
def test_parse_amount_decimal_exact(raw: str, expected: str) -> None:
    assert PageDetector._parse_amount_decimal(f"${raw}") == Decimal(expected)
    assert PageDetector._parse_amount_decimal(f"$ {raw} CAD") == Decimal(expected)


@pytest.mark.parametrize("raw,expected", PRECISION_VALUES)
def test_quote_ingest_prefers_exact_decimal(raw: str, expected: str) -> None:
    raw_quote = RawQuoteObservation(
        registry_id="mock-insurer",
        observed_at=dt.datetime.now(dt.timezone.utc),
        annual_amount_raw=f"${raw}",
        annual_amount_parsed=float(raw),
        annual_amount_decimal=Decimal(expected),
        monthly_amount_raw=None,
        monthly_amount_parsed=None,
        monthly_amount_decimal=None,
        currency="CAD",
        reference_present=True,
        private_reference_handle="opaque-ref",
        is_firm_quote=True,
    )
    obs = BrowserObservation(
        observation_type=BrowserObservationType.QUOTE_DETECTED,
        page_signature="quote-page",
        quote=BrowserQuoteObservation(quote_present=True, reference_present=True, raw=raw_quote),
    )
    quote = quote_from_browser_observation(
        "intake-1",
        obs,
        plan_id="plan-1",
        planned_route_id="mock-insurer",
        registry_id="mock-insurer",
        distinct_rate_source_id="RS-MOCK-INSURER",
        attempt_id="att-1",
    )
    assert quote is not None
    assert quote.annual_premium == Decimal(expected)


@pytest.mark.parametrize("raw,expected", PRECISION_VALUES)
def test_float_to_decimal_fallback_is_exact(raw: str, expected: str) -> None:
    # The fallback boundary Decimal(str(float)) is exact for these values.
    assert _float_to_decimal(float(raw)) == Decimal(expected)


async def test_detector_produces_exact_decimal_from_real_quote_page(mock_site) -> None:
    cfg = build_mock_route_config(start_url=mock_site.url("/page-d"))
    browser = BrowserManager(headless=True)
    await browser.start()
    try:
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(mock_site.url("/page-d?variant=quote"), wait_until="domcontentloaded")
        await page.wait_for_timeout(150)
        result = await PageDetector().quote_detected(page, cfg)
        assert result is not None
        # The exact Decimal parsed from the original "$1,234.56" text.
        assert result.annual_amount_decimal == Decimal("1234.56")
        assert result.annual_amount_parsed == 1234.56
        # Evidence ingest uses the exact Decimal (no float drift).
        from app.services.evidence.ingest import _quote_amount

        assert _quote_amount(result.annual_amount_decimal, result.annual_amount_parsed) == Decimal("1234.56")
    finally:
        await browser.stop()
