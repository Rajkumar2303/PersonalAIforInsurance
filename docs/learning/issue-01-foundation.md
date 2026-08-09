# Issue #1 — Project Setup, Architecture & Observability

**Status:** ✅ Implemented & verified
**Milestone:** Foundation only — no insurance logic yet.

---

## 1. What was built

A clean monorepo for the Ontario All-Quote Agent, with only the **foundation**:

- **`backend/`** — FastAPI + Pydantic v2 + LangGraph + LangSmith + Playwright (interface only) + pytest.
- **`frontend/`** — minimal Vite + React 18 shell with a live backend health indicator.
- **Root files** — `README.md`, `.env.example` (placeholders only), `.gitignore`.

Implemented behaviors:
- `GET /health` → `{"status": "ok"}`.
- `POST /api/v1/demo/workflow` runs a minimal **2-node LangGraph workflow** (`stage_one → stage_two`), returns the stages + a `request_id`, and uploads the run to **LangSmith (EU instance)**.
- Structured, privacy-aware logging with correlation fields.
- A reusable **sensitive-data redaction** utility used by logging (and designed for tracing/future modules).
- A `BrowserManager` async wrapper — **interface only**, no insurer automation.

Explicitly **not** built (future milestones): insurance profile, Ontario market registry, route planner, quote retrieval, terminal-status handling, evidence store, normalization, comparability, voice handoff, dashboard.

---

## 2. Why it was built

The project's end goal is an **evidence-first insurance-shopping operator**, not a
chatbot: it must make clear what markets were discovered, which routes were
attempted, what succeeded/failed and why, whether results are comparable, whether
routes map to duplicate rate sources, and what evidence backs every outcome.

Issue #1 existed to establish the **observable, privacy-safe foundation** every later
issue depends on — orchestration (LangGraph), tracing (LangSmith), structured
redacting logs, configuration, a test harness, and a run/trace correlation story —
before any insurance logic exists. Getting these right early is what makes a failed
quote journey debuggable end-to-end later.

---

## 3. How the implementation works

### 3.1 Configuration (`backend/app/core/config.py`)

- `Settings(BaseSettings)` reads from **environment variables**, then a repo-root
  `.env` file, then declared defaults. `REPO_ROOT` is computed as
  `Path(__file__).resolve().parents[3]`, so the `.env` is found regardless of CWD.
- `get_settings()` is `@lru_cache`d (singleton).
- Key fields: `app_env`, `app_name`, `api_host`, `api_port`, `frontend_origins`
  (comma-separated → `cors_origins` property), `langsmith_tracing`,
  `langsmith_api_key`, `langsmith_project`, `langsmith_endpoint`, and placeholders
  `database_url`, `llm_*` (unused in Issue 1).

### 3.2 FastAPI app (`backend/app/main.py`)

