"""Demo/mock runtime package (Issue #8.5 integration checkpoint).

Contains the reusable localhost mock quote site and the canonical synthetic
demo persona, plus the mode-scoped demo runtime that builds a fully-isolated
registry / route-requirements / rate-sources / browser-config overlay used ONLY
for ``execution_mode=mock``.

Production/application runtime code never imports from ``tests/``; tests and
demos may import the shared implementation from here.
"""
