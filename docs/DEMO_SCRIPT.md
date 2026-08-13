# DEMO SCRIPT — 4-Minute Judge Demo (Deterministic, Local)

> **All demo numbers are synthetic local sandbox data — NOT live insurance quotes.**
> No insurer/broker/aggregator is contacted during this demo. The deterministic
> generator reuses the real evidence + normalization pipeline so the demo is
> exactly reproducible end-to-end.
>
> Target: **4 minutes**. Every step is scripted; no live credentials are needed.

---

## 0:00 — Title & setup (30s)

- **What you see:** the repo README title "Ontario All-Quote Agent — personal-use,
  evidence-first insurance-shopping assistant".
- **Say:** "This is a personal-use assistant for shopping Ontario auto insurance.
  It's evidence-first: every market, route, attempt, and outcome is recorded."
- **Open** `docs/ARCHITECTURE_AND_SAFETY.md` and point to the **Safety boundaries**
  table (one line): declarations never automated, payment/signature/purchase
  prohibited, CAPTCHA never bypassed, no licensed advice.

## 0:30 — Deterministic demo generation (30s)

Run (repo root):

```powershell
Push-Location backend
.\.venv\Scripts\python.exe demos\submission_demo.py
Pop-Location
```

- **Say:** "This runs the deterministic submission demo. It reuses the real evidence
  and normalization services — no live provider is contacted."
- **Point to the console output:**
  - `Ontario Sandbox Direct: estimate_only annual=2400`
  - `Ontario Sandbox Broker: estimate_only annual=2180`
  - `Sonnet: unresolved quote_returned=False`
  - `Handoff: manual_handoff handoff_executed=False`

## 1:00 — Two sandbox outcomes, honestly labelled (45s)

Open `reports/submission/demo_run_report.json`.

- **Say:** "Two sandbox estimates. Notice the labels on each: `status: estimate_only`,
  `source_environment: local_sandbox`, `not_a_live_quote: true`. The lower estimate —
  Sandbox Broker at $2,180 — has a **higher collision deductible ($1,500)** and **no
  accident forgiveness**. So the lower number is NOT labelled 'best'; the comparison
  engine only reports coverage differences."
- **Point to** `coverage_variances` and `comparisons.lower_premium_is_not_labeled_best: true`.

## 1:45 — Honest Sonnet outcome (30s)

Scroll to `sonnet_outcome`.

- **Say:** "The real bounded attempt on Sonnet did not return a quote. We report it
  exactly as it happened: `status: unresolved`, `quote_returned: false`,
  `last_confirmed_stage: province_page`. No fabricated quote, no fake reference ID —
  attempt and session IDs are honestly `unavailable`."

## 2:15 — Manual handoff (30s)

Scroll to `manual_handoff`.

- **Say:** "Where a licensed representative or applicant interaction is needed, we
  produce a handoff record — but `handoff_executed: false`. No broker was called.
  Only canonical **field names** are listed, never values."

## 2:45 — Market registry export + dedup (45s)

Open `reports/submission/market_registry.csv` and `.json`.

- **Say:** "The market registry export lists 31 Ontario auto markets with a single
  `distinct_rate_source_id` per rate source. Aggregator brands that map to an
  underlying insurer are suppressed as duplicates — never double-counted."
- **Point to** `metrics.duplicate_suppression_count` and `market_completion: 2/31`.

## 3:30 — Privacy: no sensitive data (25s)

- **Say:** "A privacy meta-test asserts none of these artifacts contain licence
  numbers, VINs, DOBs, addresses, phone numbers, or email addresses."
- Run (or point to the passing test):

```powershell
Push-Location backend
.\.venv\Scripts\python.exe -m pytest tests\test_submission_demo.py -q
Pop-Location
```

## 3:55 — Close (15s)

- **Say:** "Evidence-first, honest about failures, safe by construction: sandbox
  estimates are never presented as live quotes, barriers are never bypassed, and
  the system stops before declarations, signatures, and payment."

**End at ~4:10.**

---

## Backup slides (if time allows)

- `docs/KNOWN_LIMITATIONS.md` — what is not implemented / not claimed.
- `reports/submission/demo_run_report.md` — readable summary of the same report.
- Full test suite command (repo root):

```powershell
Push-Location backend
.\.venv\Scripts\python.exe -m pytest -q
Pop-Location
```
