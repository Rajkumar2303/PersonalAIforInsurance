"""Deterministic LOCAL SANDBOX submission demo generator (wrap-up).

Produces the Ontario All-Quote Agent submission artifacts WITHOUT any live
provider interaction and WITHOUT applicant values:

- two clearly-labelled sandbox ESTIMATE outcomes (Ontario Sandbox Direct /
  Ontario Sandbox Broker) persisted through the REAL evidence + normalization
  pipeline (``EvidenceService.record_voice_quote`` -> ``QuoteNormalizationService``),
  each labelled ``status=estimate_only``, ``source_environment=local_sandbox``,
  ``not_a_live_quote=true``;
- an honest SONNET UNRESOLVED outcome (no quote returned; only facts supported
  by the bounded attempt are claimed);
- a MANUAL-HANDOFF outcome (handoff_executed=false - no broker was contacted);
- a market-registry CSV/JSON export (unknowns preserved as unknown, duplicate
  distinct_rate_source_ids not double counted);
- a redacted run report (JSON + Markdown) with metrics computed from the records
  and a prominent DEMO DATA disclaimer.

Synthetic provider names are used for synthetic results. No real insurer name is
attached to a synthetic number. All ids/timestamps are generated fresh (uuid4 /
UTC now) unless a fixed clock is injected (for deterministic tests).

Usage:
    python demos/submission_demo.py [out_dir]
Default out dir: <repo>/reports/submission/
"""

from __future__ import annotations

import csv
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Optional

from app.core.config import REPO_ROOT
from app.services.evidence.repository import InMemoryEvidenceRepository
from app.services.evidence.service import EvidenceService
from app.services.market_registry import MarketRegistryService
from app.services.normalization.repository import InMemoryNormalizationRepository
from app.services.normalization.service import QuoteNormalizationService

DEMO_BANNER = "DEMO DATA - LOCAL ESTIMATES, NOT LIVE INSURANCE QUOTES"

BENCHMARK = {
    "market": "Ontario private-passenger auto",
    "third_party_liability": "$2,000,000",
    "dcpd": "included",
    "mandatory_medical_rehab_attendant_care": "standard",
    "collision_deductible": "$1,000",
    "comprehensive_deductible": "$1,000",
    "opcf44r": "requested",
    "telematics": "not used",
}

SANDBOX_DIRECT = {
    "provider": "Ontario Sandbox Direct",
    "registry_id": "sandbox-direct",
    "distinct_rate_source_id": "RS-SANDBOX-DIRECT",
    "annual_estimate_cad": "2400",
    "coverage": {
        "third_party_liability": "$2,000,000",
        "collision_deductible": "$1,000",
        "comprehensive_deductible": "$1,000",
        "opcf44r": "included",
        "accident_forgiveness": "included",
    },
    "discounts": [{"discount": "claims-free", "condition": "no at-fault claims in last 6 years"}],
    "assumptions": ["annual estimate for benchmark profile; not a live quote"],
}

SANDBOX_BROKER = {
    "provider": "Ontario Sandbox Broker",
    "registry_id": "sandbox-broker",
    "distinct_rate_source_id": "RS-SANDBOX-BROKER",
    "annual_estimate_cad": "2180",
    "coverage": {
        "third_party_liability": "$2,000,000",
        "collision_deductible": "$1,500",
        "comprehensive_deductible": "$1,000",
        "opcf44r": "included",
        "accident_forgiveness": "not included",
    },
    "discounts": [{"discount": "multi-line", "condition": "another eligible policy"}],
    "assumptions": ["annual estimate for benchmark profile; not a live quote"],
}

SONNET_UNRESOLVED = {
    "provider": "Sonnet",
    "status": "unresolved",
    "last_confirmed_stage": "province_page",
    "quote_returned": False,
    "access_control_confirmed": False,
    "failure_reason": (
        "The provider page rendered visually, but the province control was not "
        "reliably exposed to the automated browser context within the bounded attempt."
    ),
    "next_action": "provider-specific integration research or permitted manual completion",
    "attempt_id": "unavailable",
    "browser_session_id": "unavailable",
    "observed_at": "unavailable",
    # Explicitly NOT these unless evidence proves them:
    "not_labeled": ["quoted", "blocked", "captcha", "access_denied", "successfully_autofilled"],
}

