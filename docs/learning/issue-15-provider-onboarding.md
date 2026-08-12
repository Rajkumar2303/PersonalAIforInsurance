# Issue #15 — Provider Onboarding Utility + 2 Direct Insurers + 1 Aggregator

**Status: PARTIAL (utility + safe onboarding built; real multi-source quotes NOT
yet obtained).** This is the post-Issue-#14 phase. This document describes what
was actually implemented and what was honestly observed during SAFE (no-PII)
inspection of three real Ontario providers.

## What was built and why

The goal was to move from mock-only comparison toward a real multi-provider
comparison. The deliverable is a **deterministic provider-onboarding utility**
that goes: existing Market Registry → candidate quote URL → SAFE (no-PII)
inspection → DRAFT route config → human-gated approval. No LLM, no applicant
data on the CLI, no CAPTCHA bypass, no auto-verification.

## Files / classes / functions

- `backend/app/tools/provider_onboarding.py` — CLI entry
  (`python -m app.tools.provider_onboarding --registry-id <id> ...`), with a
  human-gated `--approve` path. Refuses unknown/inactive registry ids.
- `backend/app/services/onboarding/` package:
  - `canonical.py` — deterministic label→canonical-path mapping
    (`normalize_label`, `map_label`, `map_labels`); unknown labels →
    `unmapped_field`, never guessed.
  - `draft.py` — `validate_candidate_url` (HTTPS + host), `derive_allowed_hosts`
    (final-host only), `build_draft_config` (DRAFT `BrowserRouteConfig` with
    `last_verified_at=None`), `build_report` (safe metadata), soft bot-block
    detection phrases.
  - `inspection.py` — `inspect_page` reuses `BrowserManager`,
    `live_privacy_context_kwargs`, `PageInspector`, `PageDetector`,
    `GenericQuoteSiteAdapter`; collects safe page metadata, privacy-banner
    heuristic, CAPTCHA/callback/quote detection, soft bot-block
    (`is_bot_block_text`).
  - `repository.py` — drafts dir (`backend/data/browser_routes/drafts/`),
    `save_draft`/`load_draft`, human-gated `promote_draft` (writes live config +
    marks registry verified), `mark_registry_verified`.
- Drafts generated from real inspections: `belairdirect`, `square-one`,
  `lowestrates-ca`, `insurancehotline`, `aviva-direct` (+ `.report.json` each).
- `backend/tests/test_provider_onboarding.py` — 14 hermetic tests.

## How URL discovery works

- If `MarketRegistryEntry.quote_url` exists it is used; otherwise a public
  `--candidate-url` is required (the registry seed has no quote_urls yet, so a
  candidate public homepage/quote URL is supplied on the CLI - never PII).
- The tool validates HTTPS + non-empty host, navigates, records the **final**
  URL, derives allowed hosts from the final host only (defense-in-depth:
  redirects to a different host must be re-approved; `square-one` redirected
  `squareoneinsurance.com` → `squareone.ca` and was flagged `host_allowed=False`
  as a conservative warning).
- SAFE inspection only: page heading, page signatures, visible fields
  (label/id/type/options), buttons, privacy-banner presence, CAPTCHA/bot,
  callback, quote detection. No form fill, no clicks on submission actions, no
  full-DOM dumps. Soft bot-blocks ("Sorry, you have been blocked", Cloudflare)
  are classified as `access_control_detected=True` (added as a generic
  regression: `is_bot_block_text` + expanded access-control patterns).

## How human approval works

`--registry-id X --approve` loads the existing draft, prints a URL/host/config
summary, and requires explicit confirmation (`--yes` or typing `yes`). Only
then does `promote_draft` write the config into the LIVE browser-routes dir and
mark the registry route `verified` (with `last_verified_at`). A draft is never
auto-verified; the report's `safe_to_live_test` stays `false` until approved.
Approval itself sends no applicant data.

## Selected providers & SAFE-inspection outcomes (no PII, no approval)

Per the issue, the 3 selected candidates were reported before live interaction.
SAFE inspection (this environment, headless Chromium, no applicant data):

