# Architecture & Safety — Ontario All-Quote Agent

> **Personal-use, evidence-first AI assistant** for shopping Ontario private-passenger
> auto insurance. This document describes the system's architecture and its safety,
> privacy, and honesty guarantees, written at submission time. It is not marketing:
> wherever a capability is not (or is only partially) implemented, that is stated.

---

## 1. What this system is

- A **personal-use assistant** for the participant to collect their own insurance
  information once, discover applicable Ontario auto-insurance markets, attempt the
  supported online quote journeys, and record **evidence** for what happened on each
  route (success, failure, block, estimate, handoff, or unresolved).
- It is **NOT** a licensed insurance broker or advisor. It does not give coverage
  advice, does not sell or bind policies, and cannot replace a licensed
  representative.
- It is **NOT** a public/commercial product. It is scoped to one participant
  (the applicant) on their own machine, for the hackathon, with no multi-tenant
  deployment.

---

## 2. High-level architecture

```
Frontend (React + Vite, VITE_APP_MODE=mock | live)
        │  HTTP / CORS
        ▼
Backend (FastAPI, Python 3.11)
  IntakeEngine (consent-aware progressive intake, ProfileVault encrypted at rest)
        ▼
  MarketRegistryService (#3)  +  RateSourceDedup (#4)  →  RoutePlanner (#6)
        ▼
  ComparisonRunService (#13) ── browser routes (#7) ── recovery (#8) ── voice (#9)
        │                                                     │
        ▼                                                     ▼
  EvidenceService (#10) ← evidence emitted by every node (EvidenceSink)
        ▼
  QuoteNormalizationService (#11) → CoverageLedger → ComparabilityEngine (#12)
        ▼
  Results UI + redacted run reports + market-registry export
```

Key separation: **domain logic is deterministic** (Pydantic v2 models, data-driven
configs, rule-based normalization/comparison/dedup). **LLMs are used only where
language understanding, ambiguous field mapping, planning, extraction, or
explanation is needed** (route-planning assistant, field-mapping help). Playwright
performs routine browser steps deterministically — there is **no autonomous
LLM browser loop**; the browser executor is a bounded, data-driven, deterministic
step engine.

---

## 3. Core data flow

1. **Intake** — the participant answers questions one at a time. Sensitive values
   (name, address, DOB, licence, VIN, claims) are stored in `ProfileVault`
   (encrypted at rest, directory gitignored) and are **never** written to logs,
   traces, evidence payloads, or the frontend summary (leaf-level redaction via
   `safe_dict`/`redacted_dict`).
2. **Market discovery + dedup** — the registry lists Ontario auto markets with a
   single source of truth `distinct_rate_source_id` so aggregator brands never
   double-count an underlying insurer's rate.
3. **Route planning** — for each market, a deterministic plan decides the channel
   (online / phone / callback / broker / human) and which fields are required and
   present (presence booleans only — never values).
4. **Execution** — browser routes run through the deterministic `BrowserExecutor`
   (checkpoints, JIT field fill from the vault, quote observation). Voice/manual
   routes produce callback or handoff observations **without placing calls**.
5. **Evidence** — every attempt, decision, and observation is persisted as typed
   `EvidenceRecord`s. Failed/blocked/unresolved attempts are **preserved**, never
   hidden.
6. **Normalization + comparison** — raw observations become canonical
   `NormalizedQuote`s (estimate vs firm preserved; estimates never promoted to
   comparable) and are compared only when coverage information is sufficient.
   The lowest comparable premium is labelled "lowest among comparable quotes" —
   never "best".

---

## 4. Safety boundaries (enforced)

