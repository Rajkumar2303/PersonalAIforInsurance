"""Issue #8.5 Smoke #1c - access-control & validation barrier detector hardening.

Proves generic instructional/header/footer copy is NOT a barrier:

Validation:
A. "Please select your province" normal initial form  -> validation = false
B. aria-invalid=true + visible error                  -> true
C. visible role=alert validation message              -> true
D. normal required-field instructional copy           -> false

Access control:
- normal site with a "Log in" nav link                -> false
- privacy/security footer copy                        -> false
- real synthetic CAPTCHA page                         -> true
- real synthetic "Access denied" page                 -> true
- blocking login/auth page ("Sign in to continue")    -> true

All against the local mock site (hermetic). Uses the MERGED config so the
generic default patterns are exercised.
"""

from __future__ import annotations

from app.browser.adapters import GenericQuoteSiteAdapter
from app.browser.detect import PageDetector
from app.browser.manager import BrowserManager
from app.demo.mock_quote_site import build_mock_route_config


async def _barriers(site, path: str) -> tuple[bool, bool]:
    cfg = GenericQuoteSiteAdapter().merged_config(build_mock_route_config(start_url=site.url(path)))
    browser = BrowserManager(headless=True)
    await browser.start()
    try:
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(site.url(path), wait_until="domcontentloaded")
        await page.wait_for_timeout(150)
        detector = PageDetector()
        access = await detector.access_control_detected(page, cfg)
        validation = await detector.validation_error_detected(page, cfg)
        return access, validation
    finally:
        await browser.stop()


async def _barriers_full(site, path: str) -> tuple[bool, bool, bool]:
    """Return (access, validation, bot_protection) for a mock path (merged config)."""
    cfg = GenericQuoteSiteAdapter().merged_config(build_mock_route_config(start_url=site.url(path)))
    browser = BrowserManager(headless=True)
    await browser.start()
    try:
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(site.url(path), wait_until="domcontentloaded")
        await page.wait_for_timeout(150)
        detector = PageDetector()
        access = await detector.access_control_detected(page, cfg)
        validation = await detector.validation_error_detected(page, cfg)
        bot = await detector.bot_protection_detected(page, cfg)
        return access, validation, bot
    finally:
        await browser.stop()


# --- validation ---------------------------------------------------------

async def test_instructional_select_copy_is_not_validation_error(mock_site) -> None:
    access, validation = await _barriers(mock_site, "/form-hint")
    assert validation is False  # "Please select your province" is an instruction
    assert access is False  # "Log in" header + privacy/security footer are not barriers


async def test_normal_required_field_copy_is_not_validation_error(mock_site) -> None:
    _access, validation = await _barriers(mock_site, "/form-hint")
    assert validation is False  # "required" + instructional copy only


async def test_aria_invalid_is_validation_error(mock_site) -> None:
    _access, validation = await _barriers(mock_site, "/aria-invalid")
    assert validation is True


async def test_role_alert_is_validation_error(mock_site) -> None:
    _access, validation = await _barriers(mock_site, "/role-alert")
    assert validation is True


# --- access control -----------------------------------------------------

async def test_login_nav_link_is_not_access_barrier(mock_site) -> None:
    access, _validation = await _barriers(mock_site, "/form-hint")
    assert access is False  # a normal "Log in" header link


async def test_privacy_security_footer_is_not_access_barrier(mock_site) -> None:
    access, _validation = await _barriers(mock_site, "/form-hint")
    assert access is False  # privacy/security/protected footer copy


async def test_captcha_page_is_access_barrier(mock_site) -> None:
    access, _validation = await _barriers(mock_site, "/captcha")
    assert access is True


async def test_access_denied_page_is_access_barrier(mock_site) -> None:
    access, _validation = await _barriers(mock_site, "/access-denied")
    assert access is True


async def test_login_wall_is_access_barrier(mock_site) -> None:
    access, _validation = await _barriers(mock_site, "/login")
    assert access is True


# --- access control: passive presence vs blocking barrier (#8.5 Smoke #1d) ---

async def test_recaptcha_badge_is_not_blocking(mock_site) -> None:
    access, _validation, bot = await _barriers_full(mock_site, "/recaptcha-badge")
    assert access is False  # visible footer badge alone is NOT a blocker
    assert bot is True  # bot-protection PRESENCE is still reported as metadata


async def test_hidden_recaptcha_anchor_iframe_is_not_blocking(mock_site) -> None:
    access, _validation, bot = await _barriers_full(mock_site, "/recaptcha-hidden-iframe")
    assert access is False  # hidden enterprise token iframe is NOT a blocker
    assert bot is True


async def test_visible_recaptcha_challenge_is_blocking(mock_site) -> None:
    access, _validation, _bot = await _barriers_full(mock_site, "/recaptcha-challenge")
    assert access is True  # a visible challenge widget IS a blocker


async def test_visible_hcaptcha_challenge_is_blocking(mock_site) -> None:
    access, _validation, _bot = await _barriers_full(mock_site, "/hcaptcha-challenge")
    assert access is True


async def test_access_denied_page_is_blocking(mock_site) -> None:
    access, _validation, _bot = await _barriers_full(mock_site, "/access-denied")
    assert access is True


async def test_blocking_signin_wall_is_blocking(mock_site) -> None:
    access, _validation, _bot = await _barriers_full(mock_site, "/login")
    assert access is True


# --- validation: pristine/instructional state vs real error (#8.5 Smoke #1d) ---

async def test_pristine_required_angular_form_is_not_validation_error(mock_site) -> None:
    _access, validation, _bot = await _barriers_full(mock_site, "/angular-pristine")
    assert validation is False  # ng-invalid on a pristine form is initial state


async def test_untouched_hidden_aria_invalid_is_not_validation_error(mock_site) -> None:
    _access, validation, _bot = await _barriers_full(mock_site, "/angular-pristine")
    assert validation is False  # the aria-invalid input is hidden/untouched


async def test_touched_visible_invalid_field_is_validation_error(mock_site) -> None:
    _access, validation, _bot = await _barriers_full(mock_site, "/touched-invalid")
    assert validation is True


async def test_submitted_form_visible_message_is_validation_error(mock_site) -> None:
    _access, validation, _bot = await _barriers_full(mock_site, "/submitted-message")
    assert validation is True


async def test_role_alert_visible_error_is_validation_error(mock_site) -> None:
    _access, validation, _bot = await _barriers_full(mock_site, "/role-alert")
    assert validation is True
