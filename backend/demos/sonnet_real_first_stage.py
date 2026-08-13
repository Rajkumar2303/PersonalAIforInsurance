"""Sonnet REAL first-stage Playwright diagnostic (headed, frame-aware).

Goal: prove headed Playwright can move from the real Sonnet PROVINCE screen to
the VEHICLE/DRIVER-COUNT screen. NO quote attempt and NO sensitive data entered.

Key fix: the quote form may live inside an iframe / nested browsing context, and
the "Loading..." element may persist after the form is usable. Readiness is
therefore defined as "an actionable Province/Ontario control EXISTS in some
frame" - NOT the disappearance of "Loading...". All frames are enumerated and
searched; selection/verification/continue happen in the SAME frame that owns the
real control.

Rules:
- Headed, slow_mo=500, stable locators only (role+name, label, test-id).
- Fails from unrelated PayPal / Five9 / FontAwesome resources are ignored and
  are never treated as access control.
- Any genuine access restriction (CAPTCHA/Cloudflare/other) is reported and
  stopped - never bypassed.
- Never goes beyond the vehicle/driver-count screen.
- Keeps Chromium open until Enter for proof.

Run ONLY manually:
    .\\.venv\\Scripts\\python.exe demos\\sonnet_real_first_stage.py
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from typing import Any, Optional
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

PROVINCE_URL = "https://secure.sonnet.ca/#/quoting/auto/province?lang=en"
COUNTS_URL_MARKER = "num_vehicles_drivers"

# Third-party resource failures that are unrelated to the quote form and must
# never be reported as access control or render failures.
_IGNORED_HOSTS = ("paypal", "five9", "fontawesome", "font-awesome", "gstatic",
                  "googleapis", "typekit", "adobedc", "cloudflareinsights")


def _report(*parts: object) -> None:
    print(" ".join(str(p) for p in parts))


def _frame_summary(frame: Any, idx: int) -> dict:
    try:
        parts = urlsplit(frame.url or "")
        host = parts.hostname or ""
        pathname = parts.path or ""
    except Exception:
        host, pathname = "", ""
    return {"idx": idx, "host": host, "pathname": pathname}


def _frame_has_province_control(frame: Any) -> bool:
    """True when an actionable Province/Ontario interactive control exists."""
    try:
        if frame.get_by_label("Province", exact=False).first.count():
            return True
        if frame.get_by_role("combobox", name=re.compile("province", re.I)).first.count():
            return True
        if frame.get_by_role("option", name=re.compile(r"^ontario$", re.I)).first.count():
            return True
        if frame.get_by_role("radio", name=re.compile(r"^ontario$", re.I)).first.count():
            return True
        if frame.get_by_role("button", name=re.compile(r"^ontario$", re.I)).first.count():
            return True
    except Exception:
        return False
    return False


def _print_interactive_summary(page: Any) -> None:
    """Safe per-frame summary: interactive role + accessible name only."""
    try:
        frames = page.frames
    except Exception:
        return
    for idx, frame in enumerate(frames):
        s = _frame_summary(frame, idx)
        roles: list[str] = []
        for role in ("button", "heading", "combobox", "link", "radio", "option"):
            loc = frame.get_by_role(role)
            try:
                count = loc.count()
            except Exception:
                continue
            for i in range(min(count, 12)):
                try:
                    name = (loc.nth(i).get_attribute("aria-label")
                            or loc.nth(i).inner_text(timeout=1200) or "").strip()
                except Exception:
                    name = ""
                if name and f"{role}={name[:40]}" not in roles:
                    roles.append(f"{role}={name[:40]}")
        _report(f"frame idx={s['idx']} host={s['host']} path={s['pathname']} "
                f"interactive=" + (" | ".join(roles) or "(none)"))


def _wait_for_province_frame(page: Any, timeout_s: int = 60) -> Optional[Any]:
    """Poll until an actionable Province/Ontario control exists in some frame.

    Readiness is control-presence, NOT "Loading..." disappearance. Failed
    PayPal/Five9/FontAwesome resources are ignored."""
    deadline = time.time() + timeout_s
    failed: list[tuple[str, str]] = []
    console_err: set[str] = set()

    def _host(url: str) -> str:
        try:
            return urlsplit(url or "").hostname or "?"
        except Exception:
            return "?"

    def _ignored(host: str) -> bool:
        return any(k in host for k in _IGNORED_HOSTS)

    page.on("requestfailed", lambda req: failed.append((_host(req.url), "failed")) if not _ignored(_host(req.url)) else None)
    page.on("response", lambda resp: failed.append((_host(resp.url), str(resp.status))) if resp.status >= 400 and not _ignored(_host(resp.url)) else None)
    page.on("console", lambda msg: console_err.add(msg.type) if msg.type == "error" else None)

    _report("page_state=loading")
    # Enumerate frames once after navigation (safe, no text/values).
    try:
        frames = page.frames
    except Exception:
        frames = []
    for idx, frame in enumerate(frames):
        s = _frame_summary(frame, idx)
        _report(f"frame idx={s['idx']} host={s['host']} path={s['pathname']} "
                f"has_province_control={_frame_has_province_control(frame)}")

    while time.time() < deadline:
        try:
            frames = page.frames
        except Exception:
            frames = []
        for frame in frames:
            if _frame_has_province_control(frame):
                _report("page_state=ready")
                return frame
        time.sleep(1)

    _report("page_state=loading_timeout")
    for host, status in failed[-10:]:
        _report(f"failed_request host={host} status={status}")
    for category in sorted(console_err):
        _report(f"console_error category={category}")
    return None


def _access_control_type(page: Any) -> str | None:
    """Minimal inline barrier check (never bypasses)."""
    try:
        for f in page.frames:
            src = (f.url or "").lower()
            if "challenges.cloudflare.com" in src:
                return "cloudflare"
            if any(k in src for k in ("recaptcha", "hcaptcha", "captcha")):
                return "captcha"
    except Exception:
        pass
    try:
        text = (page.locator("body").inner_text(timeout=3000) or "").lower()
    except Exception:
        return None
    for marker, kind in (("verify you are human", "captcha"),
                         ("unusual traffic", "cloudflare"),
                         ("access denied", "access_denied"),
                         ("rate limit", "rate_limit"),
                         ("automated access is not permitted", "bot_detection")):
        if marker in text:
            return kind
    return None


def _select_province_ontario(frame: Any) -> bool:
    select_candidates = [
        frame.get_by_label("Province", exact=False).first,
        frame.locator("select#province"),
        frame.locator("select[name='province']"),
        frame.locator("select").first,
    ]
    for idx, loc in enumerate(select_candidates):
        try:
            if loc.count() == 0:
                continue
            if loc.evaluate("el => el.tagName.toLowerCase()") != "select":
                continue
            loc.select_option(label="Ontario")
            _report(f"locator=select strategy={idx}")
            return True
        except Exception:
            continue
    combos = [
        frame.get_by_role("combobox").first,
        frame.get_by_label("Province", exact=False).first,
        frame.locator("[data-testid='province']"),
    ]
    for idx, combo in enumerate(combos):
        try:
            if combo.count() == 0:
                continue
            combo.click(timeout=5000)
            option = frame.get_by_role("option", name=re.compile(r"^ontario$", re.I)).first
            if option.count() == 0:
                option = frame.get_by_role("radio", name=re.compile(r"^ontario$", re.I)).first
            if option.count() == 0:
                option = frame.get_by_text("Ontario", exact=True).first
            option.click(timeout=5000)
            _report(f"locator=combobox strategy={idx}")
            return True
        except Exception:
            continue
    return False


def _province_selected(frame: Any) -> bool:
    try:
        value = frame.locator("select").evaluate(
            "el => ({v: el.value, t: Array.from(el.options).map(o => o.text)})"
        )
        if value and ((value["v"] or "").lower() in ("on", "ontario")
                      or any("ontario" in str(t).lower() for t in value["t"])):
            return True
    except Exception:
        pass
    for role in ("button", "combobox", "radio"):
        loc = frame.get_by_role(role)
        try:
            count = loc.count()
        except Exception:
            continue
        for i in range(count):
            try:
                if "ontario" in loc.nth(i).inner_text(timeout=1200).lower():
                    return True
            except Exception:
                continue
    return False


def _click_continue(frame: Any) -> bool:
    exact = frame.get_by_role("button", name="I understand and continue", exact=False).first
    try:
        if exact.count():
            exact.click(timeout=8000)
            return True
    except Exception:
        pass
    text = frame.get_by_text("I understand and continue", exact=False).first
    try:
        if text.count():
            text.click(timeout=8000)
            return True
    except Exception:
        pass
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Sonnet real first-stage diagnostic (headed)")
    parser.add_argument("--slow-ms", type=int, default=500)
    parser.add_argument("--headless", action="store_true", help="Headless (default headed)")
    parser.add_argument("--url", default=PROVINCE_URL)
    args = parser.parse_args()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless, slow_mo=args.slow_ms)
        page = browser.new_page()
        try:
            _report(f"navigate url={args.url}")
            page.goto(args.url, wait_until="domcontentloaded", timeout=45000)

            frame = _wait_for_province_frame(page)
            if frame is None:
                _report("Stopping: no actionable Province/Ontario control found in any frame within 60s.")
                _print_interactive_summary(page)
                input("Press Enter to close Chromium (inspect the page).")
                return 6

            barrier = _access_control_type(page)
            if barrier is not None:
                _report(f"access_control={barrier} detected=true")
                _report("Stopping: a genuine access restriction is present; it will NOT be bypassed.")
                _print_interactive_summary(page)
                input("Press Enter to close Chromium.")
                return 2

            if not _select_province_ontario(frame):
                _report("action=select field=province status=failed")
                _print_interactive_summary(page)
                input("Press Enter to close Chromium.")
                return 3
            if not _province_selected(frame):
                _report("action=select field=province status=failed (Ontario not confirmed selected)")
                input("Press Enter to close Chromium.")
                return 4
            _report("action=select field=province status=success")

            if not _click_continue(frame):
                _report("action=click control=province_continue status=failed")
                input("Press Enter to close Chromium.")
                return 5
            _report("action=click control=province_continue status=success")

            # Wait for the vehicle/driver-count screen (never go past it).
            try:
                page.wait_for_url(re.compile(COUNTS_URL_MARKER), timeout=20000)
            except Exception:
                pass
            url = page.url
            if COUNTS_URL_MARKER in url:
                _report("page=vehicle_driver_counts status=reached")
            elif "vehicle-information" in url or "driver" in url:
                _report("page=vehicle_driver_counts status=skipped_past (do NOT proceed)")
            else:
                _report("page=vehicle_driver_counts status=not_confirmed")
        finally:
            _report("Keep Chromium open: press Enter to close (record proof first).")
            try:
                input("Press Enter to close Chromium.")
            except (EOFError, KeyboardInterrupt):
                pass
            browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
