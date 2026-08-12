# Issue #14 — End-to-End Reliability, Demo Hardening & Submission Readiness (LITE)

**Implemented (LITE).** This document covers what was actually built for the
Issue #14 milestone: run/route safety timeouts, an environment check, frontend
polling hardening + safe fallback UI, a one-command startup script, a redacted
demo report generator, submission docs, and hermetic reliability tests.

## What was built and why

Issue #14 asks one question: *"Can I clone/run the project, complete the intake,
click Compare Quotes, and reliably see the intended multi-source comparison
without debugging?"* The answer had to be reliable **and** easy to demonstrate
in 3–5 minutes, with no external credentials and no over-engineering.

Changes, by theme:

- **Safety timeouts (backend)** — `comparison_route_timeout_seconds` and
  `comparison_run_timeout_seconds` settings; the run service now bounds each
  route's browser workflow so a stuck/slow provider page resolves to
  `unavailable` instead of hanging the whole run. A run-level backstop resolves
  any leftover queued/running routes.
- **Environment check (backend)** — `GET /api/v1/demo/env` returns a
  `DemoEnvironmentStatus` clearly separating **DEMO REQUIRED** (nothing
  external) from **OPTIONAL/LIVE** (Postgres, LangSmith, LLM, live providers) —
  presence booleans only, never secrets.
- **Frontend hardening** — the results component stops polling at a terminal
  state, survives transient request failures (keeps polling, surfaces after N),
  has a client-side max-wait backstop + timer cleanup, and shows a **safe
  fallback** ("No fully comparable quotes were available from this run")
  instead of ever implying "no insurance available" or fabricating a quote. It
  also adds the distinct-rate-source tooltip and the evidence-trail note for
  judges. The Compare button is disabled while submitting and a `useRef` guard
  prevents a double-click from firing two starts.
- **One-command startup** — `scripts/start-demo.ps1` verifies dependencies,
  starts backend + frontend, and prints URLs.
- **Redacted demo report** — `backend/demos/issue14_demo_report.py` runs the
  real demo overlay and writes a PII-safe JSON report (route statuses, distinct
  sources, premiums, evidence references — never applicant values).
- **Docs** — `README.md` Quick Demo + architecture (Mermaid) + safety
  limitations; `docs/demo-script.md` (3–5 min + judging-criteria map);
  `docs/submission-checklist.md`.

## How it works / data flow

`POST /api/v1/comparison-runs` → `ComparisonRunService.start_run` (idempotent)
→ `_run` → `_run_routes`:

1. Plan routes → per-route `RouteRunSummary` (duplicates not re-run; non-ready /
   no-online-channel / consent-required recorded without execution).
2. Routes execute under an `asyncio.Semaphore(comparison_max_concurrency)`.
3. **Per-route timeout**: `_execute_route` wraps only the browser workflow
   (`build_browser_workflow(...).ainvoke`) in
   `asyncio.wait_for(timeout=comparison_route_timeout_seconds)`; on timeout the
   route is marked `unavailable` and the `finally: manager.close(...)` still
   runs (which aborts in-flight navigation).
4. Quotes → evidence → auto-normalization → Issue #12 comparison.
5. **Run-level backstop**: if the whole run outlives
   `comparison_run_timeout_seconds`, any queued/running routes become
   `unresolved`; final status is `completed` / `completed_with_partial_results`
   / `failed` — never left `running`.

The frontend polls `GET /api/v1/comparison-runs/{id}?intake_session_id=...`
every 1s, stops on terminal, and (as a backstop) shows a timeout card after
150s instead of spinning forever.

## Key files / classes / functions / tests

- `backend/app/core/config.py` — `comparison_route_timeout_seconds`,
  `comparison_run_timeout_seconds`.
- `backend/app/services/comparison_run/service.py` — timeout in `_execute_route`
  + run-level backstop in `_run_routes`; structured `logger.warning` for
  expected route outcomes (no noisy stack traces).
- `backend/app/api/demo.py` + `backend/app/models/demo.py` —
  `GET /api/v1/demo/env` → `DemoEnvironmentStatus`.
- `frontend/src/components/ComparisonResults.jsx` — polling hardening, safe
  fallback, help notes; `frontend/src/App.jsx` + `ReviewConsent.jsx` — compare
  idempotency guard.
- `scripts/start-demo.ps1`; `backend/demos/issue14_demo_report.py`.
- `backend/tests/test_issue14_reliability.py` — 8 tests:
  `test_golden_demo_flow_shape`, `test_all_routes_fail_completes_honestly`,
  `test_partial_run_quotes_remain_visible`, `test_route_timeout_does_not_hang_run`,
  `test_demo_overlay_never_leaks_into_live_registry`,
  `test_demo_route_refused_in_live_mode`, `test_demo_env_check_no_external_credentials`,
  `test_double_start_reuses_active_run`.