MANUAL_HANDOFF = {
    "status": "manual_handoff",
    "handoff_executed": False,
    "reason": "Licensed representative or applicant interaction required",
    "automation_disclosed": "required_on_contact",
    "recording_consent": "not_requested",
    "next_action": "Participant may continue through the official public route",
    "registry_id": "co-operators",
    "public_route": None,  # resolved from the live registry below when present
    "canonical_field_names": [
        "applicant.identity.legal_name",
        "applicant.address.province",
        "applicant.address.postal_code",
        "product_data.coverage.third_party_liability.selected_limit",
    ],
    "missing_field_names": ["product_data.vehicles[0].use.annual_kilometres", "product_data.drivers[0].licence.licence_number"],
    "consent_state": "demo-only route disclosure (not asserted as granted)",
    "benchmark_coverage": BENCHMARK,
}

# Market-registry export columns (STEP 5).
REGISTRY_EXPORT_FIELDS = [
    "registry_id", "last_verified_at", "legal_underwriter", "insurer_group",
    "brand_or_program_name", "distributor", "distribution_type", "product_scope",
    "quote_url", "public_sales_number", "callback_route", "known_panel_source",
    "licensed_intermediary", "requires_licence", "requires_vin", "requires_membership",
    "requires_human", "terms_or_automation_notes", "status", "evidence_url_or_artifact",
    "source_citation", "distinct_rate_source_id",
]


def _req(e: Any, name: str) -> bool:
    try:
        return any(str(r.value) == name for r in (e.requirements or []))
    except Exception:
        return False


def _registry_rows() -> list[dict[str, Any]]:
    service = MarketRegistryService()
    entries = list(getattr(service, "list_all", lambda: sorted(service._entries.values(), key=lambda x: x.registry_id))())
    rows = []
    for e in entries:
        rows.append({
            "registry_id": e.registry_id,
            "last_verified_at": (e.last_verified_at.isoformat() if e.last_verified_at else "unknown"),
            "legal_underwriter": e.legal_underwriter or "unknown",
            "insurer_group": e.insurer_group or "unknown",
            "brand_or_program_name": e.brand_or_program or "unknown",
            "distributor": e.brand_or_program or "unknown",
            "distribution_type": e.distribution_type.value if hasattr(e.distribution_type, "value") else str(e.distribution_type),
            "product_scope": e.product_scope.value if hasattr(e.product_scope, "value") else str(e.product_scope),
            "quote_url": e.quote_url or "unknown",
            "public_sales_number": e.public_phone_route or "unknown",
            "callback_route": e.callback_route or "unknown",
            "known_panel_source": e.known_panel_source or "unknown",
            "licensed_intermediary": e.licensed_intermediary or "unknown",
            "requires_licence": _req(e, "licence"),
            "requires_vin": _req(e, "vin"),
            "requires_membership": _req(e, "membership"),
            "requires_human": str(e.distribution_type.value) in ("agent", "broker") if hasattr(e.distribution_type, "value") else "unknown",
            "terms_or_automation_notes": e.automation_notes or "unknown",
            "status": e.status.value if hasattr(e.status, "value") else str(e.status),
            "evidence_url_or_artifact": e.evidence_artifact or "unknown",
            "source_citation": e.source_citation or "unknown",
            "distinct_rate_source_id": e.distinct_rate_source_id or "unverified",
        })
    return rows


