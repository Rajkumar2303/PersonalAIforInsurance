"""Sonnet LIVE pilot - INSPECTION-ONLY smoke test #1 (no applicant data).

Opens the verified canonical quoting SPA in a HEADFUL Chromium (slow_mo ~1000 ms
by default), validates the allowed host + page signature, inspects visible
controls/labels, and reports ONLY safe metadata (page host, field labels,
control types, required flags, DOM id/name candidates, option labels, button
labels, signature candidates). It does NOT:

- submit applicant data,
- click generic "get quote"/submission actions,
- guess selections,
- navigate multiple form pages,
- attempt to obtain a quote.

No intake session and no personal values are required or used. No applicant
value ever appears in arguments, source, config, logs, or this report.

Run (from backend/, with backend on the path):

    $env:PYTHONPATH='.'
    .\\.venv\\Scripts\\python.exe demos\\issue_live_sonnet_inspect.py --slow-ms 1000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from urllib.parse import urlsplit

from app.browser.adapters import GenericQuoteSiteAdapter
from app.browser.config import BrowserRouteConfigLoader
from app.browser.detect import PageDetector
from app.browser.inspect import PageInspector
from app.browser.manager import BrowserManager
from app.browser.session import live_privacy_context_kwargs

REGISTRY_ID = "sonnet"


def _safe_url(url: str) -> str:
    """Report host + path + hash route, never a query string (may carry data)."""
    parts = urlsplit(url)
    fragment = parts.fragment or ""
    path = parts.path or "/"
    return f"{parts.netloc}{path}" + (f"#{fragment}" if fragment else "")


def _host_allowed(url: str, allowed: list[str]) -> bool:
    host = (urlsplit(url).netloc or "").lower()
    return any(host == a.lower() or host.endswith("." + a.lower()) for a in allowed)


def _sanitize_diagnostic(message: str) -> str:
    """Collapse whitespace and truncate to safe technical metadata (<=160 chars).

    Never retains raw console payloads; dev/demo tooling only.
    """
    cleaned = re.sub(r"\s+", " ", message or "").strip()
    return cleaned[:160]


#: Generic fallback readiness signals when no --wait-for-selector is given:
#: any visible interactive/form control counts as "meaningful content".
_FALLBACK_READY_SELECTOR = (
    "button, a, input, select, textarea, [role='button'], [role='radio'], [role='option'], [role='checkbox']"
)


async def _wait_for_ready(page, selectors: list[str], max_wait_ms: int, poll_ms: int):
    """Poll for ANY configured selector to become VISIBLE (DOM evidence).

    Bounded: returns (matched_selector, waited_ms) or (None, waited_ms) on
    timeout. Provider-independent; never relies on networkidle (SPAs stream
    analytics/telemetry/advertising continuously). Inspection tooling only.
    """
    candidates = selectors or [_FALLBACK_READY_SELECTOR]
    waited = 0
    while waited < max_wait_ms:
        for selector in candidates:
            try:
                locator = page.locator(selector)
                if await locator.count() and await locator.first.is_visible():
                    return selector, waited
            except Exception:
                continue
        await page.wait_for_timeout(poll_ms)
        waited += poll_ms
    return None, waited


async def _probe_options(page) -> None:
    """DEV/INSPECTION-ONLY: deep-probe option-like DOM structures.

    Generic queries (labels, radios incl. hidden, role=option/radio, tabindex,
    data-*), plus Ontario's DOM + Continue/Confirm state. Never clicks, never
    fills, never reports entered values.
    """
    print("[inspect-options] === Continue / Confirm state ===")
    for selector, label in (("#province-submit-btn", "Continue"), ("#province-selector-submit-btn", "Confirm")):
        locator = page.locator(selector)
        if await locator.count() == 0:
            print(f"   {label} ({selector}): NOT PRESENT")
            continue
        el = locator.first
        try:
            visible = await el.is_visible()
            disabled = await el.is_disabled()
            aria_disabled = await el.get_attribute("aria-disabled")
            tag = await el.evaluate("e => e.tagName.toLowerCase()")
            text = (await el.inner_text()).strip()
        except Exception as exc:
            print(f"   {label} ({selector}): probe error {type(exc).__name__}")
            continue
        print(f"   {label} ({selector}): tag={tag} visible={visible} disabled={disabled} "
              f"aria-disabled={aria_disabled} text={text[:40]!r}")
    both = (await page.locator("#province-submit-btn").count() > 0
            and await page.locator("#province-selector-submit-btn").count() > 0)
    print(f"[inspect-options] both_buttons_present={both}")

    print("[inspect-options] === radio inputs (incl. hidden; no checked state) ===")
    radios = page.locator('input[type="radio"]')
    n = await radios.count()
    for i in range(min(n, 40)):
        r = radios.nth(i)
        try:
            rid = await r.get_attribute("id")
            name = await r.get_attribute("name")
            value = await r.get_attribute("value")
            aria = await r.get_attribute("aria-label")
            label_for = None
            if rid:
                lab = page.locator(f'label[for="{rid}"]').first
                if await lab.count():
                    label_for = (await lab.inner_text()).strip()[:60]
            print(f"   radio id={rid} name={name} value={value} aria={aria} label_for={label_for}")
        except Exception:
            continue

    print("[inspect-options] === labels ===")
    labels = page.locator("label")
    n = await labels.count()
    for i in range(min(n, 60)):
        lab = labels.nth(i)
        try:
            if not await lab.is_visible():
                continue
            text = (await lab.inner_text()).strip()
            label_for = await lab.get_attribute("for")
            if text:
                print(f"   label for={label_for} text={text[:80]!r}")
        except Exception:
            continue

    print("[inspect-options] === role=option / role=radio ===")
    for role in ("option", "radio"):
        locator = page.locator(f'[role="{role}"]')
        n = await locator.count()
        for i in range(min(n, 40)):
            el = locator.nth(i)
            try:
                if not await el.is_visible():
                    continue
                text = (await el.inner_text()).strip()
                rid = await el.get_attribute("id")
                tab = await el.get_attribute("tabindex")
                sel_ = await el.get_attribute("aria-selected") or await el.get_attribute("aria-checked")
                data = await el.evaluate(
                    "e => { const o={}; for (const a of e.attributes) { if (a.name.startsWith('data-')) o[a.name]=a.value.slice(0,60);} return o; }"
                )
                print(f"   {role} id={rid} tabindex={tab} aria_selected={sel_} text={text[:60]!r} "
                      f"data={json.dumps(data, ensure_ascii=False)[:120]}")
            except Exception:
                continue

    print("[inspect-options] === tabindex elements ===")
    locator = page.locator("[tabindex]")
    n = await locator.count()
    for i in range(min(n, 60)):
        el = locator.nth(i)
        try:
            if not await el.is_visible():
                continue
            text = (await el.inner_text()).strip()
            if not text:
                continue
            tag = await el.evaluate("e => e.tagName.toLowerCase()")
            rid = await el.get_attribute("id")
            role = await el.get_attribute("role")
            print(f"   tabindex tag={tag} id={rid} role={role} text={text[:60]!r}")
        except Exception:
            continue

    print("[inspect-options] === Ontario ===")
    try:
        matches = page.get_by_text("Ontario")
        mn = await matches.count()
        print(f"   matches={mn}")
        rows = []
        for i in range(min(mn, 30)):
            el = matches.nth(i)
            try:
                meta = await el.evaluate(
                    "e => { const o={tag:(e.tagName||'').toLowerCase(), id:e.id||null,"
                    " name:e.getAttribute('name'), role:e.getAttribute('role'),"
                    " cls:(e.className && typeof e.className==='string')? e.className.slice(0,120):null,"
                    " tabindex:e.getAttribute('tabindex'),"
                    " aria_label:e.getAttribute('aria-label'), aria_labelledby:e.getAttribute('aria-labelledby'),"
                    " aria_describedby:e.getAttribute('aria-describedby'),"
                    " aria_checked:e.getAttribute('aria-checked'), aria_selected:e.getAttribute('aria-selected'),"
                    " text:(e.innerText||'').trim().slice(0,80)};"
                    " const f=e.closest('label'); if(f){o.label_for=f.htmlFor||null;}"
                    " const inp=e.querySelector('input,select,textarea');"
                    " if(inp){o.associated_input={id:inp.id||null,name:inp.name||null,type:inp.type||inp.tagName.toLowerCase()};}"
                    " let anc=e.parentElement, found=null;"
                    " while(anc && !found){ const t=(anc.tagName||'').toLowerCase(); const r=anc.getAttribute('role');"
                    " if(t==='button'||t==='a'||t==='label'||r==='button'||r==='radio'||r==='option'){"
                    " found=t+(anc.id?'#'+anc.id:'')+(r?'[role='+r+']':'');} anc=anc.parentElement; }"
                    " o.nearest_interactive_ancestor=found;"
                    " const d={}; for(const a of e.attributes){ if(a.name.startsWith('data-')) d[a.name]=a.value.slice(0,60);} o.data=d;"
                    " o.chain=[]; let n=e; for(let k=0;k<5 && n;k++){ o.chain.push((n.tagName||'').toLowerCase()"
                    " +(n.id?'#'+n.id:'')+(n.className && typeof n.className==='string'? '.'+n.className.split(' ')[0]:'')); n=n.parentElement; }"
                    " return o; }"
                )
                rows.append(meta)
            except Exception:
                continue
        # Deepest = shortest visible text; report it first.
        rows.sort(key=lambda r: len(r.get("text") or ""))
        for meta in rows[:8]:
            locator = None
            if meta.get("id"):
                locator = f"#{meta['id']}"
            elif meta.get("label_for"):
                locator = f"label[for=\"{meta['label_for']}\"]"
            elif meta.get("associated_input", {}).get("id"):
                locator = f"#{meta['associated_input']['id']}"
            elif meta.get("name"):
                locator = f"[name=\"{meta['name']}\"]"
            elif meta.get("data", {}).get("data-testid"):
                locator = f"[data-testid=\"{meta['data']['data-testid']}\"]"
            print("   Ontario: " + json.dumps(meta, ensure_ascii=False))
            print(f"      candidate_locator={locator}")
    except Exception as exc:
        print(f"   Ontario probe error: {type(exc).__name__}")

    print("[inspect-options] === province option menus (closed dropdowns; no interaction) ===")
    for menu_id in ("#provinceItems", "#provinceSelectorItems"):
        menu = page.locator(menu_id)
        try:
            count = await menu.count()
        except Exception:
            count = 0
        if count == 0:
            print(f"   menu {menu_id}: NOT PRESENT")
            continue
        items = page.locator(f"{menu_id} li")
        nitems = await items.count()
        texts = []
        sample = None
        for i in range(min(nitems, 60)):
            li = items.nth(i)
            try:
                rid = await li.get_attribute("id")
                role = await li.get_attribute("role")
                text = ((await li.text_content()) or "").strip()
                texts.append(text)
                if sample is None:
                    sample = {"id": rid, "role": role, "text": text[:60]}
            except Exception:
                continue
        print(f"   menu {menu_id}: present={count > 0} items={nitems} "
              f"sample={json.dumps(sample, ensure_ascii=False)}")
        print(f"   menu {menu_id}: options={json.dumps(texts[:60], ensure_ascii=False)}")
        print(f"   menu {menu_id}: structure=" + (
            "ul.menu > li[role=option].dropdown--menu-item (hidden native-list dropdown)"
            if sample else "unknown"))


async def _dump_matches(page, selector: str, kind: str) -> None:
    """Print sanitized metadata for elements matching a detector selector."""
    locator = page.locator(selector)
    try:
        count = await locator.count()
    except Exception as exc:
        print(f"   {kind} selector={selector} count=error({type(exc).__name__})")
        return
    print(f"   {kind} selector={selector} count={count}")
    for i in range(min(count, 10)):
        try:
            el = locator.nth(i)
            visible = await el.is_visible()
            meta = await el.evaluate(
                "e => { const o={tag:(e.tagName||'').toLowerCase(), id:e.id||null,"
                " name:e.getAttribute('name'), role:e.getAttribute('role'),"
                " cls:(e.className && typeof e.className==='string')? e.className.slice(0,100):null,"
                " src:(e.getAttribute('src')||'').slice(0,140),"
                " aria_invalid:e.getAttribute('aria-invalid'), aria_live:e.getAttribute('aria-live'),"
                " aria_label:e.getAttribute('aria-label'), text:(e.innerText||'').trim().slice(0,80)};"
                " return o; }"
            )
            print(f"      visible={visible} " + json.dumps(meta, ensure_ascii=False))
        except Exception:
            continue


async def _probe_barrier_evidence(page) -> None:
    """Report exactly which DOM evidence each barrier detector can see.

    Dev/demo tooling only; no interaction, no applicant data. Lets us
    classify a True barrier as genuine vs detector false-positive from
    observed evidence instead of guessing.
    """
    print("[inspect-barrier-evidence] access-control candidates:")
    for selector in (
        '[class*="g-recaptcha"]',
        '[class*="h-captcha"]',
        "iframe[src*='recaptcha'], iframe[src*='hcaptcha']",
    ):
        await _dump_matches(page, selector, "access")
    print("[inspect-barrier-evidence] validation candidates:")
    for selector in (
        '[aria-invalid="true"]',
        '[role="alert"]',
        '[class*="error" i]',
        '[class*="invalid" i]',
        '[aria-live="assertive"]',
    ):
        await _dump_matches(page, selector, "validation")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Sonnet LIVE pilot - inspection-only smoke test #1b")
    parser.add_argument("--slow-ms", type=int, default=1000, help="Playwright per-action delay (ms)")
    parser.add_argument("--settle-ms", type=int, default=5000, help="Wait (ms) after load for the SPA to render")
    parser.add_argument("--hold-seconds", type=int, default=0, help="Keep the browser open N seconds after reporting")
    parser.add_argument("--headless", action="store_true", help="Run headless (default: headful)")
    parser.add_argument("--inspect-options", action="store_true",
                        help="DEV ONLY: deep-probe option-like DOM structures (no clicks)")
    parser.add_argument("--wait-for-selector", action="append", default=[],
                        help="Readiness signal: wait for ANY of these selectors to become visible (repeatable)")
    parser.add_argument("--max-wait-ms", type=int, default=15000,
                        help="Bounded readiness wait budget (ms) before reporting hydration_timeout")
    parser.add_argument("--poll-ms", type=int, default=300,
                        help="Readiness poll interval (ms)")
    args = parser.parse_args()

    loader = BrowserRouteConfigLoader()
    # Merge generic safety defaults so barrier/quote detection uses the same
    # protective patterns the executor uses (generic, shared adapter).
    cfg = GenericQuoteSiteAdapter().merged_config(loader.load(REGISTRY_ID))
    start_url = cfg.start_url
    if not start_url:
        print("[inspect] ERROR: no start_url configured")
        return 2

    print(f"[inspect] route={REGISTRY_ID} start_url={_safe_url(start_url)} allowed_hosts={cfg.allowed_hosts}")
    print(f"[inspect] headful={not args.headless} slow_ms={args.slow_ms} settle_ms={args.settle_ms}")

    browser = BrowserManager(headless=args.headless, slow_mo=args.slow_ms)
    try:
        await browser.start()
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

        print("[inspect] navigating to the verified start URL ...")
        await page.goto(start_url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(args.settle_ms)  # let the SPA hydrate

        final_url = page.url
        host_ok = _host_allowed(final_url, cfg.allowed_hosts)
        print(f"[inspect] final_url={_safe_url(final_url)}")
        print(f"[inspect] host_allowed={host_ok} allowed={cfg.allowed_hosts}")

        # --- generic bounded SPA readiness wait (DOM evidence, dev tooling) ---
        matched_selector, waited = await _wait_for_ready(
            page, args.wait_for_selector, args.max_wait_ms, args.poll_ms
        )
        interactive_content_ready = matched_selector is not None
        hydration_timeout = not interactive_content_ready
        print(f"[inspect] readiness_selectors={args.wait_for_selector or ['<any interactive>']}")
        print(f"[inspect] interactive_content_ready={interactive_content_ready}")
        print(f"[inspect] readiness_selector_matched={matched_selector}")
        print(f"[inspect] hydration_wait_ms={waited}")
        print(f"[inspect] hydration_timeout={hydration_timeout}")

        inspector = PageInspector()
        page_obs = await inspector.inspect(page, 0)

        if hydration_timeout:
            # Report safe diagnostics only; never classify as a barrier.
            detector = PageDetector()
            sig = await detector.page_signature(page, page_obs, cfg)
            print(f"[inspect] HYDRATION TIMEOUT - not classified as blocked/unreachable/ineligible.")
            print(f"[inspect] page_signature={sig.signature_id if sig else None}")
            print(f"[inspect] heading={page_obs.heading!r}")
            print(f"[inspect] interactive_count={page_obs.interactives_count}")
            print(f"[inspect] readiness_selectors_present=" + json.dumps([
                bool(await page.locator(s).count()) for s in (args.wait_for_selector or [])
            ], ensure_ascii=False))
            print(f"[inspect] page_errors=" + json.dumps(page_errors[:20], ensure_ascii=False))
            print(f"[inspect] console_error_warning=" + json.dumps(console_msgs[:20], ensure_ascii=False))
            print("[inspect] INSPECTION COMPLETE (timeout) - no interaction.")
            return 0

        print(f"[inspect] controls_count={page_obs.controls_count} interactives_count={page_obs.interactives_count} heading={page_obs.heading!r}")

        print("[inspect] fields:")
        for field in page_obs.fields:
            rec = {
                "external_field_id": field.external_field_id,  # DOM id/name/label-derived (safe)
                "control_type": field.control_type,
                "label": field.label,
                "name": field.name,
                "input_type": field.input_type,
                "placeholder": field.placeholder,
                "required": field.required,
                "options": field.options_labels,
            }
            print("   " + json.dumps(rec, ensure_ascii=False))

        print("[inspect] interactive_elements:")
        for el in page_obs.interactives:
            rec = {
                "accessible_name": el.accessible_name,
                "role": el.role,
                "element_type": el.element_type,
                "id_or_name": el.external_id,
                "disabled": el.disabled,
                "aria": el.aria,
            }
            print("   " + json.dumps(rec, ensure_ascii=False))

        detector = PageDetector()
        sig = await detector.page_signature(page, page_obs, cfg)
        print(f"[inspect] page_signature={sig.signature_id if sig else None}")
        print(f"[inspect] barrier_access_control={await detector.access_control_detected(page, cfg)}")
        print(f"[inspect] barrier_callback={await detector.callback_detected(page, cfg)}")
        print(f"[inspect] barrier_validation={await detector.validation_error_detected(page, cfg)}")
        print(f"[inspect] bot_protection_present={await detector.bot_protection_detected(page, cfg)}")
        quote = await detector.quote_detected(page, cfg)
        print(f"[inspect] quote_detected={quote is not None}")

        await _probe_barrier_evidence(page)

        print("[inspect] page_errors=" + json.dumps(page_errors[:20], ensure_ascii=False))
        print("[inspect] console_error_warning=" + json.dumps(console_msgs[:20], ensure_ascii=False))

        if args.inspect_options:
            await _probe_options(page)

        print("[inspect] INSPECTION COMPLETE - no applicant data submitted, no actions clicked.")
        if args.hold_seconds > 0:
            print(f"[inspect] holding browser open {args.hold_seconds}s ...")
            await page.wait_for_timeout(args.hold_seconds * 1000)
        return 0
    finally:
        try:
            await browser.stop()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
