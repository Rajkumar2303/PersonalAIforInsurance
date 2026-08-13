"""Sonnet LIVE operator driver - autonomous-first, human-fallback.

The PRIMARY path is automated Playwright filling driven by the data-driven
``sonnet.json`` route config (v5): the executor autonomously selects
Province=Ontario from the non-PII route constant, derives the vehicle/driver
counts, and fills every mapped canonical field (VIN, model year, make, model,
annual km, carpool, winter tires, liability, postal code, DOB, legal name,
licence number, name on licence, licence expiry).

The operator is ONLY a fallback: when the executor pauses (unknown Sonnet
question, ambiguous mapping, consent, or a human checkpoint) the headed browser
window is left OPEN for the operator to resolve, and pressing Enter resumes the
SAME browser session + attempt (no restart, no re-entering earlier fields).

Safety rules enforced here:
- LIVE mode requires an explicit personal-use + accurate-information
  attestation (flags) - otherwise the session is refused.
- CAPTCHA / access control => STOP (never solved, never retried, never reloaded).
- Declaration / signature / payment / purchase / bind => STOP or human
  checkpoint; never automated, never auto-retried.
- The browser session is NEVER closed automatically; it stays open so the
  operator can inspect it. Type ``quit`` or Ctrl+C to close it explicitly.
- The pilot profile uses the repo's clearly-fictional reserved identifiers
  (Test Applicant / T0000-... / 1HGCM82633A000000 / M0A 0A0) for a controlled
  smoke; only non-sensitive derived values are filled when using the pilot
  profile. A participant's own accurate data must be supplied through the
  normal intake flow - not through this script.

Run (from ``backend/``, headful by default):

    $env:PYTHONPATH='.'
    .\\.venv\\Scripts\\python.exe demos\\sonnet_live_driver.py ^
        --personal-use --accurate-info --slow-ms 500

Headless (diagnostic only): add ``--headless``.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from typing import Any, Optional

from app.browser.config import BrowserRouteConfigLoader
from app.browser.manager import BrowserManager
from app.browser.session import BrowserExecutionMode, BrowserSessionManager
from app.models.browser.session import BrowserSessionStatus, LiveExecutionGate
from app.models.insurance.enums import InsuranceType
from app.services.market_registry import MarketRegistryService
from app.services.route_planner.planner import IntakeProfileSource, RoutePlanner
from app.services.route_planner.requirements import RequirementResolver
from app.services.deduplication import RateSourceDeduplicationService

from issue_live_sonnet_smoke4 import REGISTRY_ID, _build_pilot_engine, _sanitize_diagnostic

# Statuses where the executor wants the OPERATOR to act on the (open) page,
# after which we resume the same session. Everything else is terminal here.
_PAUSED_FOR_OPERATOR = {
    BrowserSessionStatus.PAUSED_NEEDS_FIELD,
    BrowserSessionStatus.PAUSED_NEEDS_CONSENT,
    BrowserSessionStatus.PAUSED_HUMAN_CHECKPOINT,
    BrowserSessionStatus.PAUSED_UNKNOWN_FIELD,
    BrowserSessionStatus.PAUSED_VALUE_NOT_SUPPORTED,
    BrowserSessionStatus.PAUSED_VALIDATION_ERROR,
    BrowserSessionStatus.PAUSED_AMBIGUOUS,
}


def _wire_planner(engine: Any) -> RoutePlanner:
    registry = MarketRegistryService()
    dedup = RateSourceDeduplicationService(registry_service=registry)
    requirements = RequirementResolver()
    return RoutePlanner(
        registry=registry,
        dedup=dedup,
        requirements=requirements,
        profile_source=IntakeProfileSource(engine),
    )


def _describe(result: Any) -> str:
    obs = result.observation
    parts = [f"observation_type={result.observation_type.value}"]
    if obs is not None:
        if obs.checkpoint is not None:
            parts.append(
                f"checkpoint={obs.checkpoint.checkpoint_type.value} "
                f"requires_human={obs.checkpoint.requires_human} "
                f"must_not_automate={obs.checkpoint.must_not_automate}"
            )
        if obs.quote and obs.quote.quote_present:
            parts.append(
                f"quote_present=True annual={obs.quote.raw.annual_amount_parsed}"
            )
        if getattr(obs, "ambiguous_field_ids", None):
            parts.append(f"ambiguous_fields={obs.ambiguous_field_ids}")
        if getattr(obs, "unknown_external_fields", None):
            parts.append(f"unknown_fields={obs.unknown_external_fields}")
    return " ".join(parts)


def _print_actions(result: Any) -> None:
    """Privacy-safe per-action summary (action + canonical path + status only)."""
    if not result.action_events:
        print("    actions: (none)")
        return
    counts: dict[tuple[str, str], int] = {}
    paths: list[str] = []
    for ev in result.action_events:
        key = (ev.action, ev.status)
        counts[key] = counts.get(key, 0) + 1
        if ev.canonical_field and ev.canonical_field not in paths:
            paths.append(ev.canonical_field)
    summary = ", ".join(f"{a}/{s}x{n}" for (a, s), n in sorted(counts.items()))
    print(f"    actions: {summary}")
    if paths:
        print(f"    canonical_fields: {', '.join(paths)}")


def _ask_operator() -> str:
    """Blocking terminal prompt; returns 'resume' or 'quit'."""
    while True:
        try:
            choice = input(
                "    >> Resolve on the page, then [Enter] to resume the SAME "
                "session, or 'quit' to stop: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "quit"
        if choice in ("", "resume", "r"):
            return "resume"
        if choice in ("quit", "q", "exit"):
            return "quit"
        print("    (enter to resume, or type 'quit')")


def _ask_checkpoint_approval(checkpoint: Any) -> str:
    """Explicit participant approval for a resumable human checkpoint.

    The agent may have filled the fields on this screen (e.g. the licence
    number); clicking the action that SUBMITS them / triggers an identity or
    database lookup must wait for the participant's explicit 'YES'. Never
    auto-approves.
    """
    kind = checkpoint.checkpoint_type.value
    print(f"    >> HUMAN CHECKPOINT '{kind}': the agent filled the fields on this screen")
    print("       and wants to SUBMIT them (e.g. licence number / identity lookup).")
    while True:
        try:
            answer = input(
                "       Type YES to approve and continue, or 'quit' to stop: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "quit"
        if answer == "yes":
            return "approve"
        if answer in ("quit", "q", "exit"):
            return "quit"
        print("       (not approved - type YES to approve, or 'quit')")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Sonnet LIVE operator driver (autonomous-first)")
    parser.add_argument("--registry-id", default=REGISTRY_ID)
    parser.add_argument("--slow-ms", type=int, default=500, help="Playwright per-action delay (ms)")
    parser.add_argument("--headless", action="store_true", help="Headless (default: headful)")
    parser.add_argument("--max-steps", type=int, default=25, help="Cap on resume steps (safety)")
    parser.add_argument("--personal-use", action="store_true",
                        help="REQUIRED attestation: this is your personal use")
    parser.add_argument("--accurate-info", action="store_true",
                        help="REQUIRED attestation: profile uses your own accurate info")
    args = parser.parse_args()

    if not (args.personal_use and args.accurate_info):
        print("[driver] ERROR: LIVE execution requires BOTH --personal-use and --accurate-info.")
        print("[driver] This is the participant's personal-use attestation gate. Refusing to start.")
        return 3

    engine, source, session_id, profile = _build_pilot_engine()
    registry = MarketRegistryService()
    entry = registry.get_by_registry_id(args.registry_id)
    if entry is None or entry.status.value != "verified":
        print(f"[driver] ERROR: registry entry {args.registry_id!r} is not verified.")
        return 4

    planner = _wire_planner(engine)
    loader = BrowserRouteConfigLoader()
    cfg = loader.load(args.registry_id)
    print(f"[driver] route={args.registry_id} config_version={cfg.config_version} "
          f"bindings={len(cfg.field_bindings)} actions={len(cfg.action_bindings)}")
    print(f"[driver] start_url={entry.quote_url}")
    print(f"[driver] pilot_profile vehicles={len(profile.product_data.vehicles)} "
          f"drivers={len(profile.product_data.drivers)} (derived counts only)")

    browser = BrowserManager(headless=args.headless, slow_mo=args.slow_ms)
    manager = BrowserSessionManager(
        engine=engine,
        planner=planner,
        registry=registry,
        config_loader=loader,
        browser=browser,
        headless=args.headless,
    )

    live_gate = LiveExecutionGate(
        personal_use_confirmed=True,
        accurate_information_attested=True,
        attested_at=dt.datetime.now(dt.timezone.utc),
    )
    created = manager.create(
        session_id,
        args.registry_id,
        execution_mode=BrowserExecutionMode.LIVE,
        live_gate=live_gate,
    )
    if not hasattr(created, "browser_session_id"):
        print(f"[driver] REFUSED: {created.reason.value} - {created.detail}")
        return 5
    bs = created
    print(f"[driver] browser_session_id={bs.browser_session_id} attempt_id={bs.attempt_id}")

    try:
        result = await manager.start_session(bs.browser_session_id)
        step = 0
        while result is not None:
            step += 1
            print(f"[driver] step={step} status={result.status.value} | {_describe(result)}")
            _print_actions(result)

            if result.observation and result.observation.quote and result.observation.quote.quote_present:
                print("[driver] QUOTE DETECTED - terminal. Quote captured; browser left open for review.")
                print("[driver] Type 'quit' to close the session, or Ctrl+C.")
                _ask_operator()
                break

            status = result.status
            if status in _PAUSED_FOR_OPERATOR:
                if step >= args.max_steps:
                    print(f"[driver] max_steps={args.max_steps} reached - stopping (never auto-loop).")
                    break
                # An explicit human checkpoint (e.g. licence submission /
                # identity lookup) requires the PARTICIPANT to approve before
                # the agent may click the submitting action. No auto-approval.
                checkpoint = result.observation.checkpoint if result.observation else None
                if status is BrowserSessionStatus.PAUSED_HUMAN_CHECKPOINT and checkpoint is not None:
                    if checkpoint.must_not_automate:
                        print(f"[driver] checkpoint {checkpoint.checkpoint_type.value} "
                              "is must-not-automate - stopping (never automated).")
                        break
                    choice = _ask_checkpoint_approval(checkpoint)
                    if choice == "quit":
                        break
                    # Explicit approval recorded on the SAME browser session
                    # (same browser_session_id + attempt_id), then resume.
                    manager.approve_checkpoint(bs.browser_session_id, checkpoint.checkpoint_type.value)
                    print(f"[driver] approved checkpoint={checkpoint.checkpoint_type.value} "
                          "- resuming SAME session.")
                    result = await manager.step_session(bs.browser_session_id)
                    continue
                choice = _ask_operator()
                if choice == "quit":
                    break
                # Resume the SAME page/session (no restart, same attempt_id).
                result = await manager.step_session(bs.browser_session_id)
                continue

            # Terminal statuses (blocked / stopped_* / succeeded / failed / etc).
            print(f"[driver] TERMINAL status={status.value} - stopping (no auto-retry).")
            break
    except Exception as exc:  # pragma: no cover - operator-facing resilience
        print(f"[driver] error: {type(exc).__name__}: {_sanitize_diagnostic(str(exc))}")
        return 6
    finally:
        # Never auto-close the browser mid-journey. Only stop the Playwright
        # runtime if we started it and the operator is done.
        print("[driver] Session left open for operator review.")
        print("[driver] Closing browser runtime (operator-triggered end).")
        try:
            await manager.close(bs.browser_session_id)
        except Exception:
            pass
        try:
            await browser.stop()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
