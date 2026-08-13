# Issue 16 — Sonnet Autonomous-First Autofill (Human-Assist Fallback)

**Status:** ✅ Implemented (post-Issue-14 provider-onboarding phase) + ✅ Pre-live
verification (v6 licence-submission checkpoint, JIT-path audit, multistep SPA)

**Scope:** Make the Sonnet LIVE route's PRIMARY path an automated, data-driven
Playwright fill (Province=Ontario from a non-PII route constant, derived vehicle/
driver counts, and every mapped canonical field), with the operator (human) as a
fallback ONLY when the executor pauses (unknown question, ambiguity, consent,
human checkpoint). This phase adds no LLM, no new architecture, and no new
external dependency. **v6 additionally pauses BEFORE the licence-submission click**
and requires explicit participant approval to continue.

Depends on: [Issue 7](./issue-07-browser-quote-agent.md) (browser executor /
mapping / detection), [Issue 10](./issue-10-evidence-audit.md) (evidence +
privacy), [Issue 15](./issue-15-provider-onboarding.md) (Sonnet onboarded as a
bounded controlled LIVE route), and the prior "browser-action logging" work
(`BrowserActionEvent`, privacy-safe `browser_action` logs).

---

## 1. What was built and why

Sonnet's real quote journey (confirmed by safe discovery) asks many questions
before a premium: Province, vehicle/driver counts, vehicle details (year, make,
model, VIN, annual km, carpool, commute, winter tires), address, driver identity
(DOB, legal name, licence number, name on licence, expiry), and coverage
(liability limit). The pre-existing smoke scripts filled these by hand-coded
Playwright step-by-step code — deterministic but brittle and insurer-specific.

This phase makes the **generic `BrowserExecutor`** (the same engine used for the
mock site) do the filling, driven only by the **data-driven `sonnet.json` route
config**. The operator is demoted to a fallback: when the executor pauses on an
unmapped Sonnet question or a required human checkpoint, the headed browser stays
open and the operator resolves it, then resumes the **same** browser session and
attempt.

Key additions:

- **`BrowserFieldBinding.constant_value`** — a non-PII route constant
  (Province=Ontario) that is filled without any value retrieval and is never
  logged; it is configuration, not applicant data.
- **`sonnet.json` v5** — 18 canonical field bindings covering the whole first
  Sonnet screen, plus an action binding for the Continue/Next buttons.
- **`demos/sonnet_live_driver.py`** — an interactive, attestation-gated operator
  driver that drives the real manager (autonomous-first) and only hands control
  to the operator on pause (same session/attempt resume, never auto-retries,
  never closes the browser automatically, stops before purchase).
- **`tests/test_sonnet_autofill.py`** — hermetic proof that the real `sonnet.json`
  fills the Sonnet-shaped local mock autonomously, values stay out of events/
  logs/evidence, unknown fields fall back resumably, and CAPTCHA/declaration/
  payment are never automated.

---

## 2. How it works (data flow)

```
intake profile (canonical, in-memory pilot)
   │  IntakeValueSource.get(path)            (JIT, scalar values only)
   ▼
BrowserExecutor.start()/advance()
   │  PageInspector.inspect(page)            (safe metadata only, no values)
   │  FieldMapper.map(obs, config)           (label/id/name/placeholder/etc.)
   │  FieldMapper.ambiguities(obs, config)   (never fill both)
   ▼
_fill_known(page, session, matched)
   ├─ route_disclosure_covers?  no → consent pause
   ├─ field_gate? household-consent → consent pause
   ├─ known(path)?              no → missing → pause (JIT)
   ├─ binding.constant_value is not None → value = constant  ★NEW
   ├─ transform == collection_length → value = len(collection)
   └─ else → value = values.get(path)          (never stored/logged)
   ▼
FieldFiller.fill(select/radio/text/date/checkbox)   → BrowserActionEvent
   ▼
_evaluate_actions: Continue/Next → navigate | declaration → paused_human_checkpoint
                  | signature/payment/purchase → stopped_prohibited
   ▼
PageDetector: quote page → extract premium (explicit) → quote_detected
```

The fill value for `constant_value` never enters the intake vault, never passes
through `IntakeValueSource`, and never appears in `_record_action` (which logs
only `action`, `canonical_field`, `status`).

---

## 3. Key files / classes / functions

