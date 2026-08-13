"""Focused tests for the address-screen stopped_prohibited false positive.

Root cause: the generic `payment` checkpoint binding used the bare substring
"pay", so global page navigation labels on the Sonnet ADDRESS screen (e.g.
"Pay my bill" / "Make a payment") matched it and the executor stopped the whole
quote with stopped_prohibited - a false positive.

This file proves:
- the observed address-page navigation is NOT prohibited;
- genuine application declaration / payment / purchase / binding remain
  PROHIBITED (protection not weakened);
- the address/postal binding maps to the existing intake path with a stable
  semantic matcher;
- no applicant values live in the config used for classification.
"""

from __future__ import annotations

from pathlib import Path

from app.browser.actions import ActionClassifier
from app.browser.adapters import GenericQuoteSiteAdapter
from app.browser.config import BrowserRouteConfigLoader
from app.models.browser.config import BrowserRouteConfig
from app.models.browser.session import BrowserActionSafety

REAL_ROUTES_DIR = Path(__file__).resolve().parents[1] / "data" / "browser" / "routes"

SENSITIVE = ["Test Applicant", "T0000-00000-00000", "1HGCM82633A000000", "M0A 0A0"]


def _merged():
    return GenericQuoteSiteAdapter().merged_config(BrowserRouteConfig(registry_id="sonnet"))


def test_observed_address_page_navigation_is_not_prohibited() -> None:
    """Global nav on the address screen must never be a hard prohibited stop."""
    classifier = ActionClassifier()
    merged = _merged()
    for label in ("Pay my bill", "Make a payment", "Payments", "Billing & payments",
                  "Sign in", "Cancel"):
        clickable = classifier.classify(label, merged)
        assert clickable.safety is not BrowserActionSafety.PROHIBITED, label


def test_genuine_application_declaration_still_prohibited() -> None:
    classifier = ActionClassifier()
    merged = _merged()
    clickable = classifier.classify("I agree to the declaration", merged)
    assert clickable.safety is BrowserActionSafety.PROHIBITED
    assert clickable.action_type == "application_declaration"


def test_genuine_payment_purchase_binding_still_prohibited() -> None:
    classifier = ActionClassifier()
    merged = _merged()
    cases = [("Pay now", "payment"), ("Submit payment", "payment"),
             ("Buy Now", "purchase"), ("Bind policy", {"purchase", "policy_binding"}),
             ("Sign here", "signature")]
    for label, kind in cases:
        clickable = classifier.classify(label, merged)
        assert clickable.safety is BrowserActionSafety.PROHIBITED, label
        expected = kind if isinstance(kind, set) else {kind}
        assert clickable.action_type in expected, label


def test_address_postal_binding_maps_to_existing_intake_path() -> None:
    cfg = BrowserRouteConfigLoader(config_dir=REAL_ROUTES_DIR).load("sonnet")
    postal = next(b for b in cfg.field_bindings if b.canonical_path == "applicant.address.postal_code")
    assert postal is not None
    # Stable semantic matcher for the address question label.
    assert any(p.value == "postal" for p in postal.match_patterns)
    assert postal.sensitivity.value == "sensitive"


def test_no_applicant_values_in_config_or_classification() -> None:
    cfg = BrowserRouteConfigLoader(config_dir=REAL_ROUTES_DIR).load("sonnet")
    raw = (REAL_ROUTES_DIR / "sonnet.json").read_text(encoding="utf-8")
    blob = raw + cfg.model_dump_json()
    for marker in SENSITIVE:
        assert marker not in blob
