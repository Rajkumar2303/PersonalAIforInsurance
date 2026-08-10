# Issue #8 — Terminal Status & Recovery Engine

**Ontario All-Quote Agent — learning document for GitHub Issue #8.** Status: ✅ Implemented
(Prompts 1–3). Depends on Issues #1–#7 (especially #4 dedup, #5 consent/intake, #6 route
planner, #7 browser observations).

This document is based strictly on the **code actually implemented**. Where something is
*planned for a future issue* it is marked **future**. Where a tradeoff is *inferred* it is
marked **inferred**.

---

## 1. What problem Issue #8 solves

Issue #7 produces browser **observations** (e.g. `quote_detected`, `technical_error`,
`needs_field`, `access_control_detected`) but intentionally does **not** own:
terminal outcome classification, bounded retries, retry budgets, alternate-route failover,
or final blocked/unreachable status. Issue #8 is the **deterministic recovery / orchestration
layer** that answers, for a planned route + its latest observation + prior attempts + policy
+ consent/readiness:

> resume? retry the same route? ask the user something? use an alternate route? prepare a
> voice/manual handoff? or stop with an evidence-compatible terminal status?

It is deterministic, bounded, explainable, conservative, safe, data/config-driven,
independent of quote price, and contains **no LLM** and **no insurer-specific branching**.

## 2. Where recovery fits in the architecture

```
Issue #5 Intake ──► Issue #6 Route Planner ──► Issue #7 Browser Executor
                                                        │  ExecutionObservation
                                                        ▼
                                            Issue #8 Recovery Engine
                                                        │  retry / pause / failover / terminal
                                                        ▼
                              Future #9 Voice · Future #10 Evidence · Future #11 Normalization
```

`backend/app/services/recovery/` is a self-contained deterministic core. The API surface is
`backend/app/api/recovery.py`, the orchestration graph is `backend/app/graph/recovery_workflow.py`.

## 3. Readiness vs observation vs lifecycle vs outcome (the core separation)

These are **separate enums/models** and are never collapsed:

| Concept | Model | Example |
|---|---|---|
| `RouteReadiness` | Issue #6 `PlannedRoute.is_ready` / `RouteBlocker` | `ready` |
| `BrowserObservation` | Issue #7 `BrowserObservation.observation_type` | `needs_field` |
| `ExecutionObservation` | Issue #8 `models/recovery.py` | `{source_channel, observation_type, reason, safe_context}` |
| `AttemptLifecycleStatus` | Issue #8 | `paused` |
| `Retryability` | Issue #8 | `retryable` |
| `RecoveryAction` | Issue #8 | `resume_after_user_input` |
| `RouteOutcomeStatus` | Issue #8 | `None` (or later a coverage-ledger status) |

`ready` ≠ `quoted`; `technical_error` ≠ `unreachable` (a retry may still be permitted).

## 4. ExecutionObservation

`ExecutionObservation` (`models/recovery.py`) is the **generic observation contract**: 
`source_channel` (`browser|voice|phone|manual`), `observation_type` (str), `reason`, and
`safe_context` (a dict of SAFE metadata only). `browser_observation_to_execution()` in
`classification.py` adapts an Issue #7 `BrowserObservation` into this contract. This is the
same interface Issue #9 will use for voice observations.

## 5. QuoteAttempt (attempt history record)

`AttemptRecord` (called "QuoteAttempt" in the spec) is the safe attempt-history model:
`attempt_id`, `plan_id`, `planned_route_id`, `registry_id`, `distinct_rate_source_id`,
`attempt_number` (per rate-source sequence), `channel`, `started_at/ended_at`,
`lifecycle_status`, `revision`, `last_observation_key/sequence`, `policy_version`,
`plan_version`, `observation_type`, `reason_codes`, `execution_result_kind`,
`terminal_status`, `recovery_action`, `parent_attempt_id`, `alternative_of_attempt_id`,
`quote_pending_normalization`, `notes`. No applicant values.

## 6. AttemptLifecycleStatus

`pending | running | paused | recoverable | terminal`. **Paused is not a failure.**
Transitions are validated in `attempt_store.py` via `_ALLOWED_TRANSITIONS` and
`TransitionError` (e.g. `running→paused`, `running→recoverable`, `running→terminal`,
`paused→running`, `recoverable→running`; **`paused→recoverable` and `terminal→*` are rejected**
except explicit terminal enrichment).

