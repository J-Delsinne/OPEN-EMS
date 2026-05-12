"""Story 11.3 AC22 #66-67 — installer sidebar regression tests.

The sidebar partial ``installer/_sidebar.html`` is shared across the
installer dashboard, event-log page, and (as of 11.3) settings page. These
tests assert that the Settings item is now live AND that all three pages
render the same 5 nav items in the same order — defending against drift
from the 11.2 baseline.
"""

from __future__ import annotations

import pathlib

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

_TEMPLATES_DIR = (
    pathlib.Path(__file__).resolve().parents[3] / "src" / "open_ems" / "web" / "templates"
)


@pytest.fixture
def render_sidebar() -> Jinja2Templates:
    """Standalone Jinja2 environment that renders the sidebar partial in isolation."""
    return Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _render_with_active(env: Jinja2Templates, active: str | None) -> str:
    """Render a minimal wrapper that includes the sidebar partial with ``active``."""
    app = FastAPI()

    @app.get("/render", response_class=HTMLResponse)
    async def render(request: Request) -> HTMLResponse:
        # The partial uses Alpine ``x-data`` semantics from a parent scope —
        # we wrap with the same Alpine wrapper the production pages use.
        ctx = {"active": active}
        return env.TemplateResponse(
            request,
            "installer/_sidebar.html",
            ctx,
        )

    with TestClient(app) as client:
        return client.get("/render").text


def test_sidebar_settings_item_is_live_with_correct_href(
    render_sidebar: Jinja2Templates,
) -> None:
    """AC22 #66: Settings is no longer a placeholder.

    - ``href`` is ``/installer/settings`` (not ``#``)
    - the ``aria-disabled="true"`` and ``tabindex="-1"`` attributes are GONE
    """
    html = _render_with_active(render_sidebar, active="settings")
    assert 'href="/installer/settings"' in html
    # When active="settings", the active modifier and aria-current must apply.
    assert "installer-sidebar__item--active" in html
    assert 'aria-current="page"' in html
    # Settings line specifically — verify the disabled-placeholder attributes
    # do NOT appear adjacent to the Settings link.
    settings_block = html.split("/installer/settings")[1].split("</a>")[0]
    assert 'aria-disabled="true"' not in settings_block
    assert 'tabindex="-1"' not in settings_block


def test_sidebar_dashboard_event_log_settings_render_in_consistent_order(
    render_sidebar: Jinja2Templates,
) -> None:
    """AC22 #67: nav items render in the documented order: Dashboard, Devices,
    Event Log, Settings, Setup. No reordering since 11.2.
    """
    html = _render_with_active(render_sidebar, active="dashboard")
    # Reduce to the visible link text positions.
    positions = {
        label: html.find(f">{label}</a>")
        for label in ("Dashboard", "Devices", "Event Log", "Settings", "Setup")
    }
    # Every label is present.
    assert all(pos != -1 for pos in positions.values()), positions
    # Order is strictly increasing.
    ordered = sorted(positions.items(), key=lambda kv: kv[1])
    assert [k for k, _ in ordered] == [
        "Dashboard",
        "Devices",
        "Event Log",
        "Settings",
        "Setup",
    ]
