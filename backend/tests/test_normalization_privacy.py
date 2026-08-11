"""Privacy tests for Issue #11 normalization (Issue #10 discipline).

Sensitive synthetic markers (licence, VIN, DOB, street, email, phone, claims,
raw quote references) must never appear in normalized quotes, their content
hashes, API views, or exports.
"""

from __future__ import annotations

from decimal import Decimal

from app.services.normalization.service import (
    _quote_view,
    normalized_quote_content_hash,
)

from normalization_helpers import (
    assert_no_sensitive_markers,
    make_browser_quote,
    make_normalization_env,
    record_and_normalize,
)


async def test_normalized_quote_contains_no_sensitive_markers():
    env = make_normalization_env()
    obs = make_browser_quote(
        annual=Decimal("1234.56"),
        coverage=[
            "Third Party Liability - $2,000,000",
            "Collision - $1,000 deductible",
            "Family Protection",
        ],
        discounts=["Winter Tire Discount"],
    )
    _, normalized = await record_and_normalize(env, obs, source_evidence_ids=["ev-1"])
    assert_no_sensitive_markers(normalized)


async def test_content_hash_contains_no_sensitive_markers():
    env = make_normalization_env()
    obs = make_browser_quote(coverage=["Third Party Liability - $2M"])
    _, normalized = await record_and_normalize(env, obs)
    digest = normalized_quote_content_hash(normalized)
    assert_no_sensitive_markers(digest)


async def test_api_view_contains_no_sensitive_markers():
    env = make_normalization_env()
    obs = make_browser_quote(coverage=["Collision - $500"])
    _, normalized = await record_and_normalize(env, obs)
    view = _quote_view(normalized)
    assert_no_sensitive_markers(view)


async def test_export_view_contains_no_sensitive_markers():
    from app.services.normalization.service import _export_view

    env = make_normalization_env()
    obs = make_browser_quote(coverage=["Collision - $500"])
    _, normalized = await record_and_normalize(env, obs)
    export = _export_view(env.intake_session_id, [normalized], "1")
    assert_no_sensitive_markers(export)


async def test_raw_coverage_labels_are_safe_public_wording_only():
    # Provider wording in the ledger must be public wording - no PII ever gets
    # into a coverage label at the source (browser detector) and the normalizer
    # never adds any.
    env = make_normalization_env()
    obs = make_browser_quote(coverage=["Collision - $500"])
    _, normalized = await record_and_normalize(env, obs)
    for item in normalized.coverage_ledger.ordered_items():
        for label in item.raw_labels:
            assert_no_sensitive_markers(label)
