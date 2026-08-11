"""Premium (money) normalization tests (Issue #11)."""

from __future__ import annotations

from decimal import Decimal

from app.services.normalization.money import (
    MONTHLY_TO_ANNUAL_MULTIPLIER,
    PremiumNormalizer,
)
from app.models.normalization import PremiumDerivation


def _normalizer():
    return PremiumNormalizer(currency="CAD")


def test_annual_directly_quoted():
    result = _normalizer().normalize(annual_premium=Decimal("1234.56"), currency="CAD")
    assert result.provider_presented_amount == Decimal("1234.56")
    assert result.provider_presented_frequency == "annual"
    assert result.normalized_annual_amount == Decimal("1234.56")
    assert result.annualized is False
    assert result.derivation == PremiumDerivation.DIRECTLY_QUOTED
    assert result.derivation_rule == "directly_quoted_annual"


def test_annual_wins_over_monthly():
    result = _normalizer().normalize(
        annual_premium=Decimal("1000.00"),
        monthly_premium=Decimal("99.00"),
        currency="CAD",
    )
    assert result.normalized_annual_amount == Decimal("1000.00")
    assert result.derivation == PremiumDerivation.DIRECTLY_QUOTED


def test_monthly_derived_annualized():
    result = _normalizer().normalize(monthly_premium=Decimal("102.88"), currency="CAD")
    assert result.provider_presented_amount == Decimal("102.88")
    assert result.provider_presented_frequency == "monthly"
    assert result.normalized_annual_amount == Decimal("1234.56")  # 102.88 * 12
    assert result.annualized is True
    assert result.derivation == PremiumDerivation.DERIVED_ANNUALIZED
    assert result.derivation_rule == "monthly_x_12"


def test_monthly_derivation_is_exact_decimal():
    result = _normalizer().normalize(monthly_premium=Decimal("0.01"), currency="CAD")
    assert result.normalized_annual_amount == Decimal("0.12")
    assert MONTHLY_TO_ANNUAL_MULTIPLIER == Decimal("12")


def test_no_premium_is_unknown_derivation():
    result = _normalizer().normalize()
    assert result.normalized_annual_amount is None
    assert result.derivation == PremiumDerivation.UNKNOWN
    assert result.annualized is False


def test_currency_defaults_to_cad():
    result = _normalizer().normalize(annual_premium=Decimal("10.00"))
    assert result.currency == "CAD"


def test_currency_preserved_from_source():
    result = _normalizer().normalize(annual_premium=Decimal("10.00"), currency="USD")
    # Hard-CAD scope: if a non-CAD currency slips in, we preserve the source
    # value but never convert. No conversion logic exists.
    assert result.currency == "USD"


def test_components_preserve_provider_amount():
    result = _normalizer().normalize(annual_premium=Decimal("55.50"), currency="CAD")
    assert len(result.components) == 1
    assert result.components[0].kind == "premium"
    assert result.components[0].amount == Decimal("55.50")


def test_negative_amount_is_treated_as_missing():
    result = _normalizer().normalize(annual_premium=Decimal("-5.00"))
    assert result.normalized_annual_amount is None
    assert result.derivation == PremiumDerivation.UNKNOWN
