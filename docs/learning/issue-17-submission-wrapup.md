# Issue 17 — Submission Wrap-Up: Deterministic Demo & Honest Outcomes

> One learning document per issue. This documents the final hackathon wrap-up that
> made the existing project **submission-ready and reliably demonstrable** without
> any live provider interaction. Read `issue-01-foundation.md` first; this document
> focuses on what was newly built in the wrap-up and links back where it depends on
> earlier issues.

## 1. What was built and why

The project already had a full evidence-first pipeline (Issues 1–16). The wrap-up
added a **deterministic, local, submission-ready demo** plus the artifacts a judge
needs, while enforcing honesty and safety:

- `backend/demos/submission_demo.py` — a deterministic generator that produces:
  1. two clearly-labelled **sandbox ESTIMATE outcomes** (`Ontario Sandbox Direct`
     $2,400, `Ontario Sandbox Broker` $2,180) persisted through the **real**
     `EvidenceService.record_voice_quote` → `QuoteNormalizationService` pipeline,
     each labelled `status=estimate_only`, `source_environment=local_sandbox`,
     `not_a_live_quote=true`;
  2. an honest **Sonnet UNRESOLVED** outcome (`quote_returned: false`, last
     confirmed stage `province_page`, attempt/session ids `unavailable`);
  3. a **manual-handoff** outcome with `handoff_executed: false`;
  4. a **market-registry export** (JSON + CSV, 22 columns, unknowns preserved as
     `unknown`);
  5. a **redacted run report** (JSON + Markdown) with metrics computed from the
     records and a prominent demo-data banner.
- `backend/tests/test_submission_demo.py` — 9 focused tests proving the honesty,
  comparability, evidence, and privacy claims.
- `docs/ARCHITECTURE_AND_SAFETY.md`, `docs/DEMO_SCRIPT.md`, `docs/DEMO_CHECKLIST.md`,
  `docs/KNOWN_LIMITATIONS.md` — submission docs.
- README + `.gitignore` updates (privacy hardening).

**Why:** the challenge requires a *reliable, deterministic, honest* demo. Synthetic
estimates must never be mistaken for live quotes; the real Sonnet attempt returned
no quote and that must be reported truthfully; barriers (declaration, payment,
signature, purchase, CAPTCHA) must stay enforced.

## 2. How it works

`build_submission_demo(now=None, uuidf=None, write_dir=None)` is the entry point.
It is a **pure local function** with injectable clock and id generators (for
deterministic tests) and an optional output directory (defaults to
`<repo>/reports/submission/`).

For each sandbox outcome, `_sandbox_outcome()`:
1. calls `evidence.record_voice_quote(...)` (the production evidence service,
   in-memory repo) with `firm_vs_estimate="estimate"`, `reference_present=False`;
2. calls `normalization.normalize(sid, quote_id)` to persist a `NormalizedQuote`;
3. returns a **safe record** (field names, not values; synthetic provider names only).

Synthetic provider names (`Ontario Sandbox Direct`, `Ontario Sandbox Broker`) are
used for synthetic results so no real insurer name is attached to a synthetic
number. The registry export is produced from the real `MarketRegistryService`
(31 seeded Ontario auto markets), with `duplicate_suppression_count` computed from
`distinct_rate_source_id` collisions. The report's `metrics` are derived from the
records actually represented — e.g. `comparable_quote_yield: 0/4` because sandbox
estimates are never promoted to comparable.

## 3. Data flow

```
build_submission_demo()
 ├─ MarketRegistryService() ──> market_registry.json/.csv
 ├─ EvidenceService(record_voice_quote) ──> QuoteObservation (in-memory)
 │     └─ QuoteNormalizationService.normalize() ──> NormalizedQuote
 ├─ sandbox outcome records (estimate_only, not_a_live_quote)
 ├─ sonnet outcome (unresolved, quote_returned=false)
 ├─ manual handoff (handoff_executed=false, field names only)
 ├─ registry_meta (total/verified/duplicate_suppression_count)
 └─ demo_run_report.json/.md   (banner + metrics + gaps + limitations)
```

