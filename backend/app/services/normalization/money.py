"""Premium (money) normalization (Issue #11, Prompt 1).

Deterministic, Decimal-only premium normalization. Handles firm vs estimate
(never promotes an estimate), annual vs monthly presentation, and records the
exact derivation so downstream code never has to guess. No currency conversion
(hard-CAD, per Issue #11 scope); no LLM; no fabricated amounts for voice.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from ...models.normalization import (
    PremiumComponent,
    PremiumDerivation,
    PremiumNormalized,
)

MONTHLY_TO_ANNUAL_MULTIPLIER = Decimal("12")


def _is_valid_amount(amount: Optional[Decimal]) -> bool:
    return amount is not None and amount >= 0


class PremiumNormalizer:
    """Pure, stateless premium normalizer (Decimal everywhere)."""

    def __init__(self, currency: str = "CAD") -> None:
        self.currency = currency

    def normalize(
        self,
        *,
        annual_premium: Optional[Decimal] = None,
        monthly_premium: Optional[Decimal] = None,
        currency: Optional[str] = None,
    ) -> PremiumNormalized:
        """Build the canonical premium representation from raw amounts.

        Priority: provider-presented annual wins; else monthly is annualized
        (derived, monthly_x_12). If neither is present, the premium is left
        empty with ``PremiumDerivation.UNKNOWN`` (caller decides status).
        """
        effective_currency = currency or self.currency
        components: list[PremiumComponent] = []

        if _is_valid_amount(annual_premium):
            return PremiumNormalized(
                provider_presented_amount=annual_premium,
                provider_presented_frequency="annual",
                normalized_annual_amount=annual_premium,
                currency=effective_currency,
                annualized=False,
                derivation=PremiumDerivation.DIRECTLY_QUOTED,
                derivation_rule="directly_quoted_annual",
                components=[
                    PremiumComponent(
                        kind="premium",
                        amount=annual_premium,
                        currency=effective_currency,
                    )
                ],
            )

        if _is_valid_amount(monthly_premium):
            normalized_annual = (monthly_premium * MONTHLY_TO_ANNUAL_MULTIPLIER).quantize(
                Decimal("0.01")
            )
            return PremiumNormalized(
                provider_presented_amount=monthly_premium,
                provider_presented_frequency="monthly",
                normalized_annual_amount=normalized_annual,
                currency=effective_currency,
                annualized=True,
                derivation=PremiumDerivation.DERIVED_ANNUALIZED,
                derivation_rule="monthly_x_12",
                components=[
                    PremiumComponent(
                        kind="premium",
                        amount=monthly_premium,
                        currency=effective_currency,
                    )
                ],
            )

        return PremiumNormalized(
            provider_presented_amount=None,
            provider_presented_frequency=None,
            normalized_annual_amount=None,
            currency=effective_currency,
            annualized=False,
            derivation=PremiumDerivation.UNKNOWN,
            derivation_rule=None,
            components=components,
        )
