"""Browser site adapter architecture (Issue #7).

The generic executor drives COMMON workflows via data-driven route config. A
``BrowserSiteAdapter`` is only needed for genuine site-specific behavior; the
``GenericQuoteSiteAdapter`` supplies safe DEFAULTS (navigation/checkpoint/
quote/callback/access-control bindings) that every route config inherits.

There is intentionally NO insurer-specific if/elif in the core executor.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

from ..models.browser.config import (
    AccessControlDetectionConfig,
    ActionBinding,
    BrowserRouteConfig,
    CallbackDetectionConfig,
    CheckpointBinding,
    QuoteDetectionConfig,
    ValidationDetectionConfig,
)
from ..models.browser.session import BrowserActionSafety


@runtime_checkable
class BrowserSiteAdapter(Protocol):
    """Route-specific browser behavior (optional per-site adapter)."""

    def merged_config(self, config: BrowserRouteConfig) -> BrowserRouteConfig:
        ...

    async def collect_clickables(self, page: Any) -> list[str]:
        ...


class GenericQuoteSiteAdapter:
    """Default generic adapter - safe defaults + common quote-form handling."""

    DEFAULT_ACTION_BINDINGS = [
        ActionBinding(action_type="continue", safety=BrowserActionSafety.SAFE_NAVIGATION,
                      label_patterns=["continue", "next", "continue to quote", "next step"]),
        ActionBinding(action_type="submit_quote", safety=BrowserActionSafety.DATA_SUBMISSION,
                      label_patterns=["get quote", "get my quote", "show my quote", "submit quote", "continue to results"]),
    ]

    DEFAULT_CHECKPOINT_BINDINGS = [
        CheckpointBinding(checkpoint_type="identity_lookup",
                          label_patterns=["verify identity", "identity verification", "identity lookup", "verify me"]),
        CheckpointBinding(checkpoint_type="consent_attestation",
                          label_patterns=["consent", "attestation", "authorize", "authorization"]),
        CheckpointBinding(checkpoint_type="application_declaration",
                          label_patterns=["declaration", "application declaration", "i agree to the", "terms and conditions"]),
        CheckpointBinding(checkpoint_type="signature",
                          label_patterns=["sign here", "sign and submit", "sign the application",
                                          "sign application", "sign below", "sign document",
                                          "electronic signature", "e-sign", "esign",
                                          "add signature", "confirm signature", "apply signature"]),
        CheckpointBinding(checkpoint_type="payment",
                          label_patterns=["pay now", "pay today", "pay premium", "pay your premium",
                                          "submit payment", "confirm payment", "complete payment",
                                          "payment method", "payment details", "purchase coverage"]),
        CheckpointBinding(checkpoint_type="purchase",
                          label_patterns=["buy", "buy now", "purchase", "bind", "bind policy", "bind now", "activate policy"]),
        CheckpointBinding(checkpoint_type="policy_binding",
                          label_patterns=["bind policy", "confirm binding"]),
        CheckpointBinding(checkpoint_type="renewal",
                          label_patterns=["renew", "renewal"]),
        CheckpointBinding(checkpoint_type="cancellation",
                          label_patterns=["cancel policy", "cancel my policy", "cancel your policy",
                                          "cancel coverage", "cancel renewal", "cancellation"]),
    ]

    DEFAULT_QUOTE_DETECTION = QuoteDetectionConfig(
        heading_patterns=["your quote", "your estimated premium", "quote results", "annual premium"],
        premium_label_patterns=["premium", "annual premium", "estimated annual premium", "price"],
        price_pattern=r"\$\s?([\d,]+(?:\.\d{1,2})?)",
        currency="CAD",
        reference_patterns=[r"(?:quote|reference|policy|application)[\s#]*([A-Z0-9]{4,})"],
        coverage_label_patterns=["third party liability", "accident benefits", "comprehensive", "collision", "dcpd", "uninsured"],
        discount_label_patterns=["discount", "multi-policy", "winter tire", "claims-free", "conviction-free"],
        validity_label_patterns=["valid for", "valid until", "expires", "quoted for 30 days"],
        monthly_label_patterns=["per month", "monthly"],
        annual_label_patterns=["per year", "annual", "per annum"],
        firm_quote_patterns=["firm quote", "binding quote", "locked-in rate", "guaranteed for"],
    )

    DEFAULT_CALLBACK_DETECTION = CallbackDetectionConfig(
        patterns=["call us", "we'll call you", "we will call you", "speak to a broker",
                  "speak with an advisor", "complete by phone", "phone to complete"],
    )

    DEFAULT_ACCESS_CONTROL_DETECTION = AccessControlDetectionConfig(
        patterns=["verify you are human", "are you human", "automated access is not permitted",
                  "we have detected automated", "security check", "verify your identity to continue",
                  "access denied", "access is denied", "rate limit", "too many requests",
                  "bot detection", "unusual traffic", "sign in to continue", "log in to continue",
                  "please sign in", "captcha"],
        iframe_src_patterns=["recaptcha", "hcaptcha", "captcha", "challenges.cloudflare.com"],
    )

    DEFAULT_VALIDATION_DETECTION = ValidationDetectionConfig(
        patterns=["please enter", "please select", "must be between", "is invalid",
                  "enter a valid", "please correct", "this field is required",
                  "an error occurred", "invalid value"],
    )

    def merged_config(self, config: BrowserRouteConfig) -> BrowserRouteConfig:
        """Merge route config with generic safe defaults (route takes precedence).

        Protective defaults (checkpoints/access-control) are ALWAYS present.
        """
        return config.model_copy(
            update={
                "action_bindings": [*config.action_bindings, *self.DEFAULT_ACTION_BINDINGS],
                "checkpoint_bindings": [*config.checkpoint_bindings, *self.DEFAULT_CHECKPOINT_BINDINGS],
                "quote_detection": self._merge_quote(config.quote_detection),
                "callback_detection": CallbackDetectionConfig(
                    patterns=[*config.callback_detection.patterns, *self.DEFAULT_CALLBACK_DETECTION.patterns]
                ),
                "access_control_detection": AccessControlDetectionConfig(
                    patterns=[*config.access_control_detection.patterns, *self.DEFAULT_ACCESS_CONTROL_DETECTION.patterns],
                    iframe_src_patterns=[
                        *config.access_control_detection.iframe_src_patterns,
                        *self.DEFAULT_ACCESS_CONTROL_DETECTION.iframe_src_patterns,
                    ],
                    selectors=[*config.access_control_detection.selectors, *self.DEFAULT_ACCESS_CONTROL_DETECTION.selectors],
                ),
                "validation_detection": ValidationDetectionConfig(
                    patterns=[*config.validation_detection.patterns, *self.DEFAULT_VALIDATION_DETECTION.patterns],
                    selectors=[*config.validation_detection.selectors, *self.DEFAULT_VALIDATION_DETECTION.selectors],
                ),
            }
        )

    def _merge_quote(self, configured: QuoteDetectionConfig) -> QuoteDetectionConfig:
        base = self.DEFAULT_QUOTE_DETECTION
        return QuoteDetectionConfig(
            heading_patterns=[*configured.heading_patterns, *base.heading_patterns],
            premium_label_patterns=[*configured.premium_label_patterns, *base.premium_label_patterns],
            price_pattern=configured.price_pattern or base.price_pattern,
            currency=configured.currency or base.currency,
            reference_patterns=[*configured.reference_patterns, *base.reference_patterns],
            coverage_label_patterns=[*configured.coverage_label_patterns, *base.coverage_label_patterns],
            discount_label_patterns=[*configured.discount_label_patterns, *base.discount_label_patterns],
            validity_label_patterns=[*configured.validity_label_patterns, *base.validity_label_patterns],
            monthly_label_patterns=[*configured.monthly_label_patterns, *base.monthly_label_patterns],
            annual_label_patterns=[*configured.annual_label_patterns, *base.annual_label_patterns],
            firm_quote_patterns=[*configured.firm_quote_patterns, *base.firm_quote_patterns],
        )

    async def collect_clickables(self, page: Any) -> list[str]:
        """Collect visible button labels (safe; never button values/HTML)."""
        labels: list[str] = []
        for selector in ("button", "input[type=submit]", "input[type=button]"):
            locator = page.locator(selector)
            count = await locator.count()
            for i in range(count):
                control = locator.nth(i)
                try:
                    if not await control.is_visible():
                        continue
                except Exception:
                    continue
                text = (await control.inner_text()).strip()
                if not text:
                    text = (await control.get_attribute("value")) or ""
                text = text.strip()
                if text and text not in labels:
                    labels.append(text)
        return labels

    async def click_by_label(self, page: Any, label: str) -> None:
        """Click a button/link by its exact label (deterministic)."""
        button = page.get_by_role("button", name=label, exact=True).first
        if await button.count():
            await button.click()
            return
        submit = page.locator(f'input[type=submit][value="{label}"]').first
        if await submit.count():
            await submit.click()
            return
        link = page.get_by_role("link", name=label, exact=True).first
        if await link.count():
            await link.click()
            return
        raise RuntimeError("could not locate clickable action")
