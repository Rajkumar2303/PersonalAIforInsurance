# Issue #9 — Voice / Phone Context Handoff

**Ontario All-Quote Agent — learning document for GitHub Issue #9 (Prompt 1: core
architecture + hermetic voice-handoff flow; Prompt 2: unattended voice execution,
hardening, integration, and final testing).** Status: ✅ Implemented (Prompts 1 & 2).
Depends on Issues #1–#8 (especially #5 consent/intake, #6 route planner, #8 recovery).

This document is based strictly on the **code actually implemented** for Prompts 1 & 2.
Where something is *planned for a future prompt/issue* it is marked **future**. Where a
tradeoff is *inferred* it is marked **inferred**.

> The voice layer is a **provider-agnostic, deterministic phone/voice layer** with **no real
> phone calls, no LLM, no recording, no transcription**, and **no second applicant-information
> store**. It is hermetic and fully testable.

---

## 1. What problem Issue #9 (Prompts 1 & 2) solves

Issue #8 introduced `SourceChannel.VOICE / PHONE` and a `prepare_voice_handoff` recovery
action, but there was no concrete voice/phone layer. When a browser route ends in a callback
(Sonnet's "we'll call you back") or a route is phone/broker-only, the system had no way to
represent the phone leg: what to disclose, when to pause for the applicant, how to preserve
quote-vs-estimate evidence, and how to push outcomes back into the Issue #8 recovery engine.

**Prompt 1** built the phone-handoff core: a `VoiceEngine` that prepares a voice session,
enforces automation disclosure, answers broker questions **just-in-time** from the Issue #5
intake vault (never a copy), pauses for the applicant/consent, escalates identity /
declaration / advice / applicant-required items to a human (never answers them), preserves
quote-vs-estimate, and emits every outcome to Issue #8 with `source_channel=VOICE`.

**Prompt 2** made it **unattended** so the applicant does not need to sit at the screen: the
normal path requires **zero applicant interruptions** (a quote-ready profile answers known
questions automatically), a missing field or human escalation on one route never blocks other
routes, missing fields are **batchable** (`pending_field_paths`), consent is re-checked JIT
and revocation wins immediately, values are **discarded after use**, a stable **route-status
contract** (`VoiceRouteStatus`) + **per-route summaries** are exposed for the future
orchestrator, and the browser-callback and phone-only-route integrations are demonstrated
hermetically.

## 2. Where voice fits in the architecture

```
Issue #6 Route Planner ──► Issue #7 Browser Executor ──► Issue #8 Recovery Engine
      (phone/callback route)   (callback observed)         │
                 │                    │                    │
                 ▼                    ▼                    ▼
            PhoneHandoffContext ──► VoiceEngine ──► ExecutionObservation(VOICE)
                 │                        │
                 │        JIT value reads (IntakeValueSource, never stored)
                 ▼                        ▼
        Issue #5 Intake vault ◄── pause_for_applicant / request_fields
```

The voice layer is a **thin deterministic core** (`app/services/voice/`) with a small
LangGraph wrapper (`app/graph/voice_workflow.py`) and a safe API (`app/api/voice.py`).
Issue #8 remains the **terminal-status / retry / failover / handoff authority** — the voice
engine never assigns `quoted_comparable` / `quoted_non_comparable`.

## 3. Key files & symbols

**Models — `app/models/voice.py`**
- `VoiceLifecycleStatus`: `prepared → awaiting_disclosure → active → paused_for_applicant /
  paused_for_consent / awaiting_human → completed | terminated` (distinct from Issue #8's
  `AttemptLifecycleStatus`).
- `DisclosureStatus`: `not_disclosed | disclosed | refused` (mandatory automation disclosure).
- `RecordingConsentStatus` / `TranscriptionConsentStatus`: `not_requested | granted | denied`
  (Prompt 1 never records/transcribes — status is explicit, default `not_requested`).
- `VoiceResponseAction`: `speak_disclosure, disclose_value, request_consent,
  pause_for_applicant, transfer_to_applicant, transfer_to_human, acknowledge, end_quote,
  end_terminal, manual_handoff, callback_scheduled`.
- `BrokerQuestionKind`: `canonical_field, collection_length, household_driver,
  identity_checkpoint, consent_expansion, declaration, advice_request, callback_request,
  quote_disclosure, estimate_disclosure, ineligibility, affinity_restriction, specialty_only,
  not_currently_writing, applicant_required, manual_review, broker_unavailable,
  completed_without_quote, unknown`.
- `VoiceObservationType`: voice observation strings emitted to Issue #8 (several reuse
  existing Issue #8 rows: `explicit_ineligible`, `affinity_restricted`, `specialty_only`,
  `not_currently_writing`, `technical_error`, `completed_without_quote`).