1. **belairdirect** (direct) — **BOT-BLOCKED**: "Let's confirm you are human"
   (`access_control_detected=true`). Honest: unsuitable for unattended quoting.
2. **square-one** (direct) — **OPEN**: no bot barrier; quote entry point found
   ("Enter your address to get quote", "GET A QUOTE"); privacy banner present;
   callback text present. Best real candidate.
3. **lowestrates-ca** (aggregator) — **CLOUDFLARE-BLOCKED** ("Sorry, you have
   been blocked").
   - Backup aggregator `insurancehotline` — also **CLOUDFLARE-BLOCKED**.
   - Alternate direct `aviva-direct` — candidate URLs 404 (not quote-accessible
     at the guessed paths).

**No real quote was obtained** — completing a live quote requires the
participant's own accurate data entered through the frontend/ProfileVault and a
human approval, neither of which happens in this automated session (and no
fabricated/synthetic quotes are ever produced). This is an honest
`PARTIAL` result: the utility works end-to-end, one real source is accessible,
but multi-source real comparison is not yet achieved.

## Required-field union (from inspections)

- `square-one` observed: address/postal-code entry → canonical
  `applicant.address.postal_code` (mapped); a "Select language" control →
  `unmapped_field` (localized catalog candidate).
- Blocked providers contributed no usable form fields (bot walls).
- Union of confirmed required fields across accessible/verified routes is
  **not yet established** (no verified route exists yet); the catalog-driven
  intake already collects the standard Ontario set. The utility records
  observed required fields per route so this union can be computed once routes
  are verified.

## New canonical fields discovered

None new yet; one localized-catalog candidate: **Preferred language on quote
start** (`applicant.identity.preferred_language` already exists — mapped).

## Configs / verified routes / blockers

- Drafts: 5 (belairdirect, square-one, lowestrates-ca, insurancehotline,
  aviva-direct). **Verified: 0.** Live configs promoted: **0.**
- CAPTCHA/bot blockers: belairdirect, lowestrates-ca, insurancehotline.
- No quote observations obtained; therefore no normalization/comparison
  results were produced from real providers.

## Privacy / security

- CLI accepts only a registry id + public candidate URL; **no licence/VIN/DOB/
  name/address on the CLI** (explicitly rejected by design).
- Inspection is NO-PII; context uses `live_privacy_context_kwargs`
  (no video/trace/screenshot/HAR). No full DOM dumps.
- Drafts contain public URLs/headings only; no applicant values.
- Ordinary pytest is hermetic (temp dirs, mock data) — never hits live
  insurers.

## Testing strategy

`test_provider_onboarding.py` (14 tests): URL/host validation, canonical
mapping localization, never-guess on unknown labels, draft never auto-verified,
promotion requires confirmation/draft, promotion writes live config + marks
registry verified, unknown registry id rejected, **demo registry ids refused by
the tool** (demo routes cannot become live), aggregator draft compatibility,
soft bot-block classification regression.

## Failure scenarios / debugging

- A provider with a bot wall → `access_control_detected=true`, onboarding stops,
  provider stays `discovered`/unsuitable; other providers are unaffected.
- Redirect to a new host → `host_allowed=false` (re-approval needed).
- Missing candidate URL → tool errors (registry has no quote_urls yet).
- The earlier `BrowserFieldObservation` attribute was `external_field_id` (not
  `external_id`) — fixed; `derive_allowed_hosts` must get the raw final host,
  not the sanitized URL.

## Self-test questions

1. What does the onboarding tool inspect, and what does it never do?
2. How does a draft become verified (and who approves)?
3. Why can a draft never be promoted without human confirmation?
4. What happens when a provider is CAPTCHA/Cloudflare-blocked?
5. How are demo routes prevented from becoming live?

## Rebuild exercise

Run
`python -m app.tools.provider_onboarding --registry-id square-one --candidate-url https://www.squareoneinsurance.com/auto-insurance/ --headless`,
then inspect `backend/data/browser_routes/drafts/square-one.report.json`; it
must show `verified:false` and `safe_to_live_test:false`. Do NOT approve.

## Cheat sheet

- Inspect: `python -m app.tools.provider_onboarding --registry-id <id> --candidate-url <https url>`
- Approve: `python -m app.tools.provider_onboarding --registry-id <id> --approve --yes`
- Drafts: `backend/data/browser_routes/drafts/<registry_id>.json` (+ `.report.json`)
- Tests: `pytest tests\test_provider_onboarding.py -q`

## Current readiness

**REAL MULTI-SOURCE COMPARISON READY: PARTIAL** — onboarding utility + safe
discovery work; one of three primary candidates is accessible (Square One); the
other two are bot-blocked; real quotes require the participant's data via the
frontend + human approval (future step). No verified routes or real quote
observations yet, so nothing was normalized/compared from real providers.

## Post-15: first real LIVE run — Square One callback blocker (validated & polished)

**Implemented (verified against the running backend, 2026-08-12):**

The first controlled LIVE run against the verified Square One route
(`square-one` → `RS-ZURICH-AUTO` / Zurich) was validated and preserved as a
genuine **evidence-backed live blocker**:

- **Outcome (unchanged):** `callback_required`. The Square One browser session
  started and closed normally; a callback barrier was detected on the quote
  landing page; **no quote was returned and no premium was fabricated**.
  `run_id=9be37dc55e8747dd8a922131efddcb3c`,
  `session_id=fa950e310cb6405ba538288bd5cc6598`,
  `attempt_id=606d629ee9954503a5e6444cc97611d3`.
- **Terminal reason + timestamp:** recovery decision evidence
  (`event_type=recovery_decision`) carries `terminal_status=callback_required`,
  `reason_codes=["callback_required"]`,
  `recommended_action=prepare_voice_handoff`, `retry_allowed=false`,
  `requires_human=true`; the barrier was observed at
  `2026-08-12T21:21:45.197592Z` (`callback_observed`,
  `page_signature=square-one_landing`, sanitized
  `safe_url=www.squareone.ca/auto-insurance/`, `requires_human=true`).
- **Redaction confirmed:** the exported evidence chain (consent → route_planned
  → attempt_started → callback_observed → recovery_decision → attempt_completed)
  contains **safe metadata only** — no licence, VIN, DOB, address, name, email,
  phone, or claims content. `quote_count=0`.
- **Preserved:** the redacted export and the exact route summary were captured
  into hermetic fixtures
  (`backend/tests/fixtures/live_square_one_callback_required.json` +
  `live_square_one_route_summary.json`) and locked down by
  `backend/tests/test_evidence_live_blocker.py` (8 tests: chain/timestamps,
  no-quote/no-premium, privacy scan via `assert_evidence_privacy_safe`, schema
  validity, marker-free fixtures). This preserves the blocker across server
  restarts and proves it in CI without any real browser/LLM/network.

**Polish (frontend, both verified against `npm run build`):**

- **Stale live banner fixed** (`frontend/src/App.jsx` + `api.js`): the banner is
  now **data-driven** from `GET /api/v1/markets` (verified entries with a quote
  URL) instead of the hardcoded "Not configured - no verified live route". In
  LIVE mode it now resolves to `Verified live route: Sonnet, Square One`
  (both registry entries are `status=verified` with quote URLs; Square One is
  the distinct-rate-source-verified route `RS-ZURICH-AUTO`). States are
  `loading → configured | unconfigured | unknown` (never a stale claim).
- **Results-table alignment fixed** (`frontend/src/components/ComparisonResults.jsx`
  + `index.css`): a route that returned **no quote** is no longer labelled
  "Quote" (it shows an em dash "—" in Result type); the status is rendered as a
  coloured **status pill** ("Callback required" → red callback pill); rows for
  `callback`/`handoff`/`not-ready` get their own highlight so the
  provider↔status relationship is unambiguous. The outcome itself is untouched:
  Square One stays `callback_required`, `annual_premium=null`.

**Future planned:** a real quote (not just callback) for a verified route, so
the full normalize→compare pipeline runs against a genuine premium; voice
continuation of the Square One callback via the Issue #9 handoff.

**Inferred tradeoff:** the evidence store is **in-memory by default**, so the
runtime evidence of a live run is lost on server restart — hence the fixtures
above (redacted) are the durable, committable record of the blocker.
