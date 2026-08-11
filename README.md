# Ontario All-Quote Agent

A **personal-use, evidence-first AI assistant** that helps the participant shop Ontario
private-passenger auto insurance. It collects the participant's insurance information
once, discovers applicable Ontario insurance markets, selects the correct route per
market, autofills supported quote journeys, handles voice/manual handoffs where
required, normalizes results, deduplicates underlying rate sources, and records
evidence for every successful or unsuccessful attempt.

> ⚠️ **Personal use only.** This is not a licensed insurance broker or advisor.
> It never fabricates licence numbers or identity information, never bypasses
> CAPTCHAs/auth/rate limits, and stops before declarations, signatures, payment,
> or policy changes. Sensitive data is redacted from all logs, traces, and evidence.

## Status

**Issues 1–8 complete** — foundation + intake schema + market registry +
rate-source deduplication + consent-aware intake agent + core route planner +
browser quote agent (observation-first) + terminal-status & recovery engine.

Issue #1 — Project Setup, Architecture & Observability (foundation):
- Monorepo structure (`backend/`, `frontend/`)
- FastAPI app with `/health` and a demo LangGraph workflow endpoint
- LangGraph typed state + compiled 2-node workflow
- LangSmith tracing configured via environment variables with run-ID correlation
- Structured, privacy-aware logging + reusable sensitive-data redaction utility
- Playwright browser foundation interface (no insurer automation yet)
- Minimal React frontend shell with a backend health indicator

Issue #2 — Canonical Insurance Intake Schema (see [Insurance Intake Schema](#insurance-intake-schema)):
- `InsuranceType` enum — AUTO fully implemented; HOME/TENANT/LIFE/TRAVEL/OTHER
  recognized but unsupported
- Shared applicant/contact/consent models + product-specific `AutoInsuranceProfile`
  (composition, no duplicated shared fields)
- AUTO profile covering drivers, vehicles, household/fleet, insurance & driving
  history, and coverage configuration (schema only — no premium/quote logic)