## 4. Key files / functions / tests

| Thing | Where |
| --- | --- |
| Demo generator | `backend/demos/submission_demo.py` |
| `build_submission_demo()` | module entry point (injectable `now`/`uuidf`/`write_dir`) |
| `_sandbox_outcome()` | persists one estimate through evidence + normalization |
| `_registry_rows()` / `_write_registry_export()` | 22-column registry export |
| `REGISTRY_EXPORT_FIELDS` | export column list |
| `BENCHMARK` / `SANDBOX_DIRECT` / `SANDBOX_BROKER` | coverage benchmark + two synthetic providers |
| `SONNET_UNRESOLVED` / `MANUAL_HANDOFF` | honest unresolved / handoff records |
| Demo banner | `DEMO_BANNER = "DEMO DATA - LOCAL ESTIMATES, NOT LIVE INSURANCE QUOTES"` |
| Focused tests | `backend/tests/test_submission_demo.py` (9 tests) |
| Docs | `docs/ARCHITECTURE_AND_SAFETY.md`, `docs/DEMO_SCRIPT.md`, `docs/DEMO_CHECKLIST.md`, `docs/KNOWN_LIMITATIONS.md` |

Reused production abstractions: `EvidenceService`, `InMemoryEvidenceRepository`,
`QuoteNormalizationService`, `InMemoryNormalizationRepository`,
`MarketRegistryService`, `MarketRegistryEntry` (from Issues #10, #11, #3).

## 5. Architectural decisions, alternatives & tradeoffs

- **Reuse the real pipeline instead of hand-rolling demo numbers.** The estimates
  go through the same `record_voice_quote` → `normalize` path as real quotes, so the
  demo exercises production code. Tradeoff: the demo is coupled to those services'
  method signatures (they are stable).
- **No LangGraph node for the demo.** The wrap-up is a local generator; the
  existing `comparison_run`/`browser_workflow` LangGraph orchestration is already
  covered by Issues #13/#14. Adding a new graph here would add surface without
  value.
- **In-memory repos, no Postgres.** Keeps the demo hermetic and deterministic
  (Postgres repositories are skip-gated elsewhere).
- **Synthetic provider names.** Prevents a real insurer from being associated with
  a fabricated number — an explicit honesty requirement.
- **`source_environment: local_sandbox` + `not_a_live_quote: true` on every sandbox
  record.** The belt-and-braces labelling makes accidental misrepresentation
  detectable by tests.
- **Field NAMES not values** in the handoff record, and no applicant data anywhere —
  consistent with the project's redaction discipline.

## 6. LangGraph / LangSmith

- No new LangGraph graph or LangSmith trace was added in the wrap-up. The demo is a
  local, deterministic generator; tracing is irrelevant to a pure function with no
  external side effects. The existing LangSmith wiring (env-based, metadata-only,
  run-ID correlation) continues to apply to the live LangGraph workflows and is
  documented in `issue-01-foundation.md` and `ARCHITECTURE_AND_SAFETY.md`.

## 7. Privacy / security implications

- The generator never accepts or writes applicant values; the registry export
  contains no PII; the handoff record lists field names only.
- `reports/` is gitignored, so generated artifacts do not enter source control.
- `.gitignore` was hardened (Step 10) to exclude browser profiles, session data,
  raw screenshots/traces, call recordings, temp evidence, and local DBs.
- The 9 focused tests include a privacy meta-test (`test_no_sensitive_values_in_generated_artifacts`)
  that scans the generated JSON/MD/CSV for licence/VIN/DOB/address/phone/email
  markers.

## 8. Testing strategy

`test_submission_demo.py` (9 tests) proves, without any live provider or LLM:
- sandbox outcomes are `estimate_only` + `local_sandbox` + `not_a_live_quote`;
- sandbox providers are not conflated with real providers (Sonnet/Square One);
- the comparison detects coverage differences (deductibles, accident forgiveness)
  and does **not** label the lower premium "best";
- evidence identifiers, normalized-quote ids, and timestamps exist;
- Sonnet stays `unresolved` with `quote_returned=false` and does not claim
  quoted/blocked/captcha/etc.;
- manual handoff has `handoff_executed=false` and lists field names only;
- generated artifacts contain no sensitive markers;
- registry JSON/CSV are valid and include `distinct_rate_source_id`.

The wrap-up is tested in-process (tmp dir) with injected clock/ids; there is no
network or browser use.

## 9. Failure scenarios & debugging

- If `record_voice_quote`/`normalize` change signatures, `_sandbox_outcome` fails
  fast; tests surface it immediately (in-memory, no teardown).
- If the registry is missing/corrupt, `MarketRegistryService` load errors surface
  when generating the export; the export defaults unknowns to `unknown` rather
  than guessing.
- The `not_labeled` list on the Sonnet record is documentation of what we refuse
  to claim — a common misunderstanding is to assert those words are *absent* from
  the record; the correct test asserts the outcome's status/stage do not use them
  and that the refusal list contains them.

## 10. Common misunderstandings

- **Sandbox estimates are not quotes.** They are `estimate_only`; the comparison
  engine never ranks estimates as comparable (Issue #12).
- **"Lower premium" ≠ "best".** Coverage differences (e.g. a $1,500 deductible and
  no accident forgiveness) can make the lower number worse value; the demo labels
  only the facts.
- **`unresolved` is not `blocked`.** Sonnet's outcome is unresolved because the
  control was not exposed within the bounded attempt; we do not claim CAPTCHA or
  access denial without evidence.

## 11. Interview explanation

"The final wrap-up makes the project submission-ready and honestly demonstrable. A
deterministic generator produces two sandbox estimates through the real evidence and
normalization services, labels them `estimate_only` / `local_sandbox` /
`not_a_live_quote`, reports the real Sonnet attempt as `unresolved` with no fabricated
quote or IDs, records a manual handoff that was not executed, exports the 31-market
registry with rate-source deduplication, and writes a redacted run report. Nine
focused tests prove the honesty, coverage-comparison, evidence, and privacy claims,
and the docs (`ARCHITECTURE_AND_SAFETY`, `DEMO_SCRIPT`, `DEMO_CHECKLIST`,
`KNOWN_LIMITATIONS`) make the safety boundaries and demo steps explicit. No real
provider is contacted; nothing is committed automatically."

## 12. Self-test questions

1. What makes the sandbox estimates different from live quotes? (labels + no provider contact)
2. Why is the lower estimate not labelled "best"? (coverage variances)
3. What does `quote_returned: false` mean for Sonnet? (no live quote was obtained)
4. Why does the handoff record say `handoff_executed: false`? (no broker was contacted)
5. What does `duplicate_suppression_count` measure? (entries sharing a `distinct_rate_source_id`)
6. How are privacy and honesty enforced in the demo? (no applicant data; banner + tests)
7. What safety boundaries remain enforced? (declaration never automated; payment/signature/purchase/CAPTCHA prohibited)

## 13. Rebuild exercise

Delete `reports/submission/` and re-run `python demos/submission_demo.py`; confirm
the four artifacts regenerate, the structure is identical (timestamps/ids vary),
and `pytest tests/test_submission_demo.py` passes.

## 14. Cheat sheet

- Generate: `cd backend; .\.venv\Scripts\python.exe demos\submission_demo.py`
- Artifacts: `reports/submission/{market_registry.json,market_registry.csv,demo_run_report.json,demo_run_report.md}`
- Tests: `cd backend; .\.venv\Scripts\python.exe -m pytest tests\test_submission_demo.py -q`
- Cleanup: `Remove-Item reports\submission -Recurse -Force`
- Banner: "DEMO DATA - LOCAL ESTIMATES, NOT LIVE INSURANCE QUOTES"