## 7. Retryability

`Retryability = retryable | non_retryable | requires_human | unknown`. Determined
deterministically in `classification.py` from the observation type/reason/policy. Only
explicitly `retryable` technical failures are auto-retried; `unknown` is **not** auto-retried.

## 8. RecoveryDecision

The explainable output: `decision_id`, `attempt_id`, route/source ids, `lifecycle_status`,
`recommended_action`, `reason_codes`, `retry_allowed`, `attempts_used/remaining`,
`alternative_route_id`, `terminal_status`, `quote_pending_normalization`,
`policy_version`, `plan_version`, `safe_context`, `decided_at`. Built by
`RecoveryEngine.decide()`.

## 9. RecoveryAction

`continue_current_session | resume_after_user_input | retry_same_route |
use_alternative_route | prepare_voice_handoff | manual_handoff | await_human_checkpoint |
stop_terminal | no_action`. Issue #8 **chooses** the action; it never executes browser
retries, calls, or user answers.

## 10. RouteOutcomeStatus

The 13 coverage-ledger statuses: `quoted_comparable`, `quoted_non_comparable`,
`estimate_only`, `callback_required`, `manual_handoff`, `ineligible`,
`affinity_restricted`, `specialty_only`, `duplicate_rate_source`,
`not_currently_writing`, `blocked`, `unreachable`, `unresolved`. Issue #8 sets these only on
**explicit evidence**; `quoted_comparable`/`quoted_non_comparable` are **never assigned**.

## 11. Why pauses are not failures

`needs_field`, `needs_consent` (undecided), resumable human checkpoints, unknown-field
pause, and applicant-correctable validation errors all map to `lifecycle=paused`,
`terminal_status=None`, `consumes_budget=False`. A pause consumes **no** retry budget.

## 12. Resume vs retry

- **RESUME** = user supplies field / consent granted / checkpoint completed / mapping added
  with the same browser session alive → same attempt continues (no new attempt, no budget).
- **RETRY** = new technical execution attempt after a transient failure → consumes budget.

## 13. Terminal immutability

Once terminal, `InMemoryAttemptStore.update()` refuses lifecycle changes (raises
`TransitionError`); duplicate processing is idempotent. Explicit downstream enrichment
(quote comparability) uses `RecoveryEngine.enrich_terminal()` — never silent overwrite.

## 14. Idempotency

`record_observation()` dedups by per-attempt `last_observation_key` and `revision`;
terminal reprocessing returns an idempotent decision; no duplicate attempt, no double
budget, no duplicate failover/terminal.

## 15. Stale / out-of-order observations

An `observation_sequence` on the request + `last_observation_sequence` on the attempt form
a lightweight guard: an older sequence is safely ignored (idempotent), and a terminal result
stays unchanged when a stale `technical_error` arrives.

## 16. Retry policy

`data/recovery/auto_policy.json` (version `"1"`), loaded by `RecoveryPolicyLoader` →
`RecoveryPolicy`. Defaults: `max_attempts_per_route=2` (initial + at most one retry),
`max_transient_retries=1`, `navigation_timeout_retryable=True`, `browser_crash_retryable=True`,
`alternative_route_after_exhaustion=True`, `max_attempts_per_rate_source=3`,
`max_attempts_per_plan=6`.

## 17. Route budget

`route_attempts_used(plan_id, registry_id)` counts non-pending attempts for that route;
retry allowed only while `< max_attempts_per_route`.

## 18. Rate-source budget

`rate_source_attempts_used(plan_id, rs_id)` counts attempts across **all routes sharing one
`distinct_rate_source_id`**, so Route-A retries + Route-B failover can never exceed the
rate-source cap. A failover to B consumes the rate-source budget; B gets exactly the
remaining allowance.

## 19. Plan budget

`plan_attempts_used(plan_id)` counts all non-pending attempts in the plan; retry/failover
are gated by `< max_attempts_per_plan`, so switching rate sources cannot bypass the plan cap.

## 20. Failover

