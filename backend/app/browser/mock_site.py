"""Local mock quote site for hermetic Issue #7 integration tests.

A controlled, multi-step quote-site fixture using ONLY the Python standard
library (no internet). Simulates realistic behaviour:

- Page A: applicant fields (name, postal, DOB, street)
- Page B: vehicle fields + one missing progressive field (annual kilometres)
- Page C: conditional field (one-way commute distance revealed by carpool=Yes)
- Page D: human/identity checkpoint OR quote-result variant (?variant=)
- Extra scenario pages: CAPTCHA, unknown field, callback, selector/label change

The site is served at http://127.0.0.1:<ephemeral port> and used ONLY with
synthetic data. ``build_mock_route_config`` builds the data-driven
``BrowserRouteConfig`` for the mock route (and scenario variants).
"""

from __future__ import annotations

import datetime as dt
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

from ..models.browser.config import (
    AccessControlDetectionConfig,
    ActionBinding,
    BrowserFieldBinding,
    BrowserRouteConfig,
    CallbackDetectionConfig,
    CheckpointBinding,
    FillStrategy,
    MatchPattern,
    MatchStrategy,
    PageSignatureSpec,
    QuoteDetectionConfig,
    TransformKind,
)
from ..models.browser.session import BrowserActionSafety
from ..models.intake.field_catalog import FieldSensitivity

MOCK_REGISTRY_ID = "mock-insurer"


def _page_a_html() -> str:
    return """<!doctype html><html><head><meta charset="utf-8"><title>Mock A</title></head>
<body>
<h1>Applicant Information</h1>
<form method="post" action="/submit">
  <input type="hidden" name="next" value="page-b">
  <div><label for="legal-name">Legal name</label>
    <input id="legal-name" name="legal_name" type="text" required></div>
  <div><label for="preferred-language">Preferred language</label>
    <select id="preferred-language" name="preferred_language">
      <option value="english">English</option>
      <option value="french">French</option>
    </select></div>
  <div><label for="postal-code">Postal code</label>
    <input id="postal-code" name="postal_code" type="text" required></div>
  <div><label for="date-of-birth">Date of birth</label>
    <input id="date-of-birth" name="date_of_birth" type="date" required></div>
  <div><label for="street">Street address</label>
    <input id="street" name="street" type="text" required></div>
  <button type="submit" id="continue-a">Continue</button>
</form>
</body></html>"""


def _page_b_html(annual_km_id: str = "annual-km") -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Mock B</title></head>
<body>
<h1>Vehicle Information</h1>
<form method="post" action="/submit">
  <input type="hidden" name="next" value="page-c">
  <div><label for="vin">VIN</label>
    <input id="vin" name="vin" type="text" required></div>
  <div><label for="model-year">Model year</label>
    <input id="model-year" name="model_year" type="number" required></div>
  <div><label for="{annual_km_id}">Approximate annual distance driven</label>
    <input id="{annual_km_id}" name="annual_km" type="number" required></div>
  <div><fieldset><legend>Do you have winter tires?</legend>
    <input type="checkbox" id="winter-tires" name="winter_tires" value="yes">
    <label for="winter-tires">Do you have winter tires?</label></fieldset></div>
  <button type="submit" id="continue-b">Continue</button>
</form>
</body></html>"""


def _page_c_html() -> str:
    return """<!doctype html><html><head><meta charset="utf-8"><title>Mock C</title></head>
<body>
<h1>Commuting Details</h1>
<form method="post" action="/submit">
  <input type="hidden" name="next" value="page-d">
  <input type="hidden" name="variant" value="quote">
  <fieldset><legend>Do you carpool to work or school?</legend>
    <input type="radio" id="carpool-yes" name="carpool" value="yes" onchange="toggleCommute()">
    <label for="carpool-yes">Yes</label>
    <input type="radio" id="carpool-no" name="carpool" value="no" checked onchange="toggleCommute()">
    <label for="carpool-no">No</label>
  </fieldset>
  <div id="commute-block" style="display:none">
    <label for="one-way-commute">One-way commute distance (km)</label>
    <input id="one-way-commute" name="one_way_commute" type="number">
  </div>
  <button type="submit" id="continue-c">Continue</button>
</form>
<script>
function toggleCommute() {
  var v = document.querySelector('input[name="carpool"]:checked');
  document.getElementById('commute-block').style.display =
    (v && v.value === 'yes') ? 'block' : 'none';
}
</script>
</body></html>"""


def _page_d_html(variant: str) -> str:
    if variant == "checkpoint":
        return """<!doctype html><html><head><meta charset="utf-8"><title>Mock D</title></head>
<body>
<h1>Identity Verification</h1>
<p>To continue we need to verify your identity.</p>
<button type="button" id="verify-identity">Verify identity</button>
</body></html>"""
    return """<!doctype html><html><head><meta charset="utf-8"><title>Mock D</title></head>
