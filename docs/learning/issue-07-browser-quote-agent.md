# Issue #7 — Browser Autofill & Quote Agent

**Status:** ✅ Implemented & verified (Prompts 1–3; **448 tests pass**, hermetic)
**Depends on:** [Issue #1](./issue-01-foundation.md) (Playwright foundation/tracing),
[Issue #3](./issue-03-market-registry.md) (registry), [Issue #4](./issue-04-rate-source-deduplication.md) (dedup),
[Issue #5](./issue-05-intake-agent.md) (consent/intake/vault), [Issue #6](./issue-06-route-planner.md) (route plans)

---

## 1. Problem Issue #7 solves

Issue #6 produces a deterministic **`RoutePlan`** of the Ontario AUTO market with
per-route readiness. Issue #7 turns a **READY web `PlannedRoute`** into actual
browser execution: it opens a verified quote URL, inspects the page, maps visible
questions to canonical fields, fills known values **just-in-time** from the vault,
pauses for missing fields/consent/unknowns via Issue #5, navigates the quote flow,
and **OBSERVES** the outcome (quote / callback / access control / checkpoint /
unknown / technical error).

Issue #7 **observes** — it does **not** classify terminal quote outcomes. Issue #8
converts these observations into terminal statuses, retries, failover and recovery.

## 2. Position in the architecture

```
Consent-aware intake (Issue #5)  ──┐
Route planner (Issue #6) ──────────┼─►  Browser quote agent (Issue #7)  ─►  Browser observations  ─►  Issue #8+ (future)
ProfileVault (Issue #5) ───────────┘          (Playwright + mock site)
```

## 3. RoutePlan → Browser Agent flow

`POST /api/v1/browser/sessions` accepts `intake_session_id`, `planned_route_id`,
`execution_mode` (and optional `plan_id`, `live_gate`). The manager:
1. resolves `planned_route_id` → `registry_id` via the **single centralized compat
   shim** `app/browser/route_identity.py` (`registry_id_for_planned_route`),
2. validates via the **existing** Issue #6 `RoutePlanner.plan()` (no second planner),
3. opens a Playwright context/page, runs the workflow, pauses/resumes, closes.

## 4. Playwright architecture

`app/browser/manager.py` (`BrowserManager`) owns the Chromium process; Issue #7
added `new_context()` / `close_context()` for **one isolated context per session**
(never shared across routes/users) and LIVE privacy defaults. Package:
`playwright>=1.49`; runtime `playwright install chromium`. Headless for hermetic
tests; headful available for demos (`BROWSER_HEADLESS=false`).

## 5. Sandbox vs live execution

`BrowserExecutionMode` = `sandbox | live` (`app/models/browser/session.py`).
- **sandbox**: local mock quote site (`app/browser/mock_site.py`), synthetic
  profiles allowed, external requests are **blocked** at the page level.
- **live**: registry-verified permitted route only; requires the live personal-use
  gate; LIVE context defaults disable video/tracing/screenshots/HAR.

## 6. BrowserSession lifecycle

`create → run → pause (needs_field / needs_consent / human_checkpoint /
unknown_field / value_not_supported / validation_error / ambiguous) → resume →
close → cleanup`. Statuses in `BrowserSessionStatus`. In-memory active sessions.

## 7. BrowserSessionManager

`app/browser/session.py` — owns sessions, per-session contexts/pages, the
executor, route-start validation (returns `BrowserStartRefusal` with
`BrowserRefusalReason` on refusal), `close()`, `cleanup_abandoned()`, and
`live_privacy_context_kwargs()`.

## 8. BrowserExecutor

`app/browser/executor.py` — one generic, data-driven step
(`advance()`): host re-check → consent re-check → validation/access/quote/callback
detection → checkpoint gate → inspect → map → fill (JIT) → pause/navigate →
observe. `_fill_loop` re-inspects after fills for JS-revealed conditional fields
(bounded 3 passes). `FillOutcome` aggregates safe results.

## 9. BrowserSiteAdapter / route-config architecture

`app/browser/adapters.py`: `BrowserSiteAdapter` Protocol + `GenericQuoteSiteAdapter`
providing **safe defaults** (navigation/checkpoint/quote/callback/access-control/
validation bindings). `merged_config()` merges a route config with defaults. Route
configs are data (`BrowserRouteConfigLoader` → `data/browser/routes/<id>.json`,
currently empty pending a verified live route). No insurer-specific logic in the
executor (verified by a meta-test).

## 10. Page signatures

`app/browser/detect.py` `PageDetector.page_signature` — deterministic identity
from URL regex + heading text (normalized-contains) + known field presence
(`PageSignatureSpec`). Advisory (never authoritative for filling); resilient to
heading/URL/container drift.

## 11. Field inspection

`app/browser/inspect.py` `PageInspector` — gathers visible, **enabled, non-readonly**
controls (`input/select/textarea/radio/checkbox`), groups radios/checkboxes by
`name`, extracts **safe metadata only** (label, aria-label, name, id, placeholder,
type, required attribute, option labels). Never reads input values; never dumps
DOM/HTML.

## 12. BrowserFieldBinding

`app/models/browser/config.py` — `external_field_id`, `match_patterns[]`,
`canonical_path`, `control_type`, `fill_strategy`, `transform`, `required`,
`sensitivity`, `enabled`, `option_map`, `date_format`.

## 13. Deterministic field matching

`app/browser/matchers.py` `FieldMapper` — scores each control against each
binding's `MatchStrategy` (label_text, label_contains, normalized_label, aria,
name, id, placeholder, role, css_selector, text_regex) above a threshold; one
control per binding; `ambiguities()` flags a binding matched by >1 control.

## 14. Action matching

`app/browser/actions.py` `ActionClassifier` + adapter `action_bindings` /
`checkpoint_bindings`. Only explicitly-bound actions are auto-clicked; checkpoint
bindings take precedence; unknown buttons are never clicked.

## 15. Ambiguity handling

- **field ambiguity**: `mapper.ambiguities()` → `ambiguous_field` observation +
  `paused_ambiguous` (never fill both).
- **action ambiguity**: multiple **distinct** safe actions → `ambiguous_action` +
  `paused_ambiguous` (never click arbitrarily). Identical duplicate labels
  collapse to one deterministic action (first visible).

## 16. Canonical field paths

Locked syntax from `app/models/insurance/paths.py` (e.g.
`product_data.vehicles[0].use.annual_kilometres`). The browser maps external
controls → canonical paths; presence is checked via `field_presence`; values via
`get_field_value`.

## 17. Just-in-time ProfileVault access

`IntakeEngine.get_field_value(profile_id, canonical_path)` (`app/services/intake/engine.py`)
is a **tightly scoped trusted boundary**: validates the path, returns **exactly one
scalar** (or `None`), rejects subtrees/collections, never logs/traces/serializes/
caches the value, exceptions never contain values. `IntakeValueSource`
(`app/browser/value_provider.py`) exposes `known`/`get`/`request`/`has_route_consent`/
`route_disclosure_covers`/`field_gate`. The executor calls `get` **immediately
before** each fill; the value lives only in a local variable and is discarded.

## 18. Why raw values never enter graph state

Values exist only in the executor's local fill variable and are discarded. The
LangGraph state, `BrowserSession`, observations, logs, LangSmith metadata and API
responses carry **ids, canonical paths, counts, signatures and URLs** only. This is
an architectural guarantee — not reliance on redaction (though
`SensitiveBaseModel` + `redact_kwargs` are a second layer).

## 19. Issue #5 progressive-intake integration

The executor requests missing fields through
`engine.request_fields(session_id, paths, "browser_agent")` → `FieldRequestOutcome`
(already_known / REQUESTED / UNSUPPORTED / consent_required). The applicant answers
through the normal Issue #5 API; the profile is updated; the SAME browser session
resumes.

## 20. Missing-field pause/resume

Missing mapped field(s) → `needs_field` observation, `paused_needs_field`, pending
paths stored in the session (canonical paths only). After Issue #5 collects them,
`resume` re-inspects the **current page**, fills now-known values and continues —
no browser restart.

## 21. Ask-once behavior

`request_fields` returns `already_known=True` for known fields (never re-requested);
the executor only requests genuinely-missing paths. A field asked on page 1 and
re-asked on page 3 is autofilled from the profile without re-questioning
(`test_same_field_appears_twice_reused`).

## 22. Multiple missing fields

All missing mapped fields are **batched** (deterministic sorted order, deduplicated)
and requested in one `request_fields` call (`test_multiple_missing_fields_batched`).

## 23. Conditional questions

After each fill/navigation the executor **re-inspects** and maps newly-visible
fields (`_fill_loop` 3-pass bound). The mock `/chain` page reveals
one-way-commute and rideshare-hours after radio answers.

## 24. Dynamic field architecture

Adding a normal field requires **at most**: (1) optional Pydantic field if new,
(2) Issue #5 catalog entry, (3) browser binding/config, (4) tests. Evidence:
Prompt 2 added `use.commuting` and `use.rideshare_hours_per_week` to
`app/models/insurance/auto/vehicle.py` + `tests/intake_helpers.py` catalog +
bindings in `mock_site._base_bindings` — **no executor/graph/engine change**.

## 25. Dynamic website changes

Config-only: label wording (`label_contains` update), selector/id (`change`/`label`
variants), question order (`order`), optional→required (`winter-required`),
field removed (`nomodel`), field type text→select (`annual-select` via
`fill_strategy=select` + `option_map`). Covered by
`test_dynamic_label_wording_change_via_config`, `test_field_type_change_to_select_via_config`,
etc.

## 26. Fill strategies

`FieldFiller` (`app/browser/fill.py`): `text | integer | select | radio | checkbox |
date | yes_no`, applied deterministically by `fill_strategy`. `_locate` prefers the
DOM id, then name, then label scoped to enabled non-readonly controls.

## 27. Controlled transforms

`TransformKind` registry: `none | enum_to_label | iso_date_to_dest | bool_to_yes_no |
integer_to_string`. No `eval`. Float→int key normalization for `enum_to_label`
(12000.0 → option `"12000"`). Invalid transform/fill-strategy/checkpoint-type fails
Pydantic validation.

## 28. Unsupported destination option

`OptionNotSupportedError` when a select/radio has no compatible option → executor
returns `value_not_supported` + `paused_value_not_supported` (**never** picks an
arbitrary closest option). Issue #8 classifies later.

## 29. Website validation errors

`PageDetector.validation_error_detected` + `validation_detection` patterns →
`validation_error` + `paused_validation_error`. The executor never re-submits in a
loop (bounded; no retry policy).

## 30. Unknown required field

Any unmatched **required** control → `unknown_external_field` +
`paused_unknown_field`, with sanitized label, control type, route and page
signature. Never guesses; never logs values/full DOM.

## 31. Unknown optional field policy

An unmatched **optional** control is **left blank and continued** (site marks it
optional; continuing does not imply declaration/attestation). Required unknowns
always pause.

## 32. Consent recheck

`has_route_consent` is re-checked at the **top of every step** — consent is never
assumed permanent from session start.

## 33. Disclosure-scope expansion

`IntakeEngine.route_disclosure_covers(session, registry, path)` — a path outside
the granted disclosure scope pauses `needs_consent` **before any fill**; the
applicant expands the scope (revoke + re-grant with the path) and resumes.

## 34. Consent revocation

Revoking route consent while paused → resume hits the consent recheck →
`paused_needs_consent`, no data filled.

## 35. Household-driver consent

A household-gated path (`field_gate == "household_consent_required"`) pauses
`needs_consent` **before retrieval/fill**, regardless of known status. After
`record_household_driver_consent` it fills just-in-time.

## 36. Human checkpoints

Reuses Issue #5 `CheckpointService`/`HumanCheckpointKind`. identity_lookup,
consent_attestation, application_declaration → `paused_human_checkpoint`;
signature/payment/purchase/binding/renewal/cancellation (`must_not_automate`) →
`stopped_prohibited`.

## 37. Prohibited actions

Never automated: declaration, signature, payment, policy purchase/binding,
renewal, cancellation. Detected via checkpoint bindings → stop.

## 38. CAPTCHA/access-control policy

`access_control_detected` (reCAPTCHA/hCaptcha iframes, "verify you are human",
access denied, rate limit, bot detection, login wall) → `stopped_access_control`.
**No bypass logic exists.**

## 39. Allowed-host protection

`allowed_hosts` in route config; `_host_allowed` enforces host (and subdomain)
before navigation **and** after navigation/redirect. Unexpected host →
`route_changed` + `stopped_unexpected_host`.

## 40. Redirects

Legitimate configured redirects are fine; unexpected external redirects → stop
(host re-check after `goto` and after clicks). Stored URLs are sanitized (query
stripped) so applicant data in query strings is not persisted.

## 41. Browser crash/timeout behavior

Broad try/except in `advance()` turns browser crashes / context-closed /
navigation-aborted into a safe `technical_error` (`failed`); `goto_timeout_ms`
bounds page loads; fill failures carry the **canonical path** (never the value).
No retry budget (Issue #8). Cleanup still occurs.

## 42. Session isolation

One browser context per session; two sessions → two contexts, no profile crossing
(`test_two_sessions_isolated`). In-memory store; `cleanup_abandoned` closes idle
sessions.

## 43. Raw quote observation

`RawQuoteObservation` (`app/models/browser/observation.py`): registry_id,
observed_at, source_url, annual/monthly raw + parsed (when confident), currency,
coverage/discount observations, validity text, reference_present,
private_reference_handle, is_firm_quote. **Unnormalized** (Issue #11 owns
normalization).

## 44. Annual/monthly detection

`PageDetector.quote_detected` distinguishes annual vs monthly label segments; a
monthly-only amount is **not** mis-assigned to annual. Amounts with no periodicity
are captured as the first amount line (wording preserved).

## 45. Estimate vs firm quote

`is_firm_quote` is set **only** from `firm_quote_patterns` (e.g. "valid for 30
days"). "Estimated premium" never becomes firm (`test_estimate_wording_not_firm`).

## 46. Quote-reference privacy

The raw reference is never serialized — only `reference_present` and an opaque
`private_reference_handle` (sha256 prefix) appear in safe output
(`test_private_reference_never_exposed`).

## 47. Callback/manual handoff observation

`callback_detected` observation carries registry_id, step, sanitized URL,
reference_present; **no call is placed** (Issue #9 consumes it later).

## 48. Live privacy defaults

`BrowserSessionManager.live_privacy_context_kwargs()` → LIVE context has
`no_viewport` and **no** video/trace/screenshot/HAR/network-body options.
`BROWSER_SCREENSHOT_ENABLED=false` default.

## 49. LangGraph

`app/graph/browser_workflow.py` — `BrowserWorkflowState` (safe TypedDict):
initialize → validate_route → (run: launch | resume: step) → `browser_step` loop →
END on pause/success/stop/failed or `max_steps`. One generic loop, **no node per
page/field/insurer**. `_TERMINAL_STATUSES` ends the loop.

## 50. LangSmith

Nodes call `core.tracing.set_stage`; the API wraps invokes with
`run_config(settings, request_id=..., workflow="browser_workflow", extra_metadata={"browser_session_id": ...})`
so request_id ↔ trace_id correlate. Safe metadata only (ids, registry_id, counts,
signatures, observation types) — never field values. Architecturally values never
reach state, so redaction is a backup, not the mechanism.

## 51. Structured logging

`app/browser/*` log via `logger.info/warning` with safe extras
(workflow, workflow_stage, browser_session_id, registry_id, canonical_path,
status). No DOM, form values, request bodies, cookies, or profile.

## 52. Privacy architecture

JIT single-value vault boundary → local fill variable → discard. Safe models use
`SensitiveBaseModel` (redacted repr). Sandbox blocks external requests. Privacy
tests assert exact synthetic markers absent from session/state/logs/API/exceptions.

## 53. Why Issue #7 uses no LLM

Matching, mapping, transforms, detection and classification are deterministic
(label/config-based). No LLM API key required; an optional semantic mapper could
later see **only safe question text**, never applicant answers.

## 54. Why browser automation is deterministic

All decisions (which fields, values, actions, observations) come from data-driven
bindings/config + deterministic scoring; no probabilistic/insurer branches.

## 55. Mock quote site

`app/browser/mock_site.py` — stdlib `ThreadingHTTPServer` (no internet), pages
A–D + ~40 scenario pages/variants, ephemeral port. `mock_site` pytest fixture in
`tests/conftest.py`.

## 56. Hermetic browser tests

`tests/browser_helpers.py` (`make_browser_env`) wires real engine+planner+manager
against the mock site; sandbox pages block non-localhost requests. Zero real
insurer/LLM/LangSmith/network traffic.

## 57. Config-driven second synthetic route

`make_browser_env(..., registry_id="mock-insurer-2")` + its own route config →
generic executor succeeds with no branch (`test_second_synthetic_route_via_config`).

## 58. Dynamic fields/change tests

`tests/test_browser_hardening.py` scenarios A–G + conditional chain +
`test_config_driven_site_change_no_executor_change` (original vs altered config).

## 59. Debugging guide

- `demos/issue7_browser_demo.py <scenario>` for a live walkthrough.
- Inspect `env.manager.last_result(session_id).observation` for the observation.
- Add `PYTHONPATH='tests'`; run `pytest tests/test_browser_*.py -q`.
- Logs carry safe metadata; grep by `browser_session_id` or `registry_id`.

## 60. Common failure modes

- Strict-mode fill errors (duplicate labels) → `_locate` prefers ids.
- Monthly amount assigned to annual → fixed by periodicity-aware detection.
- Optional-unknown treated as unknown-required → fixed policy.
- Static mock page shadowing dynamic variants → removed `/page-b` from `_HTML_PAGES`.
- Import-time sleep → moved `/slow` sleep to request time.

## 61. Common architecture mistakes

- Adding per-field branches to the executor (use bindings/config).
- One LangGraph node per question/page (use the generic loop).
- Serializing values into session/state (use `get_field_value` JIT only).
- `planned_route_id == registry_id` scattered (centralize in `route_identity.py`).

## 62. Alternatives/tradeoffs

- **LLM mapper** rejected (deterministic, hermetic, no key).
- **Selenium** not used (Playwright already the foundation; async, context
  isolation).
- **Remote browsers / distributed infra** deferred (in-memory per-session contexts
  are sufficient for the hackathon).
- **Live-first** deferred: no verified permitted route → mock site is authoritative
  for automated tests.

## 63. Limitations

See [Known limitations](#known-limitations) in the final Prompt-3 report: no
verified live route; in-memory sessions; no distributed execution; no Issue #8/9/10/11;
European ambiguous price formats not normalized; live screenshots/tracing disabled;
website changes may need route-config/adapter maintenance.

## 64. Future Issue #8 boundary

Issue #7 returns **observations** only. Issue #8 will convert them into terminal
statuses (`quoted_comparable`, `blocked`, ...), bounded retries, failover, recovery.

## 65. Future Issue #9 boundary

`callback_detected` / `manual_contact_detected` observations carry safe handoff
context; Issue #9 will consume them (voice/phone, transcription, broker
conversation). Issue #7 never places calls.

## 66. 30-second interview explanation

> "Issue #7 is the browser execution layer. It takes a ready web route from Issue
> #6's plan, opens the verified quote URL in Playwright, inspects the page, maps
> each question to a canonical field via data-driven bindings, fills known values
> just-in-time from the vault, pauses through Issue #5 when a field is missing or
> consent is needed, and navigates the flow until it observes a quote, a callback,
> a CAPTCHA, a human checkpoint, an unknown field, or a technical error. It's fully
> deterministic and data-driven - no LLM, no applicant values in any state, and it
> only observes; Issue #8 will classify outcomes later."

## 67. 2-minute interview explanation

Adds: sandbox (local mock site, external requests blocked) vs live (personal-use
gate, privacy defaults) modes; `BrowserSessionManager` lifecycle with
pause/resume; consent rechecks and disclosure-scope expansion; ambiguity,
unsupported-option and validation-error handling; the safe `get_field_value`
trusted boundary; the bounded LangGraph loop; and the privacy architecture (JIT
single-value retrieval, canonical paths not values).

## 68. Deep technical interview explanation

`BrowserExecutor.advance()` runs one generic step: host re-check → consent recheck
→ validation/access/quote/callback detection → checkpoint gate → `_fill_loop`
(inspect → `FieldMapper.map` → `_fill_known` with consent-coverage + household
gate + JIT `get_field_value` → re-inspect for conditional reveals, bounded 3
passes) → ambiguity/unsupported/unknown/consent/missing pauses → safe action click
→ host re-check → observation. All configuration (bindings, actions, checkpoints,
detection, transforms) is data; `planned_route_id` maps via one shim; the graph
state is a safe TypedDict with counts/ids only; each node is traceable and
correlated to the request id.

## 69. Self-test questions

1. How does a browser session start? 2. Where is `planned_route_id` mapped? 3. What
blocks LIVE execution? 4. When is a value retrieved from the vault? 5. What happens
to an unknown required field? 6. What about an unknown optional field? 7. How are
multiple missing fields handled? 8. How is consent rechecked? 9. What is disclosure-
scope expansion? 10. How is a household-driver field gated? 11. What stops on
signature/payment/purchase? 12. How is a CAPTCHA handled? 13. How are ambiguous
fields handled? 14. How are ambiguous actions handled? 15. What is an unsupported
option? 16. How is a website validation error handled? 17. How are monthly vs
annual amounts distinguished? 18. When is a quote "firm"? 19. How is the quote
reference protected? 20. How are unexpected hosts blocked? 21. What happens on a
browser crash/timeout? 22. How is session isolation achieved? 23. What does the
LangGraph loop look like? 24. Why is there no LLM? 25. Why do raw values never reach
state?

## 70. Answers

1. `POST /browser/sessions` → manager validates (AUTO, web channel, ready, consent,
mode) then workflow `launch` navigates the registry-backed URL. 2. One shim in
`app/browser/route_identity.py`. 3. LIVE requires personal_use_confirmed +
accurate_information_attested + route consent + verified/permitted route. 4.
Immediately before the fill, via `IntakeEngine.get_field_value`. 5. `needs_field`/
unknown → `paused_unknown_field` with safe metadata. 6. Left blank, continue. 7.
Batched, sorted, deduplicated, one `request_fields` call. 8. `has_route_consent`
each step. 9. `route_disclosure_covers` pauses before filling a path outside the
granted scope. 10. `field_gate == household_consent_required` pauses before
retrieval. 11. `stopped_prohibited`. 12. `stopped_access_control`, no bypass. 13.
`ambiguous_field` + `paused_ambiguous`, never fill both. 14. `ambiguous_action` +
pause, never click arbitrarily. 15. `value_not_supported` pause, never closest
option. 16. `validation_error` pause, no loop. 17. Periodicity-label-aware
detection; monthly-only never becomes annual. 18. Only from `firm_quote_patterns`
(e.g. "valid for 30 days"). 19. Only `reference_present` + private hash handle.
20. `allowed_hosts` + post-navigation host re-check. 21. Safe `technical_error`,
bounded goto timeout, no retry budget. 22. One context per session; separate state.
23. initialize → validate_route → launch/step → browser_step loop → END. 24. All
matching/transform/detection is deterministic config. 25. JIT retrieval into a local
variable, discarded; safe state holds paths/ids/counts only.

## 71. Rebuild-from-scratch exercise

1. Models (`app/models/browser/*`): execution mode, statuses, observations, route
config, workflow state. 2. Playwright manager contexts + LIVE privacy kwargs. 3.
Inspector (visible enabled non-readonly controls, safe metadata). 4. Matcher
(scoring + ambiguity). 5. Filler + transforms. 6. Actions/checkpoints classifier.
7. Detector (signature/quote/callback/access/validation). 8. Value provider +
`get_field_value` boundary. 9. Executor step + fill loop. 10. Session manager +
route-start validation. 11. Mock site. 12. LangGraph loop. 13. API. 14. Tests.

## 72. Cheat sheet

```python
# from backend/ with tests on path
$env:PYTHONPATH='tests'
.\.venv\Scripts\python.exe -m pytest tests/test_browser_*.py -q      # Issue #7 tests
.\.venv\Scripts\python.exe demos\issue7_browser_demo.py happy        # mock happy-path demo
.\.venv\Scripts\python.exe demos\issue7_browser_demo.py missing      # pause/resume demo
.\.venv\Scripts\python.exe demos\issue7_browser_demo.py unknown      # unknown-field demo
```

Key names: `BrowserExecutor`, `BrowserSessionManager`, `BrowserRouteConfig`,
`BrowserFieldBinding`, `FieldMapper`, `PageInspector`, `FieldFiller`, `ActionClassifier`,
`PageDetector`, `GenericQuoteSiteAdapter`, `IntakeValueSource`, `get_field_value`,
`route_disclosure_covers`, `live_privacy_context_kwargs`, `build_browser_workflow`,
`RawQuoteObservation`, `BrowserObservationType`, `BrowserSessionStatus`.

**Related:** [learning index](./README.md) · [Issue #5](./issue-05-intake-agent.md) · [Issue #6](./issue-06-route-planner.md)