`create_app()`:
1. `configure_tracing(settings)` — copies LangSmith settings into `os.environ`
   (`LANGSMITH_TRACING`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT`,
   `LANGSMITH_ENDPOINT`) so the LangSmith SDK picks them up.
2. `setup_logging()` — installs the redacting structured handler.
3. Adds CORS middleware (`allow_origins=settings.cors_origins`).
4. Registers the `health` and `demo` routers; sets a `lifespan` that logs startup/shutdown.

A module-level `app = create_app()` makes it runnable via `uvicorn app.main:app`.

### 3.3 LangGraph workflow (`backend/app/graph/`)

- `state.py` — `DemoWorkflowState(TypedDict, total=False)` with `input_text`,
  `stage`, `steps` (`Annotated[list[str], operator.add]` — an **additive reducer** so
  nodes append to one list), and `final_output`.
- `workflow.py` — `stage_one()` normalizes input and appends `"stage_one"`;
  `stage_two()` builds `"processed: <input>"`. `build_demo_workflow()` wires
  `START → stage_one → stage_two → END` in a `StateGraph` and compiles it.
  `WORKFLOW_NAME = "demo_workflow"`.

### 3.4 API layer (`backend/app/api/`)

- `health.py` — `GET /health`.
- `demo.py` — `POST /api/v1/demo/workflow`:
  1. Generates `request_id = uuid.uuid4().hex`.
  2. `set_log_context(request_id=..., workflow=..., workflow_stage="start", status="running")`.
  3. Builds `run_config(settings, request_id=request_id, workflow=WORKFLOW_NAME)`.
  4. `await graph.ainvoke({"input_text": ...}, config=config)` (async-friendly).
  5. Returns `DemoWorkflowResponse` (`request_id`, `workflow`, `stages`, `final_output`, `status`).
  6. On failure → `HTTPException(500)` with `request_id`; `finally: clear_log_context()`.

### 3.5 Pydantic v2 models (`backend/app/models/demo.py`)

- `DemoWorkflowRequest` (`input_text: str = "hello"`, `extra="forbid"`).
- `DemoWorkflowResponse` (see above). **No insurance schemas** — intentionally.

### 3.6 Browser foundation (`backend/app/browser/manager.py`)

`BrowserManager` is an async context manager around Playwright Chromium: `start()`
(lazy-imports `playwright.async_api` so the app imports even if Playwright is
missing; raises `BrowserRuntimeError` with install instructions), `stop()`,
`is_running`, `__aenter__`/`__aexit__`. **No page automation in Issue 1.**

### 3.7 Frontend (`frontend/`)

Vite + React 18 (JS). `App.jsx` shows the title/tagline; `HealthStatus.jsx` polls
`/health` every 15 s and renders an online/offline/loading dot. `vite.config.js`
proxies `/health` → `http://localhost:8000` in dev.

---

## 4. How data flows through it

```mermaid
sequenceDiagram
    participant FE as React shell (HealthStatus)
    participant API as FastAPI demo endpoint
    participant Ctx as log context (contextvars)
    participant G as LangGraph demo_workflow
    participant LS as LangSmith (EU)

    FE->>API: GET /health
    API-->>FE: {"status":"ok"}

    Note over API: POST /api/v1/demo/workflow
    API->>API: request_id = uuid4().hex
    API->>Ctx: set_log_context(request_id, workflow, stage=start, status=running)
    API->>G: ainvoke(input_text, config{metadata, tags, run_id=request_id})
    G->>G: stage_one (set_stage; logs; append "stage_one")
    G->>G: stage_two (set_stage; logs; append "stage_two"; final_output)
    G->>LS: upload trace (trace_id = request_id; node runs as children)
    G-->>API: {"stage":"stage_two","steps":[...],"final_output":...}
    API->>Ctx: status=success; finally clear_log_context()
    API-->>FE: {request_id, workflow, stages, final_output, status}
```

**Correlation contract:** the API's `request_id` is set as the LangGraph config
`run_id`, which becomes the LangSmith **root run id and trace id**. Every node run
has its own run id but shares the same `trace_id`, so a trace is greppable by
`request_id`. (Verified end-to-end against the EU instance.)

---

## 5. Important files / classes / functions

| Concern | Location | Symbol(s) |
|---|---|---|
| Settings | `backend/app/core/config.py` | `Settings`, `get_settings()`, `cors_origins` |
| Structured logging | `backend/app/core/logging.py` | `set_log_context`, `clear_log_context`, `RedactingContextFilter`, `RedactingFormatter`, `setup_logging`, `STRUCTURED_FIELDS` |
| Redaction | `backend/app/core/redaction.py` | `REDACTED`, `is_sensitive`, `redact_text`, `redact_data`, `redact_value`, `redact_kwargs` |
| LangSmith wiring | `backend/app/core/tracing.py` | `configure_tracing`, `run_config`, `set_stage`, `tracing_enabled` |
| Workflow state | `backend/app/graph/state.py` | `DemoWorkflowState` |
| Workflow | `backend/app/graph/workflow.py` | `stage_one`, `stage_two`, `build_demo_workflow`, `WORKFLOW_NAME` |
| Routes | `backend/app/api/health.py`, `backend/app/api/demo.py` | `health`, `run_demo_workflow` |
| Models | `backend/app/models/demo.py` | `DemoWorkflowRequest`, `DemoWorkflowResponse` |
| Browser | `backend/app/browser/manager.py` | `BrowserManager`, `BrowserRuntimeError` |
| App factory | `backend/app/main.py` | `create_app` |
| Tests | `backend/tests/` | `conftest.py`, `test_health.py`, `test_workflow.py`, `test_config.py`, `test_redaction.py` |
| Env template | `.env.example` | placeholders for `LANGSMITH_*`, `APP_*`, etc. |

---

## 6. Architectural decisions

- **Evidence-first operator, not chatbot.** Shaped everything: run/trace correlation,
  structured logs, redaction — so failures are preserved and debuggable.
- **LangGraph for orchestration with typed state.** Explicit state transitions
  (`START → stage_one → stage_two → END`); reducers (`operator.add`) for append-style
  state. This is the base for future workflows (intake, route planning, quote runs).
- **Pydantic v2 for all structured data.** Typed request/response, strict extra
  fields (`extra="forbid"`), and the future home for domain models.
- **Playwright for deterministic actions; LLMs only where needed.** Only the
  foundation interface exists now.
- **Domain logic separate from infrastructure.** `core/` (infra) vs `services/`
  (future domain) vs `graph/` (orchestration) vs `browser/` (automation).
- **Observability configured by env vars only.** No hardcoded credentials.
- **`request_id` == LangSmith `run_id`/`trace_id`.** A single ID links HTTP → log →
  trace, making a failed journey debuggable end-to-end.
- **Hermetic tests.** `conftest.py` forces `LANGSMITH_TRACING=false` so `pytest`
  never uploads to the real project.
- **No Docker.** Not needed for Issue 1 (kept minimal).

---

## 7. Alternatives and tradeoffs (and why they were not used)

- **Settings via `python-dotenv` vs `pydantic-settings`.** Chose `pydantic-settings`
  — typed, validated, default-aware, single source. `python-dotenv` only loads raw
  strings.
- **Sync vs async FastAPI/browser.** Chose async (uvicorn + `await graph.ainvoke`,
  async `BrowserManager`) to suit future Playwright/HTTP work. Tradeoff: LangGraph's
  sync `invoke` is also available for scripts/tests.
- **`contextvars`-based log context vs thread-locals/globals.** Contextvars are
  async-task-safe — the right fit for FastAPI. Tradeoff: it must be set/cleared per
  request (done in the endpoint + `finally`).
- **Redaction by key-name + regex vs single regex on whole JSON dump.** Key-based
  redaction is precise and cheap for structured data; regex patterns catch values in
  free text. Tradeoff: regex can over-redact (accepted — privacy-first).
- **`run_id` in config vs custom callback handler.** Setting `config["run_id"]` is
  minimal and gives direct `request_id ↔ trace_id` correlation without extra code.
- **Committed `.env.example` placeholders + gitignored `.env`** vs committing `.env`.
  The latter would leak secrets — never done.
- **Not used:** a full multi-agent LangGraph design (overkill for the foundation),
  Docker, a database or LLM dependency (not required by Issue 1), and an
  autonomous LLM browser loop (explicitly deferred).

---

## 8. LangGraph behavior (as observed)

- A compiled graph is callable via `invoke` (sync) and `ainvoke` (async).
- Reducer-annotated keys (`steps: Annotated[list, operator.add]`) accumulate across
  nodes in one run — node `stage_one` and `stage_two` both append.
- When tracing is enabled, LangGraph wraps the **graph** and **each node** as runs
  automatically (no per-node instrumentation needed).
- Setting `config["run_id"]` makes the root run use that id; node runs get their own
  ids but share the root's `trace_id` (verified: `trace_id == request_id`,
  `node_run_id != request_id`).