<body>
<h1>Your Quote</h1>
<p>Annual premium: $1,234.56</p>
<p>Quote reference: MOCK-8F3K-2026</p>
<ul>
  <li>Third party liability - $2,000,000</li>
  <li>Accident benefits - increased limits</li>
  <li>Comprehensive - $500 deductible</li>
</ul>
<p>Discount: winter tires</p>
<p>Valid for 30 days</p>
<button type="button" id="buy-now">Buy Now</button>
</body></html>"""


def _captcha_html() -> str:
    return """<!doctype html><html><head><meta charset="utf-8"><title>Mock CAPTCHA</title></head>
<body>
<h1>Security Check</h1>
<iframe src="https://www.google.com/recaptcha/api2/anchor"></iframe>
<p>Verify you are human before continuing.</p>
</body></html>"""


def _unknown_html() -> str:
    return """<!doctype html><html><head><meta charset="utf-8"><title>Mock Unknown</title></head>
<body>
<h1>Additional Information</h1>
<form method="post" action="/submit">
  <div><label for="some-unknown-field">What is your shoe size?</label>
    <input id="some-unknown-field" name="shoe_size" type="text" required></div>
  <button type="submit" id="continue-unknown">Continue</button>
</form>
</body></html>"""


def _callback_html() -> str:
    return """<!doctype html><html><head><meta charset="utf-8"><title>Mock Callback</title></head>
<body>
<h1>Complete by Phone</h1>
<p>Please call us at 1-800-555-0199 to complete your quote.</p>
</body></html>"""


def _buy_html() -> str:
    return """<!doctype html><html><head><meta charset="utf-8"><title>Mock Buy</title></head>
<body>
<h1>Almost There</h1>
<p>Your policy is ready to be bound.</p>
<button type="button" id="buy-now">Buy Now</button>
</body></html>"""


def _newfield_html() -> str:
    return """<!doctype html><html><head><meta charset="utf-8"><title>Mock New Field</title></head>
<body>
<h1>New Field Discovery</h1>
<form method="post" action="/submit">
  <input type="hidden" name="next" value="page-d">
  <input type="hidden" name="variant" value="quote">
  <div><label for="email">Email address</label>
    <input id="email" name="email" type="email"></div>
  <button type="submit" id="continue-new">Continue to quote</button>
</form>
</body></html>"""


def _multi_missing_html() -> str:
    return """<!doctype html><html><head><meta charset="utf-8"><title>Mock Multi Missing</title></head>
<body>
<h1>Multiple Missing Fields</h1>
<form method="post" action="/submit">
  <input type="hidden" name="next" value="page-d">
  <input type="hidden" name="variant" value="quote">
  <div><label for="annual-km">Approximate annual distance driven</label>
    <input id="annual-km" name="annual_km" type="number" required></div>
  <div><label for="years-address">Years at current address</label>
    <input id="years-address" name="years_address" type="number" required></div>
  <div><label for="tpl-limit">Liability limit</label>
    <input id="tpl-limit" name="tpl_limit" type="number" required></div>
  <button type="submit" id="continue-multi">Continue</button>
</form>
</body></html>"""


def _annual_twice_html(step: int) -> str:
    nxt = "annual-2" if step == 1 else "page-d"
    variant = "" if step == 1 else "<input type=\"hidden\" name=\"variant\" value=\"quote\">"
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Mock Annual {step}</title></head>
<body>
<h1>Annual Question {step}</h1>
<form method="post" action="/submit">
  <input type="hidden" name="next" value="{nxt}">
  {variant}
  <div><label for="annual-km">Approximate annual distance driven</label>
    <input id="annual-km" name="annual_km" type="number" required></div>
  <button type="submit" id="continue-annual{step}">Continue</button>
</form>
</body></html>"""


def _chain_html() -> str:
    return """<!doctype html><html><head><meta charset="utf-8"><title>Mock Chain</title></head>
<body>
<h1>Commuting & Rideshare</h1>
<form method="post" action="/submit">
  <input type="hidden" name="next" value="page-d">
  <input type="hidden" name="variant" value="quote">
  <fieldset><legend>Do you use the vehicle for commuting?</legend>
    <input type="radio" id="commute-yes" name="commuting" value="yes" onchange="toggleChain()">
    <label for="commute-yes">Yes</label>
    <input type="radio" id="commute-no" name="commuting" value="no" checked onchange="toggleChain()">
    <label for="commute-no">No</label>
  </fieldset>
  <div id="commute-block" style="display:none">
    <label for="one-way-commute">One-way commute distance (km)</label>
    <input id="one-way-commute" name="one_way_commute" type="number">
  </div>
  <fieldset><legend>Do you drive for rideshare?</legend>
    <input type="radio" id="rideshare-yes" name="rideshare" value="yes" onchange="toggleChain()">
    <label for="rideshare-yes">Yes</label>
    <input type="radio" id="rideshare-no" name="rideshare" value="no" checked onchange="toggleChain()">
    <label for="rideshare-no">No</label>
  </fieldset>
  <div id="rideshare-block" style="display:none">
    <label for="rideshare-hours">Average rideshare hours per week</label>
    <input id="rideshare-hours" name="rideshare_hours" type="number">
  </div>
  <button type="submit" id="continue-chain">Continue</button>
</form>
<script>
function toggleChain() {
  var c = document.querySelector('input[name="commuting"]:checked');
  document.getElementById('commute-block').style.display = (c && c.value === 'yes') ? 'block' : 'none';
  var r = document.querySelector('input[name="rideshare"]:checked');
  document.getElementById('rideshare-block').style.display = (r && r.value === 'yes') ? 'block' : 'none';
}
</script>
</body></html>"""


