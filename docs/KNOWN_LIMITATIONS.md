# Known Limitations — Ontario All-Quote Agent (Submission)

Honest inventory of what the system does **not** do, what was **not** achieved in
this milestone, and the boundaries that are intentional.

## Not implemented / not claimed

- **No live quotes were returned in this submission.** The deterministic demo
  produces **synthetic sandbox estimates** (`estimate_only`, `local_sandbox`,
  `not_a_live_quote: true`). The real bounded Sonnet attempt returned **no quote**
  and is reported `unresolved` (`quote_returned: false`).
- **No real insurer/broker/aggregator transaction** is performed or claimed.
  Verified routes (Square One, Sonnet) were onboarded and inspected, but no
  production quote was completed.
- **No phone calls are placed.** Voice/manual routes produce callback or handoff
  observations; the manual handoff record has `handoff_executed: false`.
- **No identity verification or database lookup** is automated; participant
  confirmation is required and never assumed.
- **No LLM browser loop.** Browser automation is deterministic and bounded; there is
  no autonomous agent that keeps trying a page until it succeeds.

## Intentional safety boundaries

- **Application declarations are never automated** (`must_not_automate=True`).
- **Payment, signature, purchase, binding, cancellation, and policy modification
  are prohibited** and blocked by the safe-action rules.
- **CAPTCHA, authentication, bot controls, and rate limits are never bypassed**;
  a barrier stops and classifies the route.
- The system **does not give licensed insurance advice** and is **not a public or
  commercial product** (single participant, personal use).

## Reliability & scope limitations

- The real Sonnet journey could not be driven to a quote in the automated context
  (province control not reliably exposed within the bounded attempt). Further work
  requires provider-specific integration research or permitted manual completion.
- Market registry is seeded from the public brief: **2/31** entries are verified
  (Square One, Sonnet); the rest remain `discovered`. `distinct_rate_source_id` is
  verified where evidence exists and `unverified` otherwise.
- Voice/manual-handoff flow is simulated; telephony integration is not present.
- Persistence is in-memory for the demo; the Postgres-backed repositories exist but
  are **skip-gated** (no cloud DB required).
- Frontend live mode is a gated personal-use operator; the primary demo path is the
  deterministic generator plus mock-mode click-through.

## Data & privacy posture

- Sensitive applicant values are redacted from logs, traces, evidence, fixtures, and
  reports; privacy meta-tests enforce this.
- The generated `reports/submission/` artifacts are disposable demo output and are
  gitignored; retention is demo-only.

## What would change these limitations

- A permitted, provider-supported integration for Sonnet (or another carrier) with
  real participant data and human approval could produce a genuine quote; that
  remains future planned work and was deliberately **not** fabricated here.
