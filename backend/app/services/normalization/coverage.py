"""Coverage normalization (Issue #11, Prompt 1).

Maps SAFE provider coverage label segments (``coverage_observations`` /
``discount_observations`` from Issue #7/#10 evidence) onto the canonical
``CoverageLedger`` via the data-driven ``CoverageMappingRegistry``.

Rules:
- Exact alias matching only (post-normalization); unknown labels are preserved
  under ``unmapped_coverage`` and NEVER guessed or discarded.
- ``unknown`` state is first-class - it is never collapsed into ``excluded``.
- No ``if registry_id`` branching; every mapping comes from config.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Optional

from ...models.normalization import (
    BooleanCoverageValue,
    CoverageItemState,
    CoverageLedger,
    CoverageLedgerItem,
    CoverageProvenance,
    EndorsementCoverageValue,
    MoneyCoverageValue,
    UnmappedCoverageObservation,
)
from .config import CoverageMappingRegistry

# Token patterns used ONLY to parse a value out of a safe provider label.
_MONEY_TOKEN = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)")
_NUMBER_TOKEN = re.compile(r"\b(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\b")
_MILLION_SUFFIX = re.compile(r"\b(\d+(?:\.\d+)?)\s?(m|million)\b", re.IGNORECASE)

_EXCLUDED_HINTS = ("excluded", "not included", "not covered", "opted out")
_INCLUDED_HINTS = ("included", "enrolled", "yes", "added", "covered")


def _normalize_state_hint(label: str) -> Optional[str]:
    """Return 'included'/'excluded' if the label explicitly states one."""
    lower = label.lower()
    if any(hint in lower for hint in _EXCLUDED_HINTS):
        return "excluded"
    if any(hint in lower for hint in _INCLUDED_HINTS):
        return "included"
    return None


def _parse_amount(text: str) -> Optional[Decimal]:
    """Extract the LARGEST money amount mentioned in a safe label.

    Supports ``$1,000,000``, ``2,000,000``, ``$500``, ``1m`` / ``2 million``.
    Returns ``None`` when no amount is present. Exposed for unit tests.
    """
    candidates: list[Decimal] = []

    for match in _MILLION_SUFFIX.finditer(text):
        try:
            base = Decimal(match.group(1).replace(",", ""))
            candidates.append(base * Decimal("1000000"))
        except (InvalidOperation, ValueError):
            continue

    for match in _MONEY_TOKEN.finditer(text):
        try:
            candidates.append(Decimal(match.group(1).replace(",", "")))
        except (InvalidOperation, ValueError):
            continue

    # Numeric tokens that are NOT part of a money-prefixed or million token.
    seen_ranges: list[tuple[int, int]] = []
    for match in _MILLION_SUFFIX.finditer(text):
        seen_ranges.append((match.start(), match.end()))
    for match in _MONEY_TOKEN.finditer(text):
        seen_ranges.append((match.start(), match.end()))
    for match in _NUMBER_TOKEN.finditer(text):
        if any(start <= match.start() < end for start, end in seen_ranges):
            continue
        if "deductible" in text.lower() and match.group(0) == "0":
            # A bare "0" in a deductible label adds no signal beyond the
            # money-token zero (e.g. "$0 deductible") already captured above.
            continue
        try:
            candidates.append(Decimal(match.group(0).replace(",", "")))
        except (InvalidOperation, ValueError):
            continue

    if not candidates:
        return None
    return max(candidates)


def _endorsement_code(key_value: str) -> str:
    """Concise public endorsement code, e.g. ``opcf_44r_family_protection``
    -> ``OPCF 44R``, ``opcf_20`` -> ``OPCF 20``."""
    parts = key_value.split("_")
    if len(parts) >= 2 and parts[0] == "opcf":
        return "OPCF " + parts[1].upper()
    return key_value.replace("_", " ").upper()


def _build_value(rule_value_type: str, amount: Optional[Decimal], present: bool, code: str):
    """Build a typed CoverageValue for a rule's value_type (or None)."""
    if rule_value_type in ("limit", "deductible", "money"):
        if amount is not None:
            return MoneyCoverageValue(amount=amount)
        return None
    if rule_value_type == "boolean":
        return BooleanCoverageValue(present=present)
    if rule_value_type == "endorsement":
        return EndorsementCoverageValue(code=code)
    return None  # "none"


class CoverageNormalizer:
    """Pure, stateless coverage normalizer over the mapping registry."""

    def __init__(self, registry: CoverageMappingRegistry) -> None:
        self._registry = registry
        self.rule_version = registry.rule_version

    def normalize(
        self,
        *,
        coverage_observations: Optional[list[str]] = None,
        discount_observations: Optional[list[str]] = None,
        source_evidence_ids: Optional[list[str]] = None,
    ) -> CoverageLedger:
        ledger = CoverageLedger()
        evidence_ids = list(source_evidence_ids or [])

        labels = list(coverage_observations or []) + list(discount_observations or [])
        for label in labels:
            label = label.strip()
            if not label:
                continue
            rule = self._registry.resolve(label)
            if rule is None:
                ledger.unmapped_coverage.append(
                    UnmappedCoverageObservation(
                        provider_label=label,
                        source_evidence_ids=evidence_ids,
                    )
                )
                continue

            state_hint = _normalize_state_hint(label)
            state = CoverageItemState(state_hint) if state_hint else CoverageItemState(rule.default_state)
            if state_hint is None and state is CoverageItemState.UNKNOWN:
                state = CoverageItemState(rule.default_state)

            amount = _parse_amount(label) if rule.value_type in ("limit", "deductible", "money") else None
            present = state is CoverageItemState.INCLUDED
            code = _endorsement_code(rule.canonical_key.value)

            value = _build_value(rule.value_type, amount, present, code)
            existing = ledger.get(rule.canonical_key)
            if existing is not None:
                # Later labels for the same key enrich provenance/evidence refs.
                existing.raw_labels = list(dict.fromkeys([*existing.raw_labels, label]))
                if existing.provenance is CoverageProvenance.UNKNOWN:
                    existing.provenance = CoverageProvenance.MAPPED_ALIAS
                existing.source_evidence_ids = list(
                    dict.fromkeys([*existing.source_evidence_ids, *evidence_ids])
                )
                if value is not None and existing.value is None:
                    existing.value = value
                continue

            ledger.set_item(
                CoverageLedgerItem(
                    item_key=rule.canonical_key,
                    state=state,
                    value=value,
                    provenance=CoverageProvenance.MAPPED_ALIAS,
                    raw_labels=[label],
                    source_evidence_ids=evidence_ids,
                )
            )

        return ledger