def _controls_ok_html() -> str:
    return """<!doctype html><html><head><meta charset="utf-8"><title>Mock Controls</title></head>
<body>
<h1>Form Controls</h1>
<form method="post" action="/submit">
  <input type="hidden" name="next" value="page-d">
  <input type="hidden" name="variant" value="quote">
  <div><label for="c-text">Legal name</label><input id="c-text" name="c_text" type="text" required></div>
  <div><label for="c-int">Model year</label><input id="c-int" name="c_int" type="number" required></div>
  <div><label for="c-dec">One-way commute distance (km)</label><input id="c-dec" name="c_dec" type="number" step="0.1"></div>
  <div><label for="c-select">Preferred language</label>
    <select id="c-select" name="c_select">
      <option value="english">English</option><option value="french">French</option>
    </select></div>
  <fieldset><legend>Do you carpool to work or school?</legend>
    <input type="radio" id="c-radio-yes" name="carpool" value="yes"><label for="c-radio-yes">Yes</label>
    <input type="radio" id="c-radio-no" name="carpool" value="no" checked><label for="c-radio-no">No</label>
  </fieldset>
  <div><fieldset><legend>Do you have winter tires?</legend>
    <input type="checkbox" id="c-check" name="winter_tires" value="yes"><label for="c-check">Do you have winter tires?</label>
  </fieldset></div>
  <div><label for="c-date">Date of birth</label><input id="c-date" name="c_date" type="date" required></div>
  <div><label for="c-postal">Postal code</label><input id="c-postal" name="c_postal" type="text" required></div>
  <button type="submit" id="continue-controls">Continue</button>
</form>
</body></html>"""


def _controls_skip_html() -> str:
    return """<!doctype html><html><head><meta charset="utf-8"><title>Mock Controls Skip</title></head>
<body>
<h1>Ignored Controls</h1>
<form method="post" action="/submit">
  <input type="hidden" name="next" value="page-d">
  <input type="hidden" name="variant" value="quote">
  <input type="hidden" name="hidden_field" value="should-be-ignored">
  <div><label for="d-disabled">Legal name</label><input id="d-disabled" name="d_disabled" type="text" disabled></div>
  <div><label for="d-readonly">Postal code</label><input id="d-readonly" name="d_readonly" type="text" value="M0A 0A0" readonly></div>
  <div><label for="legal-name">Legal name</label><input id="legal-name" name="legal_name" type="text" required></div>
  <button type="submit" id="continue-skip">Continue</button>
</form>
</body></html>"""


def _controls_aria_html() -> str:
    return """<!doctype html><html><head><meta charset="utf-8"><title>Mock Aria</title></head>
<body>
<h1>Aria Controls</h1>
<form method="post" action="/submit">
  <input type="hidden" name="next" value="page-d">
  <input type="hidden" name="variant" value="quote">
  <input id="aria-name" name="legal_name" type="text" aria-label="Legal name" required>
  <input id="ph-postal" name="postal_code" type="text" placeholder="Postal code" required>
  <button type="submit" id="continue-aria">Continue</button>
</form>
</body></html>"""


def _controls_dup_html() -> str:
    return """<!doctype html><html><head><meta charset="utf-8"><title>Mock Duplicate</title></head>
<body>
<h1>Duplicate Labels</h1>
<form method="post" action="/submit">
  <div><label for="dup-1">Legal name</label><input id="dup-1" name="legal_name_1" type="text"></div>
  <div><label for="dup-2">Legal name</label><input id="dup-2" name="legal_name_2" type="text"></div>
  <button type="submit" id="continue-dup">Continue</button>
</form>
</body></html>"""


def _controls_nolabel_html() -> str:
    return """<!doctype html><html><head><meta charset="utf-8"><title>Mock No Label</title></head>
<body>
<h1>No Label Required</h1>
<form method="post" action="/submit">
  <input id="bare-required" name="bare_required" type="text" required>
  <button type="submit" id="continue-nolabel">Continue</button>
</form>
</body></html>"""