---

## 9. LangSmith tracing & observability (as observed)

- Configured purely through env vars (`.env` → `Settings` → `configure_tracing` →
  `os.environ`). No secrets in code.
- This account uses the **EU instance**: `LANGSMITH_ENDPOINT=https://eu.api.smith.langchain.com`,
  UI at `https://eu.smith.langchain.com`, project `ontario-allquote-agent`,
  **Personal Access Token** created under **workspace 1**.
- Metadata/tags are non-sensitive identifiers only: `environment`, `workflow`,
  `workflow_stage`, `request_id` + tags `["ontario-allquote-agent", workflow, app_env]`.
- `set_stage()` annotates the current node run via `langsmith.get_current_run_tree()`
  (best-effort, wrapped in try/except).
- Verified end-to-end: `POST /api/v1/demo/workflow` → returned `request_id` was found
  in LangSmith as the root run (`name=LangGraph`, `status=success`), and
  `root_id == trace_id == request_id`.
- SDK note: the classic `Client.read_run()` is deprecated (removed ~Jan 2027) in
  favor of the async `client.runs.retrieve(run_id, project_id=...)` — a future
  migration task, not needed by the app itself.

---

## 10. Privacy / security implications

- **Never trace/log** licence numbers, full addresses, DOB, VIN, claims, phone,
  email, voice/transcripts, or other sensitive insurance data. Enforced by:
  - `redact.py` key-name patterns + text regexes (email, phone, Ontario licence
    `L####-#######-####`, 17-char VIN, `YYYY-MM-DD`).
  - `RedactingContextFilter` redacts every log message before output.
  - `run_config` metadata is constructed with identifiers only.
