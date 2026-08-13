# DEMO CHECKLIST — Ontario All-Quote Agent (Submission)

Use this to verify the submission demo and its safety/honesty guarantees before the
judge demo. Every item is deterministic and local — no live provider is contacted.

## A. Environment & prerequisites

- [ ] Python 3.11+ and the backend venv exist (`backend\.venv\Scripts\python.exe`).
- [ ] Node 18+ present (frontend not required for the deterministic demo).
- [ ] No API keys, database, telephony, or live insurer credentials required.

## B. Deterministic submission demo (Steps 2–5)

Run from repo root:

```powershell
Push-Location backend
.\.venv\Scripts\python.exe demos\submission_demo.py
Pop-Location
```

- [ ] Console prints the `DEMO DATA - LOCAL ESTIMATES, NOT LIVE INSURANCE QUOTES` banner.
- [ ] Two sandbox outcomes appear, each `estimate_only`, `local_sandbox`, `not_a_live_quote: true`:
      - [ ] Ontario Sandbox Direct $2,400
      - [ ] Ontario Sandbox Broker $2,180
- [ ] Sonnet outcome is `unresolved`, `quote_returned: false` — no fabricated quote.
- [ ] Manual handoff is `manual_handoff`, `handoff_executed: false`.
- [ ] Artifacts written to `reports/submission/`:
      - [ ] `market_registry.json`
      - [ ] `market_registry.csv`
      - [ ] `demo_run_report.json`
      - [ ] `demo_run_report.md`

## C. Report content checks

- [ ] Every sandbox outcome has `status=estimate_only`, `source_environment=local_sandbox`,
      `not_a_live_quote=true`, a timestamp, and a persisted evidence/normalized-quote id.
- [ ] The lower estimate ($2,180) is **not** labelled "best"; coverage variances
      (collision deductible, accident forgiveness) are listed.
- [ ] Sonnet section says `quote_returned: false` and does **not** claim
      quoted/blocked/captcha/access_denied/successfully_autofilled.
- [ ] Handoff section lists canonical **field names** only (never values) and
      `recording_consent: not_requested`.
- [ ] Market registry export contains 31 entries with `distinct_rate_source_id`,
      `duplicate_suppression_count` is a number, unknown values preserved as `unknown`.
- [ ] Banner text appears in `demo_run_report.md`.

## D. Safety boundaries (asserted by tests)

- [ ] `application_declaration` is `must_not_automate=True` (declaration never automated).
- [ ] Payment / signature / purchase / cancellation patterns block automation
      (`stopped_prohibited`), and global nav ("Pay my bill", "Sign in") is **not** misread
      as a boundary (regression test `test_sonnet_address_prohibited_fix.py`).
- [ ] CAPTCHA / access control stops the route and is never bypassed.
- [ ] No autonomous LLM browser loop — Playwright steps are deterministic.
- [ ] Identity/database lookup requires participant confirmation.

## E. Tests

Run from repo root:

```powershell
Push-Location backend
.\.venv\Scripts\python.exe -m pytest tests\test_submission_demo.py -q   # demo artifacts
.\.venv\Scripts\python.exe -m pytest tests\test_sonnet_address_prohibited_fix.py -q
.\.venv\Scripts\python.exe -m pytest tests\test_comparison_run_sonnet_operator.py -q
.\.venv\Scripts\python.exe -m pytest tests\test_browser_quote_endpoint.py -q
.\.venv\Scripts\python.exe -m pytest tests\test_sonnet_live_driver_client.py -q
Pop-Location
```

- [ ] All focused tests pass.
- [ ] (Optional, ~6 min) Full suite: `.\scripts\run-tests.ps1` or
      `python -m pytest -q` from `backend`.

## F. Privacy scan

- [ ] `.gitignore` excludes browser profiles, session data, raw screenshots/traces,
      call recordings, temp evidence, and local DBs (see Step 10 additions).
- [ ] No sensitive markers (licence / VIN / DOB / address / phone / email / `.env` /
      keys) in tracked or generated artifacts. The privacy meta-test
      `test_evidence_auto_privacy.py` passes.
- [ ] `reports/` (including `reports/submission/`) is gitignored.

## G. Final sign-off

- [ ] Demo runs deterministically twice with identical structure (timestamps/ids vary).
- [ ] Nothing was committed or pushed automatically.
- [ ] No real insurer/broker/aggregator was contacted during the demo.
