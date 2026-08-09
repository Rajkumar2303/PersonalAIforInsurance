"""Issue #7 - repeatable local mock browser demo (safe metadata only).

Runs the Browser Agent against the LOCAL mock quote site (no internet, no real
insurers, no LLM). Uses the real IntakeEngine + RoutePlanner + BrowserSessionManager.

Usage (from backend/, with tests + backend on the path):

    $env:PYTHONPATH='tests;.'
    .\.venv\Scripts\python.exe demos\issue7_browser_demo.py happy
    .\.venv\Scripts\python.exe demos\issue7_browser_demo.py missing
    .\.venv\Scripts\python.exe demos\issue7_browser_demo.py unknown
    .\.venv\Scripts\python.exe demos\issue7_browser_demo.py safety
    .\.venv\Scripts\python.exe demos\issue7_browser_demo.py callback
    .\.venv\Scripts\python.exe demos\issue7_browser_demo.py dynamic
    .\.venv\Scripts\python.exe demos\issue7_browser_demo.py second-route
    .\.venv\Scripts\python.exe demos\issue7_browser_demo.py all

All output is SAFE metadata - never applicant values.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from app.browser.mock_site import MOCK_REGISTRY_ID, MockQuoteSite, build_mock_route_config
from app.browser.session import BrowserExecutionMode
from app.graph.browser_workflow import build_browser_workflow
from app.models.browser.config import BrowserFieldBinding, FillStrategy, MatchPattern, MatchStrategy, TransformKind
from browser_helpers import make_browser_env
from personas import make_standard_auto_profile

ANNUAL_KM = "product_data.vehicles[0].use.annual_kilometres"


async def _run(env, session_id: str, entry: str = "run", max_steps: int = 20) -> dict:
    return await build_browser_workflow(env.manager).ainvoke(
        {"entry": entry, "browser_session_id": session_id, "max_steps": max_steps}
    )


async def _stop(env, session_id: str) -> None:
    try:
        await env.manager.close(session_id)
    except Exception:
        pass
    try:
        await env.browser_manager.stop()
    except Exception:
        pass


def _print_state(label: str, state: dict) -> None:
    print(f"[{label}] status={state.get('workflow_status')} observation={state.get('observation_type')}")


async def happy(tmp_path: Path, site: MockQuoteSite) -> None:
    env = make_browser_env(tmp_path, site)  # STANDARD_COMPLETE_PROFILE
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        _print_state("happy path", state)
        session = env.manager.get(bs.browser_session_id)
        print(f"  known fields autofilled ({len(session.observed_field_ids)}): {sorted(session.observed_field_ids)}")
        raw = env.manager.last_result(bs.browser_session_id).observation.quote.raw
        print(f"  quote annual=${raw.annual_amount_parsed} firm={raw.is_firm_quote} "
              f"reference_present={raw.reference_present} coverage={len(raw.coverage_observations)}")
    finally:
        await _stop(env, bs.browser_session_id)


async def missing(tmp_path: Path, site: MockQuoteSite) -> None:
    env = make_browser_env(
        tmp_path, site,
        persona=make_standard_auto_profile(annual_kilometres=None),
    )  # PROGRESSIVE_INCOMPLETE_PROFILE
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        _print_state("missing-field first", state)
        print(f"  paused pending_paths={env.manager.get(bs.browser_session_id).pending_field_paths}")
        result = env.engine.submit_answer(env.session_id, ANNUAL_KM, 12000)
        print(f"  Issue #5 collected annual_kilometres validation_success={result.validation_success}")
        state2 = await _run(env, bs.browser_session_id, entry="resume")
        _print_state("missing-field resume", state2)
        # ask-once: the field was requested once, then known
        print(f"  requested_fields={env.engine.get_session(env.session_id).requested_fields}")
    finally:
        await _stop(env, bs.browser_session_id)


async def unknown(tmp_path: Path, site: MockQuoteSite) -> None:
    env = make_browser_env(tmp_path, site, scenario="unknown")
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        _print_state("unknown required field", state)
        obs = env.manager.last_result(bs.browser_session_id).observation
        for f in obs.unknown_field_observations:
            print(f"  sanitized label={f.label!r} control_type={f.control_type} required={f.required}")
    finally:
        await _stop(env, bs.browser_session_id)


async def safety(tmp_path: Path, site: MockQuoteSite) -> None:
    for scenario, label in (("captcha", "CAPTCHA/access control"), ("checkpoint", "identity lookup"),
                            ("signature", "signature"), ("bind", "purchase/bind")):
        env = make_browser_env(tmp_path, site, scenario=scenario)
        bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
        try:
            state = await _run(env, bs.browser_session_id)
            _print_state(label, state)
        finally:
            await _stop(env, bs.browser_session_id)


async def callback(tmp_path: Path, site: MockQuoteSite) -> None:
    env = make_browser_env(tmp_path, site, scenario="callback")
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        _print_state("callback/handoff", state)
        print(f"  {env.manager.last_result(bs.browser_session_id).message}")
    finally:
        await _stop(env, bs.browser_session_id)


async def dynamic(tmp_path: Path, site: MockQuoteSite) -> None:
    # Original config over the re-worded page fails to map (proves the change matters)...
    # ...then a CONFIG-only update (new label wording) reaches the quote.
    config = build_mock_route_config(MOCK_REGISTRY_ID, start_url=site.url("/page-b?label=1"))
    new_binding = BrowserFieldBinding(
        external_field_id="annual-km",
        match_patterns=[MatchPattern(strategy=MatchStrategy.LABEL_CONTAINS, value="how far do you drive")],
        canonical_path=ANNUAL_KM, fill_strategy=FillStrategy.INTEGER,
        transform=TransformKind.INTEGER_TO_STRING,
    )
    config = config.model_copy(update={
        "field_bindings": [b for b in config.field_bindings if b.external_field_id != "annual-km"] + [new_binding],
    })
    env = make_browser_env(tmp_path, site, scenario="label-changed", route_config=config)
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        _print_state("config-driven label change (annual-km -> reworded)", state)
    finally:
        await _stop(env, bs.browser_session_id)

    # Second synthetic route, config only - no executor branch.
    env2 = make_browser_env(tmp_path, site, scenario="applicant", registry_id="mock-insurer-2")
    bs2 = env2.manager.create(env2.session_id, "mock-insurer-2", BrowserExecutionMode.SANDBOX)
    try:
        state2 = await _run(env2, bs2.browser_session_id)
        _print_state("second synthetic route (mock-insurer-2, config only)", state2)
    finally:
        await _stop(env2, bs2.browser_session_id)


async def main() -> None:
    scenario = sys.argv[1] if len(sys.argv) > 1 else "all"
    site = MockQuoteSite().start()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        print("mock site:", site.base_url)
        handlers = {
            "happy": happy,
            "missing": missing,
            "unknown": unknown,
            "safety": safety,
            "callback": callback,
            "dynamic": dynamic,
            "second-route": dynamic,
        }
        if scenario == "all":
            for handler in (happy, missing, unknown, safety, callback, dynamic):
                await handler(tmp, site)
        elif scenario in handlers:
            await handlers[scenario](tmp, site)
        else:
            print(f"unknown scenario {scenario!r}; use one of: happy missing unknown safety callback dynamic second-route all")
    site.stop()


if __name__ == "__main__":
    asyncio.run(main())
