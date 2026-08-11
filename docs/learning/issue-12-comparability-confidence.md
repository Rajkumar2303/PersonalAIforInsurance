# Issue #12 — Lite Comparability + Simple Confidence/Reasoning (MVP)

**Status:** ✅ Implemented (Issue #12, LITE)

**Depends on:** [Issue #4](./issue-04-rate-source-deduplication.md), [Issue #11](./issue-11-quote-normalization.md)

---

## What was built and why

Turns Issue #11 `NormalizedQuote`s into **simple comparison-ready results** so the
frontend (Issue #13) can show "lowest annual premium among comparable quotes" without any
scoring, recommendation, or advice engine. Deterministic rules only.

- `QuoteComparisonResult` — one normalized quote classified for comparison.
- `QuoteComparisonService.evaluate(normalized_quotes, requested_coverage=...)` → `ComparisonPlanResult`
  with `comparable_quotes` (sorted), `estimates`, `duplicates`, `insufficient`, and a
  frontend-ready `ComparisonSummary`.
- API: `GET /api/v1/comparisons/plans/{plan_id}` and `GET /api/v1/comparisons/routes/{planned_route_id}`
  (both `intake_session_id`-scoped; optional requested-coverage query params).

## Comparison statuses

- `comparable`
- `insufficient_coverage_information` (unknown/missing essential coverage)
- `estimate_only`
- `duplicate_rate_source` (confirmed same `distinct_rate_source_id`)
- `normalization_incomplete` (Issue #11 status insufficient / no premium)
- `coverage_mismatch` (requested vs quoted essential coverage differs)

Downstream route semantics mapping (Issue #8 vocabulary):
`comparable → quoted_comparable`; `insufficient / mismatch / normalization_incomplete → quoted_non_comparable`;
`estimate_only → estimate_only`; `duplicate_rate_source → duplicate_rate_source`. Issue #11
records are never modified.

## Essential coverage rules

MVP compares only three essential fields already in the `CoverageLedger`:
- third-party liability limit
- collision deductible
- comprehensive deductible

If any is **unknown/missing** → `insufficient_coverage_information` with typed reason codes
(`missing_liability_limit`, `missing_collision_deductible`, `missing_comprehensive_deductible`,
and `unknown_coverage_preserved` when the item exists in the `unknown` state). No Ontario
defaults are invented.

## Requested vs quoted coverage

`RequestedCoverage` (from the Issue #2 `CoverageConfiguration` via
`from_coverage_configuration`) is kept **separate** from provider-quoted coverage. When the
user requested e.g. liability $2M / collision $1,000 and a provider quoted something
different, deterministic reason codes are produced (`liability_limit_mismatch`,
`collision_deductible_mismatch`, `comprehensive_deductible_mismatch`) → `coverage_mismatch`
(`quoted_non_comparable`). No silent equivalence.

## Estimate handling

Estimates → `estimate_only`; they are **not** put in the comparable-price ranking but are
still returned in `estimates`.

## Rate-source duplicate handling

Confirmed duplicates are detected only by **exact** `distinct_rate_source_id` (Issue #4
confirmed underlying source). One deterministic representative is kept for the main
comparison (prefer: firm > more-complete coverage > direct/primary > lowest premium);
others → `duplicate_rate_source`. `duplicate_possible` / unresolved identity is **never**
treated as a confirmed duplicate. Raw/normalized results are untouched.

## Why price alone is unsafe

A cheaper quote with unknown essential coverage must not rank above a fully-known one
(`insufficient_coverage_information` quotes are excluded from the ranking). Comparable firm
quotes sort ascending by `normalized_annual_amount`; wording is "Lowest annual premium among
comparable quotes" — never "best plan".

## Deterministic rules (summary)

Pure function of `NormalizedQuote`s + optional requested coverage; no LLM, no probabilistic
matching, no recommendation, no policy advice, no new persistence (recomputed on the fly).

## Key files

- `app/models/comparison.py` — enums, `RequestedCoverage`, `QuoteComparisonResult`, `ComparisonSummary`, `ComparisonPlanResult`
- `app/services/comparison/service.py` — `QuoteComparisonService` (classification, completeness, mismatch, duplicate handling, sorting, summary)
- `app/api/comparisons.py` — `/api/v1/comparisons/plans/{plan_id}`, `/routes/{planned_route_id}`
- `tests/comparison_helpers.py`, `tests/test_comparison_lite.py` — 21 tests (A–K + aggregator + partial-coverage demo)

## Self-test questions

1. When is a quote `insufficient_coverage_information` vs `coverage_mismatch`?
2. How is a confirmed duplicate distinguished from `duplicate_possible`?
3. Why can't an unknown coverage item mean "excluded"?
4. What wording is allowed vs prohibited for the sorted result?

## Known MVP limitations

- Only three essential coverage fields are compared; no tolerances/partial-match scoring.
- Requested coverage is optional (via query params or `RequestedCoverage`); no intake-store coupling.
- `routes_attempted` in the summary is derived from the normalized quotes' planned routes (comparison has no attempt visibility).
