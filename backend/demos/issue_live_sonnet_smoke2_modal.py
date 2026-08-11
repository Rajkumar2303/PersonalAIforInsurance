"""Sonnet LIVE pilot - PHASE 2: Option A interaction (fresh-session modal path).

Authorized interaction sequence (Ontario -> Confirm) using ONLY observed
selectors from Phase 1:

    #provinceSelectorButton                      (modal province trigger, role=listbox)
    #provinceSelectorItems li#provinceSelectorOption9   (Ontario, index 9)
    #province-selector-submit-btn                 (Confirm)

The modal is the canonical fresh-session path - we do NOT close or bypass it.

The ONLY value used is province = Ontario (controlled, non-sensitive). No
applicant PII, no intake profile. No field/action bindings are added to the
Sonnet route config; this stays a controlled dev pilot.

Flow: open -> readiness -> verify host/signature -> verify modal visible ->
click #provinceSelectorButton -> verify Ontario visible -> select Ontario ->
verify selected state -> CAPTCHA gate -> click Confirm -> wait for resulting
state -> validate host -> record safe URL/hash + heading + controls + detectors
-> STOP (nothing filled on the resulting page, no further navigation).

CAPTCHA escalation: after EACH interaction we re-run blocking barrier
detection. bot_protection_present=true (passive reCAPTCHA Enterprise) is
EXPECTED and is NOT blocked. If a VISIBLE challenge appears we STOP IMMEDIATELY
(access_control_detected=true, outcome=blocked) - never solve/retry/reload/
circumvent.

Run (from backend/, with backend on the path):

    $env:PYTHONPATH='.'
    .\\.venv\\Scripts\\python.exe demos\\issue_live_sonnet_smoke2_modal.py --slow-ms 1000
"""

from __future__ import annotations

import argparse
import asyncio
import json
from urllib.parse import urlsplit

from app.browser.adapters import GenericQuoteSiteAdapter
from app.browser.config import BrowserRouteConfigLoader
from app.browser.detect import PageDetector
from app.browser.inspect import PageInspector
from app.browser.manager import BrowserManager
from app.browser.session import live_privacy_context_kwargs
from issue_live_sonnet_inspect import (
    REGISTRY_ID,
    _host_allowed,
    _safe_url,
    _sanitize_diagnostic,
    _wait_for_ready,
)

READINESS_SELECTORS = ["#province-selector-submit-btn", "#province-submit-btn"]
MODAL_TRIGGER = "#provinceSelectorButton"
ONTARIO_SELECTOR = "#provinceSelectorItems li#provinceSelectorOption9"
CONFIRM_SELECTOR = "#province-selector-submit-btn"


