"""Provider onboarding service package (post-Issue #14 phase).

Deterministic, SAFE provider onboarding: registry-first, URL discovery,
no-PII inspection, DRAFT route-config generation, and human-gated approval.
"""

from .canonical import FieldMapping, map_label, map_labels, normalize_label
from .draft import (
    OnboardingError,
    build_draft_config,
    build_report,
    derive_allowed_hosts,
    mark_unmapped_fields,
    validate_candidate_url,
)
from .repository import (
    default_drafts_dir,
    default_live_dir,
    default_registry_path,
    load_draft,
    mark_registry_verified,
    promote_draft,
    save_draft,
)
from .inspection import InspectionResult, inspect_page

__all__ = [
    "FieldMapping",
    "InspectionResult",
    "OnboardingError",
    "build_draft_config",
    "build_report",
    "default_drafts_dir",
    "default_live_dir",
    "default_registry_path",
    "derive_allowed_hosts",
    "inspect_page",
    "load_draft",
    "map_label",
    "map_labels",
    "mark_registry_verified",
    "mark_unmapped_fields",
    "normalize_label",
    "promote_draft",
    "save_draft",
    "validate_candidate_url",
]
