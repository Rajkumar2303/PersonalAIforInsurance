"""Provider-onboarding draft repository + human-approval promotion.

Drafts live in ``backend/data/browser_routes/drafts/`` (never the live dir).
Promotion requires an existing draft + explicit human confirmation, then:
- writes the approved config into the LIVE browser route config dir, and
- marks the registry route ``verified`` (with ``last_verified_at``).

No applicant data is ever sent or stored by this module.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Optional

from ...core.config import BACKEND_ROOT, get_settings
from ...models.browser.config import BrowserRouteConfig
from ...models.registry import MarketRegistryEntry, RegistryStatus
from .draft import OnboardingError

#: Drafts are kept OUT of the live config dir so a draft can never be loaded
#: as an approved route.
DRAFT_SUBDIR = "drafts"


def default_drafts_dir() -> Path:
    """backend/data/browser_routes/drafts (dev onboarding artifacts)."""
    return BACKEND_ROOT / "data" / "browser_routes" / DRAFT_SUBDIR


def default_live_dir() -> Path:
    settings = get_settings()
    if settings.browser_route_config_dir:
        return Path(settings.browser_route_config_dir)
    return BACKEND_ROOT / "data" / "browser" / "routes"


def default_registry_path() -> Path:
    settings = get_settings()
    if settings.market_registry_dir:
        return Path(settings.market_registry_dir) / "auto.json"
    return BACKEND_ROOT / "data" / "market_registry" / "auto.json"


def draft_path(drafts_dir: Path, registry_id: str) -> Path:
    return drafts_dir / f"{registry_id}.json"


def report_path(drafts_dir: Path, registry_id: str) -> Path:
    return drafts_dir / f"{registry_id}.report.json"


def save_draft(
    drafts_dir: Path,
    registry_id: str,
    config: BrowserRouteConfig,
    report: dict,
) -> Path:
    """Write a DRAFT config + report. Never touches the live dir or registry."""
    drafts_dir.mkdir(parents=True, exist_ok=True)
    config_path = draft_path(drafts_dir, registry_id)
    config_path.write_text(
        json.dumps(config.model_dump(mode="json"), indent=2), encoding="utf-8"
    )
    report_path(drafts_dir, registry_id).write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    return config_path


def load_draft(drafts_dir: Path, registry_id: str) -> Optional[BrowserRouteConfig]:
    path = draft_path(drafts_dir, registry_id)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return BrowserRouteConfig.model_validate(data)


def promote_draft(
    *,
    drafts_dir: Path,
    live_dir: Path,
    registry_path: Path,
    registry_id: str,
    confirmed: bool = False,
    source_url: Optional[str] = None,
    evidence_artifact: Optional[str] = None,
) -> dict:
    """Human-gated promotion: draft -> live config + registry verified.

    Raises :class:`OnboardingError` if no draft exists or confirmation is
    missing. Requires explicit ``confirmed=True`` (interactive CLI prompt or
    ``--yes``) - approval is never automatic.
    """
    draft = load_draft(drafts_dir, registry_id)
    if draft is None:
        raise OnboardingError(
            f"no draft exists for {registry_id!r} (run onboarding first, then re-run with --approve)"
        )
    if draft.last_verified_at is not None:
        raise OnboardingError(f"draft for {registry_id!r} is already approved")
    if not confirmed:
        raise OnboardingError("approval requires explicit confirmation (--yes)")

    now = dt.datetime.now(dt.timezone.utc)
    approved = draft.model_copy(
        update={
            "last_verified_at": now,
            "automation_notes": (draft.automation_notes or "") +
                                f" APPROVED by human on {now.isoformat()}.",
        }
    )
    live_dir.mkdir(parents=True, exist_ok=True)
    live_path = live_dir / f"{registry_id}.json"
    live_path.write_text(
        json.dumps(approved.model_dump(mode="json"), indent=2), encoding="utf-8"
    )
    mark_registry_verified(
        registry_path=registry_path,
        registry_id=registry_id,
        verified_at=now,
        source_url=source_url,
        evidence_artifact=evidence_artifact,
    )
    return {"live_config_path": str(live_path), "verified_at": now.isoformat()}


def mark_registry_verified(
    *,
    registry_path: Path,
    registry_id: str,
    verified_at: dt.datetime,
    source_url: Optional[str] = None,
    evidence_artifact: Optional[str] = None,
) -> MarketRegistryEntry:
    """Update one registry record to ``verified`` (preserving other fields)."""
    data = json.loads(registry_path.read_text(encoding="utf-8"))
    comment = data.get("_comment")
    records = data.get("records")
    if not isinstance(records, list):
        raise OnboardingError("registry file has no records list")

    target = next((r for r in records if r.get("registry_id") == registry_id), None)
    if target is None:
        raise OnboardingError(f"registry_id {registry_id!r} not found in {registry_path}")

    target["status"] = RegistryStatus.VERIFIED.value
    target["last_verified_at"] = verified_at.isoformat()
    if source_url:
        target["source_url"] = source_url
    if evidence_artifact:
        target["evidence_artifact"] = evidence_artifact
    # Freshness/consistency: a verified record must validate.
    updated = MarketRegistryEntry.model_validate(target)

    out: dict = {"records": records}
    if comment is not None:
        out["_comment"] = comment
    # Deterministic, stable JSON output.
    registry_path.write_text(
        json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return updated