def _hash_route(url: str) -> str:
    return (urlsplit(url).fragment or "")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Sonnet LIVE pilot - Smoke #2 Option A (modal path)")
    parser.add_argument("--slow-ms", type=int, default=1000, help="Playwright per-action delay (ms)")
    parser.add_argument("--settle-ms", type=int, default=5000, help="Initial SPA settle (ms)")
    parser.add_argument("--hold-seconds", type=int, default=0, help="Keep browser open N seconds after reporting")
    parser.add_argument("--headless", action="store_true", help="Run headless (default: headful)")
    parser.add_argument("--max-wait-ms", type=int, default=15000, help="Readiness wait budget (ms)")
    parser.add_argument("--poll-ms", type=int, default=300, help="Readiness poll interval (ms)")
    args = parser.parse_args()

    loader = BrowserRouteConfigLoader()
    cfg = GenericQuoteSiteAdapter().merged_config(loader.load(REGISTRY_ID))
    start_url = cfg.start_url or ""
    if not start_url:
        print("[smoke2-modal] ERROR: no start_url configured")
        return 2

    print(f"[smoke2-modal] route={REGISTRY_ID} start_url={_safe_url(start_url)} allowed_hosts={cfg.allowed_hosts}")
    print(f"[smoke2-modal] headful={not args.headless} slow_ms={args.slow_ms} settle_ms={args.settle_ms}")

    browser = BrowserManager(headless=args.headless, slow_mo=args.slow_ms)
    page = None  # type: ignore[assignment]
    try:
        await browser.start()
    except Exception as exc:
        print(f"[smoke2-modal] browser start failed: {type(exc).__name__}")
        return 2

    try:
        context = await browser.new_context(**live_privacy_context_kwargs())
        page = await context.new_page()

        page_errors: list[str] = []
        console_msgs: list[str] = []
        page.on("pageerror", lambda e: page_errors.append(_sanitize_diagnostic(str(e))))
        page.on("console", lambda m: console_msgs.append(_sanitize_diagnostic(m.text)) if m.type in ("error", "warning") else None)

        inspector = PageInspector()
        detector = PageDetector()

        async def stop_if_blocked(stage: str) -> bool:
            if await detector.access_control_detected(page, cfg):
                print(f"[smoke2-modal] {stage}: VISIBLE CAPTCHA/challenge - STOP IMMEDIATELY "
                      f"(access_control_detected=true, outcome=blocked).")
                return True
            return False

        async def report(tag: str) -> object:
            obs = await inspector.inspect(page, 0)
            sig = await detector.page_signature(page, obs, cfg)
            print(f"[smoke2-modal] {tag} host_allowed={_host_allowed(page.url, cfg.allowed_hosts)} "
                  f"url={_safe_url(page.url)}")
            print(f"[smoke2-modal] {tag} signature={sig.signature_id if sig else None} heading={obs.heading!r}")
            return obs

        # 1. open canonical page
        print("[smoke2-modal] (1) opening canonical SPA ...")
        await page.goto(start_url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(args.settle_ms)

        # 2. wait for interactive readiness (bounded)
        matched, waited = await _wait_for_ready(page, READINESS_SELECTORS, args.max_wait_ms, args.poll_ms)
        print(f"[smoke2-modal] (2) interactive_content_ready={matched is not None} matched={matched} waited_ms={waited}")
        if matched is None:
            print("[smoke2-modal] hydration timeout - STOP, no interaction.")
            return 0

        # 3. verify allowed host
        if not _host_allowed(page.url, cfg.allowed_hosts):
            print("[smoke2-modal] (3) host NOT allowed - STOP.")
            return 3

        # 4. verify page signature
        obs0 = await inspector.inspect(page, 0)
        sig0 = await detector.page_signature(page, obs0, cfg)
        print(f"[smoke2-modal] (4) signature={sig0.signature_id if sig0 else None} heading={obs0.heading!r}")
        if sig0 is None:
            print("[smoke2-modal] (4) signature NOT matched - STOP (drift).")
            return 4

        # 5. verify Welcome to Sonnet modal is visible
        dialogs = page.locator('[role="dialog"]')
        modal_visible = False
        for i in range(min(await dialogs.count(), 10)):
            try:
                if await dialogs.nth(i).is_visible():
                    modal_visible = True
                    break
            except Exception:
                continue
        print(f"[smoke2-modal] (5) welcome_modal_visible={modal_visible}")
        if not modal_visible:
            print("[smoke2-modal] (5) modal NOT visible - STOP (drift).")
            return 5

        # pre-interaction barrier gate (pristine validation + passive badge OK)
        pre_access = await detector.access_control_detected(page, cfg)
        pre_val = await detector.validation_error_detected(page, cfg)
        print(f"[smoke2-modal] pre access_control={pre_access} validation={pre_val} "
              f"bot_protection={await detector.bot_protection_detected(page, cfg)}")
        if pre_access:
            print("[smoke2-modal] blocking barrier BEFORE interaction - STOP (drift).")
            return 5

        # 6. open the modal province selector (observed control)
        trig = page.locator(MODAL_TRIGGER)
        if not await trig.count() or not await trig.first.is_visible():
            print(f"[smoke2-modal] (6) modal trigger {MODAL_TRIGGER} NOT visible - STOP (drift).")
            return 6
        print(f"[smoke2-modal] (6) clicking {MODAL_TRIGGER} (open modal province selector) ...")
        await trig.first.click(timeout=8000)
        await page.wait_for_timeout(600)
        if await stop_if_blocked("(6) after opening selector"):
            return 10

        # 7. verify Ontario option is visible (bounded)
        ontario = page.locator(ONTARIO_SELECTOR)
        ontario_visible = False
        for _ in range(12):
            try:
                if await ontario.count() and await ontario.first.is_visible():
                    ontario_visible = True
                    break
            except Exception:
                pass
            await page.wait_for_timeout(300)
        print(f"[smoke2-modal] (7) Ontario ({ONTARIO_SELECTOR}) present={bool(await ontario.count())} "
              f"visible={ontario_visible}")
        if not ontario_visible:
            print("[smoke2-modal] (7) Ontario NOT visible after opening - STOP (drift, no guessed selector).")
            return 7

        # 8. select Ontario (observed locator)
        print("[smoke2-modal] (8) selecting Ontario ...")
        await ontario.first.click(timeout=8000)
        await page.wait_for_timeout(400)
        if await stop_if_blocked("(8) after selecting Ontario"):
            return 10

        # 9. verify selected state / text if detectable
        try:
            aria_selected = await ontario.first.get_attribute("aria-selected")
        except Exception:
            aria_selected = None
        try:
            trig_text = (await trig.first.inner_text()).strip()
        except Exception:
            trig_text = ""
        try:
            act_desc = await trig.first.get_attribute("aria-activedescendant")
        except Exception:
            act_desc = None
        print(f"[smoke2-modal] (9) Ontario aria-selected={aria_selected} "
              f"trigger_text={trig_text[:40]!r} aria-activedescendant={act_desc}")

        # 10. CAPTCHA gate (also checked in 8); 11. click Confirm if no blocking challenge
        conf = page.locator(CONFIRM_SELECTOR)
        if not await conf.count() or await conf.first.is_disabled():
            print(f"[smoke2-modal] (11) Confirm {CONFIRM_SELECTOR} missing/disabled - STOP.")
            return 11
        print(f"[smoke2-modal] (11) clicking {CONFIRM_SELECTOR} (Confirm) ...")
        await conf.first.click(timeout=8000)

        # 12. wait for resulting application state/page (bounded: hash route change)
        prev_hash = _hash_route(page.url)
        advanced = False
        for _ in range(30):
            await page.wait_for_timeout(400)
            cur = _hash_route(page.url)
            if cur != prev_hash or "province" not in cur:
                advanced = True
                break
        if await stop_if_blocked("(12) after Confirm"):
            return 10
        await page.wait_for_timeout(1500)
        print(f"[smoke2-modal] (12) advanced={advanced}")

        # 13-17. validate host; record URL/hash, heading, controls, detectors
        obs1 = await report("post")
        if not _host_allowed(page.url, cfg.allowed_hosts):
            print("[smoke2-modal] (13) resulting page left allowed_hosts - STOP.")
            return 13

        print(f"[smoke2-modal] (14) safe_url={_safe_url(page.url)} hash_route={_hash_route(page.url)!r}")
        print(f"[smoke2-modal] (15) heading={obs1.heading!r}")

        print("[smoke2-modal] (16) === visible fields ===")
        for f in obs1.fields[:50]:
            rec = {"id": f.external_field_id, "type": f.control_type, "label": f.label,
                   "name": f.name, "required": f.required, "options": f.options_labels[:6]}
            print("   field " + json.dumps(rec, ensure_ascii=False))
        print("[smoke2-modal] (16) === visible interactive controls ===")
        for el in obs1.interactives[:80]:
            rec = {"name": el.accessible_name, "role": el.role, "type": el.element_type,
                   "id": el.external_id, "disabled": el.disabled}
            print("   interactive " + json.dumps(rec, ensure_ascii=False))

        print(f"[smoke2-modal] (17) access_control={await detector.access_control_detected(page, cfg)}")
        print(f"[smoke2-modal] (17) bot_protection={await detector.bot_protection_detected(page, cfg)}")
        print(f"[smoke2-modal] (17) validation_error={await detector.validation_error_detected(page, cfg)}")
        print(f"[smoke2-modal] (17) callback={await detector.callback_detected(page, cfg)}")
        print(f"[smoke2-modal] (17) quote_detected={await detector.quote_detected(page, cfg) is not None}")
        print("[smoke2-modal] page_errors=" + json.dumps(page_errors[:20], ensure_ascii=False))
        print("[smoke2-modal] console_error_warning=" + json.dumps(console_msgs[:20], ensure_ascii=False))

        print("[smoke2-modal] SMOKE #2 (Option A) COMPLETE - nothing filled on the resulting page, "
              "no further navigation.")
        if args.hold_seconds > 0:
            print(f"[smoke2-modal] holding browser open {args.hold_seconds}s ...")
            await page.wait_for_timeout(args.hold_seconds * 1000)
        return 0
    except Exception as exc:
        print(f"[smoke2-modal] interaction interrupted ({type(exc).__name__}) - STOP (drift/error preserved).")
        if page is not None:
            try:
                print(f"[smoke2-modal] on-stop url={_safe_url(page.url)} "
                      f"host_allowed={_host_allowed(page.url, cfg.allowed_hosts)}")
            except Exception:
                pass
        print("[smoke2-modal] no data filled, no PII used, no CAPTCHA solved.")
        return 10
    finally:
        try:
            await browser.stop()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