- `BrokerQuestion`, `VoiceSession` (safe metadata only), `VoiceDecision` (never the value),
  `PhoneHandoffContext` (ids + public route metadata + canonical paths).
- **Prompt 2 — `VoiceRouteStatus`**: the stable route-local contract for the frontend /
  future orchestrator: `prepared, running, paused_missing_information, applicant_required,
  manual_handoff, callback_scheduled, quote_pending_normalization, estimate_only, completed,
  failed`. Derived by `derive_voice_route_status(session)` (terminal status wins, then quote
  pending normalization, then lifecycle; `awaiting_human` splits into `applicant_required` vs
  `manual_handoff` by checkpoint).
- **Prompt 2 — `VoiceSession` safe counters**: `automated_answers`, `applicant_interruptions`,
  `quote_pending_normalization`, `route_status`; and `VoiceRouteSummary` (per-route
  orchestrator summary: status, terminal, `pending_field_paths`, counters).
  `recovery_attempt_id` records this voice session's OWN Issue #8 attempt identity.
- `sanitize_voice_context()` + `_VOICE_SAFE_CONTEXT_KEYS` allowlist; `VoiceWorkflowState`
  TypedDict (SAFE METADATA ONLY, includes `route_status`).

**Services — `app/services/voice/`**
- `transport.py`: `VoiceTransport` protocol + `MockVoiceTransport` + `ScriptedBrokerSimulator`
  (hermetic; no real calls). `safe_render()` renders a JIT value to a spoken sentence.
  **Prompt 2**: `speak(session_id, text, path=None)` records only SAFE metadata
  (`last_spoken_path`, `spoken_count`) and `discard_last_spoken()` clears the value right
  after use; a raw transcript is retained only when `retain_transcript=True` (test-only).
- `question_interpreter.py`: `DeterministicBrokerQuestionInterpreter` — data-driven alias
  table + the Issue #5 catalog; unknown/removed-field wording → `UNKNOWN` (never guessed).
- `session_store.py`: `VoiceSessionStore` protocol + `InMemoryVoiceSessionStore`.
- `handoff.py`: `handoff_context_from_recovery()` (from an Issue #8 `prepare_voice_handoff`
  decision) and `handoff_context_from_phone_route()` (from a phone/callback/broker
  `PlannedRoute`).
- `engine.py`: `VoiceEngine` (single authority), `VoiceValueSource` (Issue #5 `IntakeValueSource`
  subclass with a `voice_agent` source-context label).
- `__init__.py`: `get_voice_engine()` singleton (hermetic mock transport + real intake/recovery).

**Graph — `app/graph/voice_workflow.py`**: `WORKFLOW_NAME="voice_workflow"`;
`initialize → prepare_handoff → automation_disclosure → receive_broker_event →
classify_and_respond → emit_observation → END`; every node calls `set_stage(...)`.

**API — `app/api/voice.py`** (wired in `app/main.py`):
- `POST /api/v1/voice/handoffs` (prepare), `GET /api/v1/voice/sessions/{id}`,
  `GET /api/v1/voice/summaries?intake_session_id=` (Prompt 2 per-route summaries),
