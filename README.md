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

**Issues 1–2 complete** — foundation + canonical insurance intake schema.

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

Later milestones add: Ontario market registry, route planner, quote retrieval,
terminal-status handling, evidence store, normalization, comparability engine,
coverage ledger, and the dashboard API.

## Architecture Overview

```
┌────────────┐   HTTP/CORS   ┌────────────────────────────────────────────┐
│  frontend/ │ ────────────▶ │  backend/app/main.py  (FastAPI)            │
│  React+Vite│               │  ┌──────────────────────────────────────┐  │
└────────────┘               │  │ core/  config · logging · tracing    │  │
                             │  │        redaction                     │  │
                             │  ├──────────────────────────────────────┤  │
                             │  │ graph/  typed state + workflow       │  │
                             │  │ api/    routes (health, demo)        │  │
                             │  ├──────────────────────────────────────┤  │
                             │  │ browser/  Playwright interface (TBD) │  │
                             │  │ models/   Pydantic v2 models         │  │
                             │  │ services/ domain logic (future)      │  │
                             │  └──────────────────────────────────────┘  │
                             └──────────────┬─────────────────────────────┘
                                            │  LangSmith tracing (env-configured)
                                            ▼
                                      LangSmith traces
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
│   │   ├── services/   # domain services (future milestones)
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
- (Optional, later) Playwright browser runtime

## Backend Setup

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # PowerShell
pip install -r requirements-dev.txt
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

## Playwright Installation

Issue 1 only defines the browser foundation — **no insurer websites are automated yet**.

```powershell
pip install playwright
playwright install chromium
```

The `BrowserManager` interface in `app/browser/` is an async wrapper ready for later
milestones. It never bypasses CAPTCHAs, auth, bot controls, or rate limits.

## Running Tests

```powershell
cd backend
.\.venv\Scripts\Activate.ps1
pytest
```

The suite covers:

- `GET /health` endpoint
- Demo LangGraph workflow execution (direct + via API)
- Configuration loading / env overrides
- Sensitive-data redaction utility

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
