# Browser route configs (Issue #7)

This directory holds data-driven **browser route configurations** — one JSON
file per registry id (`<registry_id>.json`) that tells the generic
`BrowserExecutor` how to interact with a quote route without changing executor
code.

## When a config is needed

A route config is required for **LIVE** execution of a manually-verified web
route. The current AUTO registry seed has **no verified live web route**
(`status: discovered`, no `quote_url`) — so this directory is intentionally
empty. The Issue #7 core is fully exercised against the **local mock quote
site** (`app/browser/mock_site.py`) in hermetic tests; no real insurer website
is automated.

Expected result for the hackathon pilot: `no_verified_live_browser_route` until
a route is manually verified and a config is added here.

## Config format (validated by `BrowserRouteConfig` in `app/models/browser/`)

```jsonc
{
  "registry_id": "example-direct",
  "config_version": 1,
  "start_url": "https://example.example/quote/start",
  "allowed_hosts": ["example.example"],
  "page_signatures": [
    { "signature_id": "applicant", "url_pattern": "/quote/applicant",
      "heading_patterns": ["Applicant Information"], "field_ids": ["legal-name"] }
  ],
  "field_bindings": [
    { "external_field_id": "legal-name",
      "match_patterns": [ { "strategy": "label_text", "value": "Legal name" } ],
      "canonical_path": "applicant.identity.legal_name",
      "control_type": "input", "fill_strategy": "text", "transform": "none",
      "required": true, "sensitivity": "sensitive", "enabled": true }
  ],
  "action_bindings": [
    { "action_type": "continue", "safety": "safe_navigation", "label_patterns": ["Continue"] }
  ],
  "checkpoint_bindings": [
    { "checkpoint_type": "identity_lookup", "label_patterns": ["Verify identity"] }
  ],
  "quote_detection": {
    "heading_patterns": ["Your Quote"],
    "price_pattern": "\\$\\s?([\\d,]+(?:\\.\\d{1,2})?)",
    "currency": "CAD",
    "reference_patterns": ["(?:quote|reference)[\\s#]*([A-Z0-9]{4,})"]
  },
  "automation_notes": "Record verification date/source and any known limitations.",
  "last_verified_at": "2026-01-01T00:00:00Z"
}
```

See `app/models/browser/config.py` for the full validated schema (match
strategies, fill strategies, transforms, detection configs). Question-wording
changes, selector changes, new known canonical fields, disabled fields, and new
generic routes are handled by editing this configuration — never the executor.
