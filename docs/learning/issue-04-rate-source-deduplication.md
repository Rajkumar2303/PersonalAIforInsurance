# Issue #4 — Rate-Source Deduplication

**Status:** ✅ Implemented & verified (133 tests pass; Issue #1–#3 still green; hermetic)
**Depends on:** [Issue #1](./issue-01-foundation.md), [Issue #2](./issue-02-insurance-schema.md), [Issue #3](./issue-03-market-registry.md)

---

## 1. What was built

A deterministic, evidence-based deduplication layer over the Issue #3 market registry:

- `models/dedup.py` — `DeduplicationStatus`, `Confidence`, `ReasonCode`, `DistinctRateSource`, `DeduplicationDecision`, `DuplicateCandidate`, `DuplicateGroup`, `DeduplicatedMarket`.
- `services/deduplication.py` — `RateSourceDeduplicationService` (load/validate/candidates/classify/views/metrics).
- `data/rate_sources/auto_rate_sources.json` — data-driven `DistinctRateSource` records (starts **empty**; nothing verified yet).
- Read-only API: `GET /api/v1/rate-sources`, `/rate-sources/{id}`, `/markets/{registry_id}/duplicates`, `/dedup/metrics`.

## 2. Why deduplication matters

The brief's mission is "every distinct Ontario PPA rate source". A consumer brand, a legal
underwriting company, an insurer group and a broker panel can all describe the **same**
underlying rate source; conversely one group can expose **different** programs through
direct/affinity/broker channels. Without dedup, the same rate gets counted multiple times and
"coverage" is inflated. Duplicate suppression is one of the brief's coverage metrics.

## 3. Brand vs insurer vs underwriter vs distributor vs rate source

```
Brand A ─┐
         ├─ Broker X ── Legal Underwriter Y ──▶ Rate Source RS-001
Brand B ─┘              │
                        └ Direct route ────────▶ (same) RS-001
```

The registry keeps these separate (`brand_or_program`, `distribution_type`,
`legal_underwriter`, `insurer_group`); the dedup layer maps routes to a distinct rate source.
**Same group does NOT mean same rate; different brands do NOT mean different rates.**

## 4. Core design principles

- **Evidence-based**: only explicit verified mappings or documented relationships count.
- **Deterministic**: `evaluate_pair` is a fixed priority rule chain — no randomness, no LLM.
- **Conservative**: when unsure, stay `unresolved` — never fabricate an id or collapse routes.
- **Explainable**: every pair decision carries a `reason_code`, `confidence`, and `evidence`.
- **Data-driven**: mappings live in data (`MarketRegistryEntry.distinct_rate_source_id` +
  `data/rate_sources/*.json`); editing data changes behavior, not code.
- **No fuzzy matching as authority**: name similarity only *suggests* candidates, never confirms.

## 5. Statuses — three separate axes

```
registry status (Issue #3):  discovered | verified | stale | inactive | unknown
deduplication status:        unique | duplicate_confirmed | duplicate_possible | unresolved
quote terminal status:       (future issue, e.g. quoted_comparable | blocked | ...)
```

They are deliberately kept in separate enums; conflating them would blur "is the route
verified?" vs "are these two routes the same rate source?" vs "how did this quote attempt end?".

## 6. DistinctRateSource + the authoritative mapping

`MarketRegistryEntry.distinct_rate_source_id` (Issue #3) is the **single source of truth**
(route → source). `DistinctRateSource` is the description of a known source (program,
underwriters, evidence) and carries optional `related_registry_ids` as *evidence metadata*.

**Consistency guarantee (your condition):** on load, the service validates every
`related_registry_id` — it must exist in the registry AND its authoritative
`distinct_rate_source_id` must equal the claiming source. Any contradiction raises
`DedupLoadError`. Grouping is always derived from the registry mapping, so the two can never
diverge.

## 7. Candidate detection vs confirmation

`find_duplicate_candidates(registry_id)` surfaces *possible* duplicates from safe/public
signals (same explicit id, same verified program, same legal underwriter, same insurer group)
— but that is **not** confirmation. `evaluate_pair` runs the classification; only
`duplicate_confirmed` suppresses from distinct counts.

## 8. Classification priority (deterministic)

1. Same explicit verified `distinct_rate_source_id` → **confirmed** (`same_verified_rate_source`, HIGH).
2. A verified `DistinctRateSource` lists both routes → **confirmed** (`same_verified_program`, HIGH).
3. Different explicit ids → **unique** (`explicitly_distinct_program`, HIGH).
4. Same legal underwriter (no program) → **possible** (`same_underwriter_possible_duplicate`, LOW).
5. Same insurer group only → **possible** (`same_group_only_insufficient`, LOW) — **not collapsed**.
6. Otherwise → **unresolved** (`insufficient_evidence`, LOW).

## 9. Confidence

`high` = explicit verified evidence · `medium` = strong but incomplete · `low` = candidate.
Only `duplicate_confirmed` counts a route as a duplicate; LOW never collapses routes.

## 10. Views & metrics (foundation)

- `get_duplicate_groups()` — confirmed groups (≥2 routes per source).
- `get_unresolved_mappings()` — routes with no mapping (stay visible, not dropped).
- `deduplicated_registry_view()` — one row per confirmed source (group members listed) +
  every possible/unresolved route individually; original registry records untouched.
- `metrics()` — `raw_route_count`, `confirmed_rate_sources`, `confirmed_duplicates`,
  `unresolved_mappings`, `possible_duplicates` (feeds the brief's duplicate-suppression metric).
- `trace_metadata()` — counts + reason-code distribution (safe, no PII).

Real seed reports honestly: `confirmed_rate_sources: 0`, `confirmed_duplicates: 0`,
`unresolved_mappings: 31` (nothing verified yet).

## 11. Data-driven relationships

Edit `data/market_registry/auto.json` (`distinct_rate_source_id`) and/or
`data/rate_sources/auto_rate_sources.json` to:
- map a new route to an existing source (add one record),
- remap a route (change its id),
- split one source into two (new ids),
- merge routes under one source.

No `if registry_id == "td": ...` logic exists anywhere in the dedup service.

## 12. Files / functions

| Concern | Location | Symbols |
|---|---|---|
| Dedup models | `models/dedup.py` | `DeduplicationStatus`, `Confidence`, `ReasonCode`, `DistinctRateSource`, `DeduplicationDecision`, `DuplicateCandidate`, `DuplicateGroup`, `DeduplicatedMarket` |
| Service | `services/deduplication.py` | `RateSourceDeduplicationService`, `DedupLoadError`, `DedupLookupError`, `default_rate_sources_dir`, `get_deduplication_service` |
| Data | `data/rate_sources/auto_rate_sources.json` | empty seed (verification-pending) |
| API | `api/dedup.py` + `main.py` | `list_rate_sources`, `get_rate_source`, `duplicate_candidates`, `dedup_metrics` |
| Config | `core/config.py` | `rate_sources_dir` |

## 13. Tracing / logging

- Deterministic — no LLM, no artificial spans.
- `evaluate_pair` logs a DEBUG structured record with **safe** fields only
  (`registry_id`, `candidate_registry_id`, `distinct_rate_source_id`, `dedup_status`,
  `reason_code`, `confidence`).
- `trace_metadata()` exposes counts for future LangGraph correlation
  (`request_id/trace_id → registry → dedup → route planning`).
- No applicant PII anywhere in dedup models, logs, or traces.

## 14. Testing strategy

`tests/test_deduplication.py` (core + scenarios) and `tests/test_dedup_api.py`. All hermetic
(tmp-dir synthetic data; no network/LangSmith/LLM). Covers the 20 core cases and the 5
dynamic scenarios: new route auto-groups · remap via data · same-group-distinct-programs stay
separate · unknown relationship not collapsed · new-field-agnostic dedup.

## 15. Failure scenarios & debugging

| Scenario | Behavior |
|---|---|
| `DedupLoadError: duplicate distinct_rate_source_id` | two rate-source records share an id → fix `auto_rate_sources.json` |
| `DedupLoadError: inconsistent mapping` | a `related_registry_id` contradicts the registry id → align data |
| `DedupLoadError: related registry_id ... not found` | a listed id isn't in the registry |
| `DedupLookupError` | unknown `registry_id` passed to `evaluate_pair`/`find_duplicate_candidates` |
| Two routes share a group but aren't collapsing | **correct** — same-group-only is `duplicate_possible`, not confirmed |

## 16. Common misunderstandings

- Same insurer group ≠ same rate source; shared logo ≠ duplicate.
- Same legal underwriter is only a **candidate** until a program is verified.
- `duplicate_possible`/`unresolved` routes stay visible — never silently dropped.
- `DistinctRateSource.related_registry_ids` is evidence, not the source of truth; the registry
  mapping is authoritative and consistency-checked.
- Metrics must not inflate completion by ignoring unresolved entries.

## 17. Interview explanation (30-second version)

> "Issue #4 is the rate-source deduplication layer. The market registry keeps brands,
> distributors, underwriters, groups and programs separate; dedup answers 'which routes are
> the same underlying rate source?' Deterministically. Each route carries an authoritative
> `distinct_rate_source_id`, and a `DistinctRateSource` record describes the source. The
> service surfaces candidate duplicates from public signals but only confirms on verified
> evidence — same group alone is never enough. Every pair decision is explainable with a
> reason code and confidence, confirmed duplicates count once, and unresolved routes stay
> visible. It's fully data-driven: you change mappings in JSON, not code. The real seed
> honestly reports zero confirmed sources until routes are verified."

## 18. Self-test questions

1. Why is `related_registry_ids` validated against the registry mapping on load?
2. Same insurer group + null ids → what status? (duplicate_possible / same_group_only_insufficient)
3. Two routes with different verified ids → what status? (unique)
4. Which status suppresses a route from distinct counts? (duplicate_confirmed)
5. What makes a decision explainable? (reason_code + confidence + evidence)
6. How do you remap a route to a new source? (edit data, no code)
7. Where does candidate detection happen vs confirmation?
8. What are the three separate status axes?
9. How is the real seed reported today? (0 / 0 / 31)
10. Why is fuzzy name similarity not authoritative?

## 19. Rebuild exercise

1. `DeduplicationStatus`/`Confidence`/`ReasonCode` enums + `DistinctRateSource`/`DeduplicationDecision`.
2. `evaluate_pair` priority chain (id → program → underwriter → group → unresolved).
3. `find_duplicate_candidates` (never confirming).
4. Loader with uniqueness + `related_registry_ids` consistency checks.
5. `deduplicated_registry_view()` + `metrics()`.
6. Empty `auto_rate_sources.json` seed; read-only API endpoints.
7. Tests: 20 core + 5 dynamic scenarios.

## 20. Concise cheat sheet

```python
from app.services.deduplication import get_deduplication_service
svc = get_deduplication_service()
svc.metrics()                      # {'raw_route_count':31,'confirmed_rate_sources':0,...}
svc.evaluate_pair("brand-b","brand-c")   # decision + reason_code + confidence + evidence
svc.find_duplicate_candidates("aviva-direct")
svc.get_unresolved_mappings()
svc.deduplicated_registry_view()
```

Key names: `RateSourceDeduplicationService`, `evaluate_pair`, `find_duplicate_candidates`,
`deduplicated_registry_view`, `metrics`, `DistinctRateSource`, `DeduplicationDecision`,
`DeduplicationStatus`, `ReasonCode`, `Confidence`, `DedupLoadError`, `DedupLookupError`.

**Related:** [learning index](./README.md) · [Issue #1](./issue-01-foundation.md) · [Issue #2](./issue-02-insurance-schema.md) · [Issue #3](./issue-03-market-registry.md)
