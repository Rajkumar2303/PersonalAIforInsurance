"""Deterministic broker-question interpreter (Issue #9, Prompt 1).

Maps normalized broker wording -> structured ``BrokerQuestion`` WITHOUT an LLM.
The mapping is data-driven: a localized alias table plus the Issue #5 intake
catalog (so adding/renaming a field is a one-row change, never an engine
change). Unknown wording is mapped to ``UNKNOWN`` - the engine then pauses for
manual mapping instead of guessing.

PRIVACY: ``raw_safe_text`` is generic wording (e.g. "postal code"), never
applicant data and never a raw transcript.
"""

from __future__ import annotations

import re
from typing import Optional, Protocol, runtime_checkable

from ...models.voice import BrokerQuestion, BrokerQuestionKind
from ..intake.catalog import IntakeFieldCatalog

# Default alias table: normalized broker wording -> canonical path.
# Keys are lowercased; punctuation/whitespace is normalized before lookup.
DEFAULT_ALIASES: dict[str, str] = {
    "postal code": "applicant.address.postal_code",
    "postal": "applicant.address.postal_code",
    "zip code": "applicant.address.postal_code",
    "home address": "applicant.address.street",
    "street address": "applicant.address.street",
    "annual mileage": "product_data.vehicles[0].use.annual_kilometres",
    "annual kilometres": "product_data.vehicles[0].use.annual_kilometres",
    "kilometres per year": "product_data.vehicles[0].use.annual_kilometres",
    "how many kilometres": "product_data.vehicles[0].use.annual_kilometres",
    "year of vehicle": "product_data.vehicles[0].specs.year",
    "vehicle year": "product_data.vehicles[0].specs.year",
    "number of vehicles": "product_data.vehicles",
    "how many vehicles": "product_data.vehicles",
    "number of drivers": "product_data.drivers",
    "how many drivers": "product_data.drivers",
}

# Heuristic phrase -> question kind for non-field statements.
_PHRASE_KINDS: tuple[tuple[str, BrokerQuestionKind], ...] = (
    ("confirm your identity", BrokerQuestionKind.IDENTITY_CHECKPOINT),
    ("verify your identity", BrokerQuestionKind.IDENTITY_CHECKPOINT),
    ("confirm identity", BrokerQuestionKind.IDENTITY_CHECKPOINT),
    ("licence number", BrokerQuestionKind.IDENTITY_CHECKPOINT),
    ("declaration", BrokerQuestionKind.DECLARATION),
    ("confirm all statements", BrokerQuestionKind.DECLARATION),
    ("advise", BrokerQuestionKind.ADVICE_REQUEST),
    ("recommend", BrokerQuestionKind.ADVICE_REQUEST),
    ("should the customer", BrokerQuestionKind.ADVICE_REQUEST),
    ("household driver", BrokerQuestionKind.HOUSEHOLD_DRIVER),
    ("household member", BrokerQuestionKind.HOUSEHOLD_DRIVER),
    ("consent to share", BrokerQuestionKind.CONSENT_EXPANSION),
    ("consent to disclose", BrokerQuestionKind.CONSENT_EXPANSION),
    ("call you back", BrokerQuestionKind.CALLBACK_REQUEST),
    ("callback", BrokerQuestionKind.CALLBACK_REQUEST),
    ("annual premium", BrokerQuestionKind.QUOTE_DISCLOSURE),
    ("your quote", BrokerQuestionKind.QUOTE_DISCLOSURE),
    ("quoted price", BrokerQuestionKind.QUOTE_DISCLOSURE),
    ("estimate", BrokerQuestionKind.ESTIMATE_DISCLOSURE),
    ("cannot offer coverage", BrokerQuestionKind.INELIGIBILITY),
    ("not eligible", BrokerQuestionKind.INELIGIBILITY),
    ("affinity group", BrokerQuestionKind.AFFINITY_RESTRICTION),
    ("specialty market", BrokerQuestionKind.SPECIALTY_ONLY),
    ("not writing", BrokerQuestionKind.NOT_CURRENTLY_WRITING),
    ("no longer accepting", BrokerQuestionKind.NOT_CURRENTLY_WRITING),
    ("need to speak with the customer", BrokerQuestionKind.APPLICANT_REQUIRED),
    ("customer needs to confirm", BrokerQuestionKind.APPLICANT_REQUIRED),
    ("manual review", BrokerQuestionKind.MANUAL_REVIEW),
    ("cannot process automatically", BrokerQuestionKind.MANUAL_REVIEW),
    ("thank you for calling", BrokerQuestionKind.COMPLETED_WITHOUT_QUOTE),
    ("goodbye", BrokerQuestionKind.COMPLETED_WITHOUT_QUOTE),
)


