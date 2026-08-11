"""Deterministic integrity hashing for evidence/audit records (Issue #10).

Per-record SHA-256 over a canonicalized serialization of the SAFE protected
contents. Never hashes raw applicant data (none exists here). Purpose:
- detect accidental mutation of persisted evidence;
- support audit/export verification.

A per-attempt hash chain is intentionally NOT implemented in Prompt 1: the
per-record hash + deterministic per-attempt ``sequence`` ordering already
detect mutation and give stable audit order; a ``previous_hash`` chain would
add ordering/concurrency coupling with little benefit here (documented in the
learning doc).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from decimal import Decimal
from typing import Any


def _utc_naive_iso(value: dt.datetime) -> str:
    """Normalize a datetime to UTC-naive isoformat for stable hashing.

    Aware UTC ``2026-01-01T12:00:00+00:00`` and naive UTC (as returned by
    SQLite round-trips) must hash identically, so any aware value is converted
    to UTC and the tzinfo stripped before formatting.
    """
    if value.tzinfo is not None:
        value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return value.isoformat()


def _canonical(value: Any) -> Any:
    """Normalize a value for deterministic JSON hashing."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dt.datetime):
        return _utc_naive_iso(value)
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    return value


def canonical_json(data: dict[str, Any]) -> str:
    """Deterministic JSON (sorted keys, compact separators)."""
    return json.dumps(
        _canonical(data),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def sha256_hex(text: str) -> str:
    """SHA-256 hex digest of a string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# Fields considered part of the protected evidence contents (everything
# semantic; operational keys like evidence_id/created_at/sequence and the
# idempotency_key, plus content_hash itself, are excluded so the service can
# hash before the repository assigns ordering atomically, and a recompute from
# stored fields matches).
EVIDENCE_HASH_FIELDS = (
    "event_type",
    "observed_at",
    "intake_session_id",
    "plan_id",
    "planned_route_id",
    "registry_id",
    "distinct_rate_source_id",
    "attempt_id",
    "parent_attempt_id",
    "source_channel",
    "source_session_id",
    "page_signature",
    "safe_url",
    "observation_type",
    "reason_code",
    "evidence_source",
    "payload_version",
    "payload",
    "quote_observation_id",
    "registry_snapshot_ref",
    "config_version",
    "attachments",
)


def evidence_content_hash(record: dict[str, Any]) -> str:
    """SHA-256 over the protected safe contents of an evidence record."""
    payload = {k: v for k, v in record.items() if k in EVIDENCE_HASH_FIELDS}
    return sha256_hex(canonical_json(payload))


QUOTE_HASH_FIELDS = (
    "intake_session_id",
    "attempt_id",
    "parent_attempt_id",
    "plan_id",
    "planned_route_id",
    "registry_id",
    "distinct_rate_source_id",
    "aggregator_registry_id",
    "presented_carrier",
    "observed_at",
    "annual_premium",
    "monthly_premium",
    "currency",
    "firm_vs_estimate",
    "reference_present",
    "private_reference_handle",
    "coverage_raw_present",
    "quote_pending_normalization",
)


def quote_content_hash(quote: dict[str, Any]) -> str:
    """SHA-256 over the protected safe contents of a quote observation."""
    payload = {k: v for k, v in quote.items() if k in QUOTE_HASH_FIELDS}
    return sha256_hex(canonical_json(payload))


AUDIT_HASH_FIELDS = ("intake_session_id", "event_name", "occurred_at", "actor", "safe_metadata")


def audit_content_hash(event: dict[str, Any]) -> str:
    """SHA-256 over the protected safe contents of an audit event."""
    payload = {k: v for k, v in event.items() if k in AUDIT_HASH_FIELDS}
    return sha256_hex(canonical_json(payload))