- `docs/demo-script.md`, `docs/submission-checklist.md`, README updates.

## Architectural decisions & tradeoffs

- **Per-route timeout wraps only the browser workflow, not the session close.**
  Cancelling a Playwright coroutine mid-navigation (via a bare
  `asyncio.wait_for` around the whole `_execute_route`) deadlocked the browser,
  and a non-cancelling "shield" leaked the task and hung process teardown. The
  chosen design times out just the workflow and always closes the session in a
  `finally`, which aborts in-flight work and lets the run terminate cleanly.
  **Implemented and verified by `test_route_timeout_does_not_hang_run`.**
- **No blind retries** — a timed-out route is terminal (`unavailable`); other
  routes continue. This reuses existing Issue #8 terminal semantics.
- **No WebSockets** — polling only (per the issue).
- **No persistence for refresh recovery** — the run store is in-memory (Issue
  #13), so a hard refresh loses the run; documented as a limitation instead of
  spending time on persistence (per §10 of the issue).
- **Frontend verified by build + backend E2E** — there is no frontend test
  framework in the repo; per §15's "OR" option, reliability is proven with
  backend/API E2E tests rather than introducing a new browser-test framework.

## LangGraph / LangSmith / observability

No new LangGraph graph was added for #14; the existing `browser_workflow` and
run orchestration are reused. Logging is structured with metadata
(`registry_id`, `workflow_stage`, `status`, `error_type`) and **no stack traces
for expected outcomes** (route timeout / captcha / unavailable are `warning`
with `status` — unexpected exceptions still use `logger.exception`). No PII in
any log/trace/payload.

## Privacy / security implications

- `GET /api/v1/demo/env` exposes only booleans (never keys/URLs/secrets).
- The demo report contains only safe result metadata (registry ids, statuses,
  premiums, evidence ids) — no applicant values.
- Demo/live isolation is preserved and now **tested explicitly**: demo routes
  are absent from the real registry and LIVE refuses them
  (`NO_VERIFIED_ROUTE`), so demo mode can never reach a live provider.
- `.gitignore` covers `reports/`, junit XMLs, logs, `.env`.

## Testing strategy

Hermetic tests against the local mock quote site; no live insurers, no LLM, no
LangSmith. Coverage:
- golden demo result shape (5 routes / 4 quotes / 3 distinct sources / 2
  comparable / 1 estimate / 1 duplicate / 1 CAPTCHA);
- all-failure honest termination (no fabricated quote, `running_routes == 0`);
- partial results remain visible;
- stuck-route timeout terminates the run;
- demo/live isolation (real registry + LIVE refusal);
- env check (demo needs no external credentials);
- double-click idempotency.

## Failure scenarios

- A provider page hangs → route `unavailable` after the route timeout; run
  completes with partial results; UI shows "Temporarily unavailable".
- All routes fail → run `completed_with_partial_results`; UI shows the safe
  fallback, never a fake quote.
- Backend unavailable mid-poll → frontend keeps polling for 3 failures, then
  shows a clear error with "Retrying…"; terminal states always stop the spinner.

## Debugging approach

- Reproduce a run against the mock site via `backend/demos/issue14_demo_report.py`.
- Inspect `GET /api/v1/demo/env` to confirm demo readiness / optional flags.
- Structured logs carry `registry_id` + `workflow_stage` (`route_timeout`,
  `route_failed`) for per-route diagnosis.

## Common misunderstandings

- The route timeout does **not** cancel the browser task in a way that can
  deadlock Playwright — it bounds the workflow and closes the session.
- "No fully comparable quotes" is an honest, safe fallback — it is **not**
  "no insurance available".
- Estimates, CAPTCHA, and duplicate-rate-source results are *expected* demo
  outcomes, not bugs.

## Self-test questions

1. What stops a single slow provider from hanging the whole demo run?
2. What does `GET /api/v1/demo/env` report and what does it never report?
3. What does the frontend do when polling hits 3 consecutive failures?
4. Why is the safe fallback wording important (vs "no insurance available")?
5. How is demo/live isolation proven in tests?

## Rebuild exercise

In `test_issue14_reliability.py`, change the route list in
`test_all_routes_fail_completes_honestly` to a mix of `quote` + `captcha` and
re-run; confirm `completed_with_partial_results` keeps quotes visible while
failures are reported.

## Cheat sheet

- Env check: `GET /api/v1/demo/env`.
- Start demo: `.\scripts\start-demo.ps1`.
- Redacted report: `python demos/issue14_demo_report.py`.
- Demo-critical tests: `pytest tests\test_comparison_run.py tests\test_issue14_reliability.py -q`.
- Timeout settings: `COMPARISON_ROUTE_TIMEOUT_SECONDS`, `COMPARISON_RUN_TIMEOUT_SECONDS`.
