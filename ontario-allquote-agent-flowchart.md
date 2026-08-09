# Ontario All-Quote Agent — Architecture Flow

```mermaid
flowchart TD

    U[User] --> P[Product Selection<br/>Auto / Future Products]

    P --> I[Consent-Aware Progressive Intake<br/>Issue #5]

    I --> CP[Canonical Insurance Profile<br/>Pydantic + Profile Vault]

    CP --> MR[Ontario Market Registry<br/>Issue #3]
    CP --> RP[Market Route Planner<br/>Issue #6]

    MR --> DD[Rate-Source Deduplication<br/>Issue #4]
    DD --> RP

    I --> RP

    RP -->|Web route ready| BA[Browser Autofill & Quote Agent<br/>Issue #7]
    RP -->|Phone / broker route| VA[Voice / Human Handoff<br/>Issue #9]
    RP -->|Needs information| I
    RP -->|Needs consent| I
    RP -->|Unverified / unresolved| UR[Keep Visible in Coverage Ledger]

    BA -->|Missing field discovered| I
    BA -->|Human checkpoint / CAPTCHA / consent boundary| HH[Pause for Human]
    BA -->|Quote or route observation| TS[Terminal Status & Recovery<br/>Issue #8]

    VA -->|Missing field discovered| I
    VA -->|Human confirmation required| HH
    VA -->|Call / broker result| TS

    TS --> EA[Evidence & Audit Store<br/>Issue #10]
    EA --> QN[Quote Normalization & Coverage Ledger<br/>Issue #11]
    QN --> CC[Comparability & Confidence Engine<br/>Issue #12]
    CC --> DB[Evidence-First Dashboard<br/>Issue #13]

    DB --> OUT[Final User View<br/>Quotes + Coverage Differences +<br/>Unresolved Markets + Evidence]

    classDef completed fill:#d9f7df,stroke:#2f7d32,stroke-width:1.5px;
    classDef current fill:#fff4cc,stroke:#b8860b,stroke-width:1.5px;
    classDef future fill:#e8eefc,stroke:#4b67a1,stroke-width:1.5px;
    classDef safety fill:#fde8e8,stroke:#a94442,stroke-width:1.5px;

    class I,CP,MR,DD,RP completed;
    class BA current;
    class VA,TS,EA,QN,CC,DB,OUT,UR future;
    class HH safety;
```

## What is completed

- **Issue #1:** Foundation, FastAPI, LangGraph, LangSmith, logging and redaction
- **Issue #2:** Canonical auto-insurance schema
- **Issue #3:** Progressive profiles and Ontario market registry
- **Issue #4:** Rate-source deduplication
- **Issue #5:** Consent-aware progressive intake
- **Issue #6:** Product-aware market route planner

## Currently being built

- **Issue #7:** Browser Autofill & Quote Agent using Playwright

## Planned next

- **Issue #8:** Terminal status and recovery
- **Issue #9:** Voice / broker handoff
- **Issue #10:** Evidence and audit
- **Issue #11:** Quote normalization
- **Issue #12:** Comparability and confidence
- **Issue #13:** Evidence-first dashboard

## Core design principle

The system does not simply visit a list of insurance websites.

It first:

1. builds one canonical applicant profile,
2. understands the Ontario insurance market,
3. avoids counting confirmed duplicate rate sources twice,
4. determines readiness independently for each route,
5. asks the user only when genuinely new information is required,
6. preserves consent and human checkpoints,
7. then executes web or voice routes,
8. and finally presents quotes with evidence and honest market coverage.

## Key runtime loop

```mermaid
flowchart LR
    R[Ready Route] --> B[Browser / Voice Agent]
    B --> Q{New information required?}
    Q -->|No| C[Continue Quote Journey]
    Q -->|Yes| I[Intake Agent]
    I --> V[Validate + Update Canonical Profile]
    V --> B
    C --> O[Quote / Handoff / Blocker Observation]
```

## Safety boundaries

```mermaid
flowchart LR
    A[Automation] --> X{Sensitive checkpoint?}

    X -->|Normal quote navigation| C[Continue]
    X -->|Identity lookup| H[Human Handoff]
    X -->|Consent attestation| H
    X -->|Declaration| H
    X -->|Signature| S[Stop]
    X -->|Payment / Purchase / Binding| S
    X -->|CAPTCHA / Bot Control| S
```
