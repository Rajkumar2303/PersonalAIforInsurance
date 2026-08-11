"""Sonnet LIVE pilot - SMOKE #2: controlled first interaction (Ontario -> Continue).

Dev-pilot interaction ONLY, using EXACTLY the selectors observed in Smoke #1d:

    #provinceButton                     (open the province dropdown)
    #provinceItems li#provinceOption9   (Ontario - observed option, index 9)
    #province-submit-btn                (Continue)

The ONLY value used is province = Ontario (controlled, non-sensitive). No
applicant PII, no intake profile, no name/DOB/postal/address/licence/VIN/claims/
phone/email. No field or action bindings are added to the Sonnet route config;
this stays a controlled dev pilot.

Flow: open -> readiness wait -> validate host/signature/barriers -> click
#provinceButton -> verify Ontario visible -> select Ontario -> verify selected
state if detectable -> click Continue -> wait for resulting page -> validate
host -> capture safe URL/hash route + heading + signature + visible
controls/interactives -> STOP (nothing filled on the resulting page, no further
navigation).

CAPTCHA escalation: after EACH interaction we re-run blocking barrier detection.
If a VISIBLE challenge appears we STOP IMMEDIATELY and report
access_control_detected=true / outcome=blocked. We never solve, retry, reload,
switch fingerprints/proxies, or alter automation to evade.

Run (from backend/, with backend on the path):

    $env:PYTHONPATH='.'
    .\\.venv\\Scripts\\python.exe demos\\issue_live_sonnet_smoke2.py --slow-ms 1000
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
ONTARIO_SELECTOR = "#provinceItems li#provinceOption9"


async def main() -> int:
    parser = argparse.ArgumentParser(description="Sonnet LIVE pilot - Smoke #2 (controlled first interaction)")
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
        print("[smoke2] ERROR: no start_url configured")
        return 2

    print(f"[smoke2] route={REGISTRY_ID} start_url={_safe_url(start_url)} allowed_hosts={cfg.allowed_hosts}")
    print(f"[smoke2] headful={not args.headless} slow_ms={args.slow_ms} settle_ms={args.settle_ms}")

    browser = BrowserManager(headless=args.headless, slow_mo=args.slow_ms)
    page = None  # type: ignore[assignment]
    try:
        await browser.start()
    except Exception as exc:
        print(f"[smoke2] browser start failed: {type(exc).__name__}")
        return 2
    try:
        context = await browser.new_context(**live_privacy_context_kwargs())
        page = await context.new_page()

        page_errors: list[str] = []
        console_msgs: list[str] = []

        def _on_pageerror(exc) -> None:  # type: ignore[no-untyped-def]
            page_errors.append(_sanitize_diagnostic(str(exc)))

        def _on_console(msg) -> None:  # type: ignore[no-untyped-def]
            if msg.type in ("error", "warning"):
                console_msgs.append(_sanitize_diagnostic(msg.text))

        page.on("pageerror", _on_pageerror)
        page.on("console", _on_console)

        inspector = PageInspector()
        detector = PageDetector()

        async def stop_if_blocked(stage: str) -> bool:
            """CAPTCHA escalation gate: a VISIBLE challenge means STOP (Issue #8 outcome=blocked)."""
            if await detector.access_control_detected(page, cfg):
                print(f"[smoke2] {stage}: VISIBLE CAPTCHA/challenge detected - STOP IMMEDIATELY (outcome=blocked).")
                return True
            return False

        async def report(tag: str) -> object:
            obs = await inspector.inspect(page, 0)
            sig = await detector.page_signature(page, obs, cfg)
            access = await detector.access_control_detected(page, cfg)
            validation = await detector.validation_error_detected(page, cfg)
            callback = await detector.callback_detected(page, cfg)
            bot = await detector.bot_protection_detected(page, cfg)
            quote = await detector.quote_detected(page, cfg)
            print(f"[smoke2] {tag} host_allowed={_host_allowed(page.url, cfg.allowed_hosts)} url={_safe_url(page.url)}")
            print(f"[smoke2] {tag} signature={sig.signature_id if sig else None} heading={obs.heading!r}")
            print(f"[smoke2] {tag} access_control={access} validation_error={validation} "
                  f"callback={callback} bot_protection={bot} quote_detected={quote is not None}")
            return obs

        # 1. open the province page
        print("[smoke2] (1) opening verified start URL ...")
        await page.goto(start_url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(args.settle_ms)

        # 2. wait for interactive readiness (bounded)
        matched, waited = await _wait_for_ready(page, READINESS_SELECTORS, args.max_wait_ms, args.poll_ms)
        print(f"[smoke2] (2) interactive_content_ready={matched is not None} matched={matched} waited_ms={waited}")
        if matched is None:
            print("[smoke2] hydration timeout - STOP, no interaction (not classified as blocked).")
            return 0

        # 3. validate host / signature / barriers before interacting
        obs0 = await report("pre")
        if not _host_allowed(page.url, cfg.allowed_hosts):
            print("[smoke2] (3) host NOT allowed - STOP.")
            return 3
        if obs0.page_signature is None and (await detector.page_signature(page, obs0, cfg)) is None:
            print("[smoke2] (3) page signature NOT matched - STOP (drift).")
            return 3
        if await detector.access_control_detected(page, cfg) or await detector.validation_error_detected(page, cfg):
            print("[smoke2] (3) blocking barrier flagged BEFORE interaction - STOP (drift).")
            return 3

        # 4. click #provinceButton to open the dropdown
        button = page.locator("#provinceButton")
        if not await button.count() or not await button.first.is_visible():
            print("[smoke2] (4) #provinceButton NOT visible - STOP (drift).")
            return 5
        print("[smoke2] (4) clicking #provinceButton (open dropdown) ...")
        await button.first.click(timeout=8000)
        await page.wait_for_timeout(600)
        if await stop_if_blocked("(4) after opening dropdown"):
            return 9

        # 5. verify Ontario option is visible (bounded)
        ontario = page.locator(ONTARIO_SELECTOR)
        ontario_visible = False
        for _ in range(10):
            try:
                if await ontario.count() and await ontario.first.is_visible():
                    ontario_visible = True
                    break
            except Exception:
                pass
            await page.wait_for_timeout(300)
        print(f"[smoke2] (5) Ontario ({ONTARIO_SELECTOR}) present={bool(await ontario.count())} visible={ontario_visible}")
        if not ontario_visible:
            print("[smoke2] (5) Ontario option NOT visible after opening - STOP (drift, no guessed selector).")
            return 6

        # 6. select the OBSERVED Ontario option
        print("[smoke2] (6) clicking Ontario option ...")
        await ontario.first.click(timeout=8000)
        await page.wait_for_timeout(400)
        if await stop_if_blocked("(6) after selecting Ontario"):
            return 9

        # 7. verify selected state if detectable
        try:
            aria_selected = await ontario.first.get_attribute("aria-selected")
        except Exception:
            aria_selected = None
        try:
            btn_text = (await page.locator("#provinceButton").first.inner_text()).strip()
        except Exception:
            btn_text = ""
        print(f"[smoke2] (7) Ontario aria-selected={aria_selected} provinceButton_text={btn_text[:40]!r}")

        # 8. click #province-submit-btn (Continue)
        cont = page.locator("#province-submit-btn")
        if not await cont.count() or await cont.first.is_disabled():
            print("[smoke2] (8) Continue missing/disabled - STOP.")
            return 7
        print("[smoke2] (8) clicking #province-submit-btn (Continue) ...")
        await cont.first.click(timeout=8000)

        # 9. wait for the resulting page
        await page.wait_for_timeout(4000)
        if await stop_if_blocked("(9) after Continue"):
            return 9

        # 10-13. validate host; capture safe URL/hash, heading, signature, controls/interactives
        obs1 = await report("post")
        if not _host_allowed(page.url, cfg.allowed_hosts):
            print("[smoke2] (10) resulting page left allowed_hosts - STOP.")
            return 8

        print("[smoke2] (13) === resulting page controls ===")
        for f in obs1.fields[:40]:
            rec = {"id": f.external_field_id, "type": f.control_type, "label": f.label,
                   "name": f.name, "required": f.required, "options": f.options_labels[:6]}
            print("   field " + json.dumps(rec, ensure_ascii=False))
        print("[smoke2] (13) === resulting page interactives ===")
        for el in obs1.interactives[:60]:
            rec = {"name": el.accessible_name, "role": el.role, "type": el.element_type,
                   "id": el.external_id, "disabled": el.disabled}
            print("   interactive " + json.dumps(rec, ensure_ascii=False))

        print("[smoke2] sanitized page_errors=" + json.dumps(page_errors[:20], ensure_ascii=False))
        print("[smoke2] sanitized console_errors_warnings=" + json.dumps(console_msgs[:20], ensure_ascii=False))
        print("[smoke2] SMOKE #2 COMPLETE - nothing filled on the resulting page, no further navigation.")
        if args.hold_seconds > 0:
            print(f"[smoke2] holding browser open {args.hold_seconds}s ...")
            await page.wait_for_timeout(args.hold_seconds * 1000)
        return 0
    except Exception as exc:
        # Preserve + classify the interruption; never hide a failed attempt.
        print(f"[smoke2] interaction interrupted ({type(exc).__name__}) - STOP (drift/error preserved).")
        if page is not None:
            try:
                print(f"[smoke2] on-stop url={_safe_url(page.url)} "
                      f"host_allowed={_host_allowed(page.url, cfg.allowed_hosts)}")
                dialogs = page.locator('[role="dialog"]')
                dcount = await dialogs.count()
                print(f"[smoke2] on-stop visible_dialogs={dcount}")
                for i in range(min(dcount, 5)):
                    try:
                        d = dialogs.nth(i)
                        if not await d.is_visible():
                            continue
                        h = (await d.locator("h1,h2,h3,.modal-title").first.inner_text()).strip()
                        cls = await d.get_attribute("class") or ""
                        print(f"   dialog#{i} heading={h[:60]!r} class={cls[:80]!r}")
                    except Exception:
                        continue
            except Exception:
                pass
        print("[smoke2] no data filled, no PII used, no CAPTCHA solved.")
        return 10
    finally:
        try:
            await browser.stop()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