| File | Role |
|------|------|
| `backend/app/models/browser/config.py` | `BrowserFieldBinding.constant_value: Optional[str]` (non-PII route constant; `SensitiveBaseModel`, `extra="forbid"`) |
| `backend/app/browser/executor.py` | `_fill_known()` constant short-circuit; `_record_action()` (action + canonical path + status only); `_evaluate_actions()` human-checkpoint vs prohibited classification |
| `backend/app/browser/session.py` | `BrowserSessionManager.start_session()/step_session()` — resume keeps the same page/session/attempt; `_emit_step_result()` emits one `FIELD_INTERACTION_OBSERVED` evidence record per `BrowserActionEvent` |
| `backend/app/browser/matchers.py` | `FieldMapper.ambiguities()` — a binding matching >1 control → pause (`ambiguous_field`) |
| `backend/data/browser/routes/sonnet.json` | v5 route config: 18 bindings + 1 action binding + detection blocks |
| `backend/app/demo/mock_quote_site.py` | `_sonnet_html()` + `/sonnet` + `mock_scenario_url(... , "sonnet")` — hermetic Sonnet-shaped page |
| `backend/tests/test_sonnet_autofill.py` | 6 hermetic proof tests (autofill, privacy, resumable fallback, barriers) |
| `backend/demos/sonnet_live_driver.py` | Interactive operator driver (autonomous-first, human fallback) |

### `sonnet.json` v5 bindings (18)

Province (`constant_value: "Ontario"`, select, non-sensitive) · vehicles count
(id, `collection_length`) · drivers count (id, `collection_length`) · vehicle
year (select) · make (select) · model (text) · **VIN (regex `\bVIN\b`)** · annual
kilometres (integer) · carpool (radio, `bool_to_yes_no`) · commute distance
(integer) · winter tires (radio, `bool_to_yes_no`) · liability limit (select,
`enum_to_label` + `$1,000,000`… option map) · postal code (sensitive) · DOB
(date, sensitive) · legal name (sensitive) · licence number (sensitive) · name on
licence (sensitive) · licence expiry (date, sensitive).

### The VIN-ambiguity gotcha (debugging highlight)

`label_contains "vin"` also matched the **Province** label (`p-r-o-`**`vin`**`-ce`),
so `FieldMapper.ambiguities()` paused with `ambiguous_field`. Fixed with a
word-boundary regex `\bVIN\b` (matches `VIN` but not `Province`). This is a great
example of why ambiguity is **paused, never guessed**.

---

## 4. Mandatory human checkpoints / barriers (never automated)

| Condition | Observation | Browser status | Resumable? |
|-----------|-------------|----------------|------------|
| CAPTCHA / access control | `access_control` | `stopped_access_control` | No — STOP (never solved/retried/reloaded) |
| Declaration / consent page | `human_checkpoint` | `paused_human_checkpoint` | Yes — operator clicks, then resume same session |
| Signature / payment / purchase / bind | `human_checkpoint` | `stopped_prohibited` | No — terminal, `must_not_automate=True` |
| Unknown Sonnet question (unmapped) | `unknown_external_field` | `paused_unknown_field` | Yes — operator answers, then resume |
| Ambiguous field/action | `ambiguous_field` | `paused_ambiguous` | Yes — operator resolves, then resume |
| Missing JIT value / consent expansion | `needs_field` / `needs_consent` | `paused_*` | Yes — operator supplies, then resume |

Declaration semantics: `requires_human=True` but `must_not_automate=False`
(resumable — the operator may click it). Payment/signature/bind:
`must_not_automate=True` (hard stop).

---

## 5. Operator driver (`demos/sonnet_live_driver.py`)

- Attestation-gated: requires `--personal-use --accurate-info`, else refuses.
- Loads the **real** `sonnet.json` (v5) and the real registry entry (verified).
- Uses the **real** `BrowserSessionManager` wiring (engine + planner + registry +
  config loader) in `BrowserExecutionMode.LIVE` with a satisfied
  `LiveExecutionGate`.
- Prints `browser_session_id` + `attempt_id` + per-step `status`, observation
  description, and a privacy-safe action summary (action/status counts +
  canonical paths only).
- On a `PAUSED_*` status: prompts the operator, then calls `step_session` to
  resume the **same** page/session/attempt (no restart, no re-entering fields).
- Stops on terminal statuses; never auto-retries; never closes the browser
  automatically; stops before purchase.

Run (from `backend/`):

```powershell
$env:PYTHONPATH='.'
.\.venv\Scripts\python.exe demos\sonnet_live_driver.py `
    --personal-use --accurate-info --slow-ms 500
