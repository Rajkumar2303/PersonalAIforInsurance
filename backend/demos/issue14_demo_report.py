"""Issue #14 - redacted demo-mode comparison report generator.

Runs the LOCAL mock quote site through the real demo overlay and writes a
PII-safe JSON report (routes attempted, route statuses, distinct rate sources,
quotes/estimates, comparison statuses, evidence references). NO applicant data
is included - only safe result metadata.

Usage:
    python demos/issue14_demo_report.py [out.json]

Default output: <repo>/reports/demo-report.json (gitignored).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from app.core.config import REPO_ROOT
from app.demo.runtime import get_demo_runtime
from app.models.insurance.enums import InsuranceType
from app.services.comparison_run import ComparisonRunService

#: Demo-only routes from the isolated demo overlay (never live).
DEMO_ROUTES = [
    "mock-insurer",
    "mock-insurer-broker",
    "mock-provider-b",
    "mock-provider-c",
    "mock-provider-d",
]


def _safe_run_report(run) -> dict:
    """Extract ONLY safe, non-sensitive fields from a finished run."""
    summary = run.comparison.summary if run.comparison else None
    return {
        "report_type": "demo-comparison-run",
        "execution_mode": run.execution_mode,
        "run_status": run.status.value if hasattr(run.status, "value") else str(run.status),
        "total_routes": run.total_routes,
        "completed_routes": run.completed_routes,
        "running_routes": run.running_routes,
        "distinct_rate_sources": summary.distinct_rate_sources if summary else None,
        "quote_results": summary.quote_results if summary else None,
        "comparable_quotes": summary.comparable_quotes if summary else None,
        "estimates": summary.estimates if summary else None,
        "duplicates": summary.duplicates if summary else None,
        "lowest_comparable_annual_premium": (
            str(summary.lowest_comparable_annual_premium) if summary else None
        ),
        "routes": [
            {
                "registry_id": r.registry_id,
                "display_name": r.display_name,
                "status": r.status.value if hasattr(r.status, "value") else str(r.status),
                "route_outcome_semantics": r.route_outcome_semantics,
                "annual_premium": str(r.annual_premium) if r.annual_premium is not None else None,
                "firm_vs_estimate": r.firm_vs_estimate,
                "is_representative": r.is_representative,
                "is_alternative": r.is_alternative,
                "evidence_status": r.evidence_status,
                "quote_observation_id": r.quote_observation_id,
                "normalized_quote_id": r.normalized_quote_id,
                "message": r.message,
            }
            for r in run.route_summaries
        ],
    }


async def _run_demo() -> dict:
    runtime = get_demo_runtime()
    runtime.start_mock_site()
    engine = runtime.intake

    from app.demo.personas import standard_auto_persona

    session, _gate = engine.create_session(InsuranceType.AUTO)
    sid = session.session_id
    for path, value in standard_auto_persona().items():
        try:
            engine.submit_answer(sid, path, value)
        except Exception as exc:  # pragma: no cover - defensive
            print(f"  [skip] {path}: {type(exc).__name__}")

    for rid in DEMO_ROUTES:
        engine.grant_route_consent(sid, rid, [], True)

    service = ComparisonRunService(mock_runtime=runtime)
    run = service.start_run(sid, "mock")
    terminal = {"completed", "completed_with_partial_results", "failed"}
    while run.status not in terminal:
        await asyncio.sleep(0.5)
        run = service.get_run(sid, run.comparison_run_id)
    return _safe_run_report(run)


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO_ROOT / "reports" / "demo-report.json"
    report = asyncio.run(_run_demo())
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Wrote redacted demo report -> {out}")
    print(f"  status={report['run_status']} routes={report['total_routes']} "
          f"distinct_sources={report['distinct_rate_sources']}")
    for route in report["routes"]:
        print(f"  - {route['display_name']}: {route['status']} "
              f"(premium={route['annual_premium']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
