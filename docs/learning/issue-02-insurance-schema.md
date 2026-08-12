# Issue #2 — Canonical Insurance Intake Schema

**Status:** ✅ Implemented & verified (51 tests pass; Issue #1 tests still green)
**Depends on:** [Issue #1 foundation](./issue-01-foundation.md) (Pydantic v2, redaction, testing conventions)

---

## 1. What was built

The canonical, **product-aware** insurance intake schema for the Ontario
All-Quote Agent, under `backend/app/models/insurance/`:

- `InsuranceType` enum — **AUTO** fully implemented; **HOME/TENANT/LIFE/TRAVEL/OTHER**
  recognized but **unsupported** (no fake placeholder schemas).
- **Shared (product-agnostic)** models: `ConsentState`, `ApplicantIdentity`,
  `ContactInformation`, `AddressInformation`, `ApplicantInformation`.
- **AUTO-specific** models in `auto/`: `DriverInformation` (licence identity,
  timeline, training, assignment, discounts, other drivers), `VehicleInformation`
  (identity/VIN, ownership, use, risk, special use), `HouseholdInformation` (members,
  dependants, fleet, driver↔vehicle assignments), `InsuranceAndDrivingHistory`
  (current insurance, licence events, cancellations, misrepresentation, fraud,
  claims, convictions), and `CoverageConfiguration` (liability, accident benefits,
  optional benefits, uninsured auto, DCPD, own damage, OPCF endorsements, discounts,
  payment).
- `InsuranceProfile` — the top-level wrapper: `schema_version`, `insurance_type`,
  shared `consent`/`applicant`, and `product_data: AutoInsuranceProfile | None`.
- `SensitiveBaseModel` — safe/redacted serialization (`redacted_dict()` /
  `safe_dict()`) and redacted `repr`/`str`.
- Lightweight schema helpers: `required_for_live_quote()`, `get_missing_fields()`,
  `trace_metadata()`.

**Not built** (deferred, per scope): intake conversation logic (Issue #5), market
routing (Issue #3), browser automation, premium/quote logic, and DB persistence.

---

## 2. Why shared and product-specific schemas are separated

The hackathon brief targets Ontario **auto** insurance, but the architecture must
support future products (home, tenant, life, travel). If every product model
duplicated `legal_name`, `date_of_birth`, `email`, `postal_code`, etc., the schema
would drift and the intake engine would have to handle N copies of the same field.

So `InsuranceProfile` holds the **shared** applicant/contact/consent once, and each
product gets its own `*Profile` with only product-specific data. Adding `HOME`
later = add `home/profile.py` + wire `InsuranceType.HOME → HomeInsuranceProfile`
in `InsuranceProfile`; shared fields are already in place.

---

## 3. Pydantic mental model

Pydantic v2 `BaseModel`s are **validated dataclasses**: declare fields with type
annotations, and Pydantic coerces + validates on construction.

- `StrEnum` fields serialize to their `.value` (lowercase strings) via
  `model_dump(mode="json")`; member names stay uppercase in code.
- `Optional[T] = None` fields can be unset — nothing requires them unless it's a
  genuinely universal field (e.g. `consent_timestamp`).
- `Field(ge=…, le=…)` gives cheap numeric constraints.
- `field_validator` handles cross-format checks (postal code regex, VIN length,
  model-year range).
- `model_validator(mode="after")` enforces cross-field invariants — here, the
  `insurance_type` ↔ `product_data` consistency rule.
- `from __future__ import annotations` + `Optional["AddressInformation"]` lets a
  model reference itself (`prior_address`).

---

## 4. Nested model composition

Instead of one giant flat model, the schema composes:

```
InsuranceProfile
├── schema_version: str = "1.0"
├── insurance_type: InsuranceType
├── consent: ConsentState
├── applicant: ApplicantInformation
│     ├── identity: ApplicantIdentity
│     ├── contact: ContactInformation
│     └── address: AddressInformation
└── product_data: AutoInsuranceProfile | None
      ├── drivers: list[DriverInformation]          (min_length=1)
      ├── vehicles: list[VehicleInformation]        (min_length=1)
      ├── household: HouseholdInformation
      ├── history: InsuranceAndDrivingHistory
      └── coverage: CoverageConfiguration
```

References between drivers and vehicles use stable string **labels**
(`vehicle_reference="vehicle_1"`), not positional indices, so assignments survive
list reordering.

---

## 5. Enums

Defined in `enums.py`, only where they add consistency and are actually used:
`InsuranceType`, `QuoteMode`, `ChannelType`, `Province`, `PreferredLanguage`,
`Gender`, `MaritalStatus`, `DriverRole`, `LicenceStatus`, `LicenceClass`,
`FuelType`, `OwnershipType`, `PurchaseState`, `VehicleUseType`,
`CoverageSelectionState`, `PaymentFrequency`, `OwnDamageCoverageType`,
`OptionalBenefitType`, `EndorsementType`, `DiscountType`, `RelationshipType`.
Extending = add a member; models don't change.

---

## 6. Required vs optional fields & data minimization

The brief requires asking only for fields a selected route needs. So:

- **Universally required** (validated at construction): `consent_timestamp`,
  `legal_name`, `date_of_birth`, address basics, licence basics, at least one
  driver/vehicle.
- **Optional** (default `None` / empty): phones, alias, purchase price, annual
  kilometres, claims, coverage selections — all can stay unset.
- `get_missing_fields()` / `required_for_live_quote()` are the lightweight,
  schema-layer seed for the future intake engine to compute "what's still
  missing for a live quote" per route (Issue #5 builds the real engine).

---

## 7. Validation rules implemented

| Field | Rule | Where |
|---|---|---|
| `postal_code` | Canadian `A1A 1A1`, normalized to `M0A 0A0` | `common.AddressInformation` validator |
| `vin` | exactly 17 chars (upper-cased) | `auto/vehicle.py` |
| `model_year` | 1900 .. current year + 1 | `auto/vehicle.py` |
| `annual_kilometres` | 0 .. 1_000_000 | `Field(ge, le)` |
| percentages | 0 .. 100 (`fault_percentage`, `business_use_percentage`, …) | `Field(ge, le)` |
| amounts/deductibles | `>= 0` | `Field(ge=0)` |
| `insurance_type ↔ product_data` | AUTO requires `AutoInsuranceProfile`; non-AUTO must have `None` | `profile.py` `model_validator` |

---

## 8. Sensitive-field handling & redacted serialization

Reuses Issue #1's `app/core/redaction.py` (`REDACTED`, `is_sensitive`,
`redact_text`) — **no second redaction framework**.

`SensitiveBaseModel` (`base.py`):
- `redacted_dict(mode="json")` / `safe_dict()` — `model_dump` then `_redact_tree`:
  - **Recurses into containers** (dicts/lists) so structural keys like `drivers`
    are not block-redacted (avoids the `\bdriver` over-match from the global
    patterns).
  - Redacts **sensitive leaves** by key: schema-level `SENSITIVE_FIELD_NAMES`
    (licence, DOB, email/phones, street/unit/city/postal, VIN, policy number,
    claim/event details) **plus** the existing `is_sensitive` patterns.
  - Runs `redact_text` over free-text strings (catches emails/phones/licence/VIN
    patterns even in unexpected keys).
  - **Booleans are always preserved** — a `True`/`False` cannot carry PII, so
    consent flags like `recording_permission` stay visible.
- `__repr__`/`__str__` use the redacted form, so logging a profile never leaks.
- `model_dump()`/`model_dump_json()` remain available (unredacted) for explicit
  callers — documented contract; only `safe_dict`/`redacted_dict` may be logged.

Note: `gender`/`marital_status` are intentionally left visible (not in the
brief's sensitive list) — a documented, privacy-reasonable choice.

---

## 9. Schema versioning

`SCHEMA_VERSION = "1.0"` in `base.py`; `InsuranceProfile.schema_version` defaults
to it. Later issues that change stored/intake fields bump the version, and market
integrations can branch on it.

---

## 10. Why only AUTO is implemented

The hackathon brief is Ontario private-passenger auto. Implementing fake
home/tenant/life/travel field sets now would be invented scope. Instead:
`InsuranceType` recognizes all six values, but non-AUTO profiles validate with
`product_data=None` and `is_supported == False`. A `model_validator` hard-rejects
smuggling an `AutoInsuranceProfile` into a HOME profile, making the boundary
type-safe.

---

## 11. How future product schemas can be added

1. Create `auto/`-style package: `home/profile.py` etc.
2. Import `HomeInsuranceProfile` in `profile.py`.
3. Add `insurance_type is InsuranceType.HOME and product_data is None → require
   HomeInsuranceProfile` (and the reverse check) to the existing validator.
4. Extend `required_for_live_quote()` for the new product's required paths.
Shared models (`consent`, `applicant`) need no changes.

---

## 12. Alternatives considered (and why not used)

- **One giant flat model / dict of dicts:** impossible to validate per group, no
  reuse, no per-field sensitivity. **Rejected** — composition wins.
- **Arbitrary JSON / `dict[str, Any]` profiles:** loses typing, validation, and the
  safe-serialization hook; nothing would catch a bad `vin`. **Rejected.**
- **Per-product duplicated shared fields:** drift and intake complexity. **Rejected.**
- **Generic abstract `ProductProfile` ABC with a registry:** adds indirection for no
  current payoff; plain typed Pydantic models are enough for a hackathon. **Deferred.**
- **`product_data: BaseModel | None`:** weaker typing than
  `AutoInsuranceProfile | None`; the concrete type gives the validator its check.
- **Block-redaction of sensitive container keys (`address`, `licence`, `drivers`):**
  initially tried, but the `\bdriver` pattern nuked whole lists; switched to
  **leaf-level redaction** so safe output keeps useful non-sensitive context
  (province, class, coverage) while hiding the actual PII values.

---

## 13. LangGraph / LangSmith (as applicable)

Issue #2 is deterministic schema work — **no LangGraph workflow added and no
artificial LangSmith spans**. To be ready for later issues:

- `InsuranceProfile.trace_metadata()` returns only safe, non-sensitive metadata
  (`insurance_type`, `schema_version`, `is_supported`, `missing_field_count`) —
  never field values.
- `conftest.py` still forces `LANGSMITH_TRACING=false`; tests make **no** network,
  LLM, or LangSmith calls (0.10s run).
- If a later workflow validates an `InsuranceProfile`, it should trace
  `trace_metadata()` + `validation_success`, never the raw model.

---

## 14. Testing strategy

New tests (32) + existing Issue #1 tests (19) = **51 passing, hermetic**:

- `tests/factories.py` — synthetic builders (`make_insurance_profile`,
  `make_full_auto_profile`, …) using obviously fake data: `Test Applicant`,
  licence `T0000-00000-00000`, VIN `1HGCM82633A000000`, postal `M0A 0A0`,
  phone `416-555-0199`, `test.applicant@example.com`.
- `tests/test_insurance_schema.py` — valid minimal/full profiles, invalid
  `insurance_type`, unsupported products, AUTO-requires-product and
  unsupported-can't-carry-data, postal/year/km/percentage validation, nested
  driver validation, claim/coverage validation, enum serialization, schema
  version, optional-unset, missing-field + trace-metadata helpers.
- `tests/test_insurance_redaction.py` — redacted dict masks licence/DOB/VIN/
  address/phone/email/claim details; no sensitive value appears in `safe_dict`
  output or `repr`/`str`; booleans preserved; structure preserved.

All acceptance-criteria tests map 1:1 to the 17 scenarios listed in the issue.

---

## 15. Failure scenarios & debugging

| Scenario | Symptom → Cause → Fix |
|---|---|
| `AUTO insurance_type requires product_data` | Forgot `product_data` on AUTO profile → pass an `AutoInsuranceProfile` |
| `cannot carry product_data` | Smuggled auto data into HOME → pass `product_data=None` |
| VIN rejected | Wrong length/illegal chars → 17 chars, no I/O/Q |
| Postal rejected | Bad format → use `A1A 1A1` (normalized automatically) |
| `TypeError: string indices must be integers` on redacted output | Block-redaction of a container (old behavior) → always recurse into containers; test per-field |
| Tests suddenly "slow" / hit network | Tracing enabled in tests → `conftest.py` sets `LANGSMITH_TRACING=false` |

Debug helpers: `profile.model_dump(mode="json")` to see raw; `profile.safe_dict()`
to see the redacted view; `profile.get_missing_fields()` for intake gaps.

---

## 16. Common misunderstandings

- **Enum serialization:** `InsuranceType.AUTO` is `"auto"` in JSON — values are
  lowercase, names uppercase.
- **`Optional` ≠ "validated as None":** `Optional[int]` with `ge=0` still rejects
  negatives when set.
- **`safe_dict()` is not `model_dump()`:** raw dumps are explicit and unredacted;
  only `safe_dict`/`redacted_dict` are log-safe.
- **Booleans aren't redacted:** a `False` consent flag isn't PII.
- **`is_supported` vs `product_data`:** a HOME profile is *valid* but
  `is_supported is False` and `product_data is None`.
- **Block vs leaf redaction:** container keys are recursed, only sensitive leaves
  are masked — so `drivers` stays a list of dicts with `licence_number: "[REDACTED]"`.

---

## 17. Interview explanation (30-second version)

> "Issue #2 is the canonical intake schema. A top-level `InsuranceProfile` holds
> the shared applicant/contact/consent data plus a product-specific profile. Only
> auto is implemented — `InsuranceType` recognizes home/tenant/life/travel/other
> but a validator keeps their `product_data` empty, so the boundary is type-safe.
> The auto profile composes nested Pydantic models for drivers, vehicles,
> household, history, and coverage — no giant flat dict. Every model extends a
> sensitive-aware base that reuses our redaction utility: `safe_dict()` masks
> licence numbers, DOB, VIN, addresses, phones, emails, and claim details, and
> even `repr()` is redacted, so accidental logging never leaks PII. We added
> schema versioning and lightweight missing-field helpers, but the actual intake
> conversation engine is deliberately deferred to a later issue."

---

## 18. Self-test questions

1. Where does shared applicant data live, and why not inside `AutoInsuranceProfile`?
2. What happens if you build a HOME profile with `product_data` set?
3. Why lowercase enum values but uppercase member names?
4. How does `safe_dict()` differ from `model_dump()`?
5. Why are containers recursed into rather than block-redacted?
6. Which validator normalizes `m0a0a0` → `M0A 0A0`?
7. What does `trace_metadata()` return, and why is it safe?
8. How would you add a `HomeInsuranceProfile` later?
9. Which fields are universal-required vs optional in this schema?
10. How do drivers reference vehicles, and why not by list index?

---

## 19. Rebuild exercise

Without looking at the code:
1. `InsuranceType` enum (six values).
2. `SensitiveBaseModel` with `redacted_dict()` recursing into containers and
   redacting sensitive leaves (reuse `app.core.redaction`).
3. Shared `ConsentState`, `ApplicantIdentity`, `ContactInformation`,
   `AddressInformation` (postal validation).
4. AUTO models: `DriverInformation`, `VehicleInformation`, `HouseholdInformation`,
   `InsuranceAndDrivingHistory`, `CoverageConfiguration`, `AutoInsuranceProfile`.
5. `InsuranceProfile` with the product-consistency `model_validator`,
   `is_supported`, `get_missing_fields()`, `trace_metadata()`.
6. Factories + tests for the 17 acceptance scenarios.

Check with `pytest` (must be hermetic).

---

## 20. Concise cheat sheet

```python
from app.models.insurance import InsuranceProfile, InsuranceType

profile = make_insurance_profile()   # from tests/factories.py (synthetic data)
profile.is_supported                 # True
profile.safe_dict()                  # redacted, log-safe dict
profile.trace_metadata()             # {'insurance_type','schema_version','is_supported','missing_field_count'}
profile.get_missing_fields()         # set() for a complete AUTO profile
```

Key files: `models/insurance/{enums,base,common,profile}.py`, `models/insurance/auto/{driver,vehicle,household,history,coverage,profile}.py`, `models/__init__.py`, `tests/factories.py`, `tests/test_insurance_{schema,redaction}.py`.

**Related:** [learning index](./README.md) · [Issue #1 foundation](./issue-01-foundation.md)
