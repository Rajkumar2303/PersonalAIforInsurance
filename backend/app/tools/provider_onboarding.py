"""Provider onboarding CLI (post-Issue #14 phase).

Deterministic, SAFE provider onboarding from the EXISTING Market Registry.
No applicant data is ever accepted on the CLI; inspection is NO-PII.

Usage (from ``backend/``):

    $env:PYTHONPATH='.'
    .\\.venv\\Scripts\\python.exe -m app.tools.provider_onboarding \
        --registry-id belairdirect --candidate-url https://www.belairdirect.com/auto/

    # Human-gated approval (requires a draft + explicit --yes):
    .\\.venv\\Scripts\\python.exe -m app.tools.provider_onboarding \
        --registry-id belairdirect --approve --yes

Workflow:
    registry -> candidate quote URL -> SAFE inspection -> DRAFT config/report
    -> human review -> --approve (promotes config + marks registry verified).

A draft is NEVER auto-verified. Approval is explicit and human-only.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Optional

from ..services.market_registry import MarketRegistryService
from ..services.onboarding import (
    OnboardingError,
    build_draft_config,
    build_report,
    default_drafts_dir,
    default_live_dir,
    default_registry_path,
    derive_allowed_hosts,
    inspect_page,
    load_draft,
    map_labels,
    mark_unmapped_fields,
    promote_draft,
    save_draft,
    validate_candidate_url,
)
from ..services.onboarding.inspection import print_report


def _registry_entry(registry_path: Optional[Path], registry_id: str):
    service = MarketRegistryService(registry_dir=registry_path.parent if registry_path else None)
    entry = service.get_by_registry_id(registry_id)
    if entry is None:
        raise OnboardingError(
            f"registry_id {registry_id!r} not found in the Market Registry - "
            "refusing to invent a provider record"
        )
    if not entry.active:
        raise OnboardingError(f"registry route {registry_id!r} is inactive")
    return entry


def _resolve_start_url(entry, candidate_url: Optional[str]) -> str:
    start = entry.quote_url or candidate_url
    if not start:
        raise OnboardingError(
            f"no quote URL for {entry.registry_id!r} and no --candidate-url given; "
            "supply a candidate public quote/homepage URL to inspect"
        )
    return validate_candidate_url(start)


async def _run_inspect(args: argparse.Namespace) -> int:
    registry_path = Path(args.registry_path) if args.registry_path else default_registry_path()
    entry = _registry_entry(registry_path, args.registry_id)
    start_url = _resolve_start_url(entry, args.candidate_url)

    print(f"[onboard] {entry.registry_id} ({entry.brand_or_program}) "
          f"distribution={entry.distribution_type.value} status={entry.status.value}")
    print(f"[onboard] candidate_quote_url={start_url} "
          f"headless={args.headless} slow_ms={args.slow_ms}")

    inspection = await inspect_page(
        entry.registry_id,
        start_url,
        headless=args.headless,
        slow_mo_ms=args.slow_ms,
        settle_ms=args.settle_ms,
        max_wait_ms=args.inspect_timeout_ms,
    )

    print(f"[onboard] final_url={inspection.final_url} host_allowed={inspection.host_allowed}")
    print(f"[onboard] heading={inspection.heading!r} "
          f"access_control_detected={inspection.access_control_detected} "
          f"callback_detected={inspection.callback_detected} "
          f"quote_detected={inspection.quote_detected} "
          f"privacy_banner={inspection.privacy_banner_detected} "
          f"hydration_timeout={inspection.hydration_timeout}")

    if inspection.access_control_detected:
        print("[onboard] CAPTCHA/bot protection detected - onboarding interaction STOPPED "
              "(never solved/bypassed). Provider may remain known but unsuitable for "
              "unattended browser quoting.")

    # Deterministic canonical mapping of observed labels (no guessing).
    labels = [(f.get("label") or "", f.get("control_type") or "input") for f in inspection.fields]
    mapped, unmapped = map_labels(labels)
    missing, proposed = mark_unmapped_fields(unmapped)

    # URL pattern for the page signature (host + path, never query).
    from urllib.parse import urlsplit
    parts = urlsplit(inspection.final_url)
    url_pattern = parts.netloc + (parts.path or "/")
    if parts.fragment:
        url_pattern += "#" + parts.fragment

    # Allowed hosts derive from the RAW final host (never a third party).
    allowed_hosts = derive_allowed_hosts(
        f"https://{inspection.final_host}/", entry.registry_id
    )

    aggregator = entry.distribution_type.value == "aggregator"
    draft = build_draft_config(
        entry,
        start_url=start_url,
        allowed_hosts=allowed_hosts,
        heading=inspection.heading,
        url_pattern=url_pattern,
        mapped_fields=mapped,
        observed_buttons=inspection.observed_buttons,
        access_control_detected=inspection.access_control_detected,
        callback_detected=inspection.callback_detected,
        aggregator=aggregator,
    )

    drafts_dir = Path(args.drafts_dir) if args.drafts_dir else default_drafts_dir()
    config_path = save_draft(drafts_dir, entry.registry_id, draft, {})

    report = build_report(
        entry,
        start_url=start_url,
        final_url=inspection.final_url,
        allowed_hosts=allowed_hosts,
        heading=inspection.heading,
        page_signature_ids=inspection.page_signature_ids,
        mapped_fields=mapped,
        unmapped_fields=unmapped,
        observed_buttons=inspection.observed_buttons,
        privacy_banner_detected=inspection.privacy_banner_detected,
        access_control_detected=inspection.access_control_detected,
        callback_detected=inspection.callback_detected,
        quote_detected=inspection.quote_detected,
        draft_path=str(config_path),
        safe_to_live_test=False,  # human approval + verification always required
        aggregator=aggregator,
        canonical_field_missing=missing,
        proposed_fields=proposed,
    )
    # Save the report alongside the draft config.
    from ..services.onboarding.repository import report_path
    report_path(drafts_dir, entry.registry_id).write_text(
        __import__("json").dumps(report, indent=2), encoding="utf-8"
    )
    print("[onboard] DRAFT saved (NOT verified). Human approval required before any "
          "applicant data may be sent.")
    print_report(report)
    return 0


def _run_approve(args: argparse.Namespace) -> int:
    drafts_dir = Path(args.drafts_dir) if args.drafts_dir else default_drafts_dir()
    live_dir = Path(args.live_dir) if args.live_dir else default_live_dir()
    registry_path = Path(args.registry_path) if args.registry_path else default_registry_path()

    draft = load_draft(drafts_dir, args.registry_id)
    if draft is None:
        raise OnboardingError(
            f"no draft for {args.registry_id!r} - run onboarding first, then --approve"
        )
    entry = _registry_entry(registry_path, args.registry_id)

    print(f"[approve] {entry.registry_id} ({entry.brand_or_program})")
    print(f"[approve] start_url={draft.start_url}")
    print(f"[approve] allowed_hosts={draft.allowed_hosts}")
    print(f"[approve] field_bindings={len(draft.field_bindings)} "
          f"page_signatures={len(draft.page_signatures)} "
          f"registry_status={entry.status.value}")
    print("[approve] This promotes the DRAFT to the LIVE config dir and marks the "
          "registry route verified. It does NOT send any applicant data.")

    if not args.yes:
        confirm = input("Type 'yes' to approve this draft: ").strip().lower()
        if confirm != "yes":
            raise OnboardingError("approval aborted - no changes made")

    result = promote_draft(
        drafts_dir=drafts_dir,
        live_dir=live_dir,
        registry_path=registry_path,
        registry_id=args.registry_id,
        confirmed=True,
        source_url=draft.start_url,
        evidence_artifact="provider_onboarding_human_approval",
    )
    print(f"[approve] DONE live_config_path={result['live_config_path']} "
          f"verified_at={result['verified_at']}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Provider onboarding utility")
    parser.add_argument("--registry-id", required=True, help="Market Registry registry_id")
    parser.add_argument("--candidate-url", help="Candidate quote/homepage URL (public, no PII)")
    parser.add_argument("--headless", action="store_true", help="Run headless (default: headful)")
    parser.add_argument("--slow-ms", type=int, default=300, help="Playwright per-action delay (ms)")
    parser.add_argument("--settle-ms", type=int, default=4000, help="SPA settle wait (ms)")
    parser.add_argument("--inspect-timeout-ms", type=int, default=20000, help="Readiness wait (ms)")
    parser.add_argument("--approve", action="store_true", help="Human-gated draft approval")
    parser.add_argument("--yes", action="store_true", help="Non-interactive approval (explicit)")
    parser.add_argument("--drafts-dir", help="Override drafts directory (tests)")
    parser.add_argument("--live-dir", help="Override live config directory (tests)")
    parser.add_argument("--registry-path", help="Override registry JSON path (tests)")
    args = parser.parse_args(argv)

    try:
        if args.approve:
            return _run_approve(args)
        return asyncio.run(_run_inspect(args))
    except OnboardingError as exc:
        print(f"[onboard] ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover - interactive abort
        print("\n[onboard] interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
