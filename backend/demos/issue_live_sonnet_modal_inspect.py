"""Sonnet LIVE pilot - PHASE 1: inspection-only probe of the "Welcome to Sonnet!" modal.

Option A (fresh-session modal path) requires OBSERVED selectors inside the
visible province-selector dialog before ANY interaction. This script:

- opens the canonical quoting SPA and waits for interactive readiness,
- locates the VISIBLE [role=dialog] ("Welcome to Sonnet!" province selector),
- reports safe DOM metadata ONLY (dialog identity/aria, heading, visible province
  trigger, province menu container, Ontario option DOM + candidate locator,
  Confirm button state, close affordance, detectors),
- performs NO clicks, NO fills, NO values, and uses NO applicant data.

The previously observed candidates (#provinceSelectorItems li#provinceSelectorOption9
and #province-selector-submit-btn) are VERIFIED again; if they drift we report and
STOP.

Run (from backend/, with backend on the path):

    $env:PYTHONPATH='.'
    .\\.venv\\Scripts\\python.exe demos\\issue_live_sonnet_modal_inspect.py --slow-ms 1000
"""

from __future__ import annotations

import argparse
import asyncio
import json

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

READINESS_SELECTORS = ["#province-submit-btn", "#province-selector-submit-btn"]
ONTARIO_CANDIDATE = "#provinceSelectorItems li#provinceSelectorOption9"
CONFIRM_CANDIDATE = "#province-selector-submit-btn"