def _controls_optional_unknown_html() -> str:
    return """<!doctype html><html><head><meta charset="utf-8"><title>Mock Optional Unknown</title></head>
<body>
<h1>Optional Unknown</h1>
<form method="post" action="/submit">
  <input type="hidden" name="next" value="page-d">
  <input type="hidden" name="variant" value="quote">
  <div><label for="opt-unknown">Do you have a loyalty number?</label>
    <input id="opt-unknown" name="opt_unknown" type="text"></div>
  <button type="submit" id="continue-opt">Continue</button>
</form>
</body></html>"""


def _select_unsupported_html() -> str:
    return """<!doctype html><html><head><meta charset="utf-8"><title>Mock Select Unsupported</title></head>
<body>
<h1>Unsupported Option</h1>
<form method="post" action="/submit">
  <div><label for="lang-unsupported">Preferred language</label>
    <select id="lang-unsupported" name="lang_unsupported">
      <option value="french">French</option>
    </select></div>
  <button type="submit" id="continue-unsupported">Continue</button>
</form>
</body></html>"""


def _validate_html(error: bool = False) -> str:
    error_div = '<p class="error">Please enter annual kilometres between 1,000 and 100,000</p>' if error else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Mock Validate</title></head>
<body>
<h1>Annual Kilometres</h1>
{error_div}
<form method="post" action="/submit-validate">
  <div><label for="annual-km">Approximate annual distance driven</label>
    <input id="annual-km" name="annual_km" type="number" required></div>
  <button type="submit" id="continue-validate">Continue</button>
</form>
</body></html>"""


def _household_html() -> str:
    return """<!doctype html><html><head><meta charset="utf-8"><title>Mock Household</title></head>
<body>
<h1>Household Driver</h1>
<form method="post" action="/submit">
  <input type="hidden" name="next" value="page-d">
  <input type="hidden" name="variant" value="quote">
  <div><label for="other-driver-name">Other household driver name</label>
    <input id="other-driver-name" name="other_driver_name" type="text" required></div>
  <button type="submit" id="continue-household">Continue</button>
</form>
</body></html>"""


def _access_page(heading: str, body: str, iframe_src: str = "") -> str:
    frame = f'<iframe src="{iframe_src}"></iframe>' if iframe_src else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{heading}</title></head>
<body>
<h1>{heading}</h1>
{frame}
<p>{body}</p>
</body></html>"""


def _checkpoint_page(heading: str, button: str) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{heading}</title></head>
<body>
<h1>{heading}</h1>
<button type="button" id="cp-{button.lower().replace(' ', '-')}">{button}</button>
</body></html>"""


def _quote_variant_html(variant: str) -> str:
    if variant == "monthly":
        body = "<p>Monthly premium: $100</p><p>Quote reference: MOCK-MONTHLY-1</p>"
    elif variant == "both":
        body = ("<p>Annual premium: $1,200</p><p>$100 per month</p>"
                "<p>Quote reference: MOCK-BOTH-2</p>")
    elif variant == "estimate":
        body = "<p>Estimated premium: $900</p>"
    elif variant == "noref":
        body = "<p>Annual premium: $1,100</p>"
    else:  # annual
        body = "<p>Annual premium: $1,200</p><p>Quote reference: MOCK-ANNUAL-3</p>"
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Quote</title></head>
<body>
<h1>Your Quote</h1>
{body}
</body></html>"""


def _slow_html() -> str:
    return """<!doctype html><html><head><meta charset="utf-8"><title>Slow</title></head>
<body><h1>Slow Page</h1></body></html>"""


def _resolve_slow(path: str) -> Optional[str]:
    import time

    if not path.startswith("/slow"):
        return None
    time.sleep(1.5)  # simulate a slow page for timeout testing
    return _slow_html()


_HTML_PAGES: dict[str, str] = {
    "/page-a": _page_a_html(),
    "/page-c": _page_c_html(),
    "/captcha": _captcha_html(),
    "/unknown": _unknown_html(),
    "/callback": _callback_html(),
    "/buy": _buy_html(),
    "/newfield": _newfield_html(),
    "/multi-missing": _multi_missing_html(),
    "/annual-1": _annual_twice_html(1),
    "/annual-2": _annual_twice_html(2),
    "/chain": _chain_html(),
    "/controls-ok": _controls_ok_html(),
    "/controls-skip": _controls_skip_html(),
    "/controls-aria": _controls_aria_html(),
    "/controls-dup": _controls_dup_html(),
    "/controls-nolabel": _controls_nolabel_html(),
    "/controls-optional-unknown": _controls_optional_unknown_html(),
    "/select-unsupported": _select_unsupported_html(),
    "/validate": _validate_html(),
    "/household": _household_html(),
    "/captcha-hcaptcha": _access_page("Security Check", "Verify you are human", "https://js.hcaptcha.com/1/api.js"),
    "/access-denied": _access_page("Access Denied", "Access is denied"),
    "/rate-limit": _access_page("Too Many Requests", "Rate limit exceeded"),
    "/bot": _access_page("Unusual Traffic", "Unusual traffic detected"),
    "/login": _access_page("Sign In", "Sign in to continue"),
    "/declaration": _checkpoint_page("Application Declaration", "I agree to the declaration"),
    "/signature": _checkpoint_page("Signature", "Sign here"),
    "/payment": _checkpoint_page("Payment", "Pay now"),
    "/bind": _checkpoint_page("Policy Binding", "Bind policy"),
    "/consent-attestation": _checkpoint_page("Consent Attestation", "Authorize"),
}


