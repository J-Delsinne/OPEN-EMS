"""Placeholder a11y test for the installer handoff guide page (Story 9.5 AC5).

Mirrors the deferred-CI-hardening pattern used by Story 9.1 AC10 / 9.2 AC8 /
9.3 AC8 / 9.4 AC9: marked ``pytest.mark.xfail`` until axe-playwright wiring
is prioritised across the wizard. The outer ``skipif(npx absent)`` guard
follows the same pattern.
"""

from __future__ import annotations

import shutil

import pytest

pytestmark = [
    pytest.mark.skipif(shutil.which("npx") is None, reason="npx absent — axe-core scan skipped"),
    pytest.mark.xfail(reason="axe-playwright wiring deferred", strict=False),
]


def test_handoff_guide_page_passes_axe_core() -> None:
    """Placeholder — once axe-playwright is wired the body asserts zero violations
    on /installer/handoff/guide (the standalone print page)."""
    raise AssertionError("axe-playwright not yet wired for the handoff guide page")