async def main() -> int:
    parser = argparse.ArgumentParser(description="Sonnet LIVE pilot - Phase 1: modal inspection (no clicks)")
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
        print("[modal] ERROR: no start_url configured")
        return 2

    print(f"[modal] route={REGISTRY_ID} start_url={_safe_url(start_url)} allowed_hosts={cfg.allowed_hosts}")
    print(f"[modal] headful={not args.headless} slow_ms={args.slow_ms} settle_ms={args.settle_ms}")

    browser = BrowserManager(headless=args.headless, slow_mo=args.slow_ms)
    try:
        await browser.start()
        context = await browser.new_context(**live_privacy_context_kwargs())
        page = await context.new_page()

        page_errors: list[str] = []
        console_msgs: list[str] = []
        page.on("pageerror", lambda e: page_errors.append(_sanitize_diagnostic(str(e))))
        page.on("console", lambda m: console_msgs.append(_sanitize_diagnostic(m.text)) if m.type in ("error", "warning") else None)

        print("[modal] opening canonical SPA ...")
        await page.goto(start_url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(args.settle_ms)

        matched, waited = await _wait_for_ready(page, READINESS_SELECTORS, args.max_wait_ms, args.poll_ms)
        print(f"[modal] interactive_content_ready={matched is not None} matched={matched} waited_ms={waited}")
        if matched is None:
            print("[modal] hydration timeout - STOP, no interaction (not classified as blocked).")
            return 0

        print(f"[modal] host_allowed={_host_allowed(page.url, cfg.allowed_hosts)} url={_safe_url(page.url)}")

        inspector = PageInspector()
        detector = PageDetector()
        obs = await inspector.inspect(page, 0)
        sig = await detector.page_signature(page, obs, cfg)
        print(f"[modal] page_signature={sig.signature_id if sig else None} heading={obs.heading!r}")

        # --- visible dialogs -------------------------------------------------
        dialogs = page.locator('[role="dialog"]')
        dcount = await dialogs.count()
        print(f"[modal] visible_dialogs={dcount}")
        visible_dialog = None
        for i in range(min(dcount, 10)):
            d = dialogs.nth(i)
            try:
                if not await d.is_visible():
                    continue
                meta = await d.evaluate(
                    "e => { const o={id:e.id||null, role:e.getAttribute('role'),"
                    " aria_modal:e.getAttribute('aria-modal'),"
                    " aria_labelledby:e.getAttribute('aria-labelledby'),"
                    " aria_describedby:e.getAttribute('aria-describedby'),"
                    " cls:(e.className && typeof e.className==='string')? e.className.slice(0,140):null};"
                    " return o; }"
                )
                try:
                    h = (await d.locator("h1,h2,h3,.modal-title").first.inner_text()).strip()
                except Exception:
                    h = ""
                print("[modal] dialog#" + str(i) + " " + json.dumps(meta, ensure_ascii=False))
                print(f"[modal]    dialog_heading={h[:80]!r}")
                visible_dialog = d
                break
            except Exception:
                continue

        if visible_dialog is None:
            print("[modal] NO visible dialog found - STOP (drift).")
            return 3

        # --- interactives INSIDE the visible dialog (incl. dropdown trigger) ---
        print("[modal] === interactive controls inside dialog ===")
        inner = visible_dialog.locator("button, a, [role='listbox'], [role='button'], input, select, textarea")
        n = await inner.count()
        for i in range(min(n, 60)):
            el = inner.nth(i)
            try:
                if not await el.is_visible():
                    continue
                m = await el.evaluate(
                    "e => { const o={tag:(e.tagName||'').toLowerCase(), id:e.id||null,"
                    " name:e.getAttribute('name'), role:e.getAttribute('role'),"
                    " aria_label:e.getAttribute('aria-label'), aria_expanded:e.getAttribute('aria-expanded'),"
                    " aria_haspopup:e.getAttribute('aria-haspopup'), disabled:e.disabled===true,"
                    " text:(e.innerText||'').trim().slice(0,50)}; return o; }"
                )
                print("   " + json.dumps(m, ensure_ascii=False))
            except Exception:
                continue

        # --- province menu container + Ontario option --------------------------
        menu = page.locator("#provinceSelectorItems")
        try:
            menu_count = await menu.count()
        except Exception:
            menu_count = 0
        print(f"[modal] province_menu #provinceSelectorItems present={menu_count > 0}")
        if menu_count:
            items = page.locator("#provinceSelectorItems li")
            nitems = await items.count()
            texts = []
            for i in range(min(nitems, 40)):
                try:
                    texts.append(((await items.nth(i).text_content()) or "").strip())
                except Exception:
                    continue
            print(f"[modal]    menu_items={nitems} options={json.dumps(texts[:40], ensure_ascii=False)}")
        ont = page.locator(ONTARIO_CANDIDATE)
        try:
            ont_count = await ont.count()
            ont_vis = bool(ont_count) and await ont.first.is_visible()
            ont_role = (await ont.first.get_attribute("role")) if ont_count else None
        except Exception:
            ont_count, ont_vis, ont_role = 0, False, None
        print(f"[modal] Ontario ({ONTARIO_CANDIDATE}) present={ont_count > 0} visible={ont_vis} role={ont_role}")

        # --- Confirm button state ----------------------------------------------
        conf = page.locator(CONFIRM_CANDIDATE)
        try:
            ccount = await conf.count()
            if ccount:
                cvis = await conf.first.is_visible()
                cdis = await conf.first.is_disabled()
                caria = await conf.first.get_attribute("aria-disabled")
                ctext = (await conf.first.inner_text()).strip()
            else:
                cvis = cdis = False
                caria = None
                ctext = ""
        except Exception:
            cvis = cdis = False
            caria = None
            ctext = ""
        print(f"[modal] Confirm ({CONFIRM_CANDIDATE}) present={ccount > 0} visible={cvis} "
              f"disabled={cdis} aria-disabled={caria} text={ctext[:40]!r}")

        # --- close affordance ----------------------------------------------------
        close = visible_dialog.locator(
            "[aria-label*='close' i], [class*='close' i], [ng-click*='dismiss' i], [ng-click*='cancel' i], .modal-close"
        )
        try:
            kcount = await close.count()
            print(f"[modal] close_affordance_candidates={kcount}")
            for i in range(min(kcount, 10)):
                try:
                    el = close.nth(i)
                    if not await el.is_visible():
                        continue
                    m = await el.evaluate(
                        "e => { const o={tag:(e.tagName||'').toLowerCase(), id:e.id||null,"
                        " aria_label:e.getAttribute('aria-label'), cls:(e.className && typeof e.className==='string')? e.className.slice(0,60):null,"
                        " text:(e.innerText||'').trim().slice(0,40)}; return o; }"
                    )
                    print("   close " + json.dumps(m, ensure_ascii=False))
                except Exception:
                    continue
        except Exception:
            print("[modal] close_affordance probe error")

        # --- detectors ------------------------------------------------------------
        print(f"[modal] access_control={await detector.access_control_detected(page, cfg)}")
        print(f"[modal] bot_protection={await detector.bot_protection_detected(page, cfg)}")
        print(f"[modal] validation_error={await detector.validation_error_detected(page, cfg)}")
        print(f"[modal] callback={await detector.callback_detected(page, cfg)}")
        print(f"[modal] quote_detected={await detector.quote_detected(page, cfg) is not None}")
        print("[modal] page_errors=" + json.dumps(page_errors[:20], ensure_ascii=False))
        print("[modal] console_error_warning=" + json.dumps(console_msgs[:20], ensure_ascii=False))
        print("[modal] PHASE 1 INSPECTION COMPLETE - no clicks, no fills, no applicant data.")
        if args.hold_seconds > 0:
            print(f"[modal] holding browser open {args.hold_seconds}s ...")
            await page.wait_for_timeout(args.hold_seconds * 1000)
        return 0
    finally:
        try:
            await browser.stop()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
