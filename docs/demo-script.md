# Demo Script — Ontario All-Quote Agent (3–5 min)

Target: **3–5 minutes**. Mock mode only. No external credentials required.

## 0:00–0:30 — Problem

- Shopping Ontario auto insurance means re-entering the same info on many
  websites, comparing apples-to-oranges, and losing track of what you tried.
- This is a **personal-use, evidence-first** assistant: one intake, many
  markets, explainable comparison.

## 0:30–1:00 — One-time intake

- Open `http://localhost:5173` (backend + frontend running via
  `scripts/start-demo.ps1`).
- **Auto Insurance** → fill the demo profile (use the synthetic persona values).
- **Review & Consent** shows exactly which canonical fields each provider
  receives, then grants collection + route-disclosure consent explicitly.

## 1:00–2:00 — Compare Quotes execution

- Click **Compare Quotes**. Watch the polled progress: routes run in parallel
  (bounded concurrency = 4), and one route failing never stops the others.

## 2:00–3:00 — Results (the money shot)

Show the result table and summary grid:

- **Comparable** — Provider A **$1,234.56**, Provider B (coverage-normalized,
  sorted ascending by annual premium).
- **Estimate** — Provider D **$900** (kept separate; never treated as a firm
  quote).
- **Duplicate rate source** — the aggregator maps to Provider A's underlying
  rate, so it is **counted once** (distinct rate sources = 3).
- **CAPTCHA blocked** — Provider C stopped safely at the security check
  (never bypassed), shown honestly as `CAPTCHA blocked`.
- Summary grid: Routes attempted **5** · Quote responses **4** · Distinct rate
  sources **3** · Comparable **2** · Estimates **1** · Duplicates **1**.

## 3:00–4:00 — Architecture / safety

- **Evidence first** — every route keeps an evidence trail (what was attempted,
  why it succeeded/failed); the UI notes this.
- **Coverage normalization** — quoted TPL/Collision/Comprehensive are mapped to
  a canonical coverage ledger before comparison; unknown coverage is shown as
  "Unknown", never assumed.
- **Deduplication** — confirmed duplicates are counted once (tooltip explains
  this for judges).
- **Unattended, safe execution** — CAPTCHA stops, no payment/purchase/binding,
  no fabricated identity, no bypasses, no blind retries, per-route timeouts.

## 4:00–5:00 — Future / live providers + voice

- Same pipeline works for **verified live providers** (LIVE stays gated:
  personal-use + accurate-information attestation + route consent + verified
  registry).
- **Voice/manual handoff** and real telephony are designed-in but intentionally
  not integrated for the demo.

---

## Private prep — judging-criteria mapping

| Criterion | Where it shows |
| --- | --- |
| **Creativity** | Multi-channel browser/aggregator/voice architecture; one intake → many markets. |
| **Domain understanding** | Coverage-aware comparison, rate-source deduplication, honest terminal states (estimate vs firm, CAPTCHA, duplicate, coverage mismatch). |
| **Technical execution** | Multi-route orchestration (bounded concurrency, route isolation), evidence trail, normalization, deterministic comparison. |
| **Communication** | One intake → shop multiple markets → explainable comparison; UI explains distinct rate sources + evidence. |
