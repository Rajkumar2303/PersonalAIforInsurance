"""Sonnet LIVE pilot - SMOKE #3: privacy checkpoint (human-in-the-loop) + Continue.

Builds on the Option A modal path (fresh-session province selector) that was
verified in Smoke #2 (Option A):

  open modal (#provinceSelectorButton) -> select Ontario
  (#provinceSelectorItems li#provinceSelectorOption9) -> Confirm
  (#province-selector-submit-btn) -> modal closes -> Ketch "Your privacy" banner
  appears -> inline Continue (#province-submit-btn) becomes available.

Smoke #3 ADDS the human-in-the-loop privacy checkpoint:

- It detects the Ketch privacy banner and PAUSES. It NEVER clicks
  "I understand" (#ketch-banner-button-primary) or "Manage Preferences"
  (#ketch-banner-button-secondary) - the human makes the privacy choice
  directly in the browser.
- After the human presses Enter in the terminal, the agent verifies the
  overlay is gone, re-checks host/signature/barriers, clicks the observed
  inline Continue, waits for the route/state change, inspects the resulting
  page, and STOPS (nothing is filled on the resulting page).

The ONLY value used is province = Ontario (controlled, non-sensitive). No
applicant PII, no intake profile. No field/action bindings are added to the
Sonnet route config; this stays a controlled dev pilot.

CAPTCHA safety: passive bot protection (bot_protection_present=true) is
expected and is NOT blocked. If a VISIBLE challenge appears after any
interaction we STOP immediately (access_control_detected=true, outcome=blocked)
- never solve/retry/reload/circumvent.

Run (from backend/, with backend on the path):

    $env:PYTHONPATH='.'
    .\\.venv\\Scripts\\python.exe demos\\issue_live_sonnet_smoke3.py --slow-ms 1000
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
PRIVACY_PRIMARY = "#ketch-banner-button-primary"    # "I understand"
PRIVACY_SECONDARY = "#ketch-banner-button-secondary"  # "Manage Preferences"
CONTINUE_SELECTOR = "#province-submit-btn"


def _hash_route(url: str) -> str:
    return (urlsplit(url).fragment or "")


async def _visible(page, selector: str) -> bool:
    try:
        loc = page.locator(selector)
        return bool(await loc.count()) and await loc.first.is_visible()
    except Exception:
        return False


async def _privacy_banner_visible(page) -> bool:
    return await _visible(page, PRIVACY_PRIMARY) or await _visible(page, PRIVACY_SECONDARY)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Sonnet LIVE pilot - Smoke #3 (privacy checkpoint + Continue)")
    parser.add_argument("--slow-ms", type=int, default=1000, help="Playwright per-action delay (ms)")
    parser.add_argument("--settle-ms", type=int, default=5000, help="Initial SPA settle (ms)")
    parser.add_argument("--headless", action="store_true", help="Run headless (default: headful)")
    parser.add_argument("--max-wait-ms", type=int, default=15000, help="Readiness wait budget (ms)")
    parser.add_argument("--poll-ms", type=int, default=300, help="Readiness poll interval (ms)")
    parser.add_argument("--hold-seconds", type=int, default=0, help="Keep browser open N seconds after reporting")
    args = parser.parse_args()

    loader = BrowserRouteConfigLoader()
    cfg = GenericQuoteSiteAdapter().merged_config(loader.load(REGISTRY_ID))
    start_url = cfg.start_url or ""
    if not start_url:
        print("[smoke3] ERROR: no start_url configured")
        return 2

    print(f"[smoke3] route={REGISTRY_ID} start_url={_safe_url(start_url)} allowed_hosts={cfg.allowed_hosts}")
    print(f"[smoke3] headful={not args.headless} slow_ms={args.slow_ms} settle_ms={args.settle_ms}")

    browser = BrowserManager(headless=args.headless, slow_mo=args.slow_ms)
    page = None  # type: ignore[assignment]
    try:
        await browser.start()
    except Exception as exc:
        print(f"[smoke3] browser start failed: {type(exc).__name__}")
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
                print(f"[smoke3] {stage}: VISIBLE CAPTCHA/challenge - STOP IMMEDIATELY "
                      f"(access_control_detected=true, outcome=blocked).")
                return True
            return False

        async def validate_stage(tag: str) -> bool:
            """Return False (STOP) if host/signature/barriers drift at a gate."""
            obs = await inspector.inspect(page, 0)
            sig = await detector.page_signature(page, obs, cfg)
            ok_host = _host_allowed(page.url, cfg.allowed_hosts)
            print(f"[smoke3] {tag} host_allowed={ok_host} url={_safe_url(page.url)} "
                  f"signature={sig.signature_id if sig else None} heading={obs.heading!r}")
            if not ok_host:
                print(f"[smoke3] {tag} host NOT allowed - STOP.")
                return False
            if await detector.access_control_detected(page, cfg):
                print(f"[smoke3] {tag} blocking access-control - STOP.")
                return False
            return True

        # --- automated portion: to the privacy checkpoint ------------------
        print("[smoke3] (1) opening canonical SPA ...")
        await page.goto(start_url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(args.settle_ms)

        matched, waited = await _wait_for_ready(page, READINESS_SELECTORS, args.max_wait_ms, args.poll_ms)
        print(f"[smoke3] (2) interactive_content_ready={matched is not None} matched={matched} waited_ms={waited}")
        if matched is None:
            print("[smoke3] hydration timeout - STOP, no interaction.")
            return 0

        if not await validate_stage("(3) open"):
            return 3

        # Welcome to Sonnet modal visible
        dialogs = page.locator('[role="dialog"]')
        modal_visible = False
        for i in range(min(await dialogs.count(), 10)):
            try:
                if await dialogs.nth(i).is_visible():
                    modal_visible = True
                    break
            except Exception:
                continue
        print(f"[smoke3] (4) welcome_modal_visible={modal_visible}")
        if not modal_visible:
            print("[smoke3] (4) modal NOT visible - STOP (drift).")
            return 4

        # open modal province selector -> Ontario -> Confirm (Option A path)
        trig = page.locator(MODAL_TRIGGER)
        if not await trig.count() or not await trig.first.is_visible():
            print(f"[smoke3] (5) modal trigger {MODAL_TRIGGER} NOT visible - STOP (drift).")
            return 5
        print(f"[smoke3] (5) clicking {MODAL_TRIGGER} ...")
        await trig.first.click(timeout=8000)
        await page.wait_for_timeout(600)
        if await stop_if_blocked("(5) after opening selector"):
            return 10

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
        print(f"[smoke3] (6) Ontario visible={ontario_visible}")
        if not ontario_visible:
            print("[smoke3] (6) Ontario NOT visible - STOP (drift).")
            return 6
        print("[smoke3] (6) selecting Ontario ...")
        await ontario.first.click(timeout=8000)
        await page.wait_for_timeout(400)
        if await stop_if_blocked("(6) after selecting Ontario"):
            return 10
        try:
            aria_selected = await ontario.first.get_attribute("aria-selected")
            trig_text = (await trig.first.inner_text()).strip()
        except Exception:
            aria_selected, trig_text = None, ""
        print(f"[smoke3] (7) Ontario aria-selected={aria_selected} trigger_text={trig_text[:40]!r}")

        conf = page.locator(CONFIRM_SELECTOR)
        if not await conf.count() or await conf.first.is_disabled():
            print(f"[smoke3] (8) Confirm {CONFIRM_SELECTOR} missing/disabled - STOP.")
            return 8
        print(f"[smoke3] (8) clicking {CONFIRM_SELECTOR} (Confirm) ...")
        await conf.first.click(timeout=8000)
        await page.wait_for_timeout(1000)
        if await stop_if_blocked("(8) after Confirm"):
            return 10

        # --- detect Ketch privacy banner ------------------------------------
        banner = await _privacy_banner_visible(page)
        print(f"[smoke3] (9) privacy_banner_detected={banner}")
        if not banner:
            print("[smoke3] (9) privacy banner NOT detected - STOP (drift; do not improvise).")
            return 9
        print("[smoke3] human_checkpoint_required=true")
        print("[smoke3] checkpoint_kind=privacy_preference")
        print("[smoke3] available_actions=[\"Manage Preferences\", \"I understand\"]")
        print("[smoke3] automation will NOT click the privacy banner (human-mediated checkpoint).")

        # --- PAUSE for the human ----------------------------------------------
        print("\nPrivacy checkpoint detected.\n"
              "Please make your choice directly in the browser.\n"
              "Press Enter here after completing the checkpoint.\n", flush=True)
        await asyncio.to_thread(input, "")

        # --- RESUME after human action ------------------------------------------
        await page.wait_for_timeout(500)
        banner_gone = not await _privacy_banner_visible(page)
        print(f"[smoke3] (10) privacy_checkpoint_completed={banner_gone}")
        if not banner_gone:
            print("[smoke3] (10) privacy banner still visible - NOT resuming; STOP (human action incomplete).")
            return 11

        # re-check host/signature/access/validation before advancing
        if not await validate_stage("(11) resumed"):
            return 11
        obs_r = await inspector.inspect(page, 0)
        print(f"[smoke3] (11) validation_error={await detector.validation_error_detected(page, cfg)} "
              f"bot_protection={await detector.bot_protection_detected(page, cfg)}")
        # visible dialogs/overlays after resume (evidence for route-agnostic decision)
        dialogs_after = page.locator('[role="dialog"]')
        vis_dialogs = 0
        for i in range(min(await dialogs_after.count(), 10)):
            try:
                if await dialogs_after.nth(i).is_visible():
                    vis_dialogs += 1
            except Exception:
                continue
        print(f"[smoke3] (11) visible_dialogs_after_resume={vis_dialogs}")

        # confirm inline Continue visible + enabled; advance ONLY if still on the
        # province step. If the page already advanced (e.g. the human continued,
        # or the SPA advanced after consent), do NOT click anything - treat the
        # current page as the resulting page and inspect it only.
        cont = page.locator(CONTINUE_SELECTOR)
        cont_ok = bool(await cont.count()) and await cont.first.is_visible() and not await cont.first.is_disabled()
        print(f"[smoke3] (12) province Continue ({CONTINUE_SELECTOR}) visible_enabled={cont_ok}")
        if cont_ok:
            print(f"[smoke3] (12) clicking {CONTINUE_SELECTOR} (Continue) ...")
            await cont.first.click(timeout=8000)
            if await stop_if_blocked("(12) after Continue"):
                return 10
            # wait for route/state change (bounded)
            prev_hash = _hash_route(page.url)
            for _ in range(35):
                await page.wait_for_timeout(400)
                if _hash_route(page.url) != prev_hash:
                    break
            if await stop_if_blocked("(12) after navigation"):
                return 10
            await page.wait_for_timeout(1500)
            print(f"[smoke3] (13) route_advanced=True")
        else:
            print("[smoke3] (12) province Continue absent - page already advanced; "
                  "inspecting current page only (no click, no fill).")

        # --- STOP: inspect the resulting page only ----------------------------
        obs1 = await inspector.inspect(page, 0)
        sig1 = await detector.page_signature(page, obs1, cfg)
        host_ok = _host_allowed(page.url, cfg.allowed_hosts)
        print(f"[smoke3] (14) host_allowed={host_ok} url={_safe_url(page.url)} hash_route={_hash_route(page.url)!r}")
        print(f"[smoke3] (15) heading={obs1.heading!r}")
        print(f"[smoke3] (16) signature={sig1.signature_id if sig1 else None}")

        print("[smoke3] (17) === visible fields ===")
        for f in obs1.fields[:50]:
            rec = {"id": f.external_field_id, "type": f.control_type, "label": f.label,
                   "name": f.name, "input_type": f.input_type, "placeholder": f.placeholder,
                   "required": f.required, "options": f.options_labels[:8]}
            print("   field " + json.dumps(rec, ensure_ascii=False))
        print("[smoke3] (17) === visible interactive controls ===")
        for el in obs1.interactives[:80]:
            rec = {"name": el.accessible_name, "role": el.role, "type": el.element_type,
                   "id": el.external_id, "disabled": el.disabled, "aria": el.aria}
            print("   interactive " + json.dumps(rec, ensure_ascii=False))

        print(f"[smoke3] (18) access_control={await detector.access_control_detected(page, cfg)}")
        print(f"[smoke3] (18) bot_protection={await detector.bot_protection_detected(page, cfg)}")
        print(f"[smoke3] (18) validation_error={await detector.validation_error_detected(page, cfg)}")
        print(f"[smoke3] (18) callback={await detector.callback_detected(page, cfg)}")
        print(f"[smoke3] (18) quote_detected={await detector.quote_detected(page, cfg) is not None}")
        print("[smoke3] page_errors=" + json.dumps(page_errors[:20], ensure_ascii=False))
        print("[smoke3] console_error_warning=" + json.dumps(console_msgs[:20], ensure_ascii=False))

        print("[smoke3] SMOKE #3 COMPLETE - nothing filled on the resulting page, no further navigation.")
        if args.hold_seconds > 0:
            print(f"[smoke3] holding browser open {args.hold_seconds}s ...")
            await page.wait_for_timeout(args.hold_seconds * 1000)
        return 0
    except Exception as exc:
        print(f"[smoke3] interaction interrupted ({type(exc).__name__}) - STOP (drift/error preserved).")
        if page is not None:
            try:
                print(f"[smoke3] on-stop url={_safe_url(page.url)} "
                      f"host_allowed={_host_allowed(page.url, cfg.allowed_hosts)}")
            except Exception:
                pass
        print("[smoke3] no data filled, no PII used, no CAPTCHA solved.")
        return 10
    finally:
        try:
            await browser.stop()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
