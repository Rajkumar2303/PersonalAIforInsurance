"""Page/quote/callback/access-control detection (Issue #7).

Deterministic detection driven by route config signals:

- page signature: URL pattern + heading + known field presence (branching-safe),
- access control: reCAPTCHA/hCaptcha iframes + "verify you are human" text -> STOP,
- callback/manual: "call us" / "we'll call you" -> handoff observation,
- quote: heading/premium/price/reference/coverage/discount/validity signals ->
  a RAW, unnormalized quote observation (normalization is Issue #11).

We never store page HTML wholesale. Amounts/labels are extracted only from
lines matching configured signals; quote/reference identifiers are exposed only
as ``reference_present`` + an opaque private handle.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from typing import Any, Optional

from ..models.browser.config import BrowserRouteConfig, PageSignatureSpec
from ..models.browser.observation import BrowserPageObservation, RawQuoteObservation


class PageSignatureMatch:
    """A matched page signature (id + the heading that matched, if any)."""

    __slots__ = ("signature_id", "heading")

    def __init__(self, signature_id: str, heading: Optional[str] = None) -> None:
        self.signature_id = signature_id
        self.heading = heading


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _strip_url(url: str) -> str:
    """Return a sanitized URL with the query string removed (network privacy)."""
    if "?" in url:
        return url.split("?", 1)[0]
    return url


class PageDetector:
    """Deterministic page-level detection for one Playwright page."""

    async def page_signature(
        self, page: Any, page_obs: BrowserPageObservation, config: BrowserRouteConfig
    ) -> Optional[PageSignatureMatch]:
        full_url = page.url if hasattr(page, "url") else ""
        observed_ids = {f.external_field_id for f in page_obs.fields}
        for spec in config.page_signatures:
            if self._signature_matches(spec, full_url, page_obs.heading, observed_ids):
                return PageSignatureMatch(signature_id=spec.signature_id, heading=page_obs.heading)
        return None

    @staticmethod
    def _signature_matches(spec: PageSignatureSpec, url: str, heading: Optional[str], ids: set[str]) -> bool:
        if spec.url_pattern and not re.search(spec.url_pattern, url, re.IGNORECASE):
            return False
        if spec.heading_patterns and heading:
            if not any(_normalize(p) in _normalize(heading) for p in spec.heading_patterns):
                return False
        elif spec.heading_patterns:
            return False
        if spec.field_ids and not any(fid in ids for fid in spec.field_ids):
            return False
        return True

    async def access_control_detected(self, page: Any, config: BrowserRouteConfig) -> bool:
        patterns = config.access_control_detection.patterns
        iframe_patterns = config.access_control_detection.iframe_src_patterns
        try:
            text = await self._body_text(page)
        except Exception:
            text = ""
        for pattern in patterns:
            if _normalize(pattern) in _normalize(text):
                return True
        if iframe_patterns:
            frames = page.locator("iframe")
            count = await frames.count()
            for i in range(count):
                src = await frames.nth(i).get_attribute("src") or ""
                for pattern in iframe_patterns:
                    if pattern.lower() in src.lower():
                        return True
        return False

    async def callback_detected(self, page: Any, config: BrowserRouteConfig) -> bool:
        patterns = config.callback_detection.patterns
        try:
            text = await self._body_text(page)
        except Exception:
            text = ""
        normalized = _normalize(text)
        return any(_normalize(p) in normalized for p in patterns)

    async def validation_error_detected(self, page: Any, config: BrowserRouteConfig) -> bool:
        """True when the page shows a visible validation/error state.

        Detected from configured patterns on the page text (e.g. a rejected
        value message). The executor PAUSES - it never invents a replacement
        and never loops. Retry semantics are Issue #8.
        """
        patterns = config.validation_detection.patterns
        try:
            text = await self._body_text(page)
        except Exception:
            text = ""
        normalized = _normalize(text)
        return any(_normalize(p) in normalized for p in patterns)

    async def quote_detected(self, page: Any, config: BrowserRouteConfig) -> Optional[RawQuoteObservation]:
        q = config.quote_detection
        try:
            text = await self._body_text(page)
        except Exception:
            text = ""
        try:
            heading = (await page.locator("h1, h2, h3").first.inner_text()) or ""
        except Exception:
            heading = ""
        # Heading signal is matched against the page HEADING only (not the whole
        # body), so phrases like "complete your quote" do not count as a result.
        heading_hit = any(_normalize(p) in _normalize(heading) for p in q.heading_patterns) or not q.heading_patterns
        if not heading_hit:
            return None
        normalized = _normalize(text)
        amounts = self._extract_amounts(text, q.price_pattern)
        currency = q.currency
        annual = self._extract_labeled_segments(text, q.annual_label_patterns)
        monthly = self._extract_labeled_segments(text, q.monthly_label_patterns)
        # Do not mis-assign: a monthly-only amount must not become annual. An
        # amount with no stated periodicity is captured as the FIRST line that
        # contains it (preserves wording, e.g. "Estimated premium: $900").
        if annual:
            annual_raw = annual[0]
        elif monthly:
            annual_raw = None
        else:
            annual_raw = self._first_amount_line(text, q.price_pattern)
        monthly_raw = monthly[0] if monthly else None
        parsed = self._parse_amount(annual_raw) if annual_raw else None
        monthly_parsed = self._parse_amount(monthly_raw) if monthly_raw else None
        coverage = self._extract_labeled_segments(text, q.coverage_label_patterns)
        discounts = self._extract_labeled_segments(text, q.discount_label_patterns)
        validity = self._extract_labeled_segments(text, q.validity_label_patterns)
        reference = self._extract_reference(text, q.reference_patterns)
        firm = any(_normalize(p) in normalized for p in q.firm_quote_patterns)
        return RawQuoteObservation(
            registry_id=config.registry_id,
            observed_at=dt.datetime.now(dt.timezone.utc),
            source_url=page.url if hasattr(page, "url") else None,
            annual_amount_raw=annual_raw,
            annual_amount_parsed=parsed,
            monthly_amount_raw=monthly_raw,
            monthly_amount_parsed=monthly_parsed,
            currency=currency,
            coverage_observations=coverage[:10],
            discount_observations=discounts[:10],
            validity_text=validity[0] if validity else None,
            reference_present=reference is not None,
            private_reference_handle=self._private_handle(reference),
            is_firm_quote=firm,
        )

    @staticmethod
    async def _body_text(page: Any) -> str:
        return (await page.locator("body").inner_text()) or ""

    @staticmethod
    def _extract_amounts(text: str, price_pattern: Optional[str]) -> list[str]:
        pattern = price_pattern or r"\$\s?([\d,]+(?:\.\d{1,2})?)"
        matches = re.findall(pattern, text)
        return [str(m) for m in matches[:5]]

    @staticmethod
    def _first_amount_line(text: str, price_pattern: Optional[str]) -> Optional[str]:
        pattern = price_pattern or r"\$\s?([\d,]+(?:\.\d{1,2})?)"
        for line in text.splitlines():
            line = line.strip()
            if line and re.search(pattern, line):
                return line[:200]
        return None

    @staticmethod
    def _parse_amount(raw: str) -> Optional[float]:
        cleaned = re.sub(r"[^0-9.]", "", raw)
        try:
            return float(cleaned)
        except ValueError:
            return None

    @staticmethod
    def _extract_labeled_segments(text: str, patterns: list[str]) -> list[str]:
        if not patterns:
            return []
        segments: list[str] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            for pattern in patterns:
                if _normalize(pattern) in _normalize(line):
                    segments.append(line[:200])
                    break
        return segments[:10]

    @staticmethod
    def _extract_reference(text: str, patterns: list[str]) -> Optional[str]:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            for pattern in patterns:
                match = re.search(pattern, line, re.IGNORECASE)
                if match:
                    return line[:200]
        return None

    @staticmethod
    def _private_handle(candidate: Optional[str]) -> Optional[str]:
        """Opaque handle for a user-specific reference (never the raw value)."""
        if not candidate:
            return None
        return hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:16]