def _page_d_for(path: str) -> Optional[str]:
    if not path.startswith("/page-d"):
        return None
    parsed = urlparse(path)
    variant = parse_qs(parsed.query).get("variant", ["quote"])[0]
    return _page_d_html(variant)


def _page_b_for(path: str) -> Optional[str]:
    if not path.startswith("/page-b"):
        return None
    parsed = urlparse(path)
    params = parse_qs(parsed.query)
    if params.get("label", ["0"])[0] == "1":
        return _page_b_html_label_changed()
    if params.get("order", ["0"])[0] == "1":
        return _page_b_html_reordered()
    if params.get("nomodel", ["0"])[0] == "1":
        return _page_b_html_no_model()
    if params.get("annual", ["0"])[0] == "select":
        return _page_b_html_annual_select()
    if params.get("winter", ["0"])[0] == "required":
        return _page_b_html_winter_required()
    if params.get("change", ["0"])[0] == "1":
        return _page_b_html(annual_km_id="distance-driven")
    return _page_b_html()


def _page_b_html_label_changed() -> str:
    return """<!doctype html><html><head><meta charset="utf-8"><title>Mock B</title></head>
<body>
<h1>Vehicle Information</h1>
<form method="post" action="/submit">
  <input type="hidden" name="next" value="page-c">
  <div><label for="vin">VIN</label><input id="vin" name="vin" type="text" required></div>
  <div><label for="model-year">Model year</label><input id="model-year" name="model_year" type="number" required></div>
  <div><label for="annual-km">Approximately how far do you drive each year?</label>
    <input id="annual-km" name="annual_km" type="number" required></div>
  <button type="submit" id="continue-b">Continue</button>
</form>
</body></html>"""


def _page_b_html_reordered() -> str:
    return """<!doctype html><html><head><meta charset="utf-8"><title>Mock B</title></head>
<body>
<h1>Vehicle Information</h1>
<form method="post" action="/submit">
  <input type="hidden" name="next" value="page-c">
  <div><label for="annual-km">Approximate annual distance driven</label>
    <input id="annual-km" name="annual_km" type="number" required></div>
  <div><label for="vin">VIN</label><input id="vin" name="vin" type="text" required></div>
  <div><label for="model-year">Model year</label><input id="model-year" name="model_year" type="number" required></div>
  <button type="submit" id="continue-b">Continue</button>
</form>
</body></html>"""


def _page_b_html_no_model() -> str:
    return """<!doctype html><html><head><meta charset="utf-8"><title>Mock B</title></head>
<body>
<h1>Vehicle Information</h1>
<form method="post" action="/submit">
  <input type="hidden" name="next" value="page-c">
  <div><label for="vin">VIN</label><input id="vin" name="vin" type="text" required></div>
  <div><label for="annual-km">Approximate annual distance driven</label>
    <input id="annual-km" name="annual_km" type="number" required></div>
  <button type="submit" id="continue-b">Continue</button>
</form>
</body></html>"""


def _page_b_html_annual_select() -> str:
    return """<!doctype html><html><head><meta charset="utf-8"><title>Mock B</title></head>
<body>
<h1>Vehicle Information</h1>
<form method="post" action="/submit">
  <input type="hidden" name="next" value="page-c">
  <div><label for="vin">VIN</label><input id="vin" name="vin" type="text" required></div>
  <div><label for="model-year">Model year</label><input id="model-year" name="model_year" type="number" required></div>
  <div><label for="annual-km">Approximate annual distance driven</label>
    <select id="annual-km" name="annual_km">
      <option value="5000">5,000 - 10,000</option>
      <option value="12000">10,000 - 15,000</option>
      <option value="20000">15,000 - 25,000</option>
    </select></div>
  <button type="submit" id="continue-b">Continue</button>
</form>
</body></html>"""