def _normalize(text: str) -> str:
    """Lowercase + collapse punctuation/whitespace for alias matching."""
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower()).strip()


@runtime_checkable
class BrokerQuestionInterpreter(Protocol):
    """Maps raw safe broker wording to a structured BrokerQuestion."""

    def interpret(self, raw_safe_text: str) -> BrokerQuestion: ...


class DeterministicBrokerQuestionInterpreter:
    """Rule-based interpreter over an alias table + the Issue #5 catalog.

    ``aliases`` may be extended by callers (dynamic-field tests) without any
    engine change. If a wording maps to an alias whose canonical path is not
    present in the catalog, the result is UNKNOWN (never guessed).
    """

    def __init__(
        self,
        catalog: Optional[IntakeFieldCatalog] = None,
        aliases: Optional[dict[str, str]] = None,
    ) -> None:
        self._catalog = catalog or IntakeFieldCatalog()
        self._aliases = {
            _normalize(k): v for k, v in {**DEFAULT_ALIASES, **(aliases or {})}.items()
        }

    # -- protocol ------------------------------------------------------

    def interpret(self, raw_safe_text: str) -> BrokerQuestion:
        text = _normalize(raw_safe_text or "")

        # Checkpoint / statement phrases first (highest priority).
        for phrase, kind in _PHRASE_KINDS:
            if phrase in text:
                return BrokerQuestion(
                    kind=kind,
                    canonical_path=None,
                    raw_safe_text=raw_safe_text,
                    is_identity_checkpoint=kind is BrokerQuestionKind.IDENTITY_CHECKPOINT,
                    is_advice_request=kind is BrokerQuestionKind.ADVICE_REQUEST,
                    is_household_driver=kind is BrokerQuestionKind.HOUSEHOLD_DRIVER,
                    requires_applicant=kind is BrokerQuestionKind.APPLICANT_REQUIRED,
                    mapping_confidence=0.9,
                    safe_context={"mapped_by": "phrase"},
                )

        # Alias -> canonical path.
        canonical = self._aliases.get(text)
        if canonical is None:
            # Try longest-substring alias match (e.g. "your postal code please").
            best = None
            for key, path in self._aliases.items():
                if key and key in text:
                    best = path
                    break
            canonical = best

        if canonical is None:
            return BrokerQuestion(
                kind=BrokerQuestionKind.UNKNOWN,
                canonical_path=None,
                raw_safe_text=raw_safe_text,
                mapping_confidence=0.0,
                safe_context={"mapped_by": "none"},
            )

        # Confirm the field exists in the Issue #5 catalog (except the two
        # collection containers, whose counts are derived by Issue #5).
        is_collection = canonical in ("product_data.vehicles", "product_data.drivers")
        if not is_collection:
            field = self._catalog.by_path(canonical)
            if field is None:
                return BrokerQuestion(
                    kind=BrokerQuestionKind.UNKNOWN,
                    canonical_path=None,
                    raw_safe_text=raw_safe_text,
                    mapping_confidence=0.0,
                    safe_context={"mapped_by": "alias_missing_in_catalog"},
                )

        return BrokerQuestion(
            kind=BrokerQuestionKind.COLLECTION_LENGTH if is_collection else BrokerQuestionKind.CANONICAL_FIELD,
            canonical_path=canonical,
            raw_safe_text=raw_safe_text,
            mapping_confidence=1.0,
            safe_context={"mapped_by": "alias"},
        )

    # -- helpers --------------------------------------------------------

    @staticmethod
    def _normalize_collection(path: str) -> str:
        if "[0]" in path:
            return path.split("[0]")[0]
        return path

    def add_alias(self, wording: str, canonical_path: str) -> None:
        """Extend the alias table at runtime (dynamic-field tests)."""
        self._aliases[_normalize(wording)] = canonical_path
