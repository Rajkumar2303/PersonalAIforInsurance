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

    async def bot_protection_detected(self, page: Any, config: BrowserRouteConfig) -> bool:
        """Passive bot-protection PRESENCE (safe metadata), NOT a blocker.

        True when ANY captcha-provider evidence exists: a visible g/h-captcha
        badge/container, or ANY (visible or hidden) captcha-provider iframe.
        Invisible/reCAPTCHA Enterprise badge mode is common on insurance SPAs and
        is NOT by itself a blocking barrier - see access_control_detected().
        """
        cfg = config.access_control_detection
        for selector in ('[class*="g-recaptcha"]', '[class*="h-captcha"]'):
            if await self._any_visible(page, selector):
                return True
        return await self._any_captcha_iframe(page, cfg.iframe_src_patterns)

    async def access_control_detected(self, page: Any, config: BrowserRouteConfig) -> bool:
        """True only with BLOCKING access-control evidence (#8.5 #1c/#1d).

        Passive/invisible bot protection - a footer "Protected by reCAPTCHA"
        badge or a hidden reCAPTCHA Enterprise token iframe - is PRESENCE, not a
        blocker. A real blocker requires ACTIVE evidence:

        - configured barrier selector (explicit)
        - a VISIBLE CAPTCHA/challenge iframe (recaptcha/hcaptcha/cloudflare)
        - a visible g-recaptcha / h-captcha challenge WIDGET (contains a visible
          challenge iframe or a visible challenge checkbox)
        - strong challenge/block wording (multi-word phrase, or in the page
          heading): "verify you are human", "access denied", "unusual traffic",
          "sign in to continue", ... (single words like "captcha"/"login" in
          body copy never count)
        """
        cfg = config.access_control_detection
        for selector in cfg.selectors:
            if await self._any_visible(page, selector):
                return True
        if await self._visible_captcha_iframe(page, cfg.iframe_src_patterns):
            return True
        for selector in ('[class*="g-recaptcha"]', '[class*="h-captcha"]'):
            if await self._visible_challenge_widget(page, selector):
                return True
        try:
            text = await self._body_text(page)
        except Exception:
            text = ""
        try:
            heading = (await page.locator("h1, h2, h3").first.inner_text()) or ""
        except Exception:
            heading = ""
        if self._strong_text_hit(text, heading, cfg.patterns):
            return True
        return False

    async def _any_captcha_iframe(self, page: Any, iframe_patterns: list[str]) -> bool:
        """Any captcha-provider iframe (visible OR hidden) = bot-protection presence."""
        if not iframe_patterns:
            return False
        frames = page.locator("iframe")
        count = await frames.count()
        for i in range(min(count, 40)):
            try:
                src = (await frames.nth(i).get_attribute("src")) or ""
            except Exception:
                continue
            lowered = src.lower()
            if any(pattern.lower() in lowered for pattern in iframe_patterns):
                return True
        return False

    async def _visible_challenge_widget(self, page: Any, selector: str) -> bool:
        """True when a VISIBLE g/h-captcha container is an ACTIVE challenge.

        A passive footer badge (e.g. "Protected by reCAPTCHA" with no widget) or
        an invisible-mode container has neither a visible challenge iframe nor a
        visible challenge checkbox, so it is NOT a blocking barrier.
        """
        locator = page.locator(selector)
        count = await locator.count()
        for i in range(min(count, 40)):
            el = locator.nth(i)
            try:
                if not await el.is_visible():
                    continue
            except Exception:
                continue
            # A visible challenge iframe inside the widget is the strongest signal.
            try:
                inner_frames = el.locator("iframe")
                inner_count = await inner_frames.count()
                for j in range(min(inner_count, 10)):
                    try:
                        if await inner_frames.nth(j).is_visible():
                            return True
                    except Exception:
                        continue
            except Exception:
                pass
            # A visible challenge checkbox (reCAPTCHA / hCaptcha widget checkbox).
            for cb in ('[role="checkbox"]', 'input[type="checkbox"]',
                       '.recaptcha-checkbox', '.h-captcha-checkbox'):
                try:
                    cbs = el.locator(cb)
                    cb_count = await cbs.count()
                    for k in range(min(cb_count, 10)):
                        try:
                            if await cbs.nth(k).is_visible():
                                return True
                        except Exception:
                            continue
                except Exception:
                    continue
        return False

    async def validation_error_detected(self, page: Any, config: BrowserRouteConfig) -> bool:
        """True only with structured validation-error evidence (#8.5 #1c/#1d).

        A pristine, untouched Angular form carrying ``ng-invalid`` (because a
        required value is still empty), a HIDDEN aria-invalid field, a VISIBLE
        required field that merely starts with aria-invalid=true before any
        interaction, or instructional copy ("Please select your province",
        "Required*") is NORMAL initial form state - NOT an error. Legitimate
        evidence:

        - configured validation-error selector (explicit)
        - a VISIBLE control with aria-invalid="true" that has ENTERED an error
          state (interaction-state class token touched/dirty/submitted, or a
          non-empty value still flagged invalid) - never a pristine untouched
          empty required field
        - a visible role="alert" carrying validation-failure wording
        - a visible aria-live="assertive" error announcement with wording
        - a visible LEAF-level error/invalid element (never the <form> wrapper)
          with validation-failure wording

        The executor PAUSES - it never invents a replacement and never loops.
        Retry semantics are Issue #8.
        """
        cfg = config.validation_detection
        for selector in cfg.selectors:
            if await self._any_visible(page, selector):
                return True
        if await self._visible_invalid_control(page):
            return True
        if await self._visible_with_text(page, '[role="alert"]', cfg.patterns):
            return True
        if await self._visible_with_text(page, '[aria-live="assertive"]', cfg.patterns):
            return True
        for selector in ('[class*="error" i]', '[class*="invalid" i]'):
            if await self._leaf_error_with_text(page, selector, cfg.patterns):
                return True
        return False

    @staticmethod
    async def _visible_invalid_control(page: Any) -> bool:
        """True when a VISIBLE aria-invalid control has ENTERED an error state.

        A pristine, untouched, empty required field that merely starts with
        aria-invalid=true is NORMAL initial form state - not an error. Error
        state requires post-interaction evidence on the control itself:

        - an interaction-state class token (touched/dirty/submitted), OR
        - a non-empty text value on a control that is still flagged invalid
          (it was filled and is still in an error state).

        Visible structured error messages are handled by their own branches
        (role="alert" / aria-live / leaf error elements).
        """
        interaction_tokens = {
            "ng-touched", "ng-dirty", "ng-submitted",
            "touched", "dirty", "submitted",
            "was-validated", "is-invalid", "has-error",
        }
        locator = page.locator('[aria-invalid="true"]')
        count = await locator.count()
        for i in range(min(count, 60)):
            el = locator.nth(i)
            try:
                if not await el.is_visible():
                    continue
                cls = set(((await el.get_attribute("class")) or "").split())
                is_text_like = await el.evaluate(
                    "e => (e.tagName && e.tagName.toLowerCase() === 'textarea')"
                    " || (e.tagName && e.tagName.toLowerCase() === 'input'"
                    " && e.type !== 'radio' && e.type !== 'checkbox')"
                )
                value = (await el.input_value()).strip() if is_text_like else ""
            except Exception:
                continue
            if cls & interaction_tokens:
                return True
            if value:
                return True
        return False

    @staticmethod
    async def _leaf_error_with_text(page: Any, selector: str, patterns: list[str]) -> bool:
        """True when a VISIBLE, LEAF-level error/invalid element carries wording.

        The parent <form>/container that merely aggregates Angular ng-invalid
        state is NOT itself error evidence: <form> tags are skipped, and any
        element wrapping another visible error/invalid element is treated as a
        container (only the deepest node counts). Hidden controls are skipped.
        """
        if not patterns:
            return False
        locator = page.locator(selector)
        count = await locator.count()
        for i in range(min(count, 60)):
            el = locator.nth(i)
            try:
                if not await el.is_visible():
                    continue
                tag = await el.evaluate("e => (e.tagName || '').toLowerCase()")
                if tag == "form":  # a <form> wrapper is never error evidence itself
                    continue
                text = (await el.inner_text()) or ""
            except Exception:
                continue
            if not any(_normalize(p) in _normalize(text) for p in patterns):
                continue
            # Skip containers that wrap another visible error/invalid element
            # (they duplicate the deepest element's text).
            try:
                nested_count = await el.locator(selector).count()
            except Exception:
                return True  # element detached mid-scan; treat the match as a leaf
            has_visible_nested = False
            for j in range(min(nested_count, 10)):
                try:
                    if await el.locator(selector).nth(j).is_visible():
                        has_visible_nested = True
                        break
                except Exception:
                    continue
            if has_visible_nested:
                continue
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

    # --- shared detection helpers --------------------------------------

    @staticmethod
    async def _any_visible(page: Any, selector: str) -> bool:
        """True when any element matching ``selector`` is visible (bounded)."""
        locator = page.locator(selector)
        count = await locator.count()
        for i in range(min(count, 60)):
            try:
                if await locator.nth(i).is_visible():
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    async def _visible_with_text(page: Any, selector: str, patterns: list[str]) -> bool:
        """True when a visible element matches ``selector`` and contains a pattern."""
        if not patterns:
            return False
        locator = page.locator(selector)
        count = await locator.count()
        for i in range(min(count, 60)):
            try:
                if not await locator.nth(i).is_visible():
                    continue
                text = (await locator.nth(i).inner_text()) or ""
            except Exception:
                continue
            if any(_normalize(p) in _normalize(text) for p in patterns):
                return True
        return False

    async def _visible_captcha_iframe(self, page: Any, iframe_patterns: list[str]) -> bool:
        """True when a VISIBLE iframe src matches a captcha provider pattern.

        Hidden/invisible token iframes (e.g. reCAPTCHA Enterprise anchor used in
        the background) are NOT a barrier.
        """
        if not iframe_patterns:
            return False
        frames = page.locator("iframe")
        count = await frames.count()
        for i in range(min(count, 40)):
            frame = frames.nth(i)
            try:
                if not await frame.is_visible():
                    continue
                src = (await frame.get_attribute("src")) or ""
            except Exception:
                continue
            lowered = src.lower()
            if any(pattern.lower() in lowered for pattern in iframe_patterns):
                return True
        return False

    @staticmethod
    def _strong_text_hit(text: str, heading: str, patterns: list[str]) -> bool:
        """A pattern counts only as a strong signal:
        - it appears in the page HEADING (any length), OR
        - it appears in the body as a MULTI-WORD phrase (single words like
          "captcha"/"login"/"security" in copy never count as barriers).
        """
        normalized_text = _normalize(text)
        normalized_heading = _normalize(heading)
        for pattern in patterns:
            normalized = _normalize(pattern)
            if not normalized:
                continue
            if normalized in normalized_heading:
                return True
            if len(normalized.split()) >= 2 and normalized in normalized_text:
                return True
        return False

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

        # Hardened (Issue #8.5 Smoke #1b): a "$" anywhere in the body is NEVER
        # enough to declare a quote. We require a parseable amount PLUS at least
        # one strong contextual signal:
        #   - the page HEADING matches a quote-result heading pattern, OR
        #   - a premium label appears on the SAME LINE as an amount.
        # The old `or not q.heading_patterns` fallback (which let any page with
        # a "$" declare a quote when no heading patterns were configured) is
        # removed - empty config must be conservative, not permissive.
        amounts = self._extract_amounts(text, q.price_pattern)
        if not amounts:
            return None
        normalized_heading = _normalize(heading)
        heading_hit = bool(q.heading_patterns) and any(
            _normalize(p) in normalized_heading for p in q.heading_patterns
        )
        premium_line_hit = self._premium_amount_line_hit(text, q.premium_label_patterns, q.price_pattern)
        if not (heading_hit or premium_line_hit):
            return None

        normalized = _normalize(text)
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
    def _premium_amount_line_hit(text: str, premium_patterns: list[str], price_pattern: Optional[str]) -> bool:
        """True when a premium label appears on the same line as an amount.

        Stronger than a page-wide contains: the label and the parsed amount must
        be on the SAME line (e.g. "Annual premium: $1,234.56"), so generic
        marketing text like "Save $100 a year" never counts.
        """
        if not premium_patterns:
            return False
        price_re = re.compile(price_pattern or r"\$\s?([\d,]+(?:\.\d{1,2})?)", re.IGNORECASE)
        for line in text.splitlines():
            line = line.strip()
            if not line or not price_re.search(line):
                continue
            if any(_normalize(p) in _normalize(line) for p in premium_patterns):
                return True
        return False

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