```

> Per the phase instruction, the final live quote is **not** run automatically by
> this work — the driver is the controlled human-gated vehicle for that.

---

## 6. LangGraph / LangSmith / observability

- Unchanged LangGraph `browser_workflow` (`entry: "run"` / `entry: "resume"`);
  every `PAUSED_*` status is terminal for the graph (the operator resumes via the
  direct manager/API path, which keeps the same page open).
- Per-action `browser_action` logs: `provider=... action=... canonical_field=...
  status=...` — **paths only, never values**.
- Evidence: each `BrowserActionEvent` → `FIELD_INTERACTION_OBSERVED` with
  `action` + `status`; no PII in traces/evidence (verified by
  `assert_evidence_privacy_safe`).

## 7. Privacy / security implications

- `constant_value` is configuration (Province=Ontario), never applicant data —
  still excluded from logs by the shared "no values in action events" rule.
- VIN/DOB/postal/licence fields are `sensitivity: sensitive`; their values are
  filled JIT and discarded, never stored in session/trace/log/evidence.
- The pilot profile uses the repo's clearly-fictional reserved identifiers
  (`Test Applicant`, `T0000-...`, `1HGCM82633A000000`, `M0A 0A0`); only
  non-sensitive derived values (counts, Province) are ever filled by the pilot.

## 8. Testing strategy

Focused hermetic set: `test_sonnet_autofill.py` (6), `test_sonnet_route.py` (10),
`test_browser_action_logging.py` (12), `test_browser_hardening.py`,
`test_browser_privacy.py`, `test_evidence_auto_privacy.py`,
`test_evidence_live_blocker.py`, `test_evidence_auto_emission.py`. Full suite at
phase end (not during iteration). No real Sonnet, no LLM, no LangSmith uploads
(`LANGSMITH_TRACING=false`).

## 9. Common misunderstandings

- **"Autonomous" does not mean unbounded.** Every step is a bounded observation-
  first pass; unknown/ambiguous/checkpoint/barrier → pause/stop, never guessed.
- **Resume ≠ restart.** `step_session` reuses the open page, so earlier fields are
  not re-entered; the comparison-run path closes the browser after a pass — use the
  direct browser API/manager for interactive resume.
- **`constant_value` is not a PII value.** It is route config (Province=Ontario);
  a real insurer might vary it (e.g. the operator picks another province), which
  would instead be a normal `select` field.

## 10. Pre-live verification (v6): licence-submission checkpoint + JIT audit

**Requirement:** the real Sonnet journey must pause immediately before submitting
the driver's licence number / triggering an identity or database lookup, and
before any declaration or third-party data consent; the licence FIELD may be
filled automatically, but the action that SUBMITS it must wait for explicit
participant approval; after approval the SAME `browser_session_id` + `attempt_id`
resume.

**Implemented behaviour (v6, data-driven + deterministic):**

- **`CheckpointBinding.post_fill_paths`** (new model field). A checkpoint binding
  with a non-empty `post_fill_paths` list becomes a **POST-FILL** checkpoint: it
  fires AFTER the executor fills the screen's fields and BEFORE it clicks the
  matching action — and only if at least one just-filled canonical path CONTAINS
  a configured substring. `sonnet.json` v6 declares
  `{checkpoint_type: identity_lookup, label_patterns: [continue, next, confirm,
  get started], post_fill_paths: [".licence."]}`.
- **`Clickable.post_fill` / `post_fill_paths`** — the classifier tags which
  checkpoint bindings are post-fill, so the pre-fill gate skips them (the licence
  field is filled) and the new **`BrowserExecutor._post_fill_checkpoint`** gate
  pauses with `paused_human_checkpoint` + `identity_lookup` right before the
  submitting Continue.
- **`_find_safe_action` fix:** a post-fill checkpoint action is treated as
  clickable on screens where its trigger paths were NOT filled (e.g. the Province
  / vehicle screens), otherwise the checkpoint binding would shadow the
  `safe_navigation` Continue and the page would never advance.
- **`BrowserSession.checkpoint_approvals`** (new field) + **
  `BrowserSessionManager.approve_checkpoint(session_id, checkpoint_type)`** —
  explicit participant approval recorded on the SAME session (same
  browser_session_id + attempt_id). MUST-NOT-AUTOMATE kinds (payment / signature /
  purchase / bind) and unknown kinds are rejected. New API endpoint
  `POST /api/v1/browser/sessions/{id}/approve-checkpoint`.
- **Declaration / third-party consent** remain PRE-FILL human checkpoints from the
  generic adapter defaults (`application_declaration`, `consent_attestation`) —
  the executor pauses BEFORE filling anything on those screens.
- **`demos/sonnet_live_driver.py`** now prompts the participant explicitly
  ("Type YES to approve…") before resuming past a `paused_human_checkpoint`.

**JIT-path audit (req 4):** every one of the 18 `field_bindings` is FULLY-QUALIFIED
(`applicant.*` or `product_data.*` — the earlier report's `coverage...` /
`vehicles[0]...` / `drivers[0]...` were display shorthand, never the real JSON).
`tests/test_sonnet_jit_paths.py` proves all 18 resolve through the SAME production
accessor the executor uses (`IntakeEngine.get_field_value` for the 16 scalars,
`IntakeEngine.get_collection_length` for the 2 derived counts) and that the
accessors reject the wrong shape (scalars reject collections and vice versa).

**Multistep SPA (req 5):** `mock_quote_site.py` now serves a real multi-screen
Sonnet mock (`/sonnet-step/1` Province+counts → `/sonnet-step/2` vehicle →
`/sonnet-step/3` driver → `/quote?variant=annual`), so the generic executor is
proven as observe → fill → Continue → observe next screen → repeat, and the
checkpoint fires only on the driver screen (filled paths contain `.licence.`).

**Missing-value pause (req 6):** a missing canonical value (e.g.
`annual_kilometres=None`) → `paused_needs_field` with NO blank fill and NO
Continue click; once supplied, the field is filled (not blank) and the screen
pauses at the licence checkpoint before any submit.

**Files:** `app/models/browser/config.py` (`post_fill_paths`),
`app/models/browser/session.py` (`checkpoint_approvals`),
`app/browser/actions.py` (`Clickable.post_fill`),
`app/browser/executor.py` (`_post_fill_checkpoint`, `_find_safe_action`),
`app/browser/session.py` (`approve_checkpoint`), `app/api/browser.py`
(`approve-checkpoint`), `data/browser/routes/sonnet.json` (v6),
`app/demo/mock_quote_site.py` (multistep screens),
`demos/sonnet_live_driver.py` (approval prompt),
`tests/test_sonnet_jit_paths.py`, `tests/test_sonnet_checkpoint_multistep.py`,
`tests/test_browser_api.py`, `tests/test_evidence_auto_privacy.py` (flaky
whole-record scan → allowlist content scan).

## 11. Self-test questions

1. Why is `Province` a `constant_value` rather than an intake field?
2. What does `FieldMapper.ambiguities()` do, and why is it paused rather than resolved?
3. Why did `label_contains "vin"` become ambiguous, and how was it fixed?
4. What is the difference between `paused_human_checkpoint` and `stopped_prohibited`?
5. How does the operator resume without restarting the quote?
6. Why must `constant_value` still never appear in logs?
7. Why is the licence-submission checkpoint POST-FILL (after filling the licence
   field) but declaration/consent PRE-FILL (before filling anything)?
8. Why did `_find_safe_action` need to treat post-fill checkpoint actions as
   clickable, and why is that still safe?
9. Which checkpoint kinds can `approve_checkpoint` accept, and which can it never?

## 12. Rebuild exercise

1. Recreate `_sonnet_html()` (all 18 controls + province constant + Continue).
2. Re-add the 18 bindings to `sonnet.json` (careful: `\bVIN\b` regex).
3. Add `constant_value` handling to `_fill_known`.
4. Write the 6 autofill tests; prove privacy + resumable fallback + barrier stops.
5. Wire the operator driver with the attestation gate + resume loop.
6. Add the v6 `post_fill_paths` licence checkpoint, `approve_checkpoint`, and the
   multistep mock; prove 2-step progress after approval (submit click then quote).

## 13. Cheat sheet

- Province constant: `{"external_field_id":"sonnet-province","constant_value":"Ontario",...}`
- VIN regex: `{"strategy":"text_regex","value":"\\bVIN\\b"}`
- Action binding: `{"action_type":"continue","safety":"safe_navigation","label_patterns":["continue","confirm","get started","next","next: vehicle details"]}`
- v6 checkpoint: `{"checkpoint_type":"identity_lookup","label_patterns":["continue","next","confirm","get started"],"post_fill_paths":[".licence."]}`
- Approve: `POST /api/v1/browser/sessions/{id}/approve-checkpoint` `{"checkpoint_type":"identity_lookup"}`
- Run focused tests: `$env:PYTHONPATH='tests'; .\.venv\Scripts\python.exe -m pytest tests/test_sonnet_jit_paths.py tests/test_sonnet_checkpoint_multistep.py tests/test_sonnet_autofill.py -q`
- Run driver: `$env:PYTHONPATH='.'; .\.venv\Scripts\python.exe demos\sonnet_live_driver.py --personal-use --accurate-info`
