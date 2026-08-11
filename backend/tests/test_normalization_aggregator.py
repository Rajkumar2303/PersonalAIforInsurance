"""Aggregator & duplicate-rate-source behavior in normalization (Issue #11).

Aggregator quotes (aggregator_registry_id set) are retained as their own
NormalizedQuote rows - normalization never deduplicates, never collapses a
direct quote into an aggregator quote (or vice versa), and never drops an
estimate when a firm quote for the same route exists. Comparability decisions
(which would deduplicate/rank) are owned by Issue #12, NOT here.
"""

from __future__ import annotations

from decimal import Decimal

from app.models.normalization import NormalizationStatus

from normalization_helpers import (
    AGGREGATOR_REGISTRY_ID,
    make_browser_quote,
    make_normalization_env,
    record_and_normalize,
)


async def test_aggregator_quote_gets_own_normalized_row():
    env = make_normalization_env()
    obs = make_browser_quote(
        annual=Decimal("100.00"), coverage=["Third Party Liability - $2M"]
    )
    # aggregator_registry_id is not a normalizer input (it lives on the source
    # observation); emulate it by overriding the registry id in the id map.
    stored, normalized = await record_and_normalize(
        env, obs, registry_id="mock-aggregator", distinct_rate_source_id="RS-AGG"
    )
    assert normalized.registry_id == "mock-aggregator"
    assert normalized.distinct_rate_source_id == "RS-AGG"
    assert normalized.normalization_status == NormalizationStatus.NORMALIZED


async def test_direct_and_aggregator_both_retained():
    env = make_normalization_env()
    direct_obs = make_browser_quote(
        annual=Decimal("100.00"), coverage=["Third Party Liability - $2M"]
    )
    _, direct = await record_and_normalize(env, direct_obs)
    agg_obs = make_browser_quote(
        annual=Decimal("95.00"), coverage=["Third Party Liability - $2M"]
    )
    _, agg = await record_and_normalize(
        env, agg_obs, attempt_id="att-agg", registry_id=AGGREGATOR_REGISTRY_ID
    )
    # No deduplication: two normalized rows with distinct sources.
    all_quotes = await env.normalization.list_by_intake(env.intake_session_id)
    assert len(all_quotes) == 2
    assert {q.source_quote_observation_id for q in all_quotes} == {
        direct.source_quote_observation_id,
        agg.source_quote_observation_id,
    }


async def test_estimate_and_firm_both_retained():
    env = make_normalization_env()
    estimate_obs = make_browser_quote(annual=Decimal("90.00"), firm=False)
    _, estimate = await record_and_normalize(env, estimate_obs)
    firm_obs = make_browser_quote(annual=Decimal("100.00"), firm=True)
    _, firm = await record_and_normalize(env, firm_obs, attempt_id="att-firm")
    assert estimate.firm_vs_estimate == "estimate"
    assert firm.firm_vs_estimate == "firm"
    all_quotes = await env.normalization.list_by_intake(env.intake_session_id)
    assert len(all_quotes) == 2
    # The estimate was never promoted to firm.
    assert {q.firm_vs_estimate for q in all_quotes} == {"estimate", "firm"}
