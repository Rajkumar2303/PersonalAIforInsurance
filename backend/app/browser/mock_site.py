"""Backward-compatibility re-export shim (Issue #8.5).

The reusable localhost mock quote site now lives in
``app/demo/mock_quote_site.py`` (production runtime never depends on
``tests/``). This module is a thin re-export so existing test/demo imports of
``app.browser.mock_site`` keep working unchanged.
"""

from __future__ import annotations

from ..demo.mock_quote_site import (  # noqa: F401  (re-export)
    MOCK_REGISTRY_ID,
    MockQuoteSite,
    build_mock_route_config,
    build_scenario_config,
    mock_scenario_url,
)

__all__ = [
    "MOCK_REGISTRY_ID",
    "MockQuoteSite",
    "build_mock_route_config",
    "build_scenario_config",
    "mock_scenario_url",
]
