"""Sonnet LIVE pilot - SMOKE #4: derived counts + "Next: vehicle details".

Builds on the verified Option A modal path + human privacy checkpoint (Smoke
#2/#3), then on the ``num_vehicles_drivers`` step:

- PROBES the actual visible labels of the two count inputs and STOPS unless
  they clearly mean "number of vehicles" / "number of drivers",
- derives the counts from the canonical profile collections
  (``len(product_data.vehicles)`` / ``len(product_data.drivers)``) through the
  generic ``transform: collection_length`` mechanism (no separate count
  fields, no insurer-specific code),
- fills vehicles-count = 1, drivers-count = 1, leaves the safe-driving
  checkbox UNTOUCHED, clicks ``#ss-auto-interstitial-next-btn`` ("Next: vehicle
  details"), then inspects the resulting page ONLY (nothing filled there).

Authorized values (controlled pilot, NO PII): province = Ontario, vehicle
count = 1, driver count = 1 (non-sensitive, derived). No name/DOB/postal/
address/licence/VIN/email/phone/claims are filled into the site.

NOTE on the pilot profile: the canonical schema REQUIRES identity on
VehicleInformation (a validated 17-char VIN) and DriverInformation
(LicenceIdentity), so a zero-identity object is not constructible. The in-memory
pilot profile uses the repo's established clearly-fictional reserved identifiers
("Test Applicant", T0000-..., 1HGCM82633A000000, M0A 0A0). Only the DERIVED
counts (1, 1) are filled into Sonnet; the synthetic identities are never filled,
never stored in session/trace/log metadata, and never logged.

CAPTCHA safety: passive bot protection (bot_protection_present=true) is
expected and is NOT blocked. A visible challenge after any interaction => STOP
immediately (access_control_detected=true, outcome=blocked) - never solve/
retry/reload/circumvent.

Run (from backend/, with backend on the path):

    $env:PYTHONPATH='.'
    .\\.venv\\Scripts\\python.exe demos\\issue_live_sonnet_smoke4.py --slow-ms 1000
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
from urllib.parse import urlsplit

from app.browser.adapters import GenericQuoteSiteAdapter
from app.browser.config import BrowserRouteConfigLoader
from app.browser.detect import PageDetector
from app.browser.fill import transform_value
from app.browser.inspect import PageInspector
from app.browser.manager import BrowserManager
from app.browser.session import live_privacy_context_kwargs
from app.browser.value_provider import IntakeValueSource
from app.models.insurance import (
    AddressInformation,
    ApplicantIdentity,
    ApplicantInformation,
    ChannelType,
    ConsentState,
    ContactInformation,
    InsuranceProfile,
    InsuranceType,
    Province,
    QuoteMode,
)
from app.models.insurance.auto.driver import DriverInformation, LicenceIdentity
from app.models.insurance.auto.profile import AutoInsuranceProfile
from app.models.insurance.auto.vehicle import VehicleIdentity, VehicleInformation
from app.models.insurance.enums import LicenceClass, LicenceStatus
from app.services.intake.catalog import IntakeFieldCatalog
from app.services.intake.consent import ConsentService
from app.services.intake.engine import IntakeEngine
from app.services.intake.session_store import InMemorySessionStore
from app.services.intake.vault import InMemoryProfileVault
from app.services.market_registry import MarketRegistryService
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
PRIVACY_PRIMARY = "#ketch-banner-button-primary"
PRIVACY_SECONDARY = "#ketch-banner-button-secondary"
CONTINUE_SELECTOR = "#province-submit-btn"
VEHICLES_INPUT = "#ss-auto-interstitial-vehicles-input"
DRIVERS_INPUT = "#ss-auto-interstitial-drivers-input"
CHECKBOX = "#ss-auto-num-vehicles-default-shift-checkbox"
NEXT_BTN = "#ss-auto-interstitial-next-btn"
NVD_SIGNATURE = "sonnet_quoting_num_vehicles_drivers"


def _hash_route(url: str) -> str:
    return (urlsplit(url).fragment or "")


def _minimal_pilot_profile() -> InsuranceProfile:
    """One vehicle + one driver with the repo's clearly-fictional identifiers.

    Only the derived counts are ever filled into Sonnet; the synthetic identity
    values live in an in-memory vault for this dev run only and are never
    logged/traced/stored.
    """
    consent = ConsentState(
        consent_timestamp=dt.datetime.now(dt.timezone.utc),
        quote_mode=QuoteMode.LIVE_QUOTE,
        permitted_channels=[ChannelType.EMAIL],
    )
    applicant = ApplicantInformation(
        identity=ApplicantIdentity(legal_name="Test Applicant"),
        contact=ContactInformation(),
        address=AddressInformation(province=Province.ON, postal_code="M0A 0A0"),
    )
    driver = DriverInformation(
        licence=LicenceIdentity(
            name_on_licence="Test Applicant",
            licence_number="T0000-0000000-0000",
            province=Province.ON,
            licence_class=LicenceClass.G,
            status=LicenceStatus.VALID,
            expiry_date=dt.date(2030, 12, 31),
        )
    )
    vehicle = VehicleInformation(
        identity=VehicleIdentity(vin="1HGCM82633A000000", model_year=2022, make="TestMake", model="TestModel")
    )
    return InsuranceProfile(
        insurance_type=InsuranceType.AUTO,
        consent=consent,
        applicant=applicant,
        product_data=AutoInsuranceProfile(drivers=[driver], vehicles=[vehicle]),
    )


def _build_pilot_engine() -> tuple[IntakeEngine, IntakeValueSource, str, InsuranceProfile]:
    """Minimal real engine + in-memory pilot profile (derived counts = 1, 1)."""
    vault = InMemoryProfileVault()
    sessions = InMemorySessionStore()
    engine = IntakeEngine(
        catalog=IntakeFieldCatalog(),
        vault=vault,
        sessions=sessions,
        consent=ConsentService(),
        registry=MarketRegistryService(),
    )
    session, _gate = engine.create_session(InsuranceType.AUTO)
    sid = session.session_id
    profile = _minimal_pilot_profile()
    pid = vault.create(profile)
    sessions.save(session.model_copy(update={"profile_id": pid}))
    engine.grant_route_consent(sid, REGISTRY_ID, [], True)
    return engine, IntakeValueSource(engine), sid, profile


async def _visible(page, selector: str) -> bool:
    try:
        loc = page.locator(selector)
        return bool(await loc.count()) and await loc.first.is_visible()
    except Exception:
        return False


async def _privacy_banner_visible(page) -> bool:
    return await _visible(page, PRIVACY_PRIMARY) or await _visible(page, PRIVACY_SECONDARY)


async def _probe_input_labels(page, selector: str) -> dict:
    """Observed label evidence for one input (no inference from the id)."""
    el = page.locator(selector)
    return await el.evaluate(
        """e => {
        const out = {id: e.id || null, name: e.getAttribute('name') || null,
                     type: e.type || e.tagName.toLowerCase()};
        out.aria_label = e.getAttribute('aria-label');
        out.aria_labelledby = e.getAttribute('aria-labelledby');
        out.aria_describedby = e.getAttribute('aria-describedby');
        const forLabel = e.id ? document.querySelector('label[for="' + e.id + '"]') : null;
        out.label_for_text = forLabel ? (forLabel.innerText || '').trim().slice(0, 100) : null;
        const closest = e.closest('label');
        out.closest_label_text = closest ? (closest.innerText || '').trim().slice(0, 100) : null;
        out.labelledby_texts = [];
        if (out.aria_labelledby) {
            for (const id of out.aria_labelledby.split(/\\s+/)) {
                const ref = document.getElementById(id);
                if (ref) out.labelledby_texts.push((ref.innerText || '').trim().slice(0, 100));
            }
        }
        let anc = e.parentElement, q = null;
        while (anc && anc.tagName !== 'FORM' && !q) {
            const t = (anc.innerText || '').trim();
            if (t && t.length < 200) q = t.slice(0, 140);
            anc = anc.parentElement;
        }
        out.question_container_text = q;
        return out;
    }"""
    )


def _label_candidates(meta: dict) -> list[str]:
    out: list[str] = []
    for key in ("label_for_text", "closest_label_text", "aria_label", "question_container_text"):
        if meta.get(key):
            out.append(meta[key])
    out.extend(meta.get("labelledby_texts") or [])
    return out


async def _wait_route_change(page, prev_hash: str, tries: int = 35) -> bool:
    for _ in range(tries):
        await page.wait_for_timeout(400)
        if _hash_route(page.url) != prev_hash:
            return True
    return False


async def main() -> int:
    parser = argparse.ArgumentParser(description="Sonnet LIVE pilot - Smoke #4 (derived counts + Next)")
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
        print("[smoke4] ERROR: no start_url configured")
        return 2

    engine, source, session_id, profile = _build_pilot_engine()
    veh_binding = next(b for b in cfg.field_bindings if b.external_field_id == "ss-auto-interstitial-vehicles-input")
    drv_binding = next(b for b in cfg.field_bindings if b.external_field_id == "ss-auto-interstitial-drivers-input")
    print(f"[smoke4] route={REGISTRY_ID} start_url={_safe_url(start_url)}")
    print(f"[smoke4] pilot_profile vehicles={len(profile.product_data.vehicles)} "
          f"drivers={len(profile.product_data.drivers)} (derived counts)")
    print(f"[smoke4] headful={not args.headless} slow_ms={args.slow_ms} settle_ms={args.settle_ms}")

    browser = BrowserManager(headless=args.headless, slow_mo=args.slow_ms)
    page = None  # type: ignore[assignment]
    try:
        await browser.start()
    except Exception as exc:
        print(f"[smoke4] browser start failed: {type(exc).__name__}")
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
                print(f"[smoke4] {stage}: VISIBLE CAPTCHA/challenge - STOP IMMEDIATELY "
                      f"(access_control_detected=true, outcome=blocked).")
                return True
            return False

        async def validate_stage(tag: str) -> bool:
            obs = await inspector.inspect(page, 0)
            sig = await detector.page_signature(page, obs, cfg)
            ok_host = _host_allowed(page.url, cfg.allowed_hosts)
            print(f"[smoke4] {tag} host_allowed={ok_host} url={_safe_url(page.url)} "
                  f"signature={sig.signature_id if sig else None} heading={obs.heading!r}")
            return ok_host and not await detector.access_control_detected(page, cfg)

        # --- to the privacy checkpoint (Option A modal path) ---------------
        print("[smoke4] (1) opening canonical SPA ...")
        await page.goto(start_url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(args.settle_ms)

        matched, waited = await _wait_for_ready(page, READINESS_SELECTORS, args.max_wait_ms, args.poll_ms)
        print(f"[smoke4] (2) interactive_content_ready={matched is not None} matched={matched} waited_ms={waited}")
        if matched is None:
            print("[smoke4] hydration timeout - STOP.")
            return 0
        if not await validate_stage("(3) open"):
            return 3

        dialogs = page.locator('[role="dialog"]')
        modal_visible = False
        for i in range(min(await dialogs.count(), 10)):
            try:
                if await dialogs.nth(i).is_visible():
                    modal_visible = True
                    break
            except Exception:
                continue
        print(f"[smoke4] (4) welcome_modal_visible={modal_visible}")
        if not modal_visible:
            print("[smoke4] (4) modal NOT visible - STOP (drift).")
            return 4

        trig = page.locator(MODAL_TRIGGER)
        if not await trig.count() or not await trig.first.is_visible():
            print(f"[smoke4] (5) modal trigger {MODAL_TRIGGER} NOT visible - STOP (drift).")
            return 5
        print(f"[smoke4] (5) clicking {MODAL_TRIGGER} ...")
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
        if not ontario_visible:
            print("[smoke4] (6) Ontario NOT visible - STOP (drift).")
            return 6
        print("[smoke4] (6) selecting Ontario ...")
        await ontario.first.click(timeout=8000)
        await page.wait_for_timeout(400)
        if await stop_if_blocked("(6) after selecting Ontario"):
            return 10

        conf = page.locator(CONFIRM_SELECTOR)
        if not await conf.count() or await conf.first.is_disabled():
            print(f"[smoke4] (7) Confirm {CONFIRM_SELECTOR} missing/disabled - STOP.")
            return 7
        print(f"[smoke4] (7) clicking {CONFIRM_SELECTOR} (Confirm) ...")
        await conf.first.click(timeout=8000)
        await page.wait_for_timeout(1000)
        if await stop_if_blocked("(7) after Confirm"):
            return 10

        banner = await _privacy_banner_visible(page)
        print(f"[smoke4] (8) privacy_banner_detected={banner} human_checkpoint_required=true "
              f"checkpoint_kind=privacy_preference")
        if not banner:
            print("[smoke4] (8) privacy banner NOT detected - STOP (drift).")
            return 8
        print("[smoke4] available_actions=[\"Manage Preferences\", \"I understand\"]")
        print("[smoke4] automation will NOT click the privacy banner (human-mediated checkpoint).")
        print("\nPrivacy checkpoint detected.\n"
              "Please make your choice directly in the browser.\n"
              "Press Enter here after completing the checkpoint.\n", flush=True)
        await asyncio.to_thread(input, "")

        # --- resume ----------------------------------------------------------
        await page.wait_for_timeout(500)
        banner_gone = not await _privacy_banner_visible(page)
        print(f"[smoke4] (9) privacy_checkpoint_completed={banner_gone}")
        if not banner_gone:
            print("[smoke4] (9) privacy banner still visible - STOP (human action incomplete).")
            return 9

        # no visible dialog/overlay should remain
        dialogs_after = page.locator('[role="dialog"]')
        vis_dialogs = 0
        for i in range(min(await dialogs_after.count(), 10)):
            try:
                if await dialogs_after.nth(i).is_visible():
                    vis_dialogs += 1
            except Exception:
                continue
        print(f"[smoke4] (9) visible_dialogs_after_resume={vis_dialogs}")
        if vis_dialogs:
            print("[smoke4] (9) an overlay/dialog still open - STOP; complete it in the browser.")
            return 9

        if not await validate_stage("(10) resumed"):
            return 10

        # --- reach/recognize num_vehicles_drivers -----------------------------
        route = _hash_route(page.url)
        print(f"[smoke4] (11) route_after_resume={route!r}")
        if "num_vehicles_drivers" not in route:
            # maybe still on province with inline Continue - advance once
            cont = page.locator(CONTINUE_SELECTOR)
            if await cont.count() and await cont.first.is_visible() and not await cont.first.is_disabled():
                print("[smoke4] (11) on province step - clicking inline Continue to reach counts page ...")
                await cont.first.click(timeout=8000)
                if not await _wait_route_change(page, _hash_route(page.url)):
                    await page.wait_for_timeout(1500)
            if await stop_if_blocked("(11) after advancing"):
                return 10
        route = _hash_route(page.url)
        if "num_vehicles_drivers" not in route:
            print(f"[smoke4] (11) did NOT reach num_vehicles_drivers (route={route!r}) - STOP (drift).")
            return 11

        obs = await inspector.inspect(page, 0)
        sig = await detector.page_signature(page, obs, cfg)
        print(f"[smoke4] (12) page_signature={sig.signature_id if sig else None} heading={obs.heading!r}")
        if not sig or sig.signature_id != NVD_SIGNATURE:
            print(f"[smoke4] (12) signature mismatch (expected {NVD_SIGNATURE}) - STOP (drift).")
            return 12

        # --- label-probe gate (section 1) --------------------------------------
        veh_meta = await _probe_input_labels(page, VEHICLES_INPUT)
        drv_meta = await _probe_input_labels(page, DRIVERS_INPUT)
        print("[smoke4] (13) vehicle-count label evidence: " + json.dumps(veh_meta, ensure_ascii=False))
        print("[smoke4] (13) driver-count label evidence: " + json.dumps(drv_meta, ensure_ascii=False))
        veh_texts = " ".join(_label_candidates(veh_meta))
        drv_texts = " ".join(_label_candidates(drv_meta))
        veh_clear = bool(veh_texts) and "vehicle" in veh_texts.lower()
        drv_clear = bool(drv_texts) and "driver" in drv_texts.lower()
        print(f"[smoke4] (13) vehicle_label_means_count={veh_clear} driver_label_means_count={drv_clear}")
        if not (veh_clear and drv_clear):
            print("[smoke4] (13) labels do NOT clearly mean number of vehicles / number of drivers - STOP.")
            return 13

        # --- derive + fill (section 7) -------------------------------------------
        veh_count = source.collection_length(session_id, veh_binding.canonical_path)
        drv_count = source.collection_length(session_id, drv_binding.canonical_path)
        veh_text = transform_value(veh_count, veh_binding)
        drv_text = transform_value(drv_count, drv_binding)
        print(f"[smoke4] (14) derived vehicle_count={veh_count} driver_count={drv_count} "
              f"fill_values=({veh_text!r}, {drv_text!r})")
        await page.locator(VEHICLES_INPUT).fill(veh_text)
        await page.locator(DRIVERS_INPUT).fill(drv_text)
        if await stop_if_blocked("(14) after filling counts"):
            return 10
        veh_entered = await page.locator(VEHICLES_INPUT).input_value()
        drv_entered = await page.locator(DRIVERS_INPUT).input_value()
        print(f"[smoke4] (15) entered vehicle_count={veh_entered!r} driver_count={drv_entered!r}")

        # safe-driving checkbox: untouched by design
        try:
            cb_checked = await page.locator(CHECKBOX).is_checked()
        except Exception:
            cb_checked = None
        print(f"[smoke4] (16) safe_driving_checkbox_untouched=True (checked={cb_checked} - not modified)")

        nxt = page.locator(NEXT_BTN)
        if not await nxt.count() or not await nxt.first.is_visible() or await nxt.first.is_disabled():
            print(f"[smoke4] (17) Next {NEXT_BTN} not visible/enabled - STOP (drift).")
            return 17
        print(f"[smoke4] (17) clicking {NEXT_BTN} (Next: vehicle details) ...")
        await nxt.first.click(timeout=8000)
        if await stop_if_blocked("(17) after Next"):
            return 10
        prev_hash = _hash_route(page.url)
        advanced = await _wait_route_change(page, prev_hash)
        if await stop_if_blocked("(17) after navigation"):
            return 10
        await page.wait_for_timeout(1500)
        print(f"[smoke4] (17) route_advanced={advanced}")

        # --- inspect the resulting page ONLY --------------------------------------
        obs1 = await inspector.inspect(page, 0)
        sig1 = await detector.page_signature(page, obs1, cfg)
        host_ok = _host_allowed(page.url, cfg.allowed_hosts)
        print(f"[smoke4] (18) host_allowed={host_ok} url={_safe_url(page.url)} hash_route={_hash_route(page.url)!r}")
        print(f"[smoke4] (18) heading={obs1.heading!r} signature={sig1.signature_id if sig1 else None}")

        print("[smoke4] (18) === visible fields ===")
        for f in obs1.fields[:60]:
            rec = {"id": f.external_field_id, "type": f.control_type, "label": f.label,
                   "name": f.name, "input_type": f.input_type, "placeholder": f.placeholder,
                   "required": f.required, "options": f.options_labels[:8]}
            print("   field " + json.dumps(rec, ensure_ascii=False))
        print("[smoke4] (18) === visible interactive controls ===")
        for el in obs1.interactives[:80]:
            rec = {"name": el.accessible_name, "role": el.role, "type": el.element_type,
                   "id": el.external_id, "disabled": el.disabled, "aria": el.aria}
            print("   interactive " + json.dumps(rec, ensure_ascii=False))

        print(f"[smoke4] (18) access_control={await detector.access_control_detected(page, cfg)}")
        print(f"[smoke4] (18) bot_protection={await detector.bot_protection_detected(page, cfg)}")
        print(f"[smoke4] (18) validation_error={await detector.validation_error_detected(page, cfg)}")
        print(f"[smoke4] (18) callback={await detector.callback_detected(page, cfg)}")
        print(f"[smoke4] (18) quote_detected={await detector.quote_detected(page, cfg) is not None}")
        print("[smoke4] page_errors=" + json.dumps(page_errors[:20], ensure_ascii=False))
        print("[smoke4] console_error_warning=" + json.dumps(console_msgs[:20], ensure_ascii=False))

        print("[smoke4] SMOKE #4 COMPLETE - nothing filled on the resulting page, no further navigation.")
        if args.hold_seconds > 0:
            print(f"[smoke4] holding browser open {args.hold_seconds}s ...")
            await page.wait_for_timeout(args.hold_seconds * 1000)
        return 0
    except Exception as exc:
        print(f"[smoke4] interaction interrupted ({type(exc).__name__}) - STOP (drift/error preserved).")
        if page is not None:
            try:
                print(f"[smoke4] on-stop url={_safe_url(page.url)} "
                      f"host_allowed={_host_allowed(page.url, cfg.allowed_hosts)}")
            except Exception:
                pass
        print("[smoke4] no data filled beyond the derived counts, no PII used, no CAPTCHA solved.")
        return 10
    finally:
        try:
            await browser.stop()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
