"""Story 11.2 — installer event-log fragment + note-POST tests.

Covers:
- AC3 / AC6: /fragments/installer/event-log-list partial render + filter
- AC10 / AC16: POST /actions/installer-note success + failure paths
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from open_ems.storage.database import get_connection
from open_ems.storage.repositories.session_repo import (
    SessionRepo,
    generate_session_token,
    hash_token,
)
from open_ems.storage.repositories.user_repo import UserRepo, hash_password
from open_ems.web.routes.actions import router as actions_router
from open_ems.web.routes.fragments import router as fragments_router


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


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(fragments_router)
    app.include_router(actions_router)
    return app


def _client() -> TestClient:
    return TestClient(_app(), base_url="https://test", follow_redirects=False)


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


async def _ensure_event_log() -> None:
    conn = get_connection()
    await conn.execute(_CREATE_EVENT_LOG)
    await conn.commit()


async def _insert(
    *,
    event_type: str = "SYSTEM",
    timestamp: datetime | None = None,
    summary: str = "test event",
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


# ── T7 — fragment route ───────────────────────────────────────────────────────


async def test_event_log_list_fragment_paginates_via_offset_param(
    session_repo: SessionRepo,
) -> None:
    """AC3: offset=N returns rows N..N+50."""
    await _ensure_event_log()
    base = datetime.now(UTC)
    for i in range(60):
        await _insert(summary=f"row-{i:04d}", timestamp=base - timedelta(minutes=i))
    raw_token = await _create_session("installer")
    response = _client().get(
        "/fragments/installer/event-log-list?offset=50",
        cookies={"session": raw_token},
    )
    assert response.status_code == 200
    body = response.text
    # Rows 50-59 expected; rows 0-49 not.
    assert "row-0050" in body
    assert "row-0059" in body
    assert "row-0000" not in body
    assert "row-0049" not in body
    # No "Load more" — only 10 rows remained after the offset.
    assert "installer-event-log-load-more" not in body


async def test_event_log_list_fragment_preserves_filter_in_load_more_link(
    session_repo: SessionRepo,
) -> None:
    """AC3 + AC7: Load-more link encodes the current filter + next offset."""
    await _ensure_event_log()
    base = datetime.now(UTC)
    for i in range(60):
        await _insert(event_type="DECISION", timestamp=base - timedelta(minutes=i))
    raw_token = await _create_session("installer")
    response = _client().get(
        "/fragments/installer/event-log-list?type=DECISION",
        cookies={"session": raw_token},
    )
    body = response.text
    assert "installer-event-log-load-more" in body
    assert "type=DECISION" in body
    assert "offset=50" in body


async def test_event_log_list_fragment_returns_just_list_wrapper_no_full_page_shell(
    session_repo: SessionRepo,
) -> None:
    """AC3: fragment returns the list wrapper section, NOT the page shell.

    Verified by absence of the sidebar markup + presence of the list-wrapper id.
    """
    await _ensure_event_log()
    await _insert()
    raw_token = await _create_session("installer")
    response = _client().get(
        "/fragments/installer/event-log-list",
        cookies={"session": raw_token},
    )
    body = response.text
    assert 'id="event-log-list-wrapper"' in body
    # The page-only chrome must NOT appear.
    assert "<html" not in body.lower()
    assert "installer-sidebar" not in body
    assert "installer-note-form" not in body


# ── T8 — POST /actions/installer-note ─────────────────────────────────────────


async def test_post_installer_note_success_returns_empty_form_plus_oob_row_swap(
    session_repo: SessionRepo,
) -> None:
    """AC10: success response is empty form fragment + OOB-swap INSTALLER row."""
    await _ensure_event_log()
    raw_token = await _create_session("installer")
    response = _client().post(
        "/actions/installer-note",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": "test-csrf"},
        data={"note": "Firmware updated, ready to monitor."},
    )
    assert response.status_code == 200
    body = response.text
    # Form re-rendered with empty value.
    assert 'id="installer-note-form"' in body
    # OOB swap target on a <li> element (afterbegin:#event-log-list-rows).
    assert 'hx-swap-oob="afterbegin:#event-log-list-rows"' in body
    # The new row's textContent appears.
    assert "Firmware updated, ready to monitor." in body
    # Should NOT include error message.
    assert 'role="alert"' not in body


async def test_post_installer_note_over_2000_chars_returns_form_with_preserved_value_and_error(
    session_repo: SessionRepo,
) -> None:
    """AC10: over-2000-char rejection preserves textarea value + shows inline error."""
    await _ensure_event_log()
    raw_token = await _create_session("installer")
    long_note = "x" * 2001
    response = _client().post(
        "/actions/installer-note",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": "test-csrf"},
        data={"note": long_note},
    )
    assert response.status_code == 200
    body = response.text
    assert "exceeds 2,000 characters" in body or "exceeds 2000 characters" in body
    # The original value is rendered inside the textarea (preserved).
    assert long_note in body
    # No OOB swap on failure.
    assert "hx-swap-oob" not in body


async def test_post_installer_note_whitespace_only_returns_form_with_inline_error(
    session_repo: SessionRepo,
) -> None:
    """AC10: empty/whitespace-only rejection."""
    await _ensure_event_log()
    raw_token = await _create_session("installer")
    response = _client().post(
        "/actions/installer-note",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": "test-csrf"},
        data={"note": "   \n  \t  "},
    )
    assert response.status_code == 200
    body = response.text
    assert "cannot be empty" in body.lower() or "empty" in body.lower()
    assert "hx-swap-oob" not in body


async def test_post_installer_note_persists_html_escaped_summary(
    session_repo: SessionRepo,
) -> None:
    """AC16: XSS payload is HTML-escaped at the service boundary."""
    await _ensure_event_log()
    raw_token = await _create_session("installer")
    response = _client().post(
        "/actions/installer-note",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": "test-csrf"},
        data={"note": "<script>alert(1)</script>"},
    )
    assert response.status_code == 200
    body = response.text
    # Raw <script> must NOT appear; escaped form does.
    assert "<script>alert(1)</script>" not in body
    # Verify the DB row carries the escaped summary.
    conn = get_connection()
    async with conn.execute("SELECT summary FROM event_log") as cur:
        row = await cur.fetchone()
    assert row is not None
    summary = str(row[0])
    assert "<script>" not in summary
    assert "&lt;script&gt;" in summary or "&lt;script" in summary


async def test_post_installer_note_homeowner_rejected(
    session_repo: SessionRepo,
) -> None:
    """Homeowner cannot post an installer note (require_installer rejects)."""
    await _ensure_event_log()
    raw_token = await _create_session("homeowner")
    response = _client().post(
        "/actions/installer-note",
        cookies={"session": raw_token},
        headers={"X-CSRF-Token": "test-csrf"},
        data={"note": "I am a homeowner trying to post."},
    )
    # ``require_installer`` rejects with redirect (302) / 401 / 403. Pin the
    # specific outcomes so a 500 from an unrelated future bug cannot
    # silently satisfy this test.
    assert response.status_code in (302, 401, 403)


@pytest.mark.asyncio
async def test_post_installer_note_csrf_token_required(
    session_repo: SessionRepo,
) -> None:
    """DF3 carry-over: CSRF middleware rejects POSTs without an X-CSRF-Token header.

    The CsrfMiddleware is global (registered first on the app). This test wires
    up the real middleware to verify the negative path for the new POST route.
    """
    from open_ems.web.csrf import CsrfMiddleware  # noqa: PLC0415

    await _ensure_event_log()
    raw_token = await _create_session("installer")
    app = FastAPI()
    app.include_router(actions_router)
    app.add_middleware(CsrfMiddleware)
    client = TestClient(app, base_url="https://test", follow_redirects=False)
    response = client.post(
        "/actions/installer-note",
        cookies={"session": raw_token},
        # NO X-CSRF-Token header
        data={"note": "Should be rejected."},
    )
    assert response.status_code == 403
    assert "CSRF" in response.text