- `POST .../disclosure`, `POST .../events` (one `BrokerQuestion`), `POST .../resume`,
  `POST .../pause` (Prompt 2, idempotent, route-local),
- `POST .../human-handoff`, `POST .../observations` (→ Issue #8 `RecoveryDecision`).

**Recovery integration — `app/services/recovery/classification.py`**: added localized voice
rows to `_TABLE` (`phone_quote_observed`, `phone_estimate_observed`, `callback_scheduled`,
`broker_requires_field`, `applicant_required`, `manual_review_required`, `phone_unreachable`,
`unknown_broker_question`) and added `voice_session_id`, `canonical_path`, `route_type` to
`_SAFE_CONTEXT_KEYS`.

**Tests — `tests/voice_helpers.py` + `test_voice_{models,engine,workflow,privacy,dynamic,
recovery,unattended}.py`** (89 tests).

## 4. The VoiceEngine decision flow

`receive_broker_event(session_id, question)` (deterministic, no LLM):

1. **Disclosure gate** — refused → `end_terminal`; not disclosed → `speak_disclosure`
   (nothing substantive happens); completed/terminated → `acknowledge`.
2. **Human boundaries** (`identity_checkpoint`, `declaration`, `advice_request`,
   `applicant_required`, `manual_review`) → emit `applicant_required` /
   `manual_review_required` to Issue #8, set `awaiting_human`, and return
   `transfer_to_applicant` / `transfer_to_human`. **The automation never answers.**
3. **Consent gates** (`consent_expansion`, `household_driver`) → re-check Issue #5 route
   disclosure / household consent live; pause for consent if not covered.
4. **Callback** → emit `callback_scheduled` → Issue #8 returns `callback_required` /
   `prepare_voice_handoff`; session → `completed`.
5. **Quote vs estimate** → emit `phone_quote_observed` (Issue #8 sets
   `quote_pending_normalization=True`, terminal status stays `None` pending #11/#12) or
   `phone_estimate_observed` (terminal `estimate_only`). Session → `completed`.
6. **Explicit terminal statements** (ineligible / affinity / specialty / not-writing) → emit
   the matching observation; Issue #8 assigns the terminal status.
7. **Broker unavailable** → emit `phone_unreachable` (failover-eligible); Issue #8 decides
   retry/failover/manual.
8. **Unknown question** → emit `unknown_broker_question`, `awaiting_human`, `manual_handoff`
   — **never guessed**.
9. **Canonical field / collection length**:
   - if route disclosure does not cover the path → `request_consent` (`paused_for_consent`);
   - if known → **JIT read** (`values.get` / `values.collection_length`) → speak through the
     transport → discard → `disclose_value` (value never stored);
   - if missing → `values.request(...)` (Issue #5) → `paused_for_applicant` /
     `paused_for_consent` with the pending path.

`resume(session_id)` re-checks **current** consent (a revocation between pause and resume is
honoured), then only discloses the pending field when it is now known.

`emit_observation(...)` builds an `ExecutionObservation(source_channel=VOICE, ...)` with a
sanitized safe-context and calls `RecoveryEngine.record_observation(...)` — Issue #8 stays
authoritative (terminal immutability, idempotency, retry budgets apply).

## 5. Data flow / privacy boundary

```
BrokerQuestion ──► VoiceEngine ──► (needs value?) ──► IntakeValueSource.get(...)
                                                        │  JIT scalar/collection read
                                                        ▼
                                              transport.speak(safe_render(value)) ──► discard
                                                        │
        VoiceDecision (ids, path, statuses ONLY — never the value) ──► API / graph state
                                                        │
        ExecutionObservation(VOICE, safe_context allowlist) ──► Issue #8 RecoveryEngine
```

- **No second applicant store.** Values live only in the Issue #5 vault; the voice layer
  reads them JIT and never caches them.
- `VoiceSession`, `VoiceDecision`, `VoiceWorkflowState`, `PhoneHandoffContext`,
  `ExecutionObservation` carry **ids / canonical paths / statuses / public route metadata**
  only. `provider_phone_route` is the **public** provider phone from the market registry
  (safe per Issue #6) — never the applicant's phone.
- `safe_render()` includes the value in the **spoken** sentence (realistic — the automation
  speaks it like a human agent), but Prompt 2 **discards it immediately after use**
  (`discard_last_spoken()`): it lives only as transient spoken text, is never persisted, and
  the default transport retains only `last_spoken_path` / `spoken_count` (SAFE metadata).

## 6. Issue #5 integration

- `VoiceValueSource(IntakeValueSource)` reuses the existing JIT value surface with a
  `voice_agent` source-context label.
- `values.known / get / collection_length / request / route_disclosure_covers /
  has_route_consent / has_collection_consent` power the field flow.
- Missing fields go through `request_fields` → the applicant answers via the normal Issue #5
  answers API → `resume()` re-checks and discloses. No duplicate question store.

## 7. Issue #8 integration

- Voice observations are **localized table rows** in `classification._TABLE` (the topology and
  the `RecoveryEngine` never change — matching Issue #8's design goal).
- The voice engine calls `RecoveryEngine.record_observation(...)` with
  `source_channel=SourceChannel.VOICE`, so terminal status, retries, budgets, failover, and
  `prepare_voice_handoff` all stay with Issue #8.
- **Each voice session owns its OWN Issue #8 attempt** (`recovery_attempt_id`): `prepare_handoff`
  calls `begin_attempt(channel=VOICE, parent_attempt_id=<source browser attempt>)` and every
  `emit_observation` passes that `attempt_id`. A **voice continuation of a browser
  callback_required route therefore has its own attempt identity** and progresses
  independently (e.g. to `quote_pending_normalization`), while the original browser attempt
  stays terminal `callback_required` and is never mutated (Issue #8 terminal immutability is
  preserved — verified by tests A–E).
- **`quoted_comparable` / `quoted_non_comparable` are NEVER assigned** by the voice layer
  (verified by `test_voice_never_assigns_comparable_anywhere`).

## 8. LangGraph behavior & LangSmith observability

- `voice_workflow` is a linear graph over the engine; nodes annotate stages via
  `set_stage(...)`. State is `VoiceWorkflowState` (SAFE METADATA ONLY).
- API routes build `run_config(..., workflow="voice_workflow", extra_metadata={registry_id,
  distinct_rate_source_id, voice_session_id, observation_type, ...})` and
  `set_log_context(...)` / `clear_log_context(...)` for structured, request-correlated logs.
- Metadata is safe ids/statuses only — never applicant values (privacy meta-tests scan graph
  state, sessions, decisions, recovery records, logs, and API responses).

## 9. Privacy / security implications

- **No recording, no transcription** — the consent statuses exist and default to
  `not_requested`, but Prompt 1 never captures audio/text.
- **No LLM** in the core flow (the interpreter is deterministic; an LLM would only be
  considered for genuinely ambiguous broker phrasing — **future**, and even then values stay
  in the vault).
- **No real calls** — the transport is a hermetic mock; a real telephony transport is
  **future**.
- Sensitive synthetic markers (`SYNTHETIC_POSTAL`, licence, VIN, DOB, street) are asserted
  absent from every persisted artifact (see `test_voice_privacy.py`).

## 10. Testing strategy (89 hermetic tests)

- **Models** (`test_voice_models.py`): enum values, `extra="forbid"`, defaults, sanitize
  allowlist, safe-metadata-only state, no value field on `VoiceDecision`.
- **Engine** (`test_voice_engine.py`): prepare/disclose; JIT disclose not cached; collection
  length; missing→pause→resume→disclose; consent required/expansion/revocation; household
  driver; identity/declaration/advice/applicant/manual boundaries (never answered); unknown →
  manual; callback→callback_required; quote vs estimate; explicit terminal statements;
  broker-unavailable; transfer-to-human; end.
- **Workflow** (`test_voice_workflow.py`): graph compiles; full callback/quote flows; state is
  safe metadata only; emit never comparable.
- **Privacy** (`test_voice_privacy.py`): value only at the transport boundary; nothing in
  logs/recovery/API; JIT not cached; end-to-end API scan.
- **Dynamic** (`test_voice_dynamic.py`): new catalog field + alias requires **no engine
  change**; renamed wording → same path; removed catalog field → `UNKNOWN`.
- **Recovery** (`test_voice_recovery.py`): classification rows exist; quote pending
  normalization; estimate `estimate_only`; callback `prepare_voice_handoff`; phone-unreachable
  failover-eligible; unknown pauses; `source_channel=VOICE`; never comparable.
- **Prompt 2 — Unattended** (`test_voice_unattended.py`): happy path with
  `applicant_interruptions == 0`; route-local isolation (missing field / user unavailable /
  failure never block another route); batchable missing fields; pre-collected consent +
  revocation-mid-call; browser-callback → voice integration; phone-only route; route-local
  pause/resume idempotency; quote boundary never comparable; **continuation tests A–E**
  (voice continuation gets its own attempt, firm quote → `quote_pending_normalization`,
  estimate → `estimate_only`, no-answer → own outcome, browser terminal attempt never
  mutated).

Run: `Push-Location "...\backend"; $env:PYTHONPATH='tests';
.\.venv\Scripts\python.exe -m pytest tests/test_voice_*.py -q`

## 11. Failure scenarios & debugging

- **Value never spoken after consent**: `resume()` re-checks `route_disclosure_covers`; if
  consent was revoked, it returns `request_consent` (verified by test).
- **`DedupLoadError` in tests**: registry entry must set `distinct_rate_source_id` to match
  its rate source's `related_registry_ids` (consistency check).
- **New field never persists**: a catalog field must map to a path that exists in the
  `InsuranceProfile` schema (Pydantic is authoritative); use a schema-backed path such as
  `product_data.vehicles[0].use.carpool`. Also, adding an `item_unit_required=True` field
  changes which unit fields gate unit materialization — keep new fields non-unit-required for
  the dynamic test.
- **Disclosure gate**: anything substantive before `disclose_automation(granted=True)` returns
  `speak_disclosure` and discloses nothing.

## 12. Common misunderstandings

- The voice layer does **not** place the call or transcribe; it orchestrates a *represented*
  phone handoff. Real telephony is **future**.
- `VoiceLifecycleStatus` ≠ `AttemptLifecycleStatus` (voice session vs Issue #8 attempt).
- "Estimate vs quote" is preserved as evidence (`estimate_only` vs `quote_pending_normalization`)
  but **comparability is not judged here** (Issues #11/#12).
- A value *is* spoken (realistic) — but only through the transport boundary, and it is never
  stored in sessions, decisions, graph state, logs, or API responses.

## 13. 30-second interview explanation

> "Issue #9 adds a deterministic, provider-agnostic phone-handoff layer that runs
> **unattended**. When a web route ends in a callback or a route is phone-only, we build a
> safe handoff context, prepare a voice session, and mandate automation disclosure. Known
> broker questions are answered just in time from the Issue #5 intake vault — never a second
> applicant store, values discarded after use. A missing field or human escalation pauses
> only that route; other routes keep running. Identity, declaration, and advice are always
> escalated to the applicant. Every outcome is pushed to the Issue #8 recovery engine as a
> VOICE observation, so terminal status and handoff decisions stay in one place, and we never
> claim a quote is comparable (that's Issues #11/#12). It's fully hermetic — no real calls,
> no LLM, no recording — with 89 tests including the unattended happy path (zero applicant
> interruptions), the callback→voice continuation (own attempt, browser attempt immutable),
> and privacy scans."

## 14. 2-minute explanation

Build on §13: the phone leg has its own lifecycle (`prepared → active → paused → completed`),
but the **route-level outcome** is owned by Issue #8. Prompt 1 built the core flow; Prompt 2
added the unattended model: the engine counts automated answers vs applicant interruptions,
accumulates missing canonical paths for batched Issue #5 collection, re-checks consent JIT
(revocation wins immediately), and derives a stable `route_status` (`running`,
`paused_missing_information`, `applicant_required`, `manual_handoff`, `callback_scheduled`,
`quote_pending_normalization`, `estimate_only`, `completed`, `failed`). A `route_summaries`
API enumerates every route session so a future orchestrator can let Route B finish while
Route A waits on the applicant. Privacy: values are read JIT, spoken, and discarded; only
safe ids/statuses/paths leave the engine.

## 15. Deep technical explanation

The engine is a deterministic state machine over `BrokerQuestionKind`s. Every
`receive_broker_event` re-evaluates disclosure → terminal → human-boundary → consent →
callback/quote/terminal-statement → field flow, returning a safe `VoiceDecision` and mutating
the `VoiceSession` (with `_touch` persisting `route_status` via `derive_voice_route_status`).
The JIT boundary is `IntakeValueSource` (scalar `get`, derived `collection_length`, and
`request_fields` for missing fields). The recovery boundary is
`RecoveryEngine.record_observation(RecoveryDecideRequest(source_channel=VOICE, ...))`, which
keeps terminal status, retries, budgets, and `prepare_voice_handoff` in Issue #8. Each voice
session owns a dedicated attempt (`begin_attempt(channel=VOICE, parent_attempt_id=<source
browser attempt>)`) and emits with that `attempt_id`, so a **voice continuation of a browser
callback_required route progresses independently** to its own outcome (e.g.
`quote_pending_normalization`) while the immutable browser attempt stays `callback_required`.
Counters
(`automated_answers`/`applicant_interruptions`) are incremented centrally in `_decision` and
persisted, giving the happy path its `== 0` assertion. Privacy hardening uses generic reasons
to Issue #8 (never raw broker text) and transport discard-after-use, so raw values never
appear in recovery `last_observation_key`, logs, traces, or API responses.

## 16. Rebuild exercise

Add a new phone route question end-to-end: (1) add a schema-backed catalog field + an alias in
`DeterministicBrokerQuestionInterpreter`; (2) prepare a handoff, disclose automation; (3) ask
the question, verify `pause_for_applicant` → Issue #5 answer → `resume` → `disclose_value`;
(4) check `ExecutionObservation(source_channel=VOICE)` reached Issue #8. No engine change.

## 17. Cheat sheet

- Engine: `VoiceEngine.prepare_handoff / disclose_automation / receive_broker_event / resume /
  pause / route_summaries / emit_observation / transfer_to_human / end_session`.
- Gate order: disclosure → terminal → human boundary → consent → callback/quote/terminal →
  field flow.
- Route status: `derive_voice_route_status(session)`; stored as `session.route_status`.
- Counters: `_decision` increments `automated_answers` (disclose) / `applicant_interruptions`
  (interrupt actions).
- Safe-context allowlist: `sanitize_voice_context` (ids, canonical paths, statuses only).
- Recovery rows added: `phone_quote_observed`, `phone_estimate_observed`, `callback_scheduled`,
  `broker_requires_field`, `applicant_required`, `manual_review_required`, `phone_unreachable`,
  `unknown_broker_question`.
- Never comparable: `quoted_comparable` / `quoted_non_comparable` are never produced by voice.

## 18. Prompt 2: unattended execution & route-local model

- **Counters (safe metadata on `VoiceSession`)**: `_decision()` increments `automated_answers`
  on `disclose_value` and `applicant_interruptions` on the interrupt actions
  (`pause_for_applicant`, `request_consent`, `transfer_to_applicant`, `transfer_to_human`,
  `manual_handoff`). The happy path asserts `applicant_interruptions == 0`.
- **Route-local status**: `derive_voice_route_status(session)` maps lifecycle + terminal status
  into the §14 `VoiceRouteStatus` contract; recomputed on every `_touch`.
- **Route summaries**: `VoiceEngine.route_summaries(intake_session_id)` returns a
  `VoiceRouteSummary` per route — the future orchestrator enumerates routes and never blocks
  one route on another.
- **Batchable missing info**: missing canonical paths **accumulate** in `pending_field_paths`
  (append, never overwrite); Issue #5 collects them; the voice layer never launches repeated
  UI prompts.
- **Transport discard-after-use**: `_speak_value` calls `transport.speak(...)` then
  `transport.discard_last_spoken()`, so the JIT value exists only as transient spoken text
  and is gone immediately after.

## 19. Prompt 2: route-local failure scenarios

Each scenario below affects only its own route/session (verified by running a second session
on the same env to completion while the first is paused/failed):

| Scenario | Voice observation | Route status | Issue #8 terminal |
|---|---|---|---|
| A. Broker no answer | `phone_unreachable` | `failed` | `unreachable` |
| B. Call disconnected | `phone_unreachable` | `failed` | `unreachable` |
| C. Callback scheduled | `callback_scheduled` | `callback_scheduled` | `callback_required` |
| D. Unknown question | `unknown_broker_question` | `manual_handoff` | paused (manual mapping) |
| E. Identity checkpoint | `applicant_required` | `applicant_required` | paused (human) |
| F. Consent denied | (pause) | `paused_missing_information` | — |
| G. Applicant unavailable | `applicant_required` | `applicant_required` | paused (human) |
| H. Firm quote | `phone_quote_observed` | `quote_pending_normalization` | pending normalization |
| I. Estimate | `phone_estimate_observed` | `estimate_only` | `estimate_only` |
| J. Explicit ineligibility | `explicit_ineligible` | `failed` | `ineligible` |

## 20. Self-test questions (20+)

1. What is `VoiceRouteStatus` and who consumes it? 2. Why is `VoiceLifecycleStatus` distinct
from `AttemptLifecycleStatus`? 3. What is the mandatory gate before any substantive voice
interaction? 4. How does the engine answer a known field without storing it? 5. What does
`applicant_interruptions == 0` prove? 6. Which actions increment `applicant_interruptions`? 7.
How are missing fields batched? 8. What re-check happens on `resume()`? 9. What wins if the
applicant revokes consent mid-call? 10. Why does a voice continuation of a callback route get
its OWN Issue #8 attempt? 11. Which Issue #8 classification rows did Issue #9 add? 12. How is a
quote different from an estimate in route-status terms? 13. Why does the engine never pass raw
broker text as `reason`? 14. What does `discard_last_spoken()` guarantee? 15. What is the
`VoiceRouteSummary` and why does it exist? 16. How is a dynamic field added without touching
the engine/graph/transport/recovery? 17. What is the route-status for a paused missing field?
18. Which checkpoint kinds map to `applicant_required` vs `manual_handoff`? 19. How does the
browser callback → voice integration link attempts? 20. Why does a phone-only route need
no `BrowserSession`? 21. What is the `_VOICE_SAFE_CONTEXT_KEYS` allowlist for? 22. How are
`automated_answers`/`applicant_interruptions` persisted safely? 23. Why is each voice session
given its own attempt rather than sharing the browser route attempt? 24. What is the `pause`
endpoint and why is it idempotent?

## 21. Self-test answers

1. The derived route-local status contract for the frontend / future orchestrator
   (`prepared, running, paused_missing_information, applicant_required, manual_handoff,
   callback_scheduled, quote_pending_normalization, estimate_only, completed, failed`). 2.
   `VoiceLifecycleStatus` is the voice session's own lifecycle; `AttemptLifecycleStatus` is
   Issue #8's per-route attempt lifecycle. 3. Automation disclosure (`disclose_automation`) —
   nothing substantive before it. 4. JIT read via `IntakeValueSource`, spoken through the
   transport, then `discard_last_spoken()`; never cached. 5. The normal unattended path
   required zero applicant interaction. 6. `pause_for_applicant`, `request_consent`,
   `transfer_to_applicant`, `transfer_to_human`, `manual_handoff`. 7. Missing canonical paths
   accumulate in `session.pending_field_paths` (append, dedupe) and are exposed via
   `route_summaries`. 8. Current Issue #5 route-disclosure consent (revocation honoured) and
   field availability. 9. Revocation — the next event returns `request_consent` and discloses
   nothing. 10. `prepare_handoff` calls `begin_attempt(channel=VOICE, parent_attempt_id=<source
   browser attempt>)`, so the voice continuation has its own attempt identity and can progress
   independently (to `quote_pending_normalization` etc.) without touching the immutable
   browser `callback_required` attempt. 11. `phone_quote_observed`,
   `phone_estimate_observed`, `callback_scheduled`, `broker_requires_field`,
   `applicant_required`, `manual_review_required`, `phone_unreachable`,
   `unknown_broker_question`. 12. Quote → `quote_pending_normalization`; estimate →
   `estimate_only`. 13. To prevent raw broker text (e.g. a quote reference) leaking into
   recovery `last_observation_key`, logs, or traces. 14. The transient spoken value is cleared
   immediately after use. 15. A safe per-route summary (status, terminal, pending paths,
   counters) so an orchestrator can run routes independently. 16. Add a schema-backed catalog
   field + an alias row; no engine/graph/transport/recovery change. 17.
   `paused_missing_information`. 18. Identity/declaration/advice/applicant-required +
   consent/household → `applicant_required`; manual review/mapping → `manual_handoff`. 19. The
   voice session's `source_attempt_id` = the browser attempt's `attempt_id`, and its dedicated
   attempt's `parent_attempt_id` points back to the browser attempt (lineage). 20. The voice
   engine depends only on `IntakeValueSource` + `RecoveryEngine`, never a browser session. 21.
   It whitelists which keys may flow into LangSmith-traced state/API (ids, paths, statuses
   only). 22. They are plain safe int fields on `VoiceSession`, incremented in `_decision`,
   persisted via `_touch`. 23. Because `_resolve_current` matches by route; a shared attempt
   would force the continuation to reflect the browser's terminal `callback_required` — a
   dedicated attempt lets it progress independently while preserving Issue #8 terminal
   immutability. 24. A route-local pause that sets `paused_for_applicant`; pausing an
   already-paused/terminal session is a safe no-op.

## 22. CURRENT vs FUTURE

**CURRENT (implemented):** mock/provider-independent voice architecture; unattended happy path
with zero interruptions; route-local pause/failure isolation; batchable missing fields; JIT
consent recheck + immediate revocation; values discarded after use; phone-only-route and
browser-callback integrations with a **dedicated voice attempt per continuation** (browser
attempt stays immutable); route-status + summaries contract; 89 hermetic tests.

**FUTURE:** real telephony adapter (Twilio/OpenAI Realtime etc.) implementing `VoiceTransport`
without business-logic changes; real LLM interpretation for genuinely ambiguous broker phrasing
(always within the JIT privacy boundary); a `ComparisonRun` orchestrator consuming
`route_summaries` to parallelize routes; Issue #10 evidence persistence, Issue #11 quote
normalization, Issue #12 comparability/confidence.

---

**Files created (Prompts 1 & 2):** `app/models/voice.py`, `app/services/voice/{__init__,
transport,question_interpreter,session_store,handoff,engine}.py`, `app/graph/voice_workflow.py`,
`app/api/voice.py`, `tests/voice_helpers.py`, `tests/test_voice_{models,engine,workflow,
privacy,dynamic,recovery,unattended}.py`, `frontend/src/components/VoiceStatus.jsx`.

**Files modified:** `app/services/recovery/classification.py` (voice rows + safe-context keys),
`app/main.py` (voice router), `tests/intake_helpers.py` (email marker), `frontend/src/api.js`,
`frontend/src/App.jsx`.