- Sensitive-aware models: `safe_dict()`/`redacted_dict()` and redacted `repr`/`str`
- Lightweight missing-field + trace-metadata helpers (full intake engine = Issue #5)

Issue #3 — Progressive-Profile Hardening + Ontario Market Registry
(see [Market Registry](#market-registry)):
- Draft profiles (zero drivers/vehicles, DOB/address optional) + `is_draft` /
  `is_live_quote_ready`; live-quote completeness via `required_for_live_quote()`
- Canonical field paths (`paths.py`) + validated immutable `updated(path, value)`
- Data-driven `MarketRegistryEntry` registry (AUTO seed from the hackathon brief:
  31 discovery records, `status: discovered`, nullable rate-source ids)
- Read-only API: `GET /api/v1/markets`, `GET /api/v1/markets/{registry_id}`

Issue #4 — Rate-Source Deduplication (see [Rate-Source Dedup](#rate-source-dedup)):
- Deterministic, evidence-based dedup layer over the market registry
  (`services/deduplication.py`)
- `MarketRegistryEntry.distinct_rate_source_id` is the authoritative route→source
  mapping; `DistinctRateSource` records (data/rate_sources) describe each source
- Same insurer group is never auto-collapsed; only `duplicate_confirmed` routes
  count once (reason code + confidence + evidence on every pair decision)
- Read-only API: `GET /api/v1/rate-sources`, `/rate-sources/{id}`,
  `/markets/{registry_id}/duplicates`, `/dedup/metrics`
- Real seed reports honestly `confirmed_rate_sources: 0, confirmed_duplicates: 0,
  unresolved_mappings: 31` until routes are verified during the hackathon

Issue #5 — Product-Aware Consent Intake Agent (see [Intake](#intake)):
- Data-driven `IntakeFieldDefinition` catalog (`data/intake/auto_fields.json`) —
  HOW TO ASK lives in data; the Pydantic schema stays authoritative for validation
- Deterministic `IntakeEngine`: product gate (AUTO vs not-implemented), seed
  bootstrap, progressive next-question, validated updates, ask-once
- `request_fields()` for future Browser (#7) / Voice (#9) agents: already_known /
  requested / unsupported / consent_required
- `ProfileVault` Protocol: in-memory (dev/tests) or Fernet encrypted-at-rest
  (key from env only; data dir gitignored)
- Typed consent: collection / route_disclosure / household_driver receipts with
  paths-not-values; route data-sharing preview + exclusion
- Human checkpoints (signature/payment/purchase = must_not_automate)
- LangGraph advance/submit flows with safe-metadata-only state (no PII in traces)
- New optional schema field `years_at_current_address` (schema 1.0 → 1.1)

Issue #6 — Core Route Planner (see [Route Planner](#route-planner)):
- Deterministic per-route pre-flight plan (`services/route_planner/`)
- Product-aware AUTO routing; non-AUTO sessions → not-applicable plan
- Readiness is PER ROUTE (never requires global `is_live_quote_ready`); a route
  can have MULTIPLE simultaneous blockers (missing field / consent / membership /
  callback / human / specialty-only / unresolved rate source)
- Issue #4 dedup integration: confirmed duplicate groups → primary + visible
  alternatives (`is_alternative`); possible/unresolved duplicates stay visible
  (never suppressed)
- Data-driven requirements (`data/routes/auto_route_requirements.json`) +
  deterministic `MarketRequirement`→path map
- Issue #5 integration: planner sees presence booleans only (`field_presence`);
  `request_missing_fields` asks for genuinely-missing fields once; `field_gate`
  surfaces household-driver consent needs as `human_required` blockers
- Deterministic ranking; route channels (online/phone/callback/broker/human/
  discovery_only)
- Safe LangGraph orchestration (`route_planner_workflow`, metadata-only state)
- Read-only API: `GET /api/v1/planner/plan`, `POST /planner/plan/{id}/request-missing`
- Part 2 hardening: 14 mandatory scenarios covered by
  `test_route_planner_hardening.py`; full suite **306 tests pass**, hermetic

Issue #7 — Browser Autofill & Quote Agent (see [Browser Quote Agent](#browser-quote-agent)):
- Playwright browser execution for a READY Issue #6 web `PlannedRoute`; route-plan
  driven, `planned_route_id` mapped via one centralized compat shim
- Sandbox mode (local mock quote site, external requests blocked) vs LIVE mode
  (personal-use gate + privacy defaults: no video/trace/screenshot/HAR)
- Deterministic `BrowserExecutor`: host recheck → consent recheck → detect →
  checkpoint gate → inspect → map → just-in-time vault fill → pause/navigate →
  observe
- Data-driven `BrowserRouteConfig`/`BrowserFieldBinding` (matching, fill
  strategies, controlled transforms) — dynamic fields/site changes are config-only
- Just-in-time `IntakeEngine.get_field_value` trusted boundary (scalar only)
- Issue #5 integration: batched missing fields, ask-once, resume same session,
  conditional fields, consent expansion/revocation, household-driver gate
- Safety: CAPTCHA/access-control stop, human checkpoints, prohibited
  signature/payment/purchase boundaries, allowed-host protection
- Observations only: `RawQuoteObservation` (annual/monthly, estimate vs firm,
  private reference handle), callback/manual handoff (no call), unknown/validation/
  ambiguity/unsupported-value/technical observations — no Issue #8 terminal logic
- Bounded LangGraph `browser_workflow` (generic loop, safe state)
- API: `POST /browser/sessions`, `/run`, `/resume`, `GET/DELETE /{id}`
- **448 tests pass** (Issues #1–#7), hermetic; local mock demo:
  `backend/demos/issue7_browser_demo.py`

Issue #8 — Terminal Status & Recovery Engine (see [Recovery Engine](#recovery-engine)):
- Deterministic decision layer between Issue #7 observations and coverage outcomes:
  resume / retry / failover / handoff / terminal — **no LLM, no insurer branching**
- State separation: `RouteReadiness` (#6) vs `ExecutionObservation` (#7/#8) vs
  `AttemptLifecycleStatus` (#8) vs `RouteOutcomeStatus` (#8) kept distinct
- Generic `ExecutionObservation` contract (browser now, voice/phone later)
- Data-driven `RecoveryPolicy` (`data/recovery/auto_policy.json`): route 2 /
  rate-source 3 / plan 6 attempt budgets, conservative + tunable
- Bounded same-route retry; alternate-route failover from Issue #6 (deterministic,
  no reuse of exhausted routes, no distinct-source inflation)
- Readiness + live consent recheck before retry/failover (`IntakeConsentSource`)
- Pause ≠ failure; resume vs retry separated; browser-session-loss handled
- Terminal immutability + explicit `enrich_terminal`; idempotency + stale guard
- CAPTCHA/auth/prohibited boundaries never retried or bypassed
- `quote_pending_normalization` — `quoted_comparable`/`quoted_non_comparable` never assigned
- `AttemptStore` Protocol (in-memory; Issue #10 can replace it)
- Generic LangGraph `recovery_workflow`; safe-context allowlist for LangSmith
- API: `POST /recovery/decisions`, `GET /attempts/{id}`, `GET /route-plans/{plan_id}/attempts`
- **590 tests pass** (Issues #1–#8), hermetic; demo: `backend/demos/issue8_recovery_demo.py`

Later milestones (all future, not implemented here): Issue #9 voice/phone handoff,
Issue #10 evidence store, Issue #11 quote normalization, Issue #12
comparability/confidence, Issue #13 dashboard API.

## Architecture Overview

```
┌────────────┐   HTTP/CORS   ┌────────────────────────────────────────────┐
│  frontend/ │ ────────────▶ │  backend/app/main.py  (FastAPI)            │
│  React+Vite│               │  ┌──────────────────────────────────────┐  │
└────────────┘               │  │ core/  config · logging · tracing    │  │
                             │  │        redaction                     │  │
                             │  ├──────────────────────────────────────┤  │
                             │  │ graph/  intake · route_planner ·     │  │
                             │  │        browser · recovery workflows  │  │
                             │  │ api/    health, markets, dedup,      │  │
                             │  │         intake, planner, browser,    │  │
                             │  │         recovery                     │  │
                             │  ├──────────────────────────────────────┤  │
                             │  │ browser/ executor, session, manager, │  │
                             │  │         inspector, matchers, fill,   │  │
                             │  │         actions, detect, adapters,   │  │
                             │  │         value_provider, mock_site    │  │
                             │  │ models/ Pydantic v2 models           │  │
                             │  │ services/ registry, dedup, intake,   │  │
                             │  │           route_planner, recovery    │  │
                             │  └──────────────────────────────────────┘  │
                             └──────────────┬─────────────────────────────┘
                                            │  LangSmith tracing (env-configured)
                                            ▼
                                      LangSmith traces
```

Flow:

```
Consent-aware intake (Issue #5) → Route planner (Issue #6) → Browser quote agent (Issue #7)
                                                                   │  ExecutionObservation
                                                                   ▼
                                                       Issue #8 Recovery Engine
                                                                   │  retry / pause / failover / terminal
                                                                   ▼
                                     Future #9 Voice · Future #10 Evidence · Future #11 Normalization
```

Key principles:
- **Evidence-first** — every outcome (success or failure) is preserved and classified.
- **LangGraph** orchestrates workflows with explicit state transitions.
- **Pydantic v2** for all important structured data.
- **Playwright** for deterministic browser actions; LLMs only where language
  understanding, planning, or extraction is actually needed.
- **Deterministic domain logic** — comparison, deduplication, and validation stay
  separate from LLM logic.
- **Privacy by default** — sensitive fields are redacted everywhere.

## Repository Layout

```
.
├── backend/
│   ├── app/
│   │   ├── api/        # FastAPI routes (health, demo workflow)
│   │   ├── core/       # config, logging, tracing, redaction
│   │   ├── graph/      # LangGraph state + workflow
│   │   ├── models/     # Pydantic v2 models (demo + insurance intake schema)
│   │   └── insurance/  # canonical intake schema (Issue #2)
│   │   ├── services/   # domain services (registry, dedup, intake, route planner)
│   │   ├── browser/    # Playwright foundation interface
│   │   └── main.py     # FastAPI app factory
│   ├── tests/          # pytest suite
│   ├── requirements.txt
│   ├── requirements-dev.txt
│   └── pyproject.toml
├── frontend/           # React + Vite shell
├── .env.example        # environment template (placeholders only)
├── .gitignore
└── README.md
```

## Market Registry

Issue #3 added a **data-driven** Ontario market registry:

- `backend/data/market_registry/auto.json` — machine-readable AUTO discovery seed
  (31 records grounded in the hackathon brief Appendix A). Add/edit/remove a market by
  editing a JSON record — no code change.
- `MarketRegistryEntry` keeps **legal underwriter**, **insurer group**, **consumer
  brand**, and **distribution type** separate, plus a nullable `distinct_rate_source_id`
  for Issue #4 deduplication (never guessed).
- Records distinguish **seeded from the brief** (`status: discovered`,
  `source_citation: hackathon_brief`) from **verified during the hackathon**
  (`status: verified` + `last_verified_at`).
- Read-only API: `GET /api/v1/markets` (filters: `product_type`, `distribution_type`,
  `product_scope`) and `GET /api/v1/markets/{registry_id}`. Public market data only — no
  applicant PII.

## Rate-Source Dedup

Issue #4 added the **deterministic, evidence-based deduplication** layer on top of the
registry:

- **Authoritative mapping** — `MarketRegistryEntry.distinct_rate_source_id` is the single
  source of truth for route→rate-source. `DistinctRateSource` records in
  `backend/data/rate_sources/auto_rate_sources.json` describe known sources (program,
  underwriters, evidence) and their `related_registry_ids` are **consistency-checked**
  against the registry on load (a contradiction raises `DedupLoadError`).
- **Candidate vs confirmed** — `find_duplicate_candidates` surfaces possibilities from
  safe/public signals only; `evaluate_pair` runs a fixed priority chain
  (explicit id → verified program → same underwriter → same group → unresolved). Same
  insurer group alone is **never** a confirmed duplicate.
- **Explainable** — every pair decision carries `reason_code` + `confidence` + `evidence`.
- **Data-driven** — map/remap/merge routes by editing JSON; no code changes.
- **Honest metrics** — `GET /api/v1/dedup/metrics` currently reports
  `raw_route_count: 31, confirmed_rate_sources: 0, confirmed_duplicates: 0,
  unresolved_mappings: 31, possible_duplicates: 11` (nothing verified yet).

## Intake

Issue #5 added the **consent-aware progressive intake agent** (the reusable interface
that the Browser (#7) and Voice (#9) agents will call):

- **Field catalog** — `backend/data/intake/auto_fields.json` (data-driven
  `IntakeFieldDefinition`). Question text, order, enabled state, and new fields change
  via JSON, not workflow code (dynamic-change Scenarios A–G are tested).
- **Deterministic engine** — product gate (AUTO supported; HOME/TENANT/LIFE/TRAVEL/
  OTHER → `product_not_implemented`), progressive `next-question`, validated
  `submit_answer` (invalid values rejected, profile intact), `request_fields` for
  just-in-time/external discovery (already_known / requested / unsupported).
- **Profile vault** — `ProfileVault` Protocol with `InMemoryProfileVault` (dev/tests)
  and `EncryptedFileProfileVault` (Fernet, key from `INTAKE_VAULT_KEY` env only, data
  dir gitignored, no plaintext PII on disk).
- **Consent** — `ConsentReceipt` scopes: collection / route_disclosure /
  household_driver. Receipts store **paths, not values**. `RouteDataDisclosure` gives
  the applicant a field-sharing preview (APPROVE or EXCLUDE a route).
- **Checkpoints** — `HumanCheckpointKind` (identity_lookup, consent_attestation,
  application_declaration, signature, payment, purchase, …); signature/payment/
  purchase/binding are `must_not_automate`.
- **Privacy** — LangGraph state carries safe metadata only; raw answers travel through
  a pending-answer inbox, never into traces/logs/receipts. Profile summaries expose
  presence + counts, never values.
- **New optional field** — `applicant.address.years_at_current_address` added to the
  canonical schema (1.0 → 1.1) to demonstrate the "newly discovered question" flow.
- **API** — `POST /intake/sessions`, `/sessions/{id}`, `/next-question`, `/answers`,
  `/request-fields`, `/profile-summary`, `/route-disclosure`, `/consent`, DELETE.

## Route Planner

Issue #6 added the **deterministic per-route route planner** (`services/route_planner/`):

- **Product-aware** — AUTO sessions are planned; non-AUTO sessions produce an empty
  "not applicable" plan.
- **Per-route readiness** — a route is ready when IT has no blockers; a global draft
  profile can still have ready routes. A route can have **multiple simultaneous
  blockers** (`missing_field`, `consent_required`, `affinity_restricted`,
  `callback_required`, `human_required`, `specialty_only`, `rate_source_unresolved`).
- **Dedup integration** — confirmed duplicate groups emit a **primary + visible
  alternatives** (`is_alternative`, same distinct rate source); possible/unresolved
  duplicates stay visible (never suppressed).
- **Data-driven requirements** — `backend/data/routes/auto_route_requirements.json`
  (default + per-route canonical paths) plus a deterministic `MarketRequirement`→path
  map. No insurer-specific code.
- **Issue #5 integration** — the planner sees only presence booleans
  (`IntakeEngine.field_presence`); `request_missing_fields` asks the applicant for the
  union of genuinely-missing required fields once; `field_gate` flags
  household-driver consent needs as `human_required` blockers.
- **Deterministic ranking** — ready first, verified-source before unresolved, fewer
  blockers first, then alphabetical.
- **Route channels** — online / phone / callback / broker / human / discovery_only
  from registry fields.
- **Safe LangGraph** — `route_planner_workflow` carries counts + registry ids only;
  the plan contains canonical paths + public market data, never applicant values.
- **API** — `GET /api/v1/planner/plan`, `POST /api/v1/planner/plan/{id}/request-missing`.
- **Part 2 hardening** — 14 mandatory scenarios in `test_route_planner_hardening.py`
  (progressive resolution, ask-once, primary/alternative, never-suppress,
  data-driven merge/split, household-driver consent, all channel kinds, unknown-path
  safety, privacy). Full suite: **306 tests pass**, hermetic.

## Browser Quote Agent

Issue #7 added the **deterministic, observation-first browser quote agent**
(`app/browser/`):

- **Route-plan driven** — execution starts from a READY Issue #6 web `PlannedRoute`;
  `planned_route_id` maps to `registry_id` through ONE centralized compat shim
  (`app/browser/route_identity.py`).
- **Sandbox vs LIVE** — sandbox runs the local mock quote site with external
  requests blocked; LIVE requires the personal-use gate (`personal_use_confirmed` +
  `accurate_information_attested` + route consent) and a registry-verified permitted
  route, with privacy defaults (no video/trace/screenshot/HAR).
- **Deterministic executor** — one generic step (`BrowserExecutor.advance`): host
  recheck → consent recheck → access/quote/callback/validation detection →
  checkpoint gate → inspect → map → just-in-time vault fill → pause/navigate →
  observe.
- **Data-driven configuration** — `BrowserRouteConfig` + `BrowserFieldBinding`
  (matching strategies, fill strategies, controlled transforms). Dynamic fields and
  website changes (label/selector/order/optional→required/removal/type) are
  **config-only** — never an executor change.
- **Just-in-time vault access** — `IntakeEngine.get_field_value` is a narrow
  scalar-only boundary; values exist only in a local fill variable, never in state.
- **Issue #5 integration** — batched missing fields, ask-once, resume the same
  session, conditional-field re-inspection, consent expansion/revocation, and the
  household-driver consent gate.
- **Safety** — CAPTCHA/access-control stops (no bypass), human checkpoints,
  prohibited declaration/signature/payment/purchase boundaries, allowed-host
  protection, crash/timeout → safe technical observation.
- **Observations only** — `RawQuoteObservation` (annual/monthly, estimate vs firm,
  private reference handle), callback/manual handoff (no call placed), unknown /
  validation / ambiguity / unsupported-value / technical observations. **No Issue #8
  terminal/retry/failover logic.**
- **LangGraph** — `browser_workflow` is a generic bounded loop (no node per
  field/page/insurer) with safe metadata-only state; each node is traceable.
- **API** — `POST /api/v1/browser/sessions`, `POST /{id}/run`, `POST /{id}/resume`,
  `GET /{id}`, `DELETE /{id}`.
- **Local mock demo** — `backend/demos/issue7_browser_demo.py` (happy / missing /
  unknown / safety / callback / dynamic / second-route scenarios).

## Recovery Engine

Issue #8 added the **deterministic terminal-status & recovery layer** (`app/services/recovery/`):

- **Decision layer only** — given a planned route + latest `ExecutionObservation` +
  prior attempts + policy + current consent/readiness, it answers: resume / retry /
  fail over / handoff / terminal. It **never** launches a browser, places a call, or
  collects answers (Issue #7 / #9 / #5 own those).
- **State separation** — `RouteReadiness` (#6) vs `ExecutionObservation` (#7/#8) vs
  `AttemptLifecycleStatus` (#8) vs `RouteOutcomeStatus` (#8) are distinct models.
- **Deterministic classification** — `classify_observation()` maps a structured
  observation (via `browser_observation_to_execution`) to execution-result kind,
  `Retryability`, reason codes, and failover eligibility. No LLM, no insurer branches.
- **Data-driven policy** — `RecoveryPolicy` (`data/recovery/auto_policy.json`):
  `max_attempts_per_route=2`, `max_attempts_per_rate_source=3`,
  `max_attempts_per_plan=6`, transient-retry toggles, failover toggle. Changing data
  changes behavior without engine code.
- **Bounded budgets** — per route, per rate source (all routes sharing one
  `distinct_rate_source_id`), and per plan; pauses consume no budget.
- **Failover** — ready alternatives from Issue #6 (`PlannerRouteSource`), deterministic
  ordering, never re-uses an exhausted route, never inflates the distinct-source count.
- **Live rechecks** — alternative readiness (fresh `plan()`) and Issue #5 consent
  (`IntakeConsentSource`) are re-checked before retry/failover; no stale copies.
- **Pause ≠ failure** — missing field / consent / resumable checkpoint / unknown field /
  correctable validation → `paused`, no budget. Resume reuses the same attempt; retry is
  a new attempt; browser-session-loss is handled explicitly.
- **Safety** — CAPTCHA/bot/auth/prohibited (signature/payment/purchase) boundaries are
  never retried, bypassed, or failover-circumvented.
- **Terminal outcomes** — evidence-backed `blocked` / `callback_required` /
  `manual_handoff` / `ineligible` / `affinity_restricted` / `specialty_only` /
  `not_currently_writing` / `unreachable` / `unresolved`; `duplicate_rate_source` for
  unused alternatives; quote → `quote_pending_normalization` (comparability deferred).
- **Hardening** — terminal immutability + explicit `enrich_terminal()`, idempotency
  (observation key + sequence), stale/out-of-order guard, transition validation
  (`TransitionError`), `AttemptStore` Protocol (in-memory; Issue #10 can replace it).
- **LangGraph** — `recovery_workflow` (`initialize → load_attempt_history →
  classify_observation → decide`), safe metadata-only state, safe-context allowlist
  for LangSmith.
- **API** — `POST /api/v1/recovery/decisions`, `GET /api/v1/attempts/{id}`,
  `GET /api/v1/route-plans/{plan_id}/attempts`.
- **Demo** — `backend/demos/issue8_recovery_demo.py` (22 scenarios, synthetic only).

## Insurance Intake Schema

Issue #2 introduced the canonical insurance intake models under
`backend/app/models/insurance/`. The architecture is **product-aware** and
**composable**:

```
InsuranceProfile
├── schema_version      # "1.0"
├── insurance_type      # InsuranceType (AUTO only product implemented)
├── consent             # ConsentState (shared)
├── applicant           # ApplicantInformation: identity + contact + address (shared)
└── product_data        # AutoInsuranceProfile | None
                            ├── drivers      (licence, timeline, training, assignment, discounts)
                            ├── vehicles     (identity/VIN, ownership, use, risk, special use)
                            ├── household    (members, dependants, fleet, assignments)
                            ├── history      (current insurance, claims, convictions, ...)
                            └── coverage     (liability, accident benefits, own damage, OPCF, ...)
```

- **AUTO** is fully implemented; **HOME/TENANT/LIFE/TRAVEL/OTHER** are recognized
  by `InsuranceType` but unsupported — `product_data` stays `None` and
  `InsuranceProfile.is_supported` is `False`.
- **Composition over a flat model**: shared applicant/consent data lives once at
  the `InsuranceProfile` level; only auto-specific data goes in
  `AutoInsuranceProfile`. No duplicated fields per product.
- **Privacy by default**: all models extend `SensitiveBaseModel`, which provides
  `redacted_dict()`/`safe_dict()` (reuses `app/core/redaction.py`) and redacted
  `repr`/`str`, so logging or tracing a profile never leaks licence numbers, DOB,
  VIN, addresses, phone/email, or claims details.
- **Schema helpers**: `required_for_live_quote()`, `get_missing_fields()`, and
  `trace_metadata()` (safe, non-sensitive metadata) are lightweight schema-layer
  hooks; the full intake engine is Issue #5.
- **No premium/quote logic** yet — coverage models are configuration only.

## Prerequisites

- Python 3.11+
- Node.js 18+ and npm
- (Optional) LangSmith account + API key for remote tracing
- Playwright + Chromium for the Issue #7 browser agent (see Backend Setup)

## Backend Setup

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # PowerShell
pip install -r requirements-dev.txt
# Issue #7 browser agent runtime (headless Chromium for tests/demos):
python -m playwright install chromium
```

Create your local environment file at the **repo root**:

```powershell
Copy-Item ..\.env.example ..\.env   # then edit values
```

Run the API:

```powershell
cd backend
uvicorn app.main:app --reload
```

- Health check: <http://localhost:8000/health>
- API docs: <http://localhost:8000/docs>
- Demo workflow: `POST /api/v1/demo/workflow`

Example demo workflow call:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/v1/demo/workflow `
  -ContentType "application/json" -Body '{"input_text":"hello"}'
```

## Frontend Setup

```powershell
cd frontend
npm install
npm run dev
```

Open <http://localhost:5173> — the shell shows the project title and a live
backend health indicator (the Vite dev server proxies `/health` to `:8000`).

## Environment Variables

See [`.env.example`](./.env.example). Key variables:

| Variable              | Purpose                                   | Default               |
|-----------------------|-------------------------------------------|-----------------------|
| `APP_ENV`             | development / test / production           | `development`         |
| `API_HOST` / `API_PORT`| API bind address / port                  | `0.0.0.0` / `8000`    |
| `FRONTEND_ORIGINS`    | CORS allowed origins (comma-separated)    | `http://localhost:5173` |
| `LANGSMITH_TRACING`   | Enable LangSmith tracing                  | `false`               |
| `LANGSMITH_API_KEY`   | LangSmith API key (never committed)       | *(empty)*             |
| `LANGSMITH_PROJECT`   | LangSmith project name                    | `ontario-allquote-agent` |
| `LANGSMITH_ENDPOINT`  | LangSmith API endpoint — this account uses the **EU** instance | `https://eu.api.smith.langchain.com` |
| `DATABASE_URL`        | Postgres placeholder (Issue 1: unused)    | *(empty)*             |
| `LLM_*`               | LLM provider placeholder (Issue 1: unused)| *(empty)*             |
| `BROWSER_HEADLESS`    | Headless Chromium (tests); `false` for demos | `true`            |
| `BROWSER_SLOW_MO_MS`  | DEV/DEMO ONLY: Playwright per-action delay (ms); 0 = none | `0`       |
| `BROWSER_LIVE_GATE_REQUIRED` | Require the LIVE personal-use gate | `true`        |
| `BROWSER_SCREENSHOT_ENABLED` | Screenshots (must be redacted; OFF for LIVE) | `false`   |
| `BROWSER_MAX_STEPS`   | Bounded browser steps per run            | `20`                 |
| `BROWSER_IDLE_TIMEOUT_SECONDS` | Abandoned-session cleanup timeout | `600`           |
| `RECOVERY_POLICY_DIR` | Issue #8: directory containing `auto_policy.json` | *(auto: `backend/data/recovery`)* |

Credentials are **never** hardcoded. Only placeholders are committed.

## LangSmith Setup

This project's LangSmith account uses the **EU instance** (workspace 1, Personal
Access Token). Configure it in `..\.env`:

```dotenv
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=<your-personal-access-token>
LANGSMITH_PROJECT=ontario-allquote-agent
LANGSMITH_ENDPOINT=https://eu.api.smith.langchain.com
```

> On the US instance instead? Use `LANGSMITH_ENDPOINT=https://api.smith.langchain.com`
> and the UI at <https://smith.langchain.com>.

Then:

1. Restart the backend.
2. Call `POST /api/v1/demo/workflow`.
3. Open the LangSmith UI — **EU instance: <https://eu.smith.langchain.com>** —
   project `ontario-allquote-agent` — and inspect the trace. The run contains the
   workflow stages as child runs, plus metadata/tags (`environment`, `workflow`,
   `workflow_stage`, `request_id`).

Tracing is designed so future nodes (route decisions, quote attempts, normalization,
comparison) correlate with a run/trace ID propagated through the workflow: the API
returns a `request_id` that doubles as the LangSmith root run id / trace id, so you
can search traces by that id.

> 🔒 **Sensitive values must NEVER be traced or logged.** Driver's licence numbers,
> full addresses, date of birth, VINs, claims information, phone numbers, email
> addresses, voice/transcript data, and other sensitive insurance fields are
> excluded from traces, prompts, logs, screenshots, test fixtures, and source
> control. The redaction utility (`app/core/redaction.py`) and the logging filter
> enforce this — see the Safety & Privacy Policy below.

To verify tracing locally without uploading, run the tests (tracing wiring is
asserted in `tests/test_workflow.py`); the test suite forces `LANGSMITH_TRACING=false`
so it stays hermetic.

## Playwright / Browser Agent

Issue #7 implements the **browser quote agent** (`app/browser/`) on top of the
Issue #1 Playwright foundation. The browser runs against the **local mock quote
site** for automated tests/demos (`app/browser/mock_site.py`) — no real insurer
websites are ever automated in tests. LIVE execution additionally requires the
personal-use gate and a registry-verified permitted route (none exists in the
current seed: `no_verified_live_browser_route`).

```powershell
python -m playwright install chromium
```

It never bypasses CAPTCHAs, auth, bot controls, or rate limits.

## Running Tests

```powershell
cd backend
.\.venv\Scripts\Activate.ps1
# full suite (Issues #1-#7):
$env:PYTHONPATH='tests'
python -m pytest
# Issue #7 browser tests only:
python -m pytest tests/test_browser_models.py tests/test_browser_config.py tests/test_browser_route_start.py tests/test_browser_executor.py tests/test_browser_observations.py tests/test_browser_privacy.py tests/test_browser_workflow.py tests/test_browser_api.py tests/test_browser_hardening_unit.py tests/test_browser_hardening.py -q
```

The suite is **hermetic**: tracing is forced off, browser tests use the local mock
quote site with external requests blocked — zero real insurer / LLM / external API /
LangSmith traffic.

## Local Mock Browser Demo

```powershell
cd backend
$env:PYTHONPATH='tests;.'
.\\.venv\Scripts\python.exe demos\issue7_browser_demo.py happy        # STANDARD profile -> quote
.\\.venv\Scripts\python.exe demos\issue7_browser_demo.py missing      # pause -> Issue #5 -> resume -> quote
.\\.venv\Scripts\python.exe demos\issue7_browser_demo.py unknown      # unknown required field pause
.\\.venv\Scripts\python.exe demos\issue7_browser_demo.py safety      # CAPTCHA / checkpoint / prohibited
.\\.venv\Scripts\python.exe demos\issue7_browser_demo.py callback    # callback handoff observation
.\\.venv\Scripts\python.exe demos\issue7_browser_demo.py dynamic     # config-only site change + second route
.\\.venv\Scripts\python.exe demos\issue7_browser_demo.py all         # everything
```

### Visual (headful) demo — watch Chromium drive the mock site

Opens a **visible Chromium window** against the local mock quote site so you can
watch the real Browser Agent fill forms (text / SELECT dropdown / radio /
checkbox / DATE / integer), click through the applicant → vehicle → commute →
quote pages, handle a JS conditional reveal, and land on the final synthetic
quote page. Uses the Playwright `slow_mo` dev delay (default 700 ms) and keeps
the browser open afterwards for inspection.

```powershell
cd backend
$env:PYTHONPATH='tests;.'
# STANDARD_COMPLETE profile -> applicant -> vehicle -> commute -> QUOTE
.\\.venv\Scripts\python.exe demos\issue7_browser_visual_demo.py happy --slow-ms 700 --hold-seconds 20
# conditional reveal (commuting=Yes -> one-way distance appears) -> QUOTE
.\\.venv\Scripts\python.exe demos\issue7_browser_visual_demo.py conditional --slow-ms 700 --hold-seconds 20
```

`--slow-ms` is a DEV/DEMO-only Playwright per-action delay (default 700; 0 = none,
identical to tests/production). `--hold-seconds` keeps the browser open N seconds;
omit it to instead wait for Enter. Synthetic data only — sandbox mode still blocks
external requests and never touches a real insurer or bypasses safety controls.

## Terminal Status & Recovery Demo (Issue #8)

Deterministic terminal-status + recovery decisions against synthetic observation
streams (no browser, no insurer, no real data):

```powershell
cd backend
$env:PYTHONPATH='tests;.'
.\\.venv\Scripts\python.exe demos\issue8_recovery_demo.py
```

Covers 22 scenarios: pause / consent pause / consent denied / bounded retry /
route exhaustion / rate-source exhaustion / failover / multi-alternative chain /
CAPTCHA no-failover / unknown field / validation subtypes / callback / manual /
ineligible / affinity / specialty / not-writing / quote pending normalization /
estimate / duplicate unused vs executed / dynamic policy 2→3 / idempotency /
privacy sanitization.

## Web End-to-End Demo (Issue #8.5 integration checkpoint)

A minimal React wizard that visually drives the real backend chain
intake (#5) → route planner (#6) → browser agent (#7) → recovery (#8), polling
a safe `ComparisonJob`. This is NOT the Issue #13 dashboard. Mock is the safe
default; LIVE stays explicit and is not configured (no verified route configs).

Launch both servers, then open **http://localhost:5173/**:

```powershell
# Terminal 1 — backend (also starts the local mock quote site on 127.0.0.1:8765)
cd backend
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# Terminal 2 — frontend
cd frontend
npm run dev
```

Flow: **Auto Insurance** → **Fill demo profile** (canonical synthetic persona from
`GET /api/v1/demo/personas/standard-auto`, mock-only) → **Continue to Review &
Consent** → review your data + provider disclosure (canonical paths shared) →
tick both consent boxes → **Compare Quotes** → the local mock site is driven by
Playwright → per-provider statuses (Searching / Quote received / Duplicate rate
source / …) → **Quote received — pending coverage normalization** (`$1,234.56/yr`).

Key endpoints added:
- `GET /api/v1/intake/catalog?product=auto` — data-driven field catalog (the UI
  renders from this; no hardcoded insurance schema in React).
- `GET /api/v1/demo/personas/standard-auto?mode=mock` — synthetic persona; refused
  for live mode (403).
- `POST /api/v1/orchestrate/compare` + `GET /api/v1/orchestrate/jobs/{id}` —
  pollable glue over #6→#7→#8 (mock = isolated `backend/data/demo` overlay;
  live = real registry + existing live gates).
- Mode-aware `GET /planner/plan?mode=mock|live` and consent/disclosure endpoints
  (`?mode=mock`) so synthetic routes never touch the real market registry.

**Demo/mock isolation:** synthetic entries live under `backend/data/demo/` and
are loaded only for `execution_mode=mock`; the real `data/market_registry`,
dedup metrics, and live execution are never affected (regression-tested).

## Verifying Tracing

1. Confirm tracing is configured:
   `python -c "from app.core.config import get_settings; s=get_settings(); print(s.langsmith_tracing, s.langsmith_endpoint)"`
2. Call the demo endpoint and note the returned `request_id` (e.g. `8f951133...`).
3. Open the LangSmith UI — **EU instance: <https://eu.smith.langchain.com>** —
   project `ontario-allquote-agent`, and **search by that `request_id`**. The trace
   tree should show `LangGraph` → `stage_one` → `stage_two` (each stage is an
   individually traced node run sharing the same trace id).
4. Confirm no sensitive fields appear in the trace payload or logs (the redaction
   utility and logging filter enforce this).

## Safety & Privacy Policy

- Never fabricate licence numbers or identity information; never use another
  person's personal information without appropriate consent.
- Never bypass CAPTCHAs, authentication, bot controls, rate limits, or access
  restrictions — stop and classify the result when a barrier is encountered.
- Stop before application declarations, signatures, payment, purchase, binding,
  renewal, cancellation, or policy modification.
- Preserve failed attempts instead of hiding them.
- Redact licence numbers, full addresses, DOB, VIN, claims, phone/email, voice
  transcripts, and other sensitive fields from logs, traces, screenshots, fixtures,
  and source control.
