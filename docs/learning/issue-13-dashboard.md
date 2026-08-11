# Issue #13 — Lite Multi-Source Comparison Run + Results UI

**Status:** ✅ Implemented (Issue #13, LITE)

**Depends on:** Issues #6, #7, #8, #10, #11, #12

---

## ComparisonRun

`ComparisonRun` (`app/models/comparison_run.py`) is a pollable, frontend-safe run
state: `comparison_run_id`, `intake_session_id`, `plan_id`, `execution_mode`,
`status`, timing, `total_routes`/`completed_routes`/`running_routes`,
`route_summaries`, and an optional `comparison` (`ComparisonPlanResult`).
Lifecycle: `prepared → running → completed | completed_with_partial_results | failed`.
No applicant PII; only safe result metadata.

Per-route `RouteRunSummary`: `registry_id`, `display_name`, `channel`, `status`
(`queued|running|quote_pending_normalization|comparable|non_comparable|
estimate_only|duplicate_rate_source|captcha_blocked|unavailable|
callback_required|manual_handoff|needs_additional_information|ineligible|...`),
`annual_premium`, `firm_vs_estimate`, `coverage_summary`, `missing_coverage_keys`,
`reason_codes`, `distinct_rate_source_id`, `is_alternative`, `is_representative`,
`evidence_status`, quote/normalized ids, `pending_field_paths`.

## Orchestration flow

`ComparisonRunService` (`app/services/comparison_run/service.py`) coordinates the
EXISTING deterministic components — no browser/recovery/normalization logic is
moved into it:

1. `start_run(intake_session_id, execution_mode)` → creates/reuses an active run
   (idempotent, so double-clicking Compare never double-submits) and starts a
   background task.
2. Plan via Issue #6 RoutePlanner; build a route summary per route.
3. Execute ready, online, consent-granted routes under a **bounded concurrency
   semaphore** (`COMPARISON_MAX_CONCURRENCY`, default 4).
4. Each route: Issue #7 browser workflow → Issue #8 recovery decision; quote →
   recorded to Issue #10 evidence.
5. **Auto-normalize** every recorded quote (closing the Issue #11 Prompt 2 gap)
   → **compare** via Issue #12 → attach `ComparisonPlanResult`.
6. Map route summaries to the final comparison classification and set the run
   status.

## Concurrency & route isolation

- `asyncio.Semaphore(comparison_max_concurrency)` — never launches 30 browsers.
- Each route runs inside its own `try/except`; a CAPTCHA, browser exception, or
  missing field marks that route only and NEVER stops the others. `run.status`
  becomes `completed_with_partial_results` when some routes failed/blocked.
- CAPTCHA → `captcha_blocked` (never solved, never pauses the run).

## Normalization / comparison pipeline

After routes finish, every `QuoteObservation` in evidence is normalized
automatically and fed to `QuoteComparisonService`. Comparable firm quotes sort by
annual premium; estimates stay separate; confirmed duplicates (same
`distinct_rate_source_id`) are shown as `duplicate_rate_source`, never counted as
an independent market.

## Demo mode vs live mode

- **Mock** (default): the isolated `backend/data/demo` overlay (5 synthetic
  routes: Provider A direct quote, aggregator/broker A duplicate, Provider B
  quote, Provider C CAPTCHA, Provider D estimate) + local mock quote site.
  `DemoRuntime.config_loader` falls back to `build_scenario_config` per
  `DEMO_SCENARIOS`. Mock runs may execute the aggregator alternative so the
  confirmed duplicate is VISIBLE (Issue #12 keeps the direct representative).
- **Live**: real singletons; alternatives are classified `duplicate_rate_source`
  WITHOUT executing (respects Issue #4/#8 — never double-submits); no real
  telephony, no LLM.

## API

- `POST /api/v1/comparison-runs` `{intake_session_id, execution_mode}` → returns
  the run quickly (idempotent; unknown session → 404).
- `GET /api/v1/comparison-runs/{run_id}?intake_session_id=...` → poll
  progress/final result (ownership-scoped).

## Frontend

`ComparisonResults.jsx` polls the run every ~1s and renders: progress
("X / Y routes completed"), a results table (Provider / Annual premium /
Coverage / Result type / Status), a summary grid (routes attempted, quote
responses, comparable, estimates, distinct rate sources, duplicates), and
"Lowest annual premium among comparable quotes" (never "best plan"). Honest
failure labels (CAPTCHA blocked, estimate, duplicate, needs information) are
shown. `App.jsx` wires the wizard: product → intake → review/consent →
Compare Quotes → results.

## Key files

- `app/models/comparison_run.py`, `app/services/comparison_run/service.py`
- `app/api/comparison_runs.py`
- `app/demo/runtime.py` (scenario-config fallback), `data/demo/market_registry/auto.json`,
  `data/demo/routes/auto_route_requirements.json`
- `frontend/src/components/ComparisonResults.jsx`, `App.jsx`, `api.js`, `index.css`
- `tests/comparison_run_helpers.py`, `tests/test_comparison_run.py`

## Known MVP limitations

- Browser produces one quote per route; "one aggregator route returns N carrier
  quotes" is emulated by an aggregator route that executes and is deduped by
  Issue #12 (documented, mock-only).
- No resume endpoint yet; `needs_additional_information` is reported per route
  but batching/resume is deferred.
- No Postgres persistence for runs (in-memory store; deterministic recompute is
  fine for the MVP).
