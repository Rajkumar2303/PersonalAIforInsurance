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
| 3 | Product-Aware Market Registry + Progressive Profile Hardening | [issue-03-market-registry.md](./issue-03-market-registry.md) | Part A: draft profiles (DOB/address optional, zero drivers/vehicles), canonical field paths (`paths.py`), validated immutable `updated(path,value)`, `is_draft`/`is_live_quote_ready`. Part B: data-driven `MarketRegistryEntry` + enums (DistributionType/ProductScope/RegistryStatus/MarketRequirement), AUTO seed from brief Appendix A (31 records, `status:discovered`, nullable rate-source ids), `MarketRegistryService`, read-only `/api/v1/markets`; seeded-vs-verified distinction; no PII | Issues 1, 2 | ✅ Implemented (Issue #3) |
| 4 | Rate Source Deduplication | [issue-04-rate-source-deduplication.md](./issue-04-rate-source-deduplication.md) | Deterministic evidence-based dedup: `DeduplicationStatus`/`Confidence`/`ReasonCode`; authoritative `MarketRegistryEntry.distinct_rate_source_id` (single source of truth) + `DistinctRateSource` records with `related_registry_ids` consistency-checked against the registry; candidate detection vs confirmation; `evaluate_pair` priority chain (id → verified program → underwriter → group → unresolved); same group never auto-collapses; `duplicate_confirmed` suppresses; data-driven mappings; read-only `/api/v1/rate-sources`, `/markets/{id}/duplicates`, `/dedup/metrics`; honest real seed 0/0/31 | Issues 1–3 | ✅ Implemented (Issue #4) |
| 5 | Product-Aware Consent Intake Agent | [issue-05-intake-agent.md](./issue-05-intake-agent.md) | Deterministic progressive intake: product gate (AUTO vs not-implemented); data-driven `IntakeFieldDefinition` catalog (`data/intake/auto_fields.json`) for HOW-TO-ASK while Pydantic validates; `IntakeEngine` (seed bootstrap, `_pick_next_field` unit-blockers∪requested∪starter, `submit_answer`, `request_fields` already_known/requested/unsupported, safe summary); `ProfileVault` Protocol + in-memory + Fernet encrypted-at-rest (key from env, dir gitignored); typed consent (collection/route_disclosure/household_driver) receipts with paths-not-values; route data-sharing preview + exclusion; human checkpoints (signature/payment/purchase must_not_automate); LangGraph advance/submit flows with safe-metadata-only state + pending-answer inbox (no PII in traces); optional `years_at_current_address` (schema 1.0→1.1); dynamic-change Scenarios A–G | Issues 1–4 | ✅ Implemented (Issue #5) |
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
