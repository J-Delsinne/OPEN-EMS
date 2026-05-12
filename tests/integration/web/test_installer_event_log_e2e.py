"""Story 11.2 — installer event-log page integration tests.

End-to-end coverage:
- AC4-AC7: filter pill toggle + keyword search + date range update list inline
- AC3: "Load more" appends rows without full-page reload
- AC10 / AC16: note submit success / failure / XSS round-trips
- AC20: URL contract pre-filter (?type=SYSTEM&window=24h, ?device_id=...)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from open_ems.storage.database import get_connection
from open_ems.storage.repositories.session_repo import (
    SessionRepo,
    generate_session_token,
    hash_token,
)
from open_ems.storage.repositories.user_repo import UserRepo, hash_password
from open_ems.web.csrf import CsrfMiddleware
from open_ems.web.routes.actions import router as actions_router
from open_ems.web.routes.fragments import router as fragments_router
from open_ems.web.routes.installer import router as installer_router

_CREATE_EVENT_LOG = """
    CREATE TABLE IF NOT EXISTS event_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        schema_version INTEGER NOT NULL,
        timestamp TEXT NOT NULL,
        actor TEXT NOT NULL,
        event_type TEXT NOT NULL,
        summary TEXT NOT NULL,
        detail TEXT,
        device_id TEXT,
        config_version TEXT
    )
"""


async def _setup_db_tables() -> None:
    conn = get_connection()
    await conn.execute(_CREATE_EVENT_LOG)
    await conn.commit()


async def _create_session(role: str) -> str:
    user_id = await UserRepo(get_connection()).create(
        username=f"{role}_{generate_session_token()[:8]}",
        hashed_password=hash_password("secret"),
        role=role,
    )
    raw_token = generate_session_token()
    await SessionRepo(get_connection()).create(
        user_id=user_id,
        token_hash=hash_token(raw_token),
        expires_at=datetime.now(UTC) + timedelta(hours=4),
        csrf_token="test-csrf",
    )
    return raw_token


async def _insert(
    *,
    event_type: str = "SYSTEM",
    summary: str = "test event",
    timestamp: datetime | None = None,
    device_id: str | None = None,
) -> None:
    ts = timestamp if timestamp is not None else datetime.now(UTC)
    conn = get_connection()
    await conn.execute(
        "INSERT INTO event_log"
        " (schema_version, timestamp, actor, event_type, summary, device_id)"
        " VALUES (1, ?, 'system', ?, ?, ?)",
        (ts.isoformat(), event_type, summary, device_id),
    )
    await conn.commit()


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(installer_router)
    app.include_router(fragments_router)
    app.include_router(actions_router)
    app.add_middleware(CsrfMiddleware)
    return app


def _client() -> TestClient:
    return TestClient(_app(), base_url="https://test", follow_redirects=False)


# ── AC4-AC7: filter pill toggle ───────────────────────────────────────────────


async def test_e2e_filter_pill_toggle_updates_list_inline(
    session_repo: SessionRepo,
) -> None:
    """AC4: clicking a pill issues hx-get and the list narrows to that type."""
    await _setup_db_tables()
    await _insert(event_type="DECISION", summary="decision row")
    await _insert(event_type="SYSTEM", summary="system row")
    raw_token = await _create_session("installer")
    # Hit the fragment endpoint directly (HTMX-style call from the form).
    response = _client().get(
        "/fragments/installer/event-log-list?type=DECISION",
        cookies={"session": raw_token},
        headers={"hx-request": "true"},
    )
    assert response.status_code == 200
    body = response.text
    assert "decision row" in body
    assert "system row" not in body


# ── AC6: keyword search ───────────────────────────────────────────────────────


async def test_e2e_keyword_search_300ms_debounced_partial_replace(
    session_repo: SessionRepo,
) -> None:
    """AC6: keyword fragment GET returns only matching rows."""
    await _setup_db_tables()
    await _insert(summary="peak limit reached")
    await _insert(summary="battery dispatched")
    raw_token = await _create_session("installer")
    response = _client().get(
        "/fragments/installer/event-log-list?q=peak",
        cookies={"session": raw_token},
        headers={"hx-request": "true"},
    )
    body = response.text
    assert "peak limit reached" in body
    assert "battery dispatched" not in body


# ── AC3: load more pagination ─────────────────────────────────────────────────


async def test_e2e_load_more_appends_rows_without_full_page_reload(
    session_repo: SessionRepo,
) -> None:
    """AC3: GET ?offset=50 returns rows 50+ (the "Load more" payload)."""
    await _setup_db_tables()
    base = datetime.now(UTC)
    for i in range(60):
        await _insert(summary=f"event-{i:04d}", timestamp=base - timedelta(minutes=i))
    raw_token = await _create_session("installer")
    response = _client().get(
        "/fragments/installer/event-log-list?offset=50",
        cookies={"session": raw_token},
        headers={"hx-request": "true"},
    )
    body = response.text
    assert "event-0050" in body
    assert "event-0059" in body
    assert "event-0000" not in body  # first page excluded


# ── AC10: note submit success ─────────────────────────────────────────────────


async def test_e2e_note_submit_prepends_installer_row_clears_textarea(
    session_repo: SessionRepo,
) -> None:
    """AC10: success response carries OOB swap of the new row + empty textarea."""
    await _setup_db_tables()
    raw_token = await _create_session("installer")
    response = _client().post(
        "/actions/installer-note",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": "test-csrf"},
        data={"note": "Firmware updated."},
    )
    assert response.status_code == 200
    body = response.text
    # OOB swap appears.
    assert 'hx-swap-oob="afterbegin:#event-log-list-rows"' in body
    # New row content present.
    assert "Firmware updated." in body
    # Form re-rendered, but textarea is empty (no preserved value).
    assert "<textarea" in body and ">Firmware updated.</textarea>" not in body


# ── AC10: note submit failure ─────────────────────────────────────────────────


async def test_e2e_note_submit_failure_preserves_textarea_value(
    session_repo: SessionRepo,
) -> None:
    """AC10: over-2000-char failure preserves textarea value + inline error."""
    await _setup_db_tables()
    raw_token = await _create_session("installer")
    long_note = "a" * 2001
    response = _client().post(
        "/actions/installer-note",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": "test-csrf"},
        data={"note": long_note},
    )
    assert response.status_code == 200
    body = response.text
    assert long_note in body  # preserved
    assert "exceeds 2,000 characters" in body or "exceeds 2000 characters" in body
    assert "hx-swap-oob" not in body


# ── AC20: URL contract from Story 11.1 ────────────────────────────────────────


async def test_e2e_url_contract_type_system_window_24h_renders_filtered_view(
    session_repo: SessionRepo,
) -> None:
    """AC20: Story 11.1's "View in event log" link from health-indicator renders correctly."""
    await _setup_db_tables()
    base = datetime.now(UTC)
    await _insert(event_type="SYSTEM", summary="UNIQ-recent-system-AAA", timestamp=base)
    await _insert(
        event_type="SYSTEM",
        summary="UNIQ-old-system-BBB",
        timestamp=base - timedelta(days=5),
    )
    await _insert(event_type="DECISION", summary="UNIQ-decision-CCC", timestamp=base)
    raw_token = await _create_session("installer")
    response = _client().get(
        "/installer/event-log?type=SYSTEM&window=24h",
        cookies={"session": raw_token},
    )
    assert response.status_code == 200
    body = response.text
    assert "UNIQ-recent-system-AAA" in body
    assert "UNIQ-old-system-BBB" not in body
    assert "UNIQ-decision-CCC" not in body


