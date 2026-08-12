"""Deterministic canonical-field mapping for provider onboarding (no LLM).

Maps a label/text observed on a provider's quote form to the canonical
insurance schema path. Unknown labels are reported as ``unmapped_field`` -
never guessed. This is the SAME deterministic vocabulary the mock site and
intake catalog use, so onboarding output is consistent with the rest of the
system. No applicant data is involved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from ...models.browser.config import FillStrategy, TransformKind
from ...models.intake.field_catalog import FieldSensitivity

_LABEL_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def normalize_label(label: str) -> str:
    """Lowercase, drop non-alphanumerics, collapse whitespace."""
    return _LABEL_NORMALIZE_RE.sub(" ", (label or "").lower()).strip()


@dataclass(frozen=True)
class CanonicalFieldSpec:
    """Deterministic mapping from a normalized label fragment to schema."""

    canonical_path: str
    control_type: str = "input"
    fill_strategy: FillStrategy = FillStrategy.TEXT
    transform: TransformKind = TransformKind.NONE
    sensitivity: FieldSensitivity = FieldSensitivity.PERSONAL
    required: bool = False
    collection_length: bool = False  # derived len() over a canonical collection
    option_map: tuple[tuple[str, str], ...] = ()


#: (required normalized tokens, spec) — a label matches when every token is
#: present in the normalized label. Ordered: more specific first.
_CURATED: list[tuple[tuple[str, ...], CanonicalFieldSpec]] = [
    (("legal name",), CanonicalFieldSpec(
        "applicant.identity.legal_name", sensitivity=FieldSensitivity.SENSITIVE)),
    (("preferred language",), CanonicalFieldSpec(
        "applicant.identity.preferred_language", control_type="select",
        fill_strategy=FillStrategy.SELECT, transform=TransformKind.ENUM_TO_LABEL,
        option_map=(("english", "English"), ("french", "French")))),
    (("postal code",), CanonicalFieldSpec("applicant.address.postal_code")),
    (("date", "birth"), CanonicalFieldSpec(
        "applicant.identity.date_of_birth", fill_strategy=FillStrategy.DATE,
        transform=TransformKind.ISO_DATE_TO_DEST, sensitivity=FieldSensitivity.SENSITIVE)),
    (("street address",), CanonicalFieldSpec(
        "applicant.address.street", sensitivity=FieldSensitivity.SENSITIVE)),
    (("vin",), CanonicalFieldSpec(
        "product_data.vehicles[0].identity.vin", sensitivity=FieldSensitivity.SENSITIVE)),
    (("model year",), CanonicalFieldSpec(
        "product_data.vehicles[0].identity.model_year", fill_strategy=FillStrategy.INTEGER,
        transform=TransformKind.INTEGER_TO_STRING)),
    (("annual", "kilometres"), CanonicalFieldSpec(
        "product_data.vehicles[0].use.annual_kilometres", fill_strategy=FillStrategy.INTEGER,
        transform=TransformKind.INTEGER_TO_STRING)),
    (("annual", "distance"), CanonicalFieldSpec(
        "product_data.vehicles[0].use.annual_kilometres", fill_strategy=FillStrategy.INTEGER,
        transform=TransformKind.INTEGER_TO_STRING)),
    (("winter tires",), CanonicalFieldSpec(
        "product_data.vehicles[0].risk.winter_tires", control_type="checkbox",
        fill_strategy=FillStrategy.CHECKBOX, transform=TransformKind.BOOL_TO_YES_NO)),
    (("carpool",), CanonicalFieldSpec(
        "product_data.vehicles[0].use.carpool", control_type="radio",
        fill_strategy=FillStrategy.RADIO, transform=TransformKind.BOOL_TO_YES_NO,
        option_map=(("True", "Yes"), ("False", "No")))),
    (("one way commute",), CanonicalFieldSpec(
        "product_data.vehicles[0].use.one_way_commute_distance_km", fill_strategy=FillStrategy.INTEGER,
        transform=TransformKind.INTEGER_TO_STRING)),
    (("commuting",), CanonicalFieldSpec(
        "product_data.vehicles[0].use.commuting", control_type="radio",
        fill_strategy=FillStrategy.RADIO, transform=TransformKind.BOOL_TO_YES_NO,
        option_map=(("True", "Yes"), ("False", "No")))),
    (("rideshare",), CanonicalFieldSpec(
        "product_data.vehicles[0].special_use.rideshare", control_type="radio",
        fill_strategy=FillStrategy.RADIO, transform=TransformKind.BOOL_TO_YES_NO,
        option_map=(("True", "Yes"), ("False", "No")))),
    (("rideshare", "hours"), CanonicalFieldSpec(
        "product_data.vehicles[0].use.rideshare_hours_per_week", fill_strategy=FillStrategy.INTEGER,
        transform=TransformKind.INTEGER_TO_STRING)),
    (("other household driver name",), CanonicalFieldSpec(
        "product_data.drivers[0].other_drivers[0].name", sensitivity=FieldSensitivity.SENSITIVE)),
    (("household driver",), CanonicalFieldSpec(
        "product_data.drivers[0].other_drivers[0].name", sensitivity=FieldSensitivity.SENSITIVE)),
    (("years at current address",), CanonicalFieldSpec(
        "applicant.address.years_at_current_address", fill_strategy=FillStrategy.INTEGER,
        transform=TransformKind.INTEGER_TO_STRING)),
    (("liability limit",), CanonicalFieldSpec(
        "product_data.coverage.third_party_liability.selected_limit", fill_strategy=FillStrategy.INTEGER,
        transform=TransformKind.INTEGER_TO_STRING)),
    (("number of vehicles",), CanonicalFieldSpec(
        "product_data.vehicles", collection_length=True)),
    (("number of drivers",), CanonicalFieldSpec(
        "product_data.drivers", collection_length=True)),
    (("vehicles",), CanonicalFieldSpec("product_data.vehicles", collection_length=True)),
    (("drivers",), CanonicalFieldSpec("product_data.drivers", collection_length=True)),
    (("email address",), CanonicalFieldSpec("applicant.contact.email_address")),
    (("phone number",), CanonicalFieldSpec(
        "applicant.contact.phone_number", sensitivity=FieldSensitivity.SENSITIVE)),
]


@dataclass(frozen=True)
class FieldMapping:
    """Result of mapping one observed form control to the canonical schema."""

    label: str
    canonical_path: Optional[str]
    control_type: str
    fill_strategy: FillStrategy
    transform: TransformKind
    sensitivity: FieldSensitivity
    required: bool
    collection_length: bool
    option_map: tuple[tuple[str, str], ...]
    mapped: bool
    reason: str = ""


def _spec_for_label(normalized: str) -> Optional[CanonicalFieldSpec]:
    if not normalized:
        return None
    for tokens, spec in _CURATED:
        if all(token in normalized for token in tokens):
            return spec
    return None


def map_label(label: str, control_type: str = "input") -> FieldMapping:
    """Map a single observed label to the canonical schema (or unmapped)."""
    normalized = normalize_label(label)
    spec = _spec_for_label(normalized)
    if spec is None:
        return FieldMapping(
            label=label, canonical_path=None, control_type=control_type,
            fill_strategy=FillStrategy.TEXT, transform=TransformKind.NONE,
            sensitivity=FieldSensitivity.PERSONAL, required=False,
            collection_length=False, option_map=(), mapped=False,
            reason="unmapped_field",
        )
    return FieldMapping(
        label=label, canonical_path=spec.canonical_path, control_type=spec.control_type,
        fill_strategy=spec.fill_strategy, transform=spec.transform,
        sensitivity=spec.sensitivity, required=spec.required,
        collection_length=spec.collection_length, option_map=spec.option_map,
        mapped=True,
    )


def map_labels(labels: list[tuple[str, str]]) -> tuple[list[FieldMapping], list[FieldMapping]]:
    """Map many ``(label, control_type)`` pairs -> (mapped, unmapped)."""
    mapped: list[FieldMapping] = []
    unmapped: list[FieldMapping] = []
    for label, control_type in labels:
        mapping = map_label(label, control_type)
        (mapped if mapping.mapped else unmapped).append(mapping)
    return mapped, unmapped


def catalog_derived_specs() -> list[CanonicalFieldSpec]:
    """Optional enrichment from the data-driven intake catalog (AUTO fields)."""
    try:
        from ...models.insurance.enums import InsuranceType
        from ...services.intake.catalog import IntakeFieldCatalog

        catalog = IntakeFieldCatalog()
        specs: list[CanonicalFieldSpec] = []
        for field in catalog.enabled(InsuranceType.AUTO):
            if not field.short_label:
                continue
            concrete = IntakeFieldCatalog.resolve_template(field.canonical_path_template)
            specs.append(
                CanonicalFieldSpec(
                    canonical_path=concrete,
                    control_type=_input_to_control(field.input_type.value),
                    fill_strategy=FillStrategy.TEXT,
                    transform=TransformKind.NONE,
                    sensitivity=field.sensitivity,
                    required=field.seed_required,
                )
            )
        return specs
    except Exception:  # pragma: no cover - defensive; curated map always works
        return []


def _input_to_control(input_type: str) -> str:
    if input_type in ("boolean",):
        return "checkbox"
    if input_type in ("single_select", "enum"):
        return "select"
    return "input"
