"""QuoteNormalizationService tests (in-memory, hermetic) (Issue #11)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.normalization import (
    CoverageItemKey,
    NormalizationStatus,
)
from app.models.recovery import SourceChannel
from app.services.normalization import QuoteNormalizationError

from normalization_helpers import (
    make_browser_quote,
    make_normalization_env,
    record_and_normalize,
    record_voice_quote,
)


async def test_normalize_browser_firm_quote_full():
    env = make_normalization_env()
    obs = make_browser_quote(
        annual=Decimal("1234.56"),
        coverage=["Third Party Liability - $2,000,000", "Family Protection"],
        discounts=["Winter Tire Discount"],
    )
    stored, normalized = await record_and_normalize(env, obs)
    assert normalized.normalization_status == NormalizationStatus.NORMALIZED
    assert normalized.source_quote_observation_id == stored.quote_id
    assert normalized.firm_vs_estimate == "firm"
    assert normalized.source_channel == SourceChannel.BROWSER
    assert normalized.premium.normalized_annual_amount == Decimal("1234.56")
    assert normalized.premium.derivation.value == "directly_quoted"
    tpl = normalized.coverage_ledger.get(CoverageItemKey.THIRD_PARTY_LIABILITY)
    assert tpl.value.amount == Decimal("2000000")


async def test_normalize_is_idempotent():
    env = make_normalization_env()
    obs = make_browser_quote(coverage=["Collision - $500 deductible"])
    _, normalized = await record_and_normalize(env, obs)
    again = await env.normalization.normalize(
        env.intake_session_id, normalized.source_quote_observation_id
    )
    assert again.normalized_quote_id == normalized.normalized_quote_id
    assert again.content_hash == normalized.content_hash


async def test_normalize_missing_source_raises():
    env = make_normalization_env()
    with pytest.raises(QuoteNormalizationError):
        await env.normalization.normalize(env.intake_session_id, "no-such-quote")


async def test_normalize_voice_without_premium_is_insufficient_evidence():
    env = make_normalization_env()
    stored = await record_voice_quote(env)
    normalized = await env.normalization.normalize(env.intake_session_id, stored.quote_id)
    assert normalized.normalization_status == NormalizationStatus.INSUFFICIENT_EVIDENCE
    assert normalized.premium.normalized_annual_amount is None
    assert normalized.source_channel == SourceChannel.VOICE
    # No fabricated amount is ever produced for a voice quote.
    assert normalized.premium.provider_presented_amount is None


async def test_normalize_voice_with_structured_amount_normalizes():
    env = make_normalization_env()
    stored = await record_voice_quote(env, annual=Decimal("999.00"))
    normalized = await env.normalization.normalize(env.intake_session_id, stored.quote_id)
    # No coverage observed from voice -> partially_normalized (premium only)
    assert normalized.normalization_status == NormalizationStatus.PARTIALLY_NORMALIZED
    assert normalized.premium.normalized_annual_amount == Decimal("999.00")


async def test_normalize_monthly_annualizes():
    env = make_normalization_env()
    obs = make_browser_quote(annual=None, monthly=Decimal("100.00"))
    _, normalized = await record_and_normalize(env, obs)
    assert normalized.premium.annualized is True
    assert normalized.premium.normalized_annual_amount == Decimal("1200.00")
    assert normalized.premium.derivation.value == "derived_annualized"
    assert normalized.normalization_status == NormalizationStatus.PARTIALLY_NORMALIZED


async def test_normalize_preserves_estimate_not_promoted():
    env = make_normalization_env()
    obs = make_browser_quote(annual=Decimal("999.00"), firm=False)
    _, normalized = await record_and_normalize(env, obs)
    assert normalized.firm_vs_estimate == "estimate"


async def test_normalize_preserves_identifiers():
    env = make_normalization_env()
    obs = make_browser_quote()
    _, normalized = await record_and_normalize(env, obs)
    assert normalized.intake_session_id == env.intake_session_id
    assert normalized.plan_id == env.plan_id
    assert normalized.planned_route_id == env.planned_route_id
    assert normalized.registry_id == env.registry_id
    assert normalized.distinct_rate_source_id == env.distinct_rate_source_id
    assert normalized.attempt_id == env.attempt_id


async def test_normalize_never_assigns_comparable_statuses():
    env = make_normalization_env()
    obs = make_browser_quote(coverage=["Third Party Liability - $2M"])
    _, normalized = await record_and_normalize(env, obs)
    assert normalized.normalization_status not in (
        "quoted_comparable",
        "quoted_non_comparable",
    )
    assert "comparable" not in normalized.normalization_status.value


async def test_normalize_unmapped_coverage_still_normalizes():
    env = make_normalization_env()
    obs = make_browser_quote(coverage=["Weird Unmapped Perk"], annual=Decimal("100.00"))
    _, normalized = await record_and_normalize(env, obs)
    # Premium present but no mapped coverage -> partially_normalized; the
    # unmapped label is preserved (never discarded/guessed).
    assert normalized.normalization_status == NormalizationStatus.PARTIALLY_NORMALIZED
    assert len(normalized.coverage_ledger.unmapped_coverage) == 1


async def test_list_by_plan_and_route():
    env = make_normalization_env()
    obs1 = make_browser_quote(annual=Decimal("100.00"), coverage=["Collision - $500"])
    await record_and_normalize(env, obs1)
    obs2 = make_browser_quote(annual=Decimal("200.00"), coverage=["Collision - $500"])
    await record_and_normalize(env, obs2, attempt_id="att-2")
    by_plan = await env.normalization.list_by_plan(env.intake_session_id, env.plan_id)
    by_route = await env.normalization.list_by_route(env.intake_session_id, env.planned_route_id)
    assert len(by_plan) == 2
    assert len(by_route) == 2


async def test_integrity_verification():
    env = make_normalization_env()
    obs = make_browser_quote(coverage=["Collision - $500"])
    _, normalized = await record_and_normalize(env, obs)
    assert await env.normalization.verify_integrity(
        env.intake_session_id, normalized.normalized_quote_id
    ) is True
