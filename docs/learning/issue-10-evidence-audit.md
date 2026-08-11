# Issue #10 — Evidence, Audit & Trace Store (Prompts 1 & 2)

> Status: **✅ Implemented (Prompt 1 — core durable evidence architecture + persistence; Prompt 2 — automatic emission, Postgres validation, hardening & finalization)**
> Depends on: [Issue #1 foundation](./issue-01-foundation.md), [Issue #7 browser](./issue-07-browser-quote-agent.md), [Issue #8 recovery](./issue-08-terminal-status-recovery.md), [Issue #9 voice](./issue-09-voice-handoff.md)

## 0. Prompt 2 at a glance

Prompt 2 closes the gap Prompt 1 deliberately left open: **evidence is now
collected AUTOMATICALLY during normal execution** — recovery decisions, browser
observations, voice observations, route/attempt lineage, consent decisions, and
quote/estimate rows flow through a narrow synchronous `EvidenceSink` injected
into the engines (never SQL/Postgres logic inside them). It also adds money
(Decimal) hardening, a persistence-failure policy, a skip-gated Postgres
integration profile, an evidence health endpoint, and a large hermetic E2E +
auto-emission test suite.

The 30-second explanation: *"Engines no longer know how to persist. They depend
on a tiny `EvidenceSink` protocol; the default is a no-op so everything keeps
working when evidence is disabled. When wired, a synchronous sink bridges to the
async `EvidenceService` on a background loop, writes are idempotent and
attempt-ordered, failures return an explicit `persistence_failed` status (never
a provider retry), and the full browser/voice/recovery journey leaves a durable,
privacy-safe, verifiable timeline."*

## 1. What was built and why

The durable evidence/audit layer answers the project's core promise: *for every
provider we attempted, what happened, when, through which route, based on what
observed evidence, and how did we reach the resulting status?*

Prompt 1 delivers the **core durable evidence architecture + persistence**:

- A typed, privacy-safe **evidence domain model** (`EvidenceRecord`, typed
  payloads, `QuoteObservation`, `AuditEvent`) with per-record SHA-256 integrity
  hashes, deterministic idempotency keys, and attempt-local ordering.
- A **repository abstraction** with two implementations: in-memory (hermetic
  default/tests) and SQLAlchemy async (Postgres via asyncpg in production,
  SQLite via aiosqlite in tests) — same dialect-portable models.
- An **`EvidenceService`** that owns the invariants (validate → sanitize → hash
  → idempotency → persist) and provides **explicit ingestion adapters** for
  browser (Issue #7), voice (Issue #9), recovery (Issue #8), route-plan,
  consent, and attempt lifecycle events.
- An **Alembic migration** (`0001_evidence_audit`) creating `evidence_records`,
  `quote_observations`, `audit_events`.
- A **read-only evidence API** with `intake_session_id` ownership scoping.

Deliberately **NOT** in Prompt 1 (future planned): auto-emission from executors,
normalization/comparability (Issues #11/#12), per-attempt hash chaining, a
dedicated attempt/plan table, and the dashboard consumption. Prompt 1 wires
evidence through **explicit service calls** (tested directly), which keeps the
existing 722-test regression untouched and the semantics of every event explicit.

## 2. Key design decisions

| Decision | Choice | Why |
|---|---|---|
| Payloads | Typed Pydantic discriminated union on `kind` | Never an unrestricted JSON dumping ground for PII; `extra="forbid"` + allowlist backstop |
| Money | `Decimal` throughout | Never float; browser float → `Decimal(str(x))` on ingest |
| Hash | Per-record SHA-256 over protected fields | Detect mutation; `sequence`/`created_at`/`evidence_id`/`idempotency_key` excluded so the service can hash before the repo assigns ordering |
| Ordering | Repo-assigned per-attempt `sequence` | Monotonic, stable timeline per attempt; independent across attempts |
| Idempotency | Deterministic `idempotency_key` (event + ownership + payload digest) + unique constraint | Redelivery collapses to one logical record |
| Ownership | Every read scoped by `intake_session_id` | Sessions can never enumerate another session's evidence |
| Raw references | Only `reference_present` + opaque `private_reference_handle` | Raw quote refs stay behind the private boundary |
| URLs | Sanitized at the boundary (`netloc[:port] + path`) | Never persist query/fragment/userinfo/tokens |
| Comparable statuses | Never assigned | Issues #11/#12 own `quoted_comparable`/`quoted_non_comparable` |

## 3. How data flows

```
Issue #7 BrowserObservation        Issue #9 voice observation
Issue #8 RecoveryDecision          Issue #5 consent / RoutePlan / attempt
        │                                  │
        ▼                                  ▼
   ingest.py (pure) ──▶ EvidenceDraft / QuoteObservation
        │                                  │
        ▼                                  ▼
   EvidenceService (validate → sanitize URL → hash → idempotency key)
        │                                  │
        ▼                                  ▼
   EvidenceRepository (append / save_quote_observation / append_audit_event)
        ├── InMemoryEvidenceRepository          (hermetic, thread-safe)
        └── SqlAlchemyEvidenceRepository        (Postgres / SQLite, unique keys)
        │
        ▼
   evidence_records / quote_observations / audit_events
        │
        ▼
   GET /api/v1/evidence/* (read-only, intake_session_id scoped)
```

Event types are **provider-independent** (`PAGE_OBSERVED`,
`BLOCKING_ACCESS_CONTROL_OBSERVED`, `CALLBACK_OBSERVED`,
`BROWSER_QUOTE_OBSERVED` / `BROWSER_ESTIMATE_OBSERVED`, `VOICE_QUOTE_OBSERVED`,
`RECOVERY_DECISION`, `CONSENT_EVENT`, …) and are **mapped deterministically**
from Issue #7/#8/#9 observation types (never insurer-specific).

## 4. Key files, classes & functions

| File | Symbols |
|---|---|
| `app/models/evidence.py` | `EvidenceEventType`, `AuditEventName`, `EvidencePayloadBase` + typed payloads (`PageObservationEvidence`, `BarrierEvidence`, `FieldRequirementEvidence`, `FieldInteractionEvidence`, `CheckpointEvidence`, `QuoteObservationEvidence`, `VoiceObservationEvidence`, `RecoveryEvidence`, `ConsentEvidence`, `RoutePlanEvidence`, `AttemptEvidence`, `SafeMetadataEvidence`), `EvidencePayload` discriminated union + `validate_evidence_payload()`, `EvidenceRecord`, `QuoteObservation`, `AuditEvent`, `EvidenceRecordView`/`QuoteObservationView`/`AuditEventView`/`EvidenceExportView`, `sanitize_evidence_safe_metadata()` + `_EVIDENCE_SAFE_METADATA_KEYS` allowlist |
| `app/services/evidence/url_sanitizer.py` | `SafeUrlInfo`, `sanitize_url()`, `safe_url_only()` |
| `app/services/evidence/hashing.py` | `canonical_json()`, `sha256_hex()`, `evidence_content_hash()`, `quote_content_hash()`, `audit_content_hash()` (UTC-naive datetime normalization) |
| `app/services/evidence/ingest.py` | `EvidenceDraft`, `browser_event_type()`, `browser_draft_from_observation()`, `quote_from_browser_observation()`, `voice_event_type()`, `voice_draft()`, `voice_quote()`, `voice_session_started_draft()`, `voice_checkpoint_draft()`, `field_interaction_draft()`, `recovery_draft_from_decision()`, `route_plan_draft()`, `consent_draft()`, `attempt_draft()` |
| `app/services/evidence/sink.py` | `EvidenceSink` (Protocol), `EvidenceWriteResult`, `EvidenceWriteStatus`, `NoopEvidenceSink`, `EvidenceServiceSink` (sync bridge on a background loop) |
| `app/services/evidence/repository.py` | `EvidenceRepository` (Protocol), `InMemoryEvidenceRepository` |
| `app/services/evidence/persistence.py` | ORM tables (`EvidenceRecordORM`, `QuoteObservationORM`, `AuditEventORM`), `SqlAlchemyEvidenceRepository` |
| `app/services/evidence/service.py` | `EvidenceService` (append/append_many, `record_browser_observation`, `record_browser_quote`, `record_voice_observation`, `record_voice_quote`, `record_recovery_decision`, `record_route_planned`, `record_consent`, `record_attempt`, `record_quote_observation`, `record_audit_event`, get/list\*, `verify_integrity`, `delete_by_intake_session`, `export`), view builders |
| `app/services/evidence/__init__.py` | `get_evidence_service()` (lru-cached; in-memory by default, SQLAlchemy when `evidence_repository_backend=postgres` + `database_url`), `get_evidence_sink()` (shared `EvidenceServiceSink`) |
| `app/db/base.py`, `app/db/__init__.py` | `Base` (DeclarativeBase + naming convention), `create_evidence_engine()`, `evidence_session_factory()` |
| `backend/alembic.ini`, `backend/migrations/env.py`, `backend/migrations/versions/0001_evidence_audit.py` | Alembic async migration |
| `app/api/evidence.py` | Read-only endpoints (`/plans/{id}`, `/routes/{id}`, `/attempts/{id}`, `/attempts/{id}/quotes`, `/{evidence_id}`, `/audit`, `/export`) |
| `app/core/config.py` | `database_url`, `evidence_repository_backend` |

## 5. Architecture decisions, alternatives & tradeoffs

- **In-memory default + SQLAlchemy when configured.** The hermetic default keeps
  tests fast and deterministic; Postgres is opt-in. Tradeoff: the in-memory repo
  is process-lifetime only (documented as such).
- **Repository assigns `sequence`; hash excludes it.** This lets the service hash
  before persistence and avoids a concurrency race on sequence at hash time. The
  hash still covers every semantic field, so mutation detection holds. SQLite
  drops tzinfo on datetime round-trips — the hasher normalizes datetimes to
  UTC-naive so aware/naive UTC hash identically (this was a real bug caught by
  the persistence tests).
- **Per-attempt hash chain deferred.** A `previous_hash` chain would add
  ordering/concurrency coupling for little benefit given per-record hashes +
  deterministic sequences already detect mutation and give stable audit order.
- **No dedicated attempt/plan table.** Lineage is event-based via
  `attempt_id` + `parent_attempt_id` + shared `plan_id`/`planned_route_id`/
  `registry_id`/`distinct_rate_source_id`. This avoids duplicating Issue #8's
  `AttemptRecord` (the plan explicitly says not to duplicate current domain data).
- **Idempotency from content digest (not request ids).** Redelivering the same
  logical observation (same event + ownership + payload) collapses regardless of
  caller-generated ids. Tradeoff: two genuinely-distinct events with identical
  content would dedup — acceptable because `observed_at`/payload differ for
  distinct events.
- **URLs sanitized at the service boundary** (not trusted from callers), keeping
  the `evidence_records.safe_url` column clean even if a caller passes a raw URL.
- **Views omit the opaque reference handle.** The API/export exposes
  `reference_present` only; the handle stays internal. (This is why the API test
  asserts the handle is *not* exported.)

## 6. LangGraph / LangSmith behavior

Prompt 1 does **not** add a new LangGraph workflow and does **not** wire
executors to auto-emit evidence. Evidence is recorded via explicit
`EvidenceService` adapter calls (demo/API and tests). This is an intentional
scope boundary: auto-emission + trace propagation is future planned.

For observability, the evidence **payloads are already LangSmith-safe**
(ids, canonical paths, counts, page signatures, sanitized URLs, typed safe
payloads). When auto-emission lands, the intended metadata (registry_id,
distinct_rate_source_id, route_type, workflow stage, run/trace id) will attach
to these records without exposing applicant data. The `intake_session_id`,
`attempt_id`, `parent_attempt_id`, and `source_channel` fields on every record
are the join keys for correlating evidence back to LangGraph runs.

## 7. Privacy / security implications

- Only safe metadata persists; **never** licence/VIN/DOB/address/email/phone/
  claims/raw quote references/screenshots/audio/transcripts/full profile.
- `SafeMetadataEvidence` has a **model-level validator** that rejects any key
  outside the allowlist (backstop beyond `sanitize_evidence_safe_metadata`).
- URLs are stripped of query/fragment/userinfo before storage.
- Every read path requires `intake_session_id`; cross-session reads return
  empty/404.
- `delete_by_intake_session` provides scoped retention.
- The privacy test suite scans records, hashes, idempotency keys, audit events,
  and exports for every sensitive marker.

## 8. Testing strategy (108 Issue #10 tests: 102 non-gated + 6 Postgres-gated)

- `test_evidence_models.py` — schema/validation, redaction, Decimal money, views.
- `test_evidence_service.py` — append/ordering/idempotency/integrity/audit/
  retention/ownership + all adapters (in-memory).
- `test_evidence_persistence.py` — SQLite SQLAlchemy repository (round-trips,
  Decimal, audit, retention, mutation detection) + **Alembic `upgrade head`** on
  a temp SQLite DB and a write-through check.
- `test_evidence_ingestion.py` — exhaustive browser/voice event-type mappings and
  pure conversion helpers.
- `test_evidence_lineage.py` — the cross-issue scenarios: callback→voice
  continuation (§47), browser quote (§48), estimate stays estimate (§49),
  aggregator 3-quotes/1-attempt (§50), CAPTCHA→blocked (§51), consent
  grant→revoke (§53), integrity mutation (§54), idempotent redelivery (§55),
  stable timeline (§56), raw reference never persisted (§46).
- `test_evidence_privacy.py` — PII meta-scans over every stored artifact + URL
  sanitizer + allowlist backstop.
- `test_evidence_api.py` — endpoints, ownership boundary, export, 422 on missing
  query param, static-path-before-catch-all route ordering.

Hermetic rules hold: no real insurer/telephony/LLM calls, no LangSmith uploads,
no applicant data. Real-site/Postgres testing is separate.

## 9. Failure scenarios & debugging

- **Hash mismatch after DB round-trip** → the datetime normalization bug; the
  fix is in `hashing._utc_naive_iso`. Check for tz-aware vs naive mismatches.
- **Idempotency not collapsing** → the idempotency key included operational
  fields (`quote_id`/`created_at`); keys must derive only from semantic fields.
- **SQLite "unable to open database file" on Windows** → Windows backslashes in
  the URL; use `Path.as_posix()` for the DB path (and don't pass a file path as
  a directory).
- **404 on `/audit` or `/export`** → the catch-all `/{evidence_id}` route
  shadowed them; static paths must be declared before the catch-all.
- **Alembic migration writes to the wrong DB** → a stale `ALEMBIC_DB_URL` env
  var overrides the configured URL; the migration test sets it explicitly.

## 10. Common misunderstandings

- Evidence records are **append-only**: a consent revoke is a *new* record, not a
  mutation of the grant.
- `sequence` is per-attempt, not global — two attempts both start at 1.
- The evidence layer never *decides* terminal status; it *records* what Issue #8
  decided (`RecoveryEvidence`).
- An estimate is never upgraded: event type + `firm_vs_estimate` both stay
  "estimate".
- The API does not expose the opaque reference handle by design.

## 11. Interview explanation

"Every provider attempt produces a typed, privacy-safe evidence trail. A service
validates a discriminated union of safe payloads, sanitizes URLs, hashes the
protected fields with SHA-256, derives a deterministic idempotency key, and
persists through a repository that assigns per-attempt ordering. Two backends:
in-memory for hermetic tests, SQLAlchemy async for Postgres/SQLite. Events are
provider-independent and mapped deterministically from the browser, voice, and
recovery layers. All reads are scoped by intake session, raw references stay
behind an opaque handle, and comparability statuses are never assigned here."

## 12. Self-test questions (Prompt 1 + Prompt 2)

1. Why is `sequence` excluded from the content hash?
2. How does idempotent redelivery collapse to one record?
3. What happens when an estimate observation arrives? (two places stay estimate)
4. Why did the SQLite persistence test initially fail integrity verification?
5. How is the ownership boundary enforced on every read?
6. Why must `/audit` and `/export` be declared before `/{evidence_id}`?
7. What is the difference between `reference_present` and
   `private_reference_handle`, and which leaves the API?
8. Why do engines depend on a narrow `EvidenceSink` instead of the service?
9. How does the synchronous sink call the async `EvidenceService` safely from
   sync engines AND async managers without a running-loop conflict?
10. What does the default `NoopEvidenceSink` guarantee for existing tests?
11. Where does browser auto-emission hook in, and what lineage does each record
    carry automatically (registry_id / distinct_rate_source_id / config_version)?
12. How is a voice session's attempt linked to a browser callback attempt?
13. Why is a persistence failure returned as `persistence_failed` rather than
    raised, and why must it never trigger a provider retry?
14. What is the difference between durable, persistence_failed, and disabled
    evidence status?
15. Why was per-attempt hash chaining rejected (and per-record hashing kept)?
16. How does the browser quote detector now produce an exact Decimal, and when
    does the ingest boundary fall back to `Decimal(str(float))`?
17. What does the Postgres integration suite validate that SQLite cannot, and
    how is it gated so the hermetic suite never needs Docker?
18. How is route isolation preserved when one route's evidence persistence fails?
19. Why does the privacy-checkpoint evidence never store banner text/labels?
20. Why is `quoted_comparable` / `quoted_non_comparable` never assigned here?

### Answers

1. `sequence` is operational ordering assigned atomically by the repo at insert;
   hashing it would make the hash depend on insert order and break the
   service-hashes-then-repo-assigns design.
2. The idempotency key is a deterministic digest of (event + ownership +
   payload); the repo has a unique key and returns the existing record on
   collision.
3. Both the event type (`BROWSER_ESTIMATE_OBSERVED` / `VOICE_ESTIMATE_OBSERVED`)
   and the `firm_vs_estimate` stay `"estimate"`; it is never upgraded.
4. SQLite drops tzinfo, so aware vs naive UTC hashed differently; the hasher now
   normalizes datetimes to UTC-naive (`_utc_naive_iso`).
5. Every repository read filters by `intake_session_id`; cross-session lookups
   return `None`/empty and the API returns 404.
6. Starlette matches in declaration order; the catch-all `/{evidence_id}` would
   shadow `/audit` and `/export`, so static paths come first.
7. `reference_present` (bool) is exposed; `private_reference_handle` (opaque)
   stays internal — the API never exports it.
8. So engines never instantiate SQLAlchemy/Postgres/repository classes; the
   sink is a narrow, swappable surface (default no-op).
9. `EvidenceServiceSink` runs the service coroutine on a dedicated background
   event loop via `run_coroutine_threadsafe(...).result()`, safe from any caller
   thread whether or not it has a running loop.
10. All existing 700+ tests keep working unchanged because engines default to a
    no-op sink (evidence disabled unless injected).
11. In `BrowserSessionManager.start_session/step_session` via
    `_emit_step_result`; records carry `attempt_id`, `registry_id`,
    `distinct_rate_source_id`, `config_version`, `page_signature`, safe URL.
12. The voice session's `_ensure_recovery_attempt` calls `begin_attempt` with
    `parent_attempt_id = source_attempt_id` (the browser callback attempt), so
    the voice attempt is a child that can progress independently while the
    browser terminal attempt stays immutable.
13. Raised exceptions could bubble into engine logic and cause retries against
    providers; returning a result keeps provider execution unaffected while the
    failure is explicit (`evidence_status = persistence_failed`).
14. `durable` = last write OK; `persistence_failed` = last write failed
    explicitly; `disabled` = Noop sink (evidence off).
15. A hash chain would need per-attempt serialization/ordering coupling to stay
    concurrency-safe; per-record SHA-256 + deterministic sequences already detect
    mutation and give stable audit order, so chaining was not worth the
    complexity.
16. `PageDetector._parse_amount_decimal` parses an exact `Decimal` from the raw
    text; the ingest `_quote_amount` prefers it, else `Decimal(str(float))`.
17. It validates Alembic upgrade, `Numeric(12,2)` money, unique idempotency
    constraints, JSON payloads, ordering, transaction rollback, and concurrent
    appends; it is `pytest.mark.skipif`-gated on `POSTGRES_EVIDENCE_TEST_URL`.
18. Evidence writes are per-route and never raise into engine logic; a failing
    sink on route A returns `persistence_failed` while route B persists normally.
19. `_browser_payload` maps a checkpoint to `CheckpointEvidence(checkpoint_type,
    automation_decision, ...)` only — never the label/message/DOM.
20. Comparability is Issues #11/#12's domain; evidence only records
    quote/estimate observations and recovery decisions.

## 13. Rebuild exercise

1. Define typed evidence payloads as a discriminated union with `extra="forbid"`.
2. Add a repository protocol with in-memory + SQLAlchemy async implementations.
3. Add hashing (canonical JSON, UTC-naive datetime normalization) and
   deterministic idempotency keys.
4. Build the service (validate → sanitize URL → hash → idempotency → persist)
   plus explicit adapters for browser/voice/recovery/route/consent/attempt.
5. Write the Alembic migration and verify `upgrade head` on temp SQLite.
6. Add read-only, session-scoped endpoints and a privacy meta-test suite.
7. Add the `EvidenceSink` protocol + no-op + background-loop bridge; inject it
   into RecoveryEngine/VoiceEngine/BrowserSessionManager/IntakeEngine.
8. Wire auto-emission at begin_attempt/decide (recovery), emit_observation/
   prepare_handoff/_speak_value (voice), and per-step results (browser).
9. Add the persistence-failure policy tests, Decimal hardening, and the
   skip-gated Postgres integration profile.

## 14. Cheat sheet

- `EvidenceService.append(draft, intake_session_id)` — the only write entry point
  (plus `record_*` adapters and `record_quote_observation`/`record_audit_event`).
- `get_evidence_service()` — in-memory by default; Postgres when
  `evidence_repository_backend=postgres` + `database_url`.
- `get_evidence_sink()` — shared synchronous sink for automatic engine emission.
- `EvidenceSink.record/record_quote/record_audit` return `EvidenceWriteResult`
  (never raise); `evidence_status()` = durable | persistence_failed | disabled.
- Hash fields exclude: `evidence_id`, `created_at`, `sequence`,
  `idempotency_key`, `content_hash`.
- Browser money: `RawQuoteObservation.annual_amount_decimal` (exact) preferred
  over `annual_amount_parsed` (float).
- Migration: `alembic upgrade head` (async, `ALEMBIC_DB_URL` honored).
- Postgres integration: `POSTGRES_EVIDENCE_TEST_URL=...` (skips when unset).
- Run: `pytest tests/test_evidence_*.py` (108 tests: 102 non-gated + 6 gated).

## 15. Prompt 2 deep-dive

### 15.1 The `EvidenceSink` abstraction

```python
class EvidenceSink(Protocol):
    enabled: bool
    def record(self, intake_session_id, draft) -> EvidenceWriteResult: ...
    def record_quote(self, intake_session_id, quote) -> EvidenceWriteResult: ...
    def record_audit(self, intake_session_id, *, event_name, actor, safe_metadata) -> EvidenceWriteResult: ...
    def evidence_status(self) -> str: ...
```

- `NoopEvidenceSink` — safe default (disabled); keeps all pre-Prompt-2 tests
  byte-identical.
- `EvidenceServiceSink` — durable sync bridge; runs the async `EvidenceService`
  on a dedicated background event loop thread so it works from sync engines
  (RecoveryEngine/VoiceEngine/IntakeEngine) and async managers
  (BrowserSessionManager) with no running-loop conflict.
- `EvidenceWriteResult(status, record_id, error_category)` — never raises; a
  failed write returns `persistence_failed` so evidence is never silently lost
  and provider execution is never retried solely due to a DB failure.

### 15.2 Automatic emission points

| Engine | Hook | Evidence |
|---|---|---|
| RecoveryEngine | `begin_attempt(..., intake_session_id=...)` | `ATTEMPT_STARTED` (with `parent_attempt_id`) |
| RecoveryEngine | `decide()` wrapper | `RECOVERY_DECISION`; terminal → `ATTEMPT_COMPLETED` |
| VoiceEngine | `prepare_handoff` | `VOICE_SESSION_STARTED` (+ attempt via recovery) |
| VoiceEngine | `disclose_automation` | `VOICE_CHECKPOINT_OBSERVED` (automation_disclosure) |
| VoiceEngine | `emit_observation` | voice observation; quote/estimate → quote row |
| VoiceEngine | `_speak_value` | `FIELD_INTERACTION_OBSERVED` (canonical path only) |
| VoiceEngine | `end_session` | `VOICE_CHECKPOINT_OBSERVED` (session_end) |
| BrowserSessionManager | `start_session`/`step_session` | per-step `BrowserObservation` + quote row |
| BrowserSessionManager | `create` | `ROUTE_PLANNED` (execution prepared) |
| IntakeEngine | `grant_route_consent` | `CONSENT_EVENT` (paths only) |

Engines take `evidence_sink: Optional[EvidenceSink] = None` (default Noop). App
singletons (`get_recovery_engine`, `get_voice_engine`, `get_browser_manager`,
`get_intake_engine`) inject `get_evidence_sink()`.

### 15.3 Persistence-failure policy (§11/§12/§29)

- The sink catches exceptions and returns `EvidenceWriteResult(status=
  persistence_failed, error_category=<exception-type>)` — exception messages/
  args (which could carry PII) are never surfaced.
- Engines continue their normal flow; provider requests are NEVER re-attempted
  due to a DB failure (avoid duplicate submissions).
- Quote-result evidence returns the REAL status so a quote is never falsely
  "durable".
- Failures are route-local: one route's failed sink does not block another.

### 15.4 Money / Decimal hardening (§16)

`PageDetector._parse_amount_decimal(raw)` parses an exact `Decimal` from the
original text; `RawQuoteObservation` gains `annual_amount_decimal` /
`monthly_amount_decimal` (optional, additive). The ingest boundary
`_quote_amount` prefers the exact Decimal and falls back to
`Decimal(str(float))`. Precision tests cover `1234.56`, `0.01`, `99999.99`.
NO float money is ever persisted.

### 15.5 Run/trace correlation & hash chaining (§9/§17)

- Correlation is via the existing `intake_session_id`, `plan_id`, `attempt_id`,
  `parent_attempt_id`, `source_channel` — no new identifier invented; LangSmith
  traces are never duplicated into Postgres.
- Per-attempt hash chaining was **rejected**: it would require per-attempt
  serialization/ordering coupling to be concurrency-safe; per-record SHA-256 +
  deterministic sequences already detect mutation. Documented, not implemented.

### 15.6 Postgres integration (§15)

`tests/test_evidence_postgres.py` is `skipif`-gated on
`POSTGRES_EVIDENCE_TEST_URL` (or `DATABASE_URL_TEST`). It validates Alembic
upgrade, `Numeric(12,2)` money, unique idempotency constraints, JSON payloads,
ordering, transaction rollback, and concurrent appends against real Postgres.
Skips cleanly when unset — the hermetic suite never needs Docker/cloud.

### 15.7 Privacy / ownership / health (§25/§28/§33/§38/§20)

- Every read is `intake_session_id`-scoped; cross-session returns empty/404
  (no enumeration).
- Export is deterministic, PII-free, and includes routes/attempts/parent-child
  lineage/timeline/quotes/outcomes/hashes.
- `GET /api/v1/evidence/health` exposes `{evidence_status, evidence_backend}`
  for the future orchestrator/dashboard (no Issue #13 dashboard).
- Persistence exceptions never leak PII; privacy checkpoints record only a safe
  kind, never banner DOM/text.
- The auto-emission PII meta-tests scan fully automatically generated records,
  hashes, and exports for every synthetic sensitive marker; premium amounts
  (explicit quote observations) are allowed.

> **Implemented vs planned:** Prompts 1 & 2 implement the durable core,
> persistence, explicit + automatic emission, persistence-failure policy, money
> hardening, Postgres integration profile, and read/health API. Future planned:
> trace-id propagation into records, hash chaining, coverage ledger integration,
> normalization/comparability (Issues #11/#12), and dashboard consumption.
