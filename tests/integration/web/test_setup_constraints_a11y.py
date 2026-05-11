"""Accessibility placeholder for the Step-3 constraints page (Story 9.3 AC8).

Inherits the same deferred shape as Story 9.1 / 9.2 a11y placeholders. The
actual a11y properties (aria-describedby, role=alert, server-rendered
disabled state, ≥4.5:1 contrast, keyboard reachability) are exercised in
the unit + template-render tests today.
"""

from __future__ import annotations

import shutil

import pytest


@pytest.mark.skipif(shutil.which("npx") is None, reason="npx absent on host")
@pytest.mark.xfail(
    reason="axe-playwright wiring deferred — same as Story 9.1 AC10 / 9.2 AC8",
    strict=False,
)
def test_setup_constraints_axe_scan_clean() -> None:
    """When axe-playwright is wired this should render
    ``/installer/setup/constraints`` and run ``axe.run()`` against it,
    asserting zero violations of WCAG 2.1 AA.
    """
    raise AssertionError("axe-playwright integration not wired yet")
