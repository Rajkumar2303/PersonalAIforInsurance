"""Issue #7 - HEADFUL visual browser demo against the LOCAL mock quote site.

Watches the REAL Browser Agent (BrowserSessionManager + BrowserExecutor) drive a
visible Chromium window through the local mock insurance website. Synthetic data
only - no internet, no real insurers, no LLM, no safety bypass (sandbox mode
still blocks external requests; allowed hosts = 127.0.0.1/localhost).

Modes:
    happy        -> STANDARD_COMPLETE profile; multi-page journey
                   /page-a (Applicant) -> /page-b (Vehicle) -> /page-c
                   (Commuting) -> /page-d (Your Quote). Shows text, SELECT
                   dropdown (Preferred language), radio (Carpool), checkbox
                   (Winter tires), DATE (Date of birth), integer (Model year /
                   Annual distance), click-through, and the final synthetic
                   quote page.
    conditional  -> SAME STANDARD profile with commuting=Yes; the commute page
                   reveals a conditional field (One-way commute distance) only
                   after "Yes" is chosen (JS conditional reveal).

Usage (from backend/):
    $env:PYTHONPATH='tests;.'
    .\.venv\Scripts\python.exe demos\issue7_browser_visual_demo.py happy
    .\.venv\Scripts\python.exe demos\issue7_browser_visual_demo.py conditional
    .\.venv\Scripts\python.exe demos\issue7_browser_visual_demo.py happy --slow-ms 700 --hold-seconds 20

Options:
    --slow-ms N        Playwright slow_mo delay (ms) between browser actions.
                       Default 700 (0 = no delay). DEV/DEMO only.
    --hold-seconds N   Keep the browser open N seconds after the run so you can
                       inspect the final page. Default: wait for Enter.

All output is SAFE metadata - never applicant values.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from app.browser.mock_site import MOCK_REGISTRY_ID, MockQuoteSite
from app.browser.session import BrowserExecutionMode
from app.models.browser.session import BrowserSessionStatus
from browser_helpers import make_browser_env
from personas import make_standard_auto_profile

# Same terminal set the LangGraph workflow uses to stop the loop.
_TERMINAL = {
    BrowserSessionStatus.PAUSED_NEEDS_FIELD,
    BrowserSessionStatus.PAUSED_NEEDS_CONSENT,
    BrowserSessionStatus.PAUSED_HUMAN_CHECKPOINT,
    BrowserSessionStatus.PAUSED_UNKNOWN_FIELD,
    BrowserSessionStatus.PAUSED_VALUE_NOT_SUPPORTED,
    BrowserSessionStatus.PAUSED_VALIDATION_ERROR,
    BrowserSessionStatus.PAUSED_AMBIGUOUS,
    BrowserSessionStatus.SUCCEEDED,
    BrowserSessionStatus.STOPPED_ACCESS_CONTROL,
    BrowserSessionStatus.STOPPED_HUMAN_CHECKPOINT,
    BrowserSessionStatus.STOPPED_PROHIBITED,
    BrowserSessionStatus.STOPPED_UNEXPECTED_HOST,
    BrowserSessionStatus.FAILED,
    BrowserSessionStatus.CLOSED,
}

COMMUTING_PATH = "product_data.vehicles[0].use.commuting"
RIDESHARE_PATH = "product_data.vehicles[0].special_use.rideshare"


def _path(url: str | None) -> str:
    """Return only the path+query of a URL (safe metadata for the mock site)."""
    if not url:
        return "-"
    return urlsplit(url).path + (f"?{urlsplit(url).query}" if urlsplit(url).query else "")


async def _run_visual(mode: str, slow_ms: int, hold_seconds: int | None) -> None:
    site = MockQuoteSite().start()
    print(f"\n>>> mock insurance site on  {site.base_url}   (synthetic data, sandbox, allowed hosts 127.0.0.1)")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        if mode == "happy":
            scenario, persona = "applicant", None  # STANDARD_COMPLETE profile
        else:  # conditional
            scenario = "chain"
            persona = make_standard_auto_profile(**{
                COMMUTING_PATH: True,   # reveal the conditional field
                RIDESHARE_PATH: False,
            })

        env = make_browser_env(tmp, site, scenario=scenario, persona=persona,
                               headless=False, slow_mo=slow_ms)

        session = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
        if not hasattr(session, "browser_session_id"):
            print(f"route refused: {session}")
            return
        sid = session.browser_session_id

        print(f"\n>>> launching headful Chromium (slow_mo={slow_ms}ms) - watch the window...")
        print(f">>> scenario: {mode} | registry={session.registry_id} | sandbox\n")

        result = await env.manager.start_session(sid)  # navigate to first page + first step
        step_no = 0
        while result is not None:
            step_no += 1
            obs = result.observation
            print(
                f"  step {step_no:>2} | {result.observation_type.value:<26} | "
                f"page={obs.page_signature if obs else '-'} | "
                f"url={_path(obs.url if obs else None)} | "
                f"filled={result.filled_field_count} | status={result.status.value}"
            )
            if result.status in _TERMINAL:
                break
            result = await env.manager.step_session(sid)

        session = env.manager.get(sid)
        print(f"\n  final status     : {session.status.value}")
        print(f"  page signature   : {session.page_signature}")
        print(f"  observed fields  : {len(session.observed_field_ids)} -> {sorted(session.observed_field_ids)}")
        last = env.manager.last_result(sid)
        if last and last.observation and last.observation.quote and last.observation.quote.quote_present:
            raw = last.observation.quote.raw
            print(f"  QUOTE            : annual=${raw.annual_amount_parsed} monthly=${raw.monthly_amount_parsed} "
                  f"firm={raw.is_firm_quote} reference_present={raw.reference_present} "
                  f"coverage_obs={len(raw.coverage_observations)}")

        # Keep the browser open for visual inspection.
        print("\n>>> the browser is OPEN on the last page - inspect it now.")
        if hold_seconds is not None:
            print(f">>> holding for {hold_seconds}s ...")
            await asyncio.sleep(hold_seconds)
        else:
            await asyncio.to_thread(input, ">>> press Enter to close the browser and finish: ")

        await env.manager.close(sid)
        await env.browser_manager.stop()
    site.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Issue #7 headful visual browser demo (local mock site)")
    parser.add_argument("mode", nargs="?", default="happy", choices=["happy", "conditional"])
    parser.add_argument("--slow-ms", type=int, default=700, help="Playwright slow_mo ms between actions (default 700)")
    parser.add_argument("--hold-seconds", type=int, default=None, help="hold open Ns; default waits for Enter")
    args = parser.parse_args()

    print("=" * 74)
    print(" Issue #7  |  HEADFUL Browser Agent demo  |  LOCAL mock quote site  |  synthetic data only")
    print("=" * 74)
    if args.mode == "happy":
        print(" happy  : STANDARD_COMPLETE profile -> applicant -> vehicle -> commute -> QUOTE")
        print("          shows text / select / radio / checkbox / date / integer + multi-page click-through")
    else:
        print(" conditional : STANDARD profile with commuting=Yes -> conditional field revealed -> QUOTE")

    asyncio.run(_run_visual(args.mode, args.slow_ms, args.hold_seconds))

    print("\n>>> done. Re-run yourself from backend/ with:")
    print("    $env:PYTHONPATH='tests;.'")
    print(f"    .\\.venv\\Scripts\\python.exe demos\\issue7_browser_visual_demo.py {args.mode}"
          f" --slow-ms {args.slow_ms} --hold-seconds 20")


if __name__ == "__main__":
    sys.exit(main())
