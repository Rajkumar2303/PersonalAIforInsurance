# Learning Journal

A **technical learning journal** for the Ontario All-Quote Agent — one document per
GitHub issue. Each document records what was built, why, how it works, how data
flows through it, the key files/symbols, architectural decisions and tradeoffs,
observability/privacy implications, testing strategy, failure modes, and enough
context to explain or rebuild it.

## Conventions

- One document per issue: `docs/learning/issue-NN-<slug>.md`.
- Create/update a document **after** the issue implementation is complete — it is
  part of the issue's definition of done.
- Base every document on the **code actually implemented** for that issue.
- Clearly distinguish **implemented** behavior, **future planned** behavior, and
  **inferred** tradeoffs.
- Link to earlier documents when a concept depends on prior work; avoid repeating
  explanations already covered.
- Keep the index below up to date whenever a new document is created.

## Index

| Issue | Title | Document | Main concepts | Depends on | Status |
|-------|-------|----------|---------------|------------|--------|
| 1 | Project Setup, Architecture & Observability | [issue-01-foundation.md](./issue-01-foundation.md) | Monorepo; FastAPI factory; Pydantic v2 Settings; LangGraph typed state + 2-node workflow; LangSmith (EU) tracing with request-id ↔ trace-id correlation; structured redacting logging; sensitive-data redaction utility; Playwright foundation; Vite+React health shell; hermetic pytest suite | — | ✅ Implemented (Issue #1) |
| 2 | Canonical Insurance Intake Schema | [issue-02-insurance-schema.md](./issue-02-insurance-schema.md) | Product-aware schema (`InsuranceType` enum; AUTO implemented, others recognized-unsupported); shared vs product-specific composition; nested Pydantic models (drivers, vehicles, household, history, coverage); enums; required-vs-optional + data minimization; postal/VIN/year/percentage validation; sensitive-aware base with leaf-level redaction (`safe_dict`/`redacted_dict`, redacted repr/str); schema versioning; missing-field + trace-metadata helpers | Issue 1 | ✅ Implemented (Issue #2) |
| 3 | Market Registry | `issue-03-market-registry.md` | — | Issue 1 | ⏳ Planned |
| 4 | Rate Source Deduplication | `issue-04-rate-source-deduplication.md` | — | Issues 1–3 | ⏳ Planned |
| 5 | Intake Agent | `issue-05-intake-agent.md` | — | Issues 1–2 | ⏳ Planned |
| 6 | Route Planner | `issue-06-route-planner.md` | — | Issues 1–4 | ⏳ Planned |
| 7 | Browser Quote Agent | `issue-07-browser-quote-agent.md` | — | Issues 1, 3, 4, 6 | ⏳ Planned |
| 8 | Terminal Status & Recovery | `issue-08-terminal-status-recovery.md` | — | Issues 1, 7 | ⏳ Planned |
| 9 | Voice Handoff | `issue-09-voice-handoff.md` | — | Issues 1, 8 | ⏳ Planned |
| 10 | Evidence & Audit | `issue-10-evidence-audit.md` | — | Issues 1, 7, 8 | ⏳ Planned |
| 11 | Quote Normalization | `issue-11-quote-normalization.md` | — | Issues 1, 7, 10 | ⏳ Planned |
| 12 | Comparability & Confidence | `issue-12-comparability-confidence.md` | — | Issues 1, 11 | ⏳ Planned |
| 13 | Dashboard | `issue-13-dashboard.md` | — | Issues 1, 10–12 | ⏳ Planned |
| 14 | Reliability & Submission | `issue-14-reliability-submission.md` | — | All of the above | ⏳ Planned |

> Future-issue titles are provisional (derived from the planned file names) and
> will be finalized when each issue is defined.