| Area | Guarantee | Where enforced |
| --- | --- | --- |
| **Identity / DB lookup** | Never performed automatically. Any identity verification or database lookup requires explicit participant confirmation and consent. | consent model; human checkpoint `identity_verification` |
| **Consent** | Collection, route data-sharing, household-driver, and disclosure consent are collected and re-checked just-in-time; revocation wins immediately. | `ConsentState`, intake engine, browser/voice gates |
| **Application declaration** | The application/declaration step is **never automated** (`must_not_automate=True`). | `IntakeCheckpoint`/`CheckpointBinding.application_declaration` |
| **Payment / signature / purchase / binding** | Prohibited. The executor's safe-action rules block any action that matches payment, signature, or cancellation/policy-modification patterns (tightened so global nav like "Pay my bill" or "Sign in" is not misread as a boundary). | `GenericQuoteSiteAdapter.DEFAULT_CHECKPOINT_BINDINGS`; `_find_safe_action` |
| **CAPTCHA / auth / bot controls** | Never bypassed. A CAPTCHA or access-control barrier stops the route and classifies it (`stopped_access_control` / `blocked`). | executor detection patterns; recovery policy never retries barriers |
| **Rate limits** | Bounded concurrency and per-route timeouts; no hammering. | `COMPARISON_MAX_CONCURRENCY`, route/run timeouts |
| **Licensed advice** | The system never gives licensed insurance advice; it reports evidence and comparison facts. | product scope + UI wording |
| **Honesty of quotes** | Sandbox numbers are labelled estimates (`estimate_only`, `local_sandbox`, `not_a_live_quote`). No synthetic figure is described as a live quote. The real Sonnet bounded attempt returned **no quote** and is reported `unresolved` with `quote_returned: false`. | `submission_demo.py` artifacts |
| **No fabrication** | Licence numbers / identity information are never fabricated; evidence and attempt IDs are only reported when actually produced. | privacy meta-tests; demo uses `unavailable` |

### Declarations, signatures, payment, purchase — the exact posture

The system stops **before**:
- any declaration attestation / signature ("sign here", "sign and submit", electronic
  signature);
- any payment or binding ("pay now", "submit payment", "complete payment", "purchase
  coverage");
- any policy modification / cancellation / renewal change.

These are hard `must_not_automate` boundaries. A route that reaches one pauses at a
human checkpoint and is classified with the appropriate terminal status. The
`application_declaration` checkpoint is explicitly prohibited from automation.

---

## 5. Honest outcomes & terminal statuses

Every quote attempt is classified with one of the exact statuses:
`quoted_comparable`, `quoted_non_comparable`, `estimate_only`, `callback_required`,
`manual_handoff`, `ineligible`, `affinity_restricted`, `specialty_only`,
`duplicate_rate_source`, `not_currently_writing`, `blocked`, `unreachable`,
`unresolved`.

Failed, blocked, and unresolved attempts are **preserved** with evidence and
explained — never deleted, never retried blindly, and never re-labeled as success.

---

## 6. Observability & privacy

- **LangSmith** tracing is configured via environment variables (EU region). Traces
  carry **metadata only** (`registry_id`, `distinct_rate_source_id`, `route_type`,
  `terminal_status`, workflow stage) — **never applicant values**.
- Every LangGraph node, LLM call, decision, retry, and failure is individually
  traceable, and a run/trace ID propagates through the workflow so quote attempts
  correlate with the LangSmith run.
- **Structured redacting logs** strip sensitive fields; expected outcomes do not
  dump stack traces.
- Evidence payloads are **type-safe and sanitized** (URL hashing, SHA-256
  idempotency keys); the repo ships **privacy meta-tests** that assert no sensitive
  markers appear in evidence content, logs, or fixtures.

### What was observed on the real Sonnet route (bounded attempt)

During controlled live verification, the Sonnet page rendered visually but the
province control was not reliably exposed to the automated browser context within
the bounded attempt. **No quote was returned.** The submission report records this
as `unresolved` (`quote_returned: false`), with the last confirmed stage
(`province_page`) and no fabricated attempt/session IDs (`unavailable`).

---

## 7. What is demo vs real (summary)

| Item | Status |
| --- | --- |
| Market registry (31 records) | Real, seeded from public brief; 2 provider routes verified (Square One, Sonnet) |
| Sandbox Direct / Sandbox Broker estimates ($2,400 / $2,180) | **Synthetic local demo data**, labelled `estimate_only`, `not_a_live_quote: true` |
| Sonnet quote | **None returned** — `unresolved`, honest |
| Co-operators manual handoff | **Not executed** — demonstration record only |
| Frontend "Compare Quotes" | Mock mode = local mock site; Live mode = gated personal-use Sonnet operator |
| Phone / voice | **No calls are placed** — callback/handoff observations only |

---

## 8. Reproduce the submission demo

```powershell
# From the repo root
Push-Location backend
.\.venv\Scripts\python.exe demos\submission_demo.py      # writes reports/submission/*
Push-Location ..
```

Artifacts: `reports/submission/market_registry.json|csv` and
`reports/submission/demo_run_report.json|md`. See `docs/DEMO_SCRIPT.md` and
`README.md`.
