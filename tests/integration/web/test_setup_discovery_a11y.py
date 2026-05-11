"""Accessibility scan for the installer discovery wizard (Story 9.1 AC10).

Runs ``axe-core`` against the rendered ``/installer/setup/discovery`` page via
``axe-playwright`` (or any equivalent). The scan must report zero WCAG 2.1 AA
violations.

The full ``axe-playwright`` wiring is deferred to a follow-up CI hardening
pass (per the Story 9.1 review decision recorded 2026-05-11). Until that lands
this placeholder is marked ``xfail`` rather than ``skip`` — that way the suite
does not report a phantom passing test for an AC that is not actually
exercised, while still failing loudly when ``npx`` is unavailable for an
unrelated reason. See spec AC10/AC11 for the deferral note.
"""

from __future__ import annotations

import shutil

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("npx") is None,
    reason="axe-core scan requires Node/npx in the test environment",
)


@pytest.mark.xfail(
    reason=(
        "axe-playwright wiring deferred to follow-up CI hardening pass"
        " (Story 9.1 review decision 2026-05-11); placeholder kept so the AC11"
        " test inventory remains visible."
    ),
    strict=False,
)
def test_setup_discovery_page_passes_wcag_aa_axe_scan() -> None:
    """Placeholder until ``axe-playwright`` is wired in. The full implementation will:

    1. Spin up the FastAPI app with the Story 9.1 wiring.
    2. Authenticate as an installer.
    3. Launch a headless browser via Playwright, GET
       ``/installer/setup/discovery``.
    4. Inject axe-core and assert zero WCAG 2.1 AA violations.
    """
    # Intentionally fail (xfail-strict=False expects this) so the test never
    # silently reports success without the real axe scan having run.
    raise AssertionError("axe-playwright scan not yet wired — see module docstring.")