- `.env` is gitignored (verified with `git check-ignore .env` and a filesystem
  sweep); `.env.example` is placeholders only. A real key accidentally placed in
  `.env.example` earlier was moved to `.env` — `.env.example` must never contain
  secrets.
- Future modules must pass sensitive values through `redact_kwargs`/`redact_data`
  before logging/tracing.

---

## 11. Testing strategy

- **Unit tests, no external calls:** `pytest` (19 tests).
- `conftest.py` sets `LANGSMITH_TRACING=false` before importing `app.main` → hermetic.
- Coverage: health endpoint, workflow (sync + async + via API), run-config
  metadata/tags/run-id, settings defaults + env overrides + CORS parsing + caching,
  redaction (key-based, text patterns, nesting, kwargs, logging-filter), and the
  `_env_file=None` trick to make `test_defaults` independent of a local `.env`.
- Real LangSmith uploads are **not** part of the automated suite; they were verified
  manually/with a temporary script (deleted).

---

## 12. Failure scenarios

| Scenario | Behavior | Where handled |
|---|---|---|
| LangSmith key missing/invalid, or wrong region | App runs; upload fails with 401/403; warning logged ("Failed to multipart ingest runs") | best-effort SDK; `configure_tracing` only sets vars |
| Wrong endpoint (US vs EU) | `/info` 200 but `/sessions`/`/runs` 403 → key has no project access | diagnose via endpoint auth checks |
| Graph node raises | `HTTPException(500)` with `request_id`; error logged with `error_type` | `demo.py` try/except |
| `.env` missing | Defaults apply; tracing off | `Settings` defaults |
| Playwright not installed | `BrowserRuntimeError` on `start()` with install hint | lazy import in `manager.py` |
| Test run with tracing on | Would upload to real project — prevented by `conftest.py` forcing it off | `conftest.py` |

---

## 13. Debugging approach

1. **Start clean:** `cd backend; .\.venv\Scripts\python.exe -m uvicorn app.main:app --reload`
   → watch the structured logs (each line has `request_id=... workflow=...
   workflow_stage=... status=...`).
2. **Hit the API:** `Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/v1/demo/workflow
   -ContentType "application/json" -Body '{"input_text":"hi"}'`.
3. **Check settings are what you expect:**
   `python -c "from app.core.config import get_settings; s=get_settings(); print(s.langsmith_tracing, s.langsmith_endpoint)"`.
4. **Trace correlation:** take the returned `request_id`, search it in the EU
   LangSmith UI → expect `LangGraph → stage_one → stage_two`.
5. **Isolate:** run one test: `pytest tests/test_workflow.py -q`.
6. **LangSmith auth triage:** a 200 on `/info` does **not** prove write access —
   check `/sessions` and `/runs`; 403 there usually means wrong region/key
   permissions, 401/Invalid token means the key/header itself is bad.