`RecoveryEngine._failover_or_terminal()` + `_pick_alternative()`: when same-route retry is
exhausted (or not allowed) and the classification is `failover_eligible`, a **ready
alternative** (from Issue #6 `PlannerRouteSource.alternatives_for`) is selected
deterministically (sorted, never re-using an already-attempted route) →
`use_alternative_route` with `alternative_route_id`.

## 21. Primary / alternate routes

Issue #6 `RoutePlan` marks confirmed-duplicate-group members: the representative is the
primary (`is_alternative=False`); group members are alternatives (`is_alternative=True`).
`PlannerRouteSource` consumes these live.

## 22. Readiness recheck

Alternatives are re-checked against a **fresh** `RoutePlanner.plan()` on every failover, so
a revoked consent / newly-missing field on an alternative is respected. If a non-ready
alternative only needs a field/consent, `blocked_alternatives()` produces a **pause**
(`resume_after_user_input`) instead of blindly executing or going terminal.

## 23. Consent recheck

`RecoveryConsentSource` / `IntakeConsentSource` read **live** Issue #5
`route_consent_state` before a same-route retry (`_consent_gate`). Consent is never copied
into attempt metadata, so a revocation between attempts blocks the retry.

## 24. Browser-session-loss behavior

A paused attempt receiving a technical failure (or `resume_session_unavailable`) is closed
explicitly (`terminal`, reason `resume_session_unavailable`) and recovery returns a bounded
`retry_same_route`/failover/terminal decision — no silent pretend-resume.

## 25. Missing-field recovery

`needs_field` → `paused` / `resume_after_user_input`. When the user supplies the field
(Issue #5) the same attempt resumes.

## 26. Unknown-field recovery

`unknown_external_field` → `paused` for developer/config mapping. After mapping added →
resume same session. If the workflow is intentionally ended with an unmappable page →
`unresolved`. No invented answer.

## 27. Validation correction

`validation_error` with `validation_kind="applicant_correctable"` (default) → `paused` for
Issue #5 correction; `"destination_incompatible"` → `value_not_supported` semantics
(failover/manual); `"unknown"` → `unresolved`. Never retries identical invalid data.

## 28. Unsupported destination value

`value_not_supported` → terminal with `failover_eligible=True` (alternate route may support
it) and fallback `manual_handoff`. Issue #8 never alters truthful applicant information to
satisfy a website.

## 29. CAPTCHA / access control

`access_control_detected` → `terminal`/`blocked`, reason `captcha_or_bot_control`,
`retry_allowed=False`, action `manual_handoff`. **No retry, no bypass, no failover to
circumvent the same control.**

## 30. Authentication walls

`authentication_required` → `terminal`/`manual_handoff`, reason `authentication_required`,
no credential guessing, no retry loop.

## 31. Human checkpoints (recoverable)

identity verification / consent attestation / household-driver consent → `paused`,
`await_human_checkpoint`, `terminal_status=None` (workflow may resume).

## 32. Prohibited automation boundaries

application declaration, signature, payment, purchase/binding, renewal, cancellation →
automation **stops**: `terminal`/`manual_handoff` (RouteOutcomeStatus `manual_handoff`).

## 33. callback_required

`callback_detected` → `terminal`, RouteOutcomeStatus `callback_required`, action
`prepare_voice_handoff`, safe context preserved (`reference_present`,
`private_reference_handle`). **No phone call** — Issue #9 consumes this context.

## 34. manual_handoff

`manual_contact_detected` / `broker_requires_manual_review` → `terminal`, RouteOutcomeStatus
`manual_handoff`, action `manual_handoff`. Kept distinct from `callback_required`.

## 35. blocked

Used for CAPTCHA/bot/access barriers (site reached but automation blocked) and
`unexpected_host` safety stops. Not "unreachable".

## 36. unreachable

Conservative: bounded technical attempts exhausted, no usable alternative, destination
unavailable. Never used for CAPTCHA, consent denial, unknown eligibility, or manual routes.

## 37. unresolved

Only when the system genuinely cannot determine a more specific evidence-backed outcome
(unknown page behavior, ambiguous completion, `route_invalid`/plan change). Not a catch-all.

## 38. ineligible

Only from explicit site evidence (`explicit_ineligible` observation). Never inferred from
age/claims/convictions/insurer group/product assumptions. Negative tests prove profile
traits alone do not trigger it.

## 39. affinity_restricted

Only from explicit membership/affinity evidence. `membership_unknown` → pause (check), not
affinity_restricted. Technical failure → technical outcome, not affinity_restricted.

## 40. specialty_only

Only from explicit market/product-scope evidence (`specialty_only` observation). Never
inferred from brand names.

## 41. not_currently_writing

Only from explicit current evidence (`not_currently_writing` observation). A 404 or a
temporary outage is **never** `not_currently_writing` (technical/retryable instead).

## 42. duplicate_rate_source

Represented for a confirmed duplicate alternative that is never executed
(`classify_unused_alternative` → `duplicate_rate_source`). If an alternative is actually
executed after primary failure, its **real execution result** is preserved (not reduced to
`duplicate_rate_source`).

## 43. quote_pending_normalization

A firm/observed quote → `terminal`, `terminal_status=None`, `quote_pending_normalization=True`,
`reason=quote_observed`, retries stop. Comparability is deferred to Issues #11/#12.

## 44. estimate_only

Explicit estimate evidence (`estimate_only`/non-firm with estimate wording) →
`terminal`/`estimate_only`, reason `estimate_observed`. Never upgraded to a firm quote.

## 45. Why comparability is deferred

Issue #8 observes execution results but cannot judge genuine comparability (coverage,
deduplication, normalization). Assigning `quoted_comparable`/`quoted_non_comparable` here
would be false classification; the `quote_pending_normalization` flag + `enrich_terminal()`
leave that to Issues #11/#12.

## 46. Dynamic recovery policy

All budgets/toggles live in `data/recovery/auto_policy.json` + `RecoveryPolicy`. Changing
route/rate-source/plan caps or failover/retryable toggles changes behavior with **no engine
code change** (proven by `test_recovery_dynamic.py` + `test_recovery_hardening.py`).

## 47. Dynamic observation mappings

New observations are added as a localized row in `classification._TABLE` (or a small special
handler). No LangGraph topology change, no engine rewrite, no route-specific logic.

## 48. Dynamic market changes

Alternatives come from Issue #6 data via `PlannerRouteSource`; adding/removing/reordering
routes in the registry is consumed automatically (proven by the market-change test).

## 49. Future voice compatibility

`SourceChannel.PHONE` and generic `ExecutionObservation` let a synthetic
`broker_requires_manual_review` phone observation reach a deterministic `manual_handoff`
decision — Issue #9 integrates through the same contract without redesign.

## 50. AttemptStore abstraction

`AttemptStore` is a `@runtime_checkable` Protocol; `InMemoryAttemptStore` is the current
implementation. `RecoveryEngine` depends only on the protocol, so a persistent Issue #10
store can replace it without engine changes (proven by the store-abstraction test).

## 51. Provenance / versioning

`AttemptRecord`/`RecoveryDecision` carry `policy_version`, `plan_version`, `plan_id`,
`planned_route_id`, `registry_id`, `distinct_rate_source_id`, timestamps, and `revision` —
so later audit/evidence can explain exactly why a retry/failover occurred.

## 52. LangGraph

`graph/recovery_workflow.py` — real nodes: `initialize → load_attempt_history →
classify_observation → decide → END` (generic, metadata-only state). ASCII:

```
initialize
    ↓
load_attempt_history
    ↓
classify_observation
    ↓
decide  (consent gate → pause / retry / failover / terminal)
    ↓
END
```

## 53. LangSmith

Each node calls `set_stage(...)`; the API passes `run_config` metadata
(`planned_route_id`, `registry_id`, `observation_type`, `workflow_stage`) and a
`request_id`. `sanitize_recovery_context()` (an allowlist in `classification.py`) drops any
non-allowlisted key before it reaches traced state. Privacy is **structural**, not just
redaction: applicant values never enter the recovery models in the first place.

## 54. Privacy

All recovery models extend `SensitiveBaseModel` (redacted repr/safe_dict). Values that
should never appear (licence, VIN, DOB, address, email/phone, claims, raw quote reference)
never enter the models; safe identifiers (`attempt_id`, `registry_id`, `canonical_path`,
`reason_code`, `reference_present`, `private_reference_handle`) may. `test_recovery_privacy.py`
+ `test_recovery_hardening.py` verify all failure paths.

## 55. Why no LLM is used

Retryability, terminal status, route switching, eligibility, blocked status, consent, and
failover are all decided from structured observation types + reason codes + policy — fully
deterministic and explainable. No LLM.

## 56. Testing strategy

Unit tests for models/enums/classification/policy/engine; integration tests with a REAL
IntakeEngine + RoutePlanner over synthetic registry/rate-source data; LangGraph workflow
tests; API tests (TestClient with a fresh engine dependency override); dynamic-change tests;
privacy meta-tests; hermetic throughout.

## 57. Hermetic testing

`tests/conftest.py` forces `LANGSMITH_TRACING=false`; all recovery tests use synthetic
observations and temp directories. No real insurer / phone / LLM / external API / LangSmith
upload / real applicant data.

## 58. Debugging

Decisions are `SensitiveBaseModel`s whose `repr` is redacted but structurally readable;
attempts are in-memory and inspectable via `engine.list_attempts()`; every decision has
`reason_codes`, `attempts_used/remaining`, `terminal_status`, and `safe_context` counts for
tracing. The `issue8_recovery_demo.py` prints all 22 core scenarios.

## 59. Common failure modes

- Treating `paused` as terminal (fixed by explicit transition validation).
- Retrying after consent revocation (fixed by the live consent gate).
- Failing over to a non-ready alternative (fixed by readiness recheck + blocked pause).
- Reusing an exhausted alternative (fixed by excluding attempted routes).
- Assigning comparability from a price (fixed by `quote_pending_normalization`).

## 60. Common architecture mistakes

Collapsing `RouteReadiness`/`BrowserObservation`/`AttemptLifecycleStatus`/`RouteOutcomeStatus`
into one enum; putting LLM calls in the decision path; making retry budgets unlimited;
copying consent/profile values into attempt metadata.

## 61. Design tradeoffs

- Decision engine is a separate service (pure, testable) vs wiring into the browser loop
  directly (**chosen**: keeps Issue #7 unchanged; full browser-loop wiring is hardening).
- In-memory store (fast, hermetic) vs persistent (Issue #10) — abstracted behind a Protocol.
- Conservative budgets (route 2 / rs 3 / plan 6) — safe defaults, data-tunable.
- Structured allowlist for safe context (strict) vs free-form metadata — privacy-first.

## 62. Limitations

- `AttemptStore` is in-memory (no persistent evidence store — Issue #10).
- No telephony / real voice observations yet (Issue #9).
- No final quote comparability classification (Issues #11/#12).
- No normalization, coverage ledger, or dashboard.
- Live route availability depends on verified providers (`no_verified_live_browser_route`).
- Recovery depends on structured observations; some unknown site behavior may remain
  `unresolved`; failover is bounded/conservative.

## 63. Issue #9 boundary

Recovery produces `prepare_voice_handoff`/`callback_required` decisions and safe context;
**it never places a call**. Issue #9 will execute the voice/callback workflow.

## 64. Issue #10 boundary

Recovery produces evidence-friendly metadata (attempt_id, reason codes, timestamps) but no
artifact persistence, hashing, screenshots, or lineage DB. Issue #10 owns that.

## 65. Issue #11/#12 boundary

`quote_pending_normalization` + `enrich_terminal()` leave quote normalization and
comparability to Issues #11/#12. `quoted_comparable`/`quoted_non_comparable` are never set.

## 66. 30-second interview explanation

"Issue #8 is a deterministic decision layer between the browser agent and the final
coverage record. Given a route, its latest browser observation, prior attempts, a data-driven
retry policy, current consent, and Issue #6 alternatives, it answers exactly one question:
should we pause, resume, retry, fail over to an alternative, prepare a human/voice handoff,
or stop with an evidence-backed terminal status? It never launches a browser, never calls, and
never guesses."

## 67. 2-minute interview explanation

Walk: observation → `ExecutionObservation` → deterministic `classify_observation` →
`RecoveryEngine.decide` (consent gate → pause/retry/failover/terminal) → `RecoveryDecision`
→ attempt recorded in the `AttemptStore` (immutable once terminal). Mention the four budgets
(route/rate-source/plan), idempotency + stale guards, terminal immutability with explicit
`enrich_terminal`, and that quote results stop retries with `quote_pending_normalization`
so comparability stays with Issues #11/#12.

## 68. Deep technical interview explanation

Go into the transition table (`_ALLOWED_TRANSITIONS`), the `_TABLE` classification registry
and special handlers (`_human_checkpoint_spec`, `_needs_consent_spec`, `_quote_spec`,
`_technical_error_spec`, `_validation_error_spec`), `PlannerRouteSource.alternatives_for`
+ `blocked_alternatives`, `IntakeConsentSource._consent_gate`, `_pick_alternative` (no reuse
of attempted routes), `_failover_or_terminal`, per-attempt `last_observation_key`/sequence
dedup, `revision`, the generic LangGraph (`initialize → load_attempt_history →
classify_observation → decide`), the safe-context allowlist for LangSmith, and the
`AttemptStore` Protocol for Issue #10.

## 69–70. Self-test questions (+ answers)

1. Q: Is a `paused` attempt a failure? A: No; it consumes no retry budget.
2. Q: What transition is illegal? A: `terminal→running` (and `paused→recoverable`).
3. Q: How is retry bounded? A: route (2) + rate-source (3) + plan (6) caps.
4. Q: Why isn't `quoted_comparable` assigned? A: comparability is deferred to #11/#12.
5. Q: What stops a CAPTCHA from being retried? A: `access_control_detected` →
   `non_retryable`, `retry_allowed=False`.
6. Q: How is consent rechecked? A: `IntakeConsentSource` live `route_consent_state` before retry.
7. Q: What happens if the browser session is lost while paused? A: paused attempt closed,
   bounded retry/failover/terminal (`resume_session_unavailable`).
8. Q: How does resume differ from retry? A: resume reuses the same attempt (no budget);
   retry creates a new attempt (consumes budget).
9. Q: What is `quote_pending_normalization`? A: quote observed; comparability deferred.
10. Q: How is a stale observation handled? A: `observation_sequence` guard → idempotent.
11. Q: Is an LLM used? A: No — deterministic tables + policy.
12. Q: How does failover avoid inflating the distinct-source count? A: it reuses the same
    `distinct_rate_source_id` and continues the rate-source attempt numbering.
13. Q: What does `ineligible` require? A: explicit site evidence.
14. Q: How are new observations added? A: localized `_TABLE` entry; no graph/engine change.
15. Q: How is Issue #9 supported? A: generic `ExecutionObservation` + `prepare_voice_handoff`.
16. Q: What is terminal immutability? A: no mutation except explicit `enrich_terminal`.
17. Q: Where are budgets configured? A: `data/recovery/auto_policy.json` → `RecoveryPolicy`.
18. Q: How is privacy structural? A: applicant values never enter recovery models; safe
    context is allowlisted.
19. Q: What does `route_invalid` do? A: records a new terminal `unresolved` attempt; prior
    history immutable.
20. Q: What backs the `AttemptStore`? A: `AttemptStore` Protocol; in-memory impl (Issue #10
    replaces it).

## 71. Rebuild-from-scratch exercise

1. Re-add enums + models in `models/recovery.py` (state separation first).
2. Add `_TABLE` classification + special handlers + `ExecutionObservation` adapter.
3. Add `RecoveryPolicy` + loader + `data/recovery/auto_policy.json`.
4. Add `AttemptStore` Protocol + `InMemoryAttemptStore` + transition validation.
5. Add `RecoveryEngine` (begin/resolve/decide/record/enrich/failover/budgets/consent gate).
6. Add `PlannerRouteSource` + `IntakeConsentSource`.
7. Add the generic LangGraph workflow + sanitize allowlist.
8. Add the API endpoints + register router.
9. Add tests (models/classification/policy/engine/workflow/api/privacy/dynamic/hardening).
10. Add the demo.

## 72. Concise cheat sheet

- Decision: `engine.record_observation(request)` → `RecoveryDecision`.
- Defaults: route 2 / rs 3 / plan 6 (data-driven).
- Pauses consume no budget; resumes reuse the attempt.
- Terminal = immutable; comparability deferred (`quote_pending_normalization`).
- `quoted_comparable`/`quoted_non_comparable` never set.
- No LLM; no insurer branches; no telephony.
- Run: `pytest tests/test_recovery_*.py tests/test_recovery_hardening.py -q`.
- Demo: `$env:PYTHONPATH='tests;.'; .\.venv\Scripts\python.exe demos\issue8_recovery_demo.py`.
