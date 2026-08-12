# Submission Checklist — Ontario All-Quote Agent

Use this to confirm the submission is ready. All items are achievable in
**mock mode** with **no external credentials**.

## Repository / safety

- [ ] Repo clean: no scratch junit/logs, no `reports/`, no local notes committed
      (`.gitignore` covers `backend/*_junit.xml`, `*.log`, `reports/`, `.env`).
- [ ] No secrets: no API keys, no telephony credentials, no live credentials in
      source control (`.env` is gitignored; `.env.example` has placeholders).
- [ ] No PII in repo: no licence numbers, DOB, VIN, addresses, claims, or voice
      recordings in tests/fixtures/docs (all demo data is synthetic).
- [ ] Demo and live are isolated: mock routes are never in the real registry;
      LIVE refuses unverified routes (covered by tests).

## Setup / run

- [ ] `scripts/start-demo.ps1` starts backend + frontend and prints URLs.
- [ ] Quick Demo in `README.md` works: prerequisites → backend → frontend →
      URLs → click-through → expected result → how to run tests.
- [ ] Demo requires no external credentials (verified by
      `GET /api/v1/demo/env` → `demo_requires_external_credentials: false`).

## Demo

- [ ] Golden flow: intake → consent → Compare Quotes → polled progress → final
      multi-source results (5 routes, 4 quote responses, 3 distinct rate
      sources, 2 comparable, 1 estimate, 1 duplicate, 1 CAPTCHA-blocked).
- [ ] All-failure run terminates honestly (no infinite spinner, no fabricated
      quote).
- [ ] Partial results remain visible next to failures.
- [ ] Double-click Compare does not double-submit (frontend guard + backend
      idempotency).
- [ ] Result ordering deterministic (comparable sorted ascending by premium).

## Tests

- [ ] Full regression green (see `README.md` / test command below).
- [ ] Demo-critical suite green (comparison-run + reliability + normalization
      E2E).
- [ ] Privacy/safety test green (no sensitive markers in run payloads;
      demo/live isolation).

## Docs / deliverables

- [ ] `README.md` — Quick Demo, architecture summary (Mermaid), safety
      limitations.
- [ ] `docs/demo-script.md` — 3–5 min script + judging-criteria mapping.
- [ ] `docs/learning/issue-14-reliability-submission.md` (and README index).
- [ ] Architecture diagram present (README Mermaid).
- [ ] Redacted demo report: `python demos/issue14_demo_report.py` → synthetic,
      PII-safe JSON (`reports/demo-report.json`).
- [ ] Market registry included (`backend/data/market_registry/auto.json`).
- [ ] 3–5 min Loom/screen recording captured (optional but recommended).

## Canonical test commands (from `backend/`)

```powershell
# Full regression
$env:PYTHONPATH='tests'; .\.venv\Scripts\python.exe -m pytest -q

# Demo-critical (fast)
$env:PYTHONPATH='tests'; .\.venv\Scripts\python.exe -m pytest `
  tests\test_comparison_run.py tests\test_issue14_reliability.py -q
```
