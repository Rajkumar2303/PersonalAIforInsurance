# Issue #3 — Product-Aware Market Registry + Progressive Profile Hardening

**Status:** ✅ Implemented & verified (102 tests pass; Issue #1/#2 still green; hermetic)
**Depends on:** [Issue #1](./issue-01-foundation.md), [Issue #2](./issue-02-insurance-schema.md)

---

## 1. What was built

Two parts:

**Part A — Progressive-profile hardening** (`backend/app/models/insurance/`):
- Draft profiles: `AutoInsuranceProfile.drivers`/`vehicles` no longer require `min_length=1`;
  `ApplicantIdentity.date_of_birth`, `AddressInformation.street`/`city` are now optional.
- `paths.py` — canonical field-path convention + generic resolver (`parse_field_path`,
  `resolve`, `is_missing`).
- `InsuranceProfile.updated(path, value)` / `set_field(...)` — validated, immutable
  single-field update (returns a NEW revalidated profile).
- `is_draft` / `is_live_quote_ready` derived from `get_missing_fields()`.

**Part B — Product-aware Ontario market registry**:
- `MarketRegistryEntry` + registry enums (`models/registry.py`).
- Data-driven seed: `backend/data/market_registry/auto.json` (31 AUTO discovery records
  grounded in the brief's Appendix A/B).
- `MarketRegistryService` (`services/market_registry.py`): load/validate/filter/freshness.
- Read-only API: `GET /api/v1/markets`, `GET /api/v1/markets/{registry_id}`.

---

## 2. Why progressive profiles matter (Part A)

Issue #2's schema required a fairly complete AUTO profile at construction. The hackathon
workflow is the opposite: initial intake collects only basics, a browser quote journey
discovers a new required field, intake asks for exactly that field, the profile is updated,
and the journey resumes. To support that, the schema must allow a **valid draft** that is
distinct from **live-quote-ready**, and updates must revalidate (Pydantic v2's
`model_copy(update=...)` does **not**).

## 3. Draft vs live-quote-ready

```
InsuranceProfile
├── constructible with just consent + basic applicant   -> VALID DRAFT (is_draft=True)
└── after all required_for_live_quote() paths are set   -> READY (is_live_quote_ready=True)
```

- Construction no longer enforces every future route requirement.
- `required_for_live_quote()` / `get_missing_fields()` enforce live-quote completeness
  separately. A draft reports missing paths like
  `product_data.drivers[0].licence.licence_number`.

## 4. Pydantic v2 `model_copy(update=...)` gotcha

Verified at runtime: `model_copy(update={'product_data': None})` on an AUTO profile is
accepted silently (no revalidation) — a profile can become invalid with no error.
`InsuranceProfile.updated()` therefore dumps the model to a dict, sets the value at the
canonical path (rejecting unknown fields / bad indexes), and **revalidates the whole
profile through `model_validate`** — invalid values and product invariants fail loudly as
`ProfileUpdateError` (path only, never the rejected value → no PII in errors/logs).

## 5. Canonical field paths

Locked syntax in `paths.py` (stable across Issue #5 intake / #7 browser / #9 voice):

```
applicant.identity.date_of_birth
applicant.address.street
product_data.drivers[0].licence.licence_number
product_data.vehicles[0].use.annual_kilometres
product_data.coverage.third_party_liability.selected_limit
```

Rules: attribute segments joined by `.`; list indexes `[n]` (0-based); top-level object is
the `InsuranceProfile`; `product_data` is the product container (no `auto` alias).
`resolve(obj, path)` and `is_missing(obj, path)` are a **generic attribute/index walk** —
no per-field branching, so adding a field never requires changing `paths.py`.

## 6. What a market registry is (Part B)

The brief requires keeping **legal underwriter ≠ insurer group ≠ consumer brand ≠
distributor** separate, plus aggregator / MGA/program / mutual / residual, and a nullable
`distinct_rate_source_id` for Issue #4 deduplication. The registry is a machine-readable
**discovery seed** — it does not claim any route currently writes business, accepts this
applicant, or exposes a direct quote.

## 7. Data model and flow

```
backend/data/market_registry/auto.json   (data-driven; edit data, not code)
        │  MarketRegistryService._load_all() -> validates each record (Pydantic),
        │  enforces registry_id uniqueness
        ▼
 MarketRegistryEntry  (models/registry.py)
        │  list_markets / get_by_registry_id / filter_by_* / applicable / freshness
        ▼
 GET /api/v1/markets[?product_type=&distribution_type=&product_scope=]
 GET /api/v1/markets/{registry_id}
```

`MarketRegistryEntry` fields: `registry_id`, `product_type` (reuses `InsuranceType`),
`legal_underwriter`, `insurer_group`, `brand_or_program`, `distribution_type`,
`product_scope`, `distinct_rate_source_id` (nullable — never guessed),
`quote_url`, `public_phone_route`, `callback_route`, `known_panel_source`,
`licensed_intermediary`, `requirements` (enum list, deduped+sorted), `automation_notes`,
`status`, `source_url`, `source_citation`, `last_verified_at`, `evidence_artifact`, `active`.

**`seeded_from_brief` vs `verified_during_hackathon`** is preserved explicitly:
```
status: discovered            # seeded from the brief (source_citation: hackathon_brief)
last_verified_at: null        # NOT yet verified

status: verified              # later, during the hackathon
last_verified_at: 2026-08-09T…  # required (model_validator enforces)
source_url: …                   # authoritative evidence
```

## 8. Enums

- `DistributionType`: direct | agent | broker | aggregator | affinity | mga_program |
  mutual | residual.
- `ProductScope`: standard_PPA | nonstandard_PPA | high_net_worth | collector |
  commercial_specialty | unknown.
- `RegistryStatus`: discovered | verified | stale | inactive | unknown — **deliberately
  separate** from quote-attempt terminal statuses.
- `MarketRequirement`: licence | vin | membership | callback | human | other — extensible;
  a new requirement = enum member + data, no service change.

## 9. Why data-driven (not hardcoded)

No `if insurer == "TD": ...` in the registry layer. Adding a market = adding a JSON record;
changing a URL/phone/requirement = editing the record. Tests prove a synthetic record loads
with zero service-code change (Scenario A) and that URL/phone/requirement edits flow
through data (Scenario C).

## 10. Freshness / verification

`last_verified_at`, `source_url`, `source_citation`, `evidence_artifact`, `status`.
Service helpers: `verified_records()`, `records_missing_verification()`,
`freshness_percentage()`, `trace_metadata()` (counts only). All 31 seed records are
`discovered` → freshness 0.0% until verified during the hackathon.

## 11. Privacy

The registry holds **public market data only**. It is a separate bounded concept from
`InsuranceProfile` (which has the PII). Tests assert the registry serialization contains no
emails, licence-format numbers, VINs, phones, or addresses.

## 12. Tracing / LangSmith

Registry operations are deterministic — no artificial spans. `trace_metadata()` returns
safe counts (`registry_total`, `product_type_counts`, verified/unverified, freshness) for
later workflows. A structured `logger.info` on load carries `result_count` (Issue #1
logging conventions). No LLM, no network, tests force `LANGSMITH_TRACING=false`.

## 13. Files / functions

| Concern | Location | Symbols |
|---|---|---|
| Draft optionality | `models/insurance/common.py`, `auto/profile.py` | `date_of_birth`, `street`/`city` Optional; `drivers`/`vehicles` no min_length |
| Paths | `models/insurance/paths.py` | `parse_field_path`, `format_field_path`, `resolve`, `is_missing`, `FieldPathError` |
| Validated update | `models/insurance/profile.py` | `updated()`, `set_field()`, `ProfileUpdateError`, `is_draft`, `is_live_quote_ready`, `_set_in_dict` |
| Registry model | `models/registry.py` | `MarketRegistryEntry`, `DistributionType`, `ProductScope`, `RegistryStatus`, `MarketRequirement` |
| Registry service | `services/market_registry.py` | `MarketRegistryService`, `RegistryLoadError`, `get_market_registry_service`, `default_registry_dir` |
| Seed data | `data/market_registry/auto.json` | 31 AUTO records |
| API | `api/markets.py`, `main.py` | `list_markets`, `get_market` |
| Config | `core/config.py` | `BACKEND_ROOT`, `market_registry_dir` |

## 14. Alternatives / tradeoffs

- **Draft enforcement removed from construction** vs keeping `min_length=1`: live-quote
  completeness moved to `required_for_live_quote()` so construction allows partial data.
- **`updated()` = dump→set→`model_validate`** vs `model_copy(update=...)`: the latter
  doesn't revalidate (Pydantic v2), so it's explicitly NOT the trusted path.
- **Registry as JSON files** vs a database / hardcoded dicts: JSON is machine-readable,
  editable, submission-friendly, and needs no DB (per scope). Validation still happens in
  Pydantic on load.
- **`requirements` as enum list (deduped+sorted)** vs many booleans: extensible and
  deterministic; a new requirement never touches service logic.
- **RegistryStatus separate** from quote terminal statuses: avoids conflating "is the
  route verified?" with "how did this quote attempt end?".

## 15. Debugging

- Registry won't load → `RegistryLoadError` (invalid record / duplicate `registry_id` /
  bad JSON). Check `data/market_registry/auto.json`.
- `updated()` fails → `FieldPathError` (unknown field / bad index) or `ProfileUpdateError`
  (invalid value). Messages contain the path only — never the value.
- Draft not ready → `get_missing_fields()` lists the exact canonical paths to fill.
- Filter returns nothing → confirm you passed the enum, not a raw string
  (`filter_by_distribution_type(DistributionType.DIRECT)`, not `"direct"`).

## 16. Common misunderstandings

- `model_copy(update=...)` does NOT revalidate — always use `profile.updated()`.
- `drivers`/`vehicles` may be empty now; live-quote completeness comes from
  `get_missing_fields()`, not construction.
- `distinct_rate_source_id: null` is intentional (unresolved), not a bug.
- `status: discovered` ≠ verified; verification requires `last_verified_at`.
- Registry statuses are NOT quote-attempt terminal statuses.
- `resolve`/`is_missing` are field-agnostic — no per-field code to update.

## 17. Interview explanation (30-second version)

> "Issue #3 hardened the intake schema for progressive journeys and added a data-driven
> Ontario market registry. On the schema side, we now allow a valid draft profile — no
> drivers/vehicles yet, DOB and address optional — and added a canonical field-path
> convention plus a validated `updated(path, value)` that revalidates through Pydantic,
> because `model_copy` doesn't. That's the safe primitive the browser and voice agents will
> use when a route discovers a new required field. On the market side, we built a
> machine-readable registry that keeps legal underwriter, insurer group, brand and
> distributor separate, seeds 31 AUTO routes from the hackathon brief with `status:
> discovered` and nullable rate-source IDs (never guessed), and serves read-only API
> endpoints. Everything is data-driven — adding a market or changing a URL is an edit to a
> JSON record, not code."

## 18. Self-test questions

1. How is a draft profile distinguished from live-quote-ready? (`is_draft` /
   `is_live_quote_ready` via `get_missing_fields()`)
2. Why is `model_copy(update=...)` not trusted for applicant updates?
3. What is the canonical path for a vehicle's annual kilometres?
   (`product_data.vehicles[0].use.annual_kilometres`)
4. What makes `paths.resolve` generic (no per-field code)?
5. How does `updated()` prevent sensitive values leaking in errors?
6. Why is `distinct_rate_source_id` null in the seed?
7. How does the model distinguish `seeded_from_brief` vs `verified_during_hackathon`?
8. Which registry enums exist and how do you add a new requirement?
9. What happens if two records share a `registry_id`?
10. How would Issue #4 use `distinct_rate_source_id`?

## 19. Rebuild exercise

1. Make DOB + address street/city optional; drop `min_length` on drivers/vehicles.
2. Write `paths.py` (parse/resolve/is_missing) with the `[n]` convention.
3. Add `InsuranceProfile.updated()` (dump → set → `model_validate`) + `ProfileUpdateError`.
4. Add `MarketRegistryEntry` + enums + the verified-requires-timestamp validator.
5. Write `auto.json` records; build `MarketRegistryService` (load/validate/uniqueness/filters).
6. Add `GET /api/v1/markets[...]` endpoints.
7. Tests: draft/resolve/update; registry load/filters/freshness/Scenario A/C. Run `pytest`.

## 20. Concise cheat sheet

```python
draft = make_draft_profile()                       # is_draft=True, 0 drivers, 0 vehicles
draft.get_missing_fields()                         # canonical paths to fill
p.updated("product_data.vehicles[0].use.annual_kilometres", 15000)  # validated, immutable
resolve(p, "product_data.coverage.third_party_liability.selected_limit")

from app.services.market_registry import MarketRegistryService
s = MarketRegistryService()
s.list_markets()            # 31 AUTO discovery records
s.filter_by_distribution_type(DistributionType.DIRECT)
s.get_by_registry_id("sonnet")
s.freshness_percentage()    # 0.0 until verified
```

Key names: `updated`, `set_field`, `ProfileUpdateError`, `FieldPathError`, `parse_field_path`,
`resolve`, `is_missing`, `MarketRegistryEntry`, `MarketRegistryService`, `RegistryLoadError`,
`DistributionType`, `ProductScope`, `RegistryStatus`, `MarketRequirement`.

**Related:** [learning index](./README.md) · [Issue #1](./issue-01-foundation.md) · [Issue #2](./issue-02-insurance-schema.md)
