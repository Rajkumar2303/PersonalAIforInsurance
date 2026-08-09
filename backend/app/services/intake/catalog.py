"""Data-driven intake field catalog service (Issue #5).

Loads every ``*.json`` field dataset from the catalog directory, validates each
``IntakeFieldDefinition`` with Pydantic, and exposes deterministic query and
template-resolution helpers. Question text/order/enabled state are editable via
catalog DATA, never agent logic.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Iterable, Optional

from pydantic import ValidationError

from ...core.config import BACKEND_ROOT, get_settings
from ...models.insurance.enums import InsuranceType
from ...models.intake.field_catalog import IntakeFieldDefinition, IntakePhase

logger = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_INDEXED_TEMPLATE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\[\{([A-Za-z_][A-Za-z0-9_]*)\}\]$")


class CatalogLoadError(RuntimeError):
    """Raised when intake catalog data is invalid or has duplicate field_ids."""


def default_catalog_dir() -> Path:
    """Resolve the intake catalog data directory (CWD-independent)."""
    settings = get_settings()
    if settings.intake_catalog_dir:
        return Path(settings.intake_catalog_dir)
    return BACKEND_ROOT / "data" / "intake"


class IntakeFieldCatalog:
    """Deterministic, data-driven field/question catalog."""

    def __init__(self, catalog_dir: Optional[Path] = None) -> None:
        self._catalog_dir = Path(catalog_dir) if catalog_dir else default_catalog_dir()
        self._fields: dict[str, IntakeFieldDefinition] = {}
        self._load_all()

    # --- loading ----------------------------------------------------

    def _load_all(self) -> None:
        self._fields = {}
        if not self._catalog_dir.exists():
            logger.warning(
                "intake catalog directory not found",
                extra={"workflow": "intake_catalog", "workflow_stage": "load", "status": "missing"},
            )
            return
        for path in sorted(self._catalog_dir.glob("*.json")):
            self._load_file(path)
        logger.info(
            "intake catalog loaded",
            extra={
                "workflow": "intake_catalog",
                "workflow_stage": "load",
                "status": "ok",
                "result_count": len(self._fields),
            },
        )

    def _load_file(self, path: Path) -> None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CatalogLoadError(f"failed to read catalog file {path.name}") from exc
        records = raw.get("fields", raw) if isinstance(raw, dict) else raw
        if not isinstance(records, list):
            raise CatalogLoadError(f"catalog file {path.name} must contain a list of fields")
        for item in records:
            try:
                field = IntakeFieldDefinition.model_validate(item)
            except ValidationError as exc:
                raise CatalogLoadError(f"invalid catalog record in {path.name}: {exc}") from exc
            if field.field_id in self._fields:
                raise CatalogLoadError(f"duplicate field_id {field.field_id!r} in {path.name}")
            self._fields[field.field_id] = field

    # --- queries -----------------------------------------------------

    def all(self) -> list[IntakeFieldDefinition]:
        return sorted(self._fields.values(), key=lambda f: (f.priority, f.field_id))

    def get(self, field_id: str) -> Optional[IntakeFieldDefinition]:
        return self._fields.get(field_id.strip())

    def for_product(self, product_type: InsuranceType) -> list[IntakeFieldDefinition]:
        return [f for f in self.all() if f.product_type is product_type]

    def enabled(self, product_type: InsuranceType) -> list[IntakeFieldDefinition]:
        return [f for f in self.for_product(product_type) if f.enabled]

    def for_phase(self, product_type: InsuranceType, phase: IntakePhase) -> list[IntakeFieldDefinition]:
        return [f for f in self.enabled(product_type) if f.intake_phase is phase]

    def seed_fields(self, product_type: InsuranceType) -> list[IntakeFieldDefinition]:
        return [f for f in self.enabled(product_type) if f.seed_required]

    def item_fields(self, product_type: InsuranceType) -> list[IntakeFieldDefinition]:
        return [f for f in self.enabled(product_type) if f.item_unit]

    def unit_fields(self, product_type: InsuranceType, item_unit: str) -> list[IntakeFieldDefinition]:
        return [
            f
            for f in self.enabled(product_type)
            if f.item_unit == item_unit and f.item_unit_required
        ]

    # --- template resolution ------------------------------------------

    @staticmethod
    def resolve_template(template: str, index_defaults: Optional[dict[str, int]] = None) -> str:
        """Resolve ``{vehicle_index}`` placeholders to concrete indexes.

        Defaults every placeholder to 0 (single-item intake in Issue #5;
        multi-item iteration is documented future work).
        """
        defaults = index_defaults or {}
        return _PLACEHOLDER_RE.sub(lambda m: str(defaults.get(m.group(1), 0)), template)

    @staticmethod
    def container_path(definition: IntakeFieldDefinition) -> Optional[str]:
        """Return the list container path for an item field.

        For ``product_data.vehicles[{vehicle_index}].identity.vin`` this is
        ``product_data.vehicles`` (the list the engine materializes).
        """
        segments = definition.canonical_path_template.split(".")
        for index, segment in enumerate(segments):
            match = _INDEXED_TEMPLATE_RE.fullmatch(segment)
            if match:
                return ".".join(segments[:index] + [match.group(1)])
        return None

    def by_path(self, concrete_path: str) -> Optional[IntakeFieldDefinition]:
        """Reverse lookup: concrete canonical path -> catalog definition."""
        for field in self.all():
            if self.resolve_template(field.canonical_path_template) == concrete_path:
                return field
        return None

    def resolve_definition(self, field: IntakeFieldDefinition) -> str:
        """Resolve one definition's template to a concrete canonical path."""
        return self.resolve_template(field.canonical_path_template)

    def definitions_for_paths(self, paths: Iterable[str]) -> list[IntakeFieldDefinition]:
        """Resolve concrete paths to catalog definitions (index-0 form)."""
        resolved = []
        for path in paths:
            field = self.by_path(path)
            if field is not None:
                resolved.append(field)
        return resolved

    # --- trace metadata ------------------------------------------------

    def trace_metadata(self) -> dict[str, object]:
        """Safe, non-sensitive catalog metadata (counts only)."""
        auto = self.for_product(InsuranceType.AUTO)
        return {
            "field_count": len(auto),
            "enabled_count": sum(1 for f in auto if f.enabled),
            "starter_count": len(self.for_phase(InsuranceType.AUTO, IntakePhase.STARTER)),
            "route_specific_count": len(self.for_phase(InsuranceType.AUTO, IntakePhase.ROUTE_SPECIFIC)),
            "sensitive_late_count": len(self.for_phase(InsuranceType.AUTO, IntakePhase.SENSITIVE_LATE)),
        }