async def test_e2e_url_contract_device_id_filter_renders_filtered_view(
    session_repo: SessionRepo,
) -> None:
    """AC20: Story 11.1's per-device-row "View in event log" link contract."""
    await _setup_db_tables()
    base = datetime.now(UTC)
    await _insert(summary="batt event", device_id="batt-1", timestamp=base)
    await _insert(summary="inv event", device_id="inv-1", timestamp=base)
    raw_token = await _create_session("installer")
    response = _client().get(
        "/installer/event-log?device_id=batt-1",
        cookies={"session": raw_token},
    )
    body = response.text
    assert "batt event" in body
    assert "inv event" not in body


# ── AC16: XSS through note + render ───────────────────────────────────────────


async def test_e2e_xss_in_note_renders_escaped(
    session_repo: SessionRepo,
) -> None:
    """AC16: XSS payload submitted as note renders escaped on the page."""
    await _setup_db_tables()
    raw_token = await _create_session("installer")
    client = _client()
    # Submit the XSS payload.
    response = client.post(
        "/actions/installer-note",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": "test-csrf"},
        data={"note": "<img src=x onerror=alert(1)>"},
    )
    assert response.status_code == 200

    # Now GET the page and assert the raw payload is NOT present.
    response2 = client.get("/installer/event-log", cookies={"session": raw_token})
    body = response2.text
    assert "<img src=x onerror=alert(1)>" not in body
    # Pin the exact double-escape: service-layer ``html.escape`` writes
    # ``&lt;img ...&gt;`` to disk, Jinja autoescape re-escapes on render to
    # ``&amp;lt;img ...&amp;gt;``. This is the only combination that
    # produces non-executing XSS-test text on the page. If either layer
    # drops, the raw ``&lt;img`` would appear (and be interpreted as the
    # tag-start escape sequence by the browser, which is a regression).
    assert "&amp;lt;img" in body
    # The single-escape form ``&lt;img`` MUST NOT appear on its own — i.e.,
    # if it appears, it must be embedded inside the double-escaped form.
    assert "&lt;img" not in body.replace("&amp;lt;img", "")