---

## 14. Common misunderstandings

- **`.env.example` vs `.env`:** the example is committed and must stay placeholder;
  the real `.env` is gitignored. Keys go only in `.env`.
- **`/info` 200 ≠ authorized:** `/info` is effectively public; real access shows on
  `/sessions` and `/runs`.
- **US vs EU instance:** this account's key only works on
  `https://eu.api.smith.langchain.com`. The US host returns 403.
- **`run_id` vs `trace_id` vs node run id:** root run id = trace id = `request_id`;
  node runs are separate ids under the same trace.
- **`app = create_app()` at import time** sets `LANGSMITH_TRACING` in `os.environ` —
  which is why the test suite must force it off before importing `app.main`.
- **`test_defaults` needs `_env_file=None` + cleared env vars**, otherwise a local
  `.env` (or process env) breaks it.
- **LangGraph reducers:** `steps` accumulates because of `operator.add`, not because
  nodes share a mutable list.
- **Playwright installed ≠ browsers installed:** the Python package is in deps; the
  browser runtime needs `playwright install chromium` (only when automation starts).

---

## 15. Interview explanation (30-second version)

> "We built the foundation of an evidence-first Ontario auto-insurance shopping
> assistant. A FastAPI backend exposes a health endpoint and a demo endpoint that
> runs a minimal two-node LangGraph workflow. Every run gets a `request_id` that is
> also set as the LangSmith `run_id`, so the HTTP request, the structured logs, and
> the LangSmith trace are all correlated by one ID — which is how we'll debug a
> failed quote journey later. Observability is env-configured (EU LangSmith
> instance), logging redacts sensitive fields like licence numbers, DOB, and VIN
> automatically, and the whole thing is covered by a hermetic pytest suite. No
> insurance logic yet — that's deliberately deferred to later issues."

---

## 16. Self-test questions

1. How is the `.env` located regardless of CWD? (`REPO_ROOT = parents[3]`)
2. What does `config["run_id"] = request_id` buy us? (trace ↔ request correlation)
3. Why is `steps` an `Annotated[list[str], operator.add]`? (additive reducer across nodes)
4. Which endpoints exist in Issue 1, and what do they return?
5. How does `RedactingContextFilter` prevent sensitive data in logs?
6. Why must `conftest.py` set `LANGSMITH_TRACING=false`?
7. What is the difference between a node `run_id` and the graph `trace_id`?
8. Why `extra="forbid"` on the Pydantic models?
9. What endpoint must be set for this account's LangSmith instance, and why?
10. How would you verify that a specific HTTP request produced a LangSmith trace?

---

## 17. Rebuild exercise

Without looking at the code, recreate:
1. `Settings` with LangSmith + CORS fields and `get_settings()` cached.
2. `DemoWorkflowState` with an additive `steps` reducer.
3. A 2-node compiled LangGraph (`stage_one → stage_two`).
4. `run_config()` that builds metadata/tags and sets `run_id` from `request_id`.
5. A FastAPI `create_app()` with CORS, tracing, logging, and the two routers.
6. A redaction utility with key-name + regex masking.
7. `conftest.py` that keeps tests hermetic.
8. Tests for health, workflow, config, and redaction.

Check your result with `pytest` and by calling both endpoints.

---

## 18. Concise cheat sheet

```powershell
# Backend
cd backend; .\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload
pytest

# Frontend
cd frontend; npm install; npm run dev

# Demo call
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/v1/demo/workflow `
  -ContentType "application/json" -Body '{"input_text":"hi"}'

# Key env (in repo-root .env, gitignored)
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=<personal-access-token>
LANGSMITH_PROJECT=ontario-allquote-agent
LANGSMITH_ENDPOINT=https://eu.api.smith.langchain.com
```

Key names to remember: `Settings`, `get_settings`, `configure_tracing`, `run_config`,
`set_stage`, `RedactingContextFilter`, `redact_data`, `DemoWorkflowState`,
`build_demo_workflow`, `create_app`, `BrowserManager`.

**Related:** [learning index](./README.md) | Issue #1 is the foundation for all later issues.
