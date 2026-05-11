"""Accessibility placeholder for the Step-2 roles page (Story 9.2 AC8).

Inherits the same shape as Story 9.1's deferred a11y placeholder: the
``axe-playwright`` wiring is deferred (skip when npx absent), and the inner
test is ``xfail`` until the CI sweep that lights up axe runs. The
specific accessibility properties (aria-label, aria-live, role=alert,
server-rendered disabled state, ≥4.5:1 contrast, keyboard navigation) are
exercised in the unit + template-render tests today.
"""

from __future__ import annotations

import shutil

import pytest


@pytest.mark.skipif(shutil.which("npx") is None, reason="npx absent on host")
@pytest.mark.xfail(
    reason="axe-playwright wiring deferred — same as Story 9.1 AC10",
    strict=False,
)
def test_setup_roles_axe_scan_clean() -> None:
    """When axe-playwright is wired, this should render the page and run
    ``axe.run()`` against it, asserting zero violations of WCAG 2.1 AA. The
    fixture-side renderer + axe runner are deferred to the CI sweep that
    Story 9.1 logged."""
    raise AssertionError("axe-playwright integration not wired yet")