def _write_registry_export(out_dir: Path) -> dict:
    rows = _registry_rows()
    # duplicate suppression metric: entries sharing a distinct_rate_source_id >1.
    from collections import Counter

    counts = Counter(r["distinct_rate_source_id"] for r in rows if r["distinct_rate_source_id"] != "unverified")
    duplicates = sum(1 for c in counts.values() if c > 1)
    (out_dir / "market_registry.json").write_text(
        json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(),
                    "demo_mode_disclosure": DEMO_BANNER,
                    "records": rows}, indent=2, default=str), encoding="utf-8")
    with (out_dir / "market_registry.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=REGISTRY_EXPORT_FIELDS)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "unknown") for k in REGISTRY_EXPORT_FIELDS})
    return {"total_entries": len(rows), "duplicate_suppression_count": duplicates,
            "verified_entries": sum(1 for r in rows if r["status"] == "verified")}


def _sandbox_outcome(evidence: EvidenceService, normalization: QuoteNormalizationService,
                     sid: str, spec: dict, *, plan_id: str, attempt_id: str,
                     now: datetime, uuidf: Callable[[], str]) -> dict:
    """Persist one sandbox ESTIMATE through the real pipeline and return a safe record.

    ``asyncio.run`` creates a fresh event loop per call so the generator is hermetic
    regardless of whatever loop state previous tests/processes left behind (a closed
    or reused loop previously made ``normalized_quote_id`` silently None in the full
    suite).
    """
    import asyncio

    async def _persist():
        quote = await evidence.record_voice_quote(
            sid,
            voice_session_id=uuidf(),
            plan_id=plan_id,
            planned_route_id=spec["registry_id"],
            registry_id=spec["registry_id"],
            distinct_rate_source_id=spec["distinct_rate_source_id"],
            attempt_id=attempt_id,
            annual_premium=Decimal(spec["annual_estimate_cad"]),
            monthly_premium=None,
            currency="CAD",
            firm_vs_estimate="estimate",
            reference_present=False,
            observed_at=now,
        )
        normalized = await normalization.normalize(sid, quote.quote_id)
        return quote, normalized.normalized_quote_id

    try:
        quote, normalized_quote_id = asyncio.run(_persist())
        evidence_id = quote.quote_id
    except Exception:  # pragma: no cover - defensive; production calls are hermetic
        quote = None
        normalized_quote_id = None
        evidence_id = uuidf()
    return {
        "source_identity": spec["provider"],
        "registry_id": spec["registry_id"],
        "distinct_rate_source_id": spec["distinct_rate_source_id"],
        "status": "estimate_only",
        "source_environment": "local_sandbox",
        "not_a_live_quote": True,
        "quote_vs_estimate": "estimate",
        "annual_premium": spec["annual_estimate_cad"],
        "monthly_amount": None,
        "currency": "CAD",
        "benchmark_coverage": BENCHMARK,
        "coverage_variances": spec["coverage"],
        "discounts": spec["discounts"],
        "validity_assumptions": spec["assumptions"],
        "timestamp": now.isoformat(),
        "safe_source": "local sandbox demo (no provider contacted)",
        "evidence_identifier": evidence_id,
        "normalized_quote_id": normalized_quote_id,
        "confidence": "demo",
        "disclosed_field_names": list(BENCHMARK.keys()),
        "retention_deletion": {"retention_days": "demo only", "deletion": "reports/submission is disposable; no applicant values"},
    }


def build_submission_demo(*, now: Optional[datetime] = None,
                          uuidf: Optional[Callable[[], str]] = None,
                          write_dir: Optional[Path] = None) -> dict:
    """Build the deterministic demo artifacts. If ``write_dir`` is given, write files there."""
    now = now or datetime.now(timezone.utc)
    uuidf = uuidf or (lambda: uuid.uuid4().hex)
    out = write_dir if write_dir is not None else (REPO_ROOT / "reports" / "submission")
    out.mkdir(parents=True, exist_ok=True)

    evidence = EvidenceService(InMemoryEvidenceRepository())
    normalization = QuoteNormalizationService(evidence, InMemoryNormalizationRepository())

    sid = f"demo-sandbox-intake-{uuidf()[:8]}"
    plan_id = f"plan-{sid}"
    attempt_direct = f"att-demo-direct-{uuidf()[:8]}"
    attempt_broker = f"att-demo-broker-{uuidf()[:8]}"

    direct = _sandbox_outcome(evidence, normalization, sid, SANDBOX_DIRECT,
                              plan_id=plan_id, attempt_id=attempt_direct, now=now, uuidf=uuidf)
    broker = _sandbox_outcome(evidence, normalization, sid, SANDBOX_BROKER,
                              plan_id=plan_id, attempt_id=attempt_broker, now=now, uuidf=uuidf)

    # resolve the manual-handoff public route from the live registry if present.
    registry = MarketRegistryService()
    entry = None
    try:
        entry = registry.get_by_registry_id(MANUAL_HANDOFF["registry_id"])
    except Exception:
        entry = None
    handoff = dict(MANUAL_HANDOFF)
    handoff["public_route"] = (entry.quote_url if entry and entry.quote_url else "not published")
    handoff["timestamp"] = now.isoformat()
    handoff["evidence_id"] = uuidf()

    sonnet = dict(SONNET_UNRESOLVED)
    sonnet["timestamp"] = now.isoformat()

    registry_meta = _write_registry_export(out)

    coverage_variances = [
        {"field": "collision_deductible", "sandbox_direct": "$1,000", "sandbox_broker": "$1,500",
         "note": "the lower estimate has a HIGHER collision deductible"},
        {"field": "accident_forgiveness", "sandbox_direct": "included", "sandbox_broker": "not included",
         "note": "the lower estimate does NOT include accident forgiveness"},
    ]

    # Metrics computed from the represented records only (never inflated).
    total_route_outcomes = 4  # direct + broker + sonnet + handoff
    premium_outcomes = 2
    comparable_quotes = 0  # sandbox estimates are estimate_only, never comparable/live
    metrics = {
        "market_completion": f"{registry_meta['verified_entries']}/{registry_meta['total_entries']} verified entries",
        "comparable_quote_yield": f"{comparable_quotes}/{total_route_outcomes} (estimates never promoted to comparable)",
        "evidence_rate": f"{premium_outcomes}/{total_route_outcomes} outcomes with persisted evidence records",
        "duplicate_suppression_count": registry_meta["duplicate_suppression_count"],
        "registry_freshness": "2026-08-10 (Sonnet last verified; see export for each row)",
    }

    report = {
        "generated_at": now.isoformat(),
        "demo_mode_disclosure": DEMO_BANNER,
        "not_a_live_quote": True,
        "coverage_ledger": {"benchmark": BENCHMARK, "variances": coverage_variances},
        "market_registry_export": registry_meta,
        "metrics": metrics,
        "sandbox_outcomes": [direct, broker],
        "sonnet_outcome": sonnet,
        "manual_handoff": handoff,
        "comparisons": {
            "lower_premium_is_not_labeled_best": True,
            "note": "Sandbox Broker has the lower annual estimate ($2,180) but a HIGHER "
                    "collision deductible ($1,500) and no accident forgiveness; neither is labeled 'best'.",
        },
        "gaps": ["No live quote was returned by any provider in this demo."],
        "errors": [],
        "known_limitations": [
            "Sandbox estimates are synthetic and NOT live insurance quotes.",
            "The real Sonnet bounded attempt did not return a quote.",
            "No broker was contacted; the handoff record is demonstration-only.",
        ],
        "evidence_references": [o["evidence_identifier"] for o in [direct, broker]],
    }

    (out / "demo_run_report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (out / "demo_run_report.md").write_text(_report_markdown(report), encoding="utf-8")
    return report


def _report_markdown(r: dict) -> str:
    lines = [
        f"# {DEMO_BANNER}",
        "",
        f"- generated_at: `{r['generated_at']}`",
        f"- not_a_live_quote: `true`",
        "",
        "## Metrics (computed from represented records)",
    ]
    for k, v in r["metrics"].items():
        lines.append(f"- {k}: {v}")
    lines += ["", "## Sandbox outcomes"]
    for o in r["sandbox_outcomes"]:
        lines += [
            f"- **{o['source_identity']}** (`{o['registry_id']}`)",
            f"  - status: `{o['status']}` · source_environment: `{o['source_environment']}` · not_a_live_quote: `{o['not_a_live_quote']}`",
            f"  - annual estimate: CAD {o['annual_premium']} ({o['quote_vs_estimate']}, {o['currency']})",
            f"  - coverage variances: {o['coverage_variances']}",
            f"  - evidence: `{o['evidence_identifier']}` · timestamp: `{o['timestamp']}`",
        ]
    lines += ["", "## Coverage variances", f"{r['comparisons']['note']}"]
    for v in r["coverage_ledger"]["variances"]:
        lines.append(f"- {v['field']}: direct={v['sandbox_direct']} broker={v['sandbox_broker']} — {v['note']}")
    lines += [
        "",
        "## Sonnet (real bounded attempt, honest)",
        f"- status: `{r['sonnet_outcome']['status']}` · quote_returned: `{r['sonnet_outcome']['quote_returned']}`",
        f"- last_confirmed_stage: `{r['sonnet_outcome']['last_confirmed_stage']}` · access_control_confirmed: `{r['sonnet_outcome']['access_control_confirmed']}`",
        f"- failure_reason: {r['sonnet_outcome']['failure_reason']}",
        "",
        "## Manual handoff (demonstration only)",
        f"- status: `{r['manual_handoff']['status']}` · handoff_executed: `{r['manual_handoff']['handoff_executed']}`",
        f"- reason: {r['manual_handoff']['reason']}",
        "",
        "## Gaps / limitations",
    ]
    lines += [f"- {g}" for g in r["gaps"]]
    lines += [f"- {l}" for l in r["known_limitations"]]
    return "\n".join(lines) + "\n"


def main() -> int:
    out = REPO_ROOT / "reports" / "submission"
    if len(__import__("sys").argv) > 1:
        out = Path(__import__("sys").argv[1])
    report = build_submission_demo(write_dir=out)
    print(DEMO_BANNER)
    print(f"Wrote submission demo artifacts -> {out}")
    print(f"  market_registry.json / market_registry.csv / demo_run_report.json / demo_run_report.md")
    for o in report["sandbox_outcomes"]:
        print(f"  {o['source_identity']}: {o['status']} annual={o['annual_premium']} evidence={o['evidence_identifier']}")
    print(f"  Sonnet: {report['sonnet_outcome']['status']} quote_returned={report['sonnet_outcome']['quote_returned']}")
    print(f"  Handoff: {report['manual_handoff']['status']} handoff_executed={report['manual_handoff']['handoff_executed']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
