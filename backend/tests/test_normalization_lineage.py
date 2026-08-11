"""Normalization lineage tests (Issue #11): normalized quote -> evidence source.

Confirms the normalized quote points back at the exact Issue #10
``QuoteObservation`` (source_quote_observation_id) plus the evidence record ids
that fed it, and that raw evidence is never mutated by normalization.
"""

from __future__ import annotations

from decimal import Decimal

from app.models.normalization import CoverageItemKey

from normalization_helpers import (
    make_browser_quote,
    make_normalization_env,
    record_and_normalize,
)


async def test_lineage_source_quote_observation_id():
    env = make_normalization_env()
    obs = make_browser_quote(coverage=["Collision - $500"])
    stored, normalized = await record_and_normalize(env, obs, source_evidence_ids=["ev-1"])
    assert normalized.source_quote_observation_id == stored.quote_id
    assert normalized.source_evidence_record_ids == ["ev-1"]
    # Coverage items carry the same evidence refs
    coll = normalized.coverage_ledger.get(CoverageItemKey.COLLISION)
    assert coll.source_evidence_ids == ["ev-1"]


async def test_normalization_does_not_mutate_raw_evidence():
    env = make_normalization_env()
    obs = make_browser_quote(
        annual=Decimal("100.00"),
        coverage=["Third Party Liability - $2M"],
        discounts=["Winter Tire Discount"],
    )
    stored, _ = await record_and_normalize(env, obs)
    # The source quote observation is byte-identical (immutable).
    reloaded = await env.evidence.get_quote_observation(env.intake_session_id, stored.quote_id)
    assert reloaded == stored
    assert reloaded.coverage_observations == ["Third Party Liability - $2M"]
    assert reloaded.discount_observations == ["Winter Tire Discount"]


async def test_two_normalizations_from_same_source_share_lineage():
    env = make_normalization_env()
    obs = make_browser_quote(coverage=["Collision - $500"])
    stored, first = await record_and_normalize(env, obs, source_evidence_ids=["ev-9"])
    second = await env.normalization.normalize(
        env.intake_session_id, stored.quote_id, source_evidence_record_ids=["ev-9"]
    )
    assert first.source_quote_observation_id == second.source_quote_observation_id
    assert first.source_quote_observation_id == stored.quote_id


async def test_delete_by_intake_session_removes_normalized_quotes_only():
    env = make_normalization_env()
    obs = make_browser_quote(coverage=["Collision - $500"])
    stored, normalized = await record_and_normalize(env, obs)
    removed = await env.normalization.delete_by_intake_session(env.intake_session_id)
    assert removed >= 1
    assert await env.normalization.get(env.intake_session_id, normalized.normalized_quote_id) is None
    # Evidence source untouched
    assert await env.evidence.get_quote_observation(env.intake_session_id, stored.quote_id) is not None