def _page_b_html_winter_required() -> str:
    return """<!doctype html><html><head><meta charset="utf-8"><title>Mock B</title></head>
<body>
<h1>Vehicle Information</h1>
<form method="post" action="/submit">
  <input type="hidden" name="next" value="page-c">
  <div><label for="vin">VIN</label><input id="vin" name="vin" type="text" required></div>
  <div><label for="model-year">Model year</label><input id="model-year" name="model_year" type="number" required></div>
  <div><fieldset><legend>Do you have winter tires?</legend>
    <input type="checkbox" id="winter-tires" name="winter_tires" value="yes" required>
    <label for="winter-tires">Do you have winter tires?</label></fieldset></div>
  <button type="submit" id="continue-b">Continue</button>
</form>
</body></html>"""


def _resolve_quote(path: str) -> Optional[str]:
    if not path.startswith("/quote"):
        return None
    variant = parse_qs(urlparse(path).query).get("variant", ["annual"])[0]
    return _quote_variant_html(variant)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:  # silence noisy logging
        return

    def do_GET(self) -> None:  # noqa: N802
        body = _resolve_page(self.path)  # keep query string (variant/change)
        if body is None:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"not found")
            return
        self._send_html(body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        data = parse_qs(raw)
        # Validation-error simulation: submitting /validate returns the same
        # page WITH an error message (the browser must detect it and pause).
        if urlparse(self.path).path == "/submit-validate":
            self._send_html(_validate_html(error=True))
            return
        next_page = (data.get("next") or ["start"])[0]
        variant = (data.get("variant") or [""])[0]
        location = f"/{next_page}"
        if variant:
            location += f"?variant={variant}"
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def _send_html(self, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def _resolve_page(path: str) -> Optional[str]:
    clean = urlparse(path).path
    if clean == "/start":
        return '<!doctype html><html><body><h1>Mock Quote Start</h1><a href="/page-a">Begin</a></body></html>'
    if clean in _HTML_PAGES:
        return _HTML_PAGES[clean]
    dynamic = _page_d_for(path) or _page_b_for(path) or _resolve_quote(path) or _resolve_slow(path)
    return dynamic


class MockQuoteSite:
    """Local, internet-free multi-page quote-site fixture."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self._host = host
        self._port = 0
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "MockQuoteSite":
        self._server = ThreadingHTTPServer((self._host, 0), _Handler)
        self._port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    @property
    def base_url(self) -> str:
        return f"http://{self._host}:{self._port}"

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


# ---------------------------------------------------------------------------
# Data-driven route config builders for the mock route + scenarios
# ---------------------------------------------------------------------------

def _text_binding(external_id: str, canonical: str, label: str, *,
                  fill: FillStrategy = FillStrategy.TEXT, transform: TransformKind = TransformKind.NONE,
                  required: bool = True) -> BrowserFieldBinding:
    return BrowserFieldBinding(
        external_field_id=external_id,
        match_patterns=[MatchPattern(strategy=MatchStrategy.LABEL_TEXT, value=label)],
        canonical_path=canonical,
        control_type="input",
        fill_strategy=fill,
        transform=transform,
        required=required,
        sensitivity=FieldSensitivity.SENSITIVE if "vin" in canonical or "licence" in canonical else FieldSensitivity.PERSONAL,
    )


def _base_bindings() -> list[BrowserFieldBinding]:
    return [
        _text_binding("legal-name", "applicant.identity.legal_name", "Legal name"),
        BrowserFieldBinding(
            external_field_id="preferred-language",
            match_patterns=[MatchPattern(strategy=MatchStrategy.LABEL_TEXT, value="Preferred language")],
            canonical_path="applicant.identity.preferred_language",
            control_type="select", fill_strategy=FillStrategy.SELECT,
            transform=TransformKind.ENUM_TO_LABEL, required=False,
            option_map={"english": "English", "french": "French"},
        ),
        _text_binding("postal-code", "applicant.address.postal_code", "Postal code"),
        _text_binding("date-of-birth", "applicant.identity.date_of_birth", "Date of birth",
                      fill=FillStrategy.DATE, transform=TransformKind.ISO_DATE_TO_DEST),
        _text_binding("street", "applicant.address.street", "Street address"),
        _text_binding("vin", "product_data.vehicles[0].identity.vin", "VIN"),
        _text_binding("model-year", "product_data.vehicles[0].identity.model_year", "Model year",
                      fill=FillStrategy.INTEGER, transform=TransformKind.INTEGER_TO_STRING),
        BrowserFieldBinding(
            external_field_id="annual-km",
            match_patterns=[MatchPattern(strategy=MatchStrategy.LABEL_CONTAINS, value="annual distance")],
            canonical_path="product_data.vehicles[0].use.annual_kilometres",
            control_type="input", fill_strategy=FillStrategy.INTEGER,
            transform=TransformKind.INTEGER_TO_STRING, required=True,
        ),
        BrowserFieldBinding(
            external_field_id="winter-tires",
            match_patterns=[MatchPattern(strategy=MatchStrategy.LABEL_TEXT, value="Do you have winter tires?")],
            canonical_path="product_data.vehicles[0].risk.winter_tires",
            control_type="checkbox", fill_strategy=FillStrategy.CHECKBOX, transform=TransformKind.NONE, required=False,
        ),
        BrowserFieldBinding(
            external_field_id="carpool",
            match_patterns=[MatchPattern(strategy=MatchStrategy.LABEL_TEXT, value="Do you carpool to work or school?")],
            canonical_path="product_data.vehicles[0].use.carpool",
            control_type="radio", fill_strategy=FillStrategy.RADIO, transform=TransformKind.BOOL_TO_YES_NO, required=False,
            option_map={"True": "Yes", "False": "No"},
        ),
        BrowserFieldBinding(
            external_field_id="one-way-commute",
            match_patterns=[MatchPattern(strategy=MatchStrategy.LABEL_TEXT, value="One-way commute distance (km)")],
            canonical_path="product_data.vehicles[0].use.one_way_commute_distance_km",
            control_type="input", fill_strategy=FillStrategy.INTEGER,
            transform=TransformKind.INTEGER_TO_STRING, required=False,
        ),
        # --- Prompt-2 dynamic/conditional/household bindings -------------
        BrowserFieldBinding(
            external_field_id="commuting",
            match_patterns=[MatchPattern(strategy=MatchStrategy.LABEL_TEXT, value="Do you use the vehicle for commuting?")],
            canonical_path="product_data.vehicles[0].use.commuting",
            control_type="radio", fill_strategy=FillStrategy.RADIO,
            transform=TransformKind.BOOL_TO_YES_NO, required=False,
            option_map={"True": "Yes", "False": "No"},
        ),
        BrowserFieldBinding(
            external_field_id="rideshare",
            match_patterns=[MatchPattern(strategy=MatchStrategy.LABEL_TEXT, value="Do you drive for rideshare?")],
            canonical_path="product_data.vehicles[0].special_use.rideshare",
            control_type="radio", fill_strategy=FillStrategy.RADIO,
            transform=TransformKind.BOOL_TO_YES_NO, required=False,
            option_map={"True": "Yes", "False": "No"},
        ),
        BrowserFieldBinding(
            external_field_id="rideshare-hours",
            match_patterns=[MatchPattern(strategy=MatchStrategy.LABEL_TEXT, value="Average rideshare hours per week")],
            canonical_path="product_data.vehicles[0].use.rideshare_hours_per_week",
            control_type="input", fill_strategy=FillStrategy.INTEGER,
            transform=TransformKind.INTEGER_TO_STRING, required=False,
        ),
        BrowserFieldBinding(
            external_field_id="other-driver-name",
            match_patterns=[MatchPattern(strategy=MatchStrategy.LABEL_TEXT, value="Other household driver name")],
            canonical_path="product_data.drivers[0].other_drivers[0].name",
            control_type="input", fill_strategy=FillStrategy.TEXT, required=True,
            sensitivity=FieldSensitivity.SENSITIVE,
        ),
        BrowserFieldBinding(
            external_field_id="years-address",
            match_patterns=[MatchPattern(strategy=MatchStrategy.LABEL_TEXT, value="Years at current address")],
            canonical_path="applicant.address.years_at_current_address",
            control_type="input", fill_strategy=FillStrategy.INTEGER,
            transform=TransformKind.INTEGER_TO_STRING, required=False,
        ),
        BrowserFieldBinding(
            external_field_id="tpl-limit",
            match_patterns=[MatchPattern(strategy=MatchStrategy.LABEL_TEXT, value="Liability limit")],
            canonical_path="product_data.coverage.third_party_liability.selected_limit",
            control_type="input", fill_strategy=FillStrategy.INTEGER,
            transform=TransformKind.INTEGER_TO_STRING, required=False,
        ),
        BrowserFieldBinding(
            external_field_id="ph-postal",
            match_patterns=[MatchPattern(strategy=MatchStrategy.PLACEHOLDER, value="Postal code")],
            canonical_path="applicant.address.postal_code",
            control_type="input", fill_strategy=FillStrategy.TEXT, required=True,
        ),
    ]


def build_mock_route_config(registry_id: str = MOCK_REGISTRY_ID,
                            start_url: Optional[str] = None) -> BrowserRouteConfig:
    """Data-driven route config for the mock quote site (all scenarios)."""
    return BrowserRouteConfig(
        registry_id=registry_id,
        config_version=1,
        start_url=start_url,
        allowed_hosts=["127.0.0.1", "localhost"],
        page_signatures=[
            PageSignatureSpec(signature_id="applicant", url_pattern=r"/page-a",
                              heading_patterns=["Applicant Information"], field_ids=["legal-name"]),
            PageSignatureSpec(signature_id="vehicle", url_pattern=r"/page-b",
                              heading_patterns=["Vehicle Information"], field_ids=["vin"]),
            PageSignatureSpec(signature_id="commute", url_pattern=r"/page-c",
                              heading_patterns=["Commuting Details"], field_ids=["carpool"]),
            PageSignatureSpec(signature_id="quote", url_pattern=r"/page-d",
                              heading_patterns=["Your Quote"]),
            PageSignatureSpec(signature_id="checkpoint", url_pattern=r"/page-d\?variant=checkpoint",
                              heading_patterns=["Identity Verification"], field_ids=["verify-identity"]),
            PageSignatureSpec(signature_id="captcha", url_pattern=r"/captcha",
                              heading_patterns=["Security Check"]),
            PageSignatureSpec(signature_id="callback", url_pattern=r"/callback",
                              heading_patterns=["Complete by Phone"]),
            PageSignatureSpec(signature_id="unknown", url_pattern=r"/unknown",
                              heading_patterns=["Additional Information"]),
        ],
        field_bindings=_base_bindings(),
        action_bindings=[
            ActionBinding(action_type="continue", safety=BrowserActionSafety.SAFE_NAVIGATION,
                          label_patterns=["Continue"]),
            ActionBinding(action_type="submit_quote", safety=BrowserActionSafety.DATA_SUBMISSION,
                          label_patterns=["Continue"]),
        ],
        checkpoint_bindings=[
            CheckpointBinding(checkpoint_type="identity_lookup",
                              label_patterns=["Verify identity"]),
            CheckpointBinding(checkpoint_type="purchase", label_patterns=["Buy Now"]),
        ],
        quote_detection=QuoteDetectionConfig(
            heading_patterns=["Your Quote"],
            premium_label_patterns=["annual premium"],
            price_pattern=r"\$\s?([\d,]+(?:\.\d{1,2})?)",
            currency="CAD",
            reference_patterns=[r"(?:quote|reference)\s*[:#]?\s*([A-Z0-9-]{6,})"],
            coverage_label_patterns=["third party liability", "accident benefits", "comprehensive"],
            discount_label_patterns=["discount"],
            validity_label_patterns=["valid for"],
            annual_label_patterns=["annual"],
            firm_quote_patterns=["valid for 30 days"],
        ),
        callback_detection=CallbackDetectionConfig(
            patterns=["call us", "complete your quote"]
        ),
        access_control_detection=AccessControlDetectionConfig(
            patterns=["verify you are human"],
            iframe_src_patterns=["recaptcha", "hcaptcha"],
        ),
        automation_notes="Mock local site - synthetic data only",
    )


def mock_scenario_url(site: MockQuoteSite, scenario: str) -> str:
    """Return the direct start URL for a scenario page."""
    paths = {
        "applicant": "/page-a",
        "vehicle": "/page-b",
        "commute": "/page-c",
        "quote": "/page-d?variant=quote",
        "checkpoint": "/page-d?variant=checkpoint",
        "captcha": "/captcha",
        "callback": "/callback",
        "unknown": "/unknown",
        "change": "/page-b?change=1",
        "buy": "/buy",
        "newfield": "/newfield",
        "multi-missing": "/multi-missing",
        "annual-twice": "/annual-1",
        "chain": "/chain",
        "controls-ok": "/controls-ok",
        "controls-skip": "/controls-skip",
        "controls-aria": "/controls-aria",
        "controls-dup": "/controls-dup",
        "controls-nolabel": "/controls-nolabel",
        "controls-optional-unknown": "/controls-optional-unknown",
        "select-unsupported": "/select-unsupported",
        "validate": "/validate",
        "household": "/household",
        "captcha-hcaptcha": "/captcha-hcaptcha",
        "access-denied": "/access-denied",
        "rate-limit": "/rate-limit",
        "bot": "/bot",
        "login": "/login",
        "declaration": "/declaration",
        "signature": "/signature",
        "payment": "/payment",
        "bind": "/bind",
        "consent-attestation": "/consent-attestation",
        "label-changed": "/page-b?label=1",
        "order": "/page-b?order=1",
        "nomodel": "/page-b?nomodel=1",
        "annual-select": "/page-b?annual=select",
        "winter-required": "/page-b?winter=required",
        "quote-annual": "/quote?variant=annual",
        "quote-monthly": "/quote?variant=monthly",
        "quote-both": "/quote?variant=both",
        "quote-estimate": "/quote?variant=estimate",
        "quote-noref": "/quote?variant=noref",
        "slow": "/slow",
    }
    return site.url(paths[scenario])


def build_scenario_config(registry_id: str, site: MockQuoteSite, scenario: str) -> BrowserRouteConfig:
    """Route config for one scenario page (start_url = the scenario page)."""
    return build_mock_route_config(registry_id=registry_id, start_url=mock_scenario_url(site, scenario))
