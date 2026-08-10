"""Data-driven browser route-config loader (Issue #7).

Route configs are DATA: ``backend/data/browser/routes/<registry_id>.json``
(overridable via ``browser_route_config_dir``). Each config is validated with
Pydantic and then merged with the generic site adapter's defaults so common
workflows work out of the box while per-route overrides stay local to the
route's config file.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from pydantic import ValidationError

from ..core.config import BACKEND_ROOT, get_settings
from ..models.browser.config import BrowserRouteConfig

logger = logging.getLogger(__name__)


class RouteConfigLoadError(RuntimeError):
    """Raised when a browser route config is invalid or missing."""


def default_route_config_dir() -> Path:
    """Resolve the browser route-config data directory (CWD-independent)."""
    settings = get_settings()
    if settings.browser_route_config_dir:
        return Path(settings.browser_route_config_dir)
    return BACKEND_ROOT / "data" / "browser" / "routes"


class BrowserRouteConfigLoader:
    """Loads and validates one ``BrowserRouteConfig`` per registry id."""

    def __init__(self, config_dir: Optional[Path] = None) -> None:
        self._dir = Path(config_dir) if config_dir else default_route_config_dir()

    def load(self, registry_id: str) -> BrowserRouteConfig:
        """Load the route config for a registry id, or raise."""
        path = self._dir / f"{registry_id}.json"
        if not path.exists():
            raise RouteConfigLoadError(f"no browser route config for {registry_id!r}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RouteConfigLoadError(f"failed to read browser route config {path.name}") from exc
        try:
            return BrowserRouteConfig.model_validate(raw)
        except ValidationError as exc:
            raise RouteConfigLoadError(f"invalid browser route config {path.name}: {exc}") from exc

    def load_or_none(self, registry_id: str) -> Optional[BrowserRouteConfig]:
        try:
            return self.load(registry_id)
        except RouteConfigLoadError:
            return None
