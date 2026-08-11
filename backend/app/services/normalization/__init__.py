"""Quote normalization & coverage ledger service (Issue #11, Prompt 1)."""

from __future__ import annotations

from .config import (
    CoverageMappingRegistry,
    NormalizationConfigError,
    get_coverage_mapping_registry,
)
from .coverage import CoverageNormalizer
from .money import PremiumNormalizer
from .repository import (
    InMemoryNormalizationRepository,
    NormalizationRepository,
    SqlAlchemyNormalizationRepository,
)
from .service import (
    QuoteNormalizationError,
    QuoteNormalizationService,
    get_quote_normalization_service,
    normalized_idempotency_key,
)

__all__ = [
    "CoverageMappingRegistry",
    "CoverageNormalizer",
    "InMemoryNormalizationRepository",
    "NormalizationConfigError",
    "NormalizationRepository",
    "PremiumNormalizer",
    "QuoteNormalizationError",
    "QuoteNormalizationService",
    "SqlAlchemyNormalizationRepository",
    "get_coverage_mapping_registry",
    "get_quote_normalization_service",
    "normalized_idempotency_key",
]
