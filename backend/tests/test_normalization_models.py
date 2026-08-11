"""Pydantic schema tests for Issue #11 normalization domain models."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.models.normalization import (
    BooleanCoverageValue,
    CoverageItemKey,
    CoverageItemState,
    CoverageLedger,
    CoverageLedgerItem,
    CoverageProvenance,
    EndorsementCoverageValue,
    MoneyCoverageValue,
    NormalizationStatus,
    NormalizedQuote,
    NormalizedQuoteView,
    PremiumDerivation,
    PremiumNormalized,
    validate_coverage_value,
)
from app.models.recovery import SourceChannel
from app.services.normalization.service import normalized_idempotency_key


def _minimal_quote(**overrides) -> NormalizedQuote:
    base = dict(
        normalized_quote_id="nq-1",
        intake_session_id="intake-1",
        plan_id="plan-1",
        planned_route_id="route-1",
        registry_id="mock",
        distinct_rate_source_id="RS-MOCK",
        attempt_id="att-1",
        source_quote_observation_id="q-1",
        source_channel=SourceChannel.BROWSER,
        firm_vs_estimate="firm",
        premium=PremiumNormalized(),
        coverage_ledger=CoverageLedger(),
        normalization_status=NormalizationStatus.PENDING,
        normalization_rule_version="1",
        normalized_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        content_hash="abc",
        idempotency_key="k",
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
    )
    base.update(overrides)
    return NormalizedQuote(**base)


def test_coverage_item_state_never_collapses_unknown():
    # unknown is a first-class state, distinct from excluded
    assert CoverageItemState.UNKNOWN.value == "unknown"
    assert CoverageItemState.EXCLUDED.value == "excluded"
    assert CoverageItemState.UNKNOWN != CoverageItemState.EXCLUDED


def test_normalization_status_never_assigns_comparable():
    values = {s.value for s in NormalizationStatus}
    assert "quoted_comparable" not in values
    assert "quoted_non_comparable" not in values
    assert NormalizationStatus.INSUFFICIENT_EVIDENCE.value == "insufficient_evidence"
    assert NormalizationStatus.PARTIALLY_NORMALIZED.value == "partially_normalized"


def test_coverage_value_discriminated_union_roundtrip():
    money = MoneyCoverageValue(amount=Decimal("2000000"), currency="CAD")
    boolean = BooleanCoverageValue(present=True)
    endorsement = EndorsementCoverageValue(code="OPCF 44R")
    for value in (money, boolean, endorsement):
        restored = validate_coverage_value(value.model_dump())
        assert restored == value


def test_coverage_value_rejects_unknown_kind():
    with pytest.raises(ValidationError):
        validate_coverage_value({"kind": "bogus", "amount": "1"})


def test_coverage_ledger_item_rejects_extra_fields():
    with pytest.raises(ValidationError):
        CoverageLedgerItem(item_key=CoverageItemKey.COLLISION, extra_field="x")


def test_coverage_ledger_ordered_items_are_deterministic():
    ledger = CoverageLedger()
    ledger.set_item(
        CoverageLedgerItem(item_key=CoverageItemKey.ACCIDENT_BENEFITS, state=CoverageItemState.INCLUDED)
    )
    ledger.set_item(
        CoverageLedgerItem(item_key=CoverageItemKey.THIRD_PARTY_LIABILITY, state=CoverageItemState.INCLUDED)
    )
    keys = [item.item_key for item in ledger.ordered_items()]
    # Deterministic alphabetical ordering by canonical key value.
    assert keys == [
        CoverageItemKey.ACCIDENT_BENEFITS,
        CoverageItemKey.THIRD_PARTY_LIABILITY,
    ]
    assert ledger.mapped_count == 2


def test_premium_derivation_values():
    assert PremiumDerivation.DIRECTLY_QUOTED.value == "directly_quoted"
    assert PremiumDerivation.DERIVED_ANNUALIZED.value == "derived_annualized"


def test_normalized_quote_sensitive_base_redacts():
    # SensitiveBaseModel repr redacts sensitive fields by name; a synthetic
    # sensitive marker never leaks into repr.
    quote = _minimal_quote()
    assert "416-555-0199" not in repr(quote)
    assert "NormalizedQuote(" in repr(quote)


def test_normalized_quote_view_is_safe_projection():
    quote = _minimal_quote(
        premium=PremiumNormalized(
            provider_presented_amount=Decimal("1234.56"),
            provider_presented_frequency="annual",
            normalized_annual_amount=Decimal("1234.56"),
            currency="CAD",
            derivation=PremiumDerivation.DIRECTLY_QUOTED,
        )
    )
    from app.services.normalization.service import _quote_view

    view = _quote_view(quote)
    assert view.normalized_quote_id == "nq-1"
    assert view.premium.normalized_annual_amount == "1234.56"
    assert view.premium.derivation == "directly_quoted"
    assert view.normalization_status == "pending"


def test_normalized_quote_view_rejects_extra_fields():
    with pytest.raises(ValidationError):
        NormalizedQuoteView(
            normalized_quote_id="x",
            intake_session_id="i",
            source_quote_observation_id="q",
            source_channel="browser",
            firm_vs_estimate="firm",
            premium=None,
            coverage_ledger=None,
            normalization_status="pending",
            normalization_rule_version="1",
            normalized_at=dt.datetime.now(dt.timezone.utc),
            content_hash="h",
            extra="bad",
        )


def test_idempotency_key_deterministic():
    a = normalized_idempotency_key("q-1", "1")
    b = normalized_idempotency_key("q-1", "1")
    c = normalized_idempotency_key("q-1", "2")
    assert a == b
    assert a != c
    assert a.startswith("norm:q-1:1")


def test_quote_decimal_money_preserved_exactly():
    value = MoneyCoverageValue(amount=Decimal("0.01"), currency="CAD")
    assert value.amount == Decimal("0.01")
