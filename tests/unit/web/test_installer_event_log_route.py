"""Story 11.2 — GET /installer/event-log page route tests.

Covers AC1-AC9, AC12, AC13, AC18, AC20: page render, sidebar active state,
filter parsing + rendering, result count, "Load more" pagination, empty states,
auth gates, UTC + Brussels timestamp emission, URL contracts from 11.1.
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
from open_ems.web.routes.installer import router as installer_router


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
    app.include_router(installer_router)
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
    actor: str = "system",
) -> None:
    ts = timestamp if timestamp is not None else datetime.now(UTC)
    conn = get_connection()
    await conn.execute(
        "INSERT INTO event_log"
        " (schema_version, timestamp, actor, event_type, summary, device_id)"
        " VALUES (1, ?, ?, ?, ?, ?)",
        (ts.isoformat(), actor, event_type, summary, device_id),
    )
    await conn.commit()


# ── AC2: sidebar with Event Log active ────────────────────────────────────────


async def test_event_log_page_renders_sidebar_with_event_log_active(
    session_repo: SessionRepo,
) -> None:
    await _ensure_event_log()
    raw_token = await _create_session("installer")
    response = _client().get("/installer/event-log", cookies={"session": raw_token})
    assert response.status_code == 200
    body = response.text
    # Event Log item has the active modifier + aria-current=page.
    assert "installer-sidebar__item--active" in body
    assert 'href="/installer/event-log"' in body
    assert 'aria-current="page"' in body
    # Dashboard item is NOT active here.
    assert 'href="/installer/dashboard"' in body


# ── AC4: filter pills ─────────────────────────────────────────────────────────


async def test_event_log_page_renders_five_filter_pills(
    session_repo: SessionRepo,
) -> None:
    await _ensure_event_log()
    raw_token = await _create_session("installer")
    response = _client().get("/installer/event-log", cookies={"session": raw_token})
    body = response.text
    for pill in ("DECISION", "DEVICE", "SYSTEM", "CONSTRAINT", "INSTALLER"):
        # Pills render the type name as button text content. The toggle value
        # is carried in ``hx-vals`` (JSON) — for a "no current filter" page,
        # each pill's toggle adds itself.
        assert f">{pill}<" in body
        assert f'"type": "{pill}"' in body
    # All five pills are type="button" (not submit) so a click fires the
    # button's own hx-get with the precomputed toggle set rather than
    # submitting the form and dropping the other active pills.
    assert body.count('class="event-log-filter-pill') == 5


# ── AC10: note form ───────────────────────────────────────────────────────────


async def test_event_log_page_renders_note_form_with_2000_char_maxlength(
    session_repo: SessionRepo,
) -> None:
    await _ensure_event_log()
    raw_token = await _create_session("installer")
    response = _client().get("/installer/event-log", cookies={"session": raw_token})
    body = response.text
    assert 'id="installer-note-form"' in body
    assert 'maxlength="2000"' in body
    assert 'name="note"' in body


# ── AC3: 50-row initial render + load more ────────────────────────────────────


async def test_event_log_page_initial_render_shows_50_entries_max(
    session_repo: SessionRepo,
) -> None:
    await _ensure_event_log()
    # Insert 60 rows; only the first 50 should appear.
    base = datetime.now(UTC)
    for i in range(60):
        await _insert(
            summary=f"unique-marker-{i:04d}",
            timestamp=base - timedelta(minutes=i),
        )
    raw_token = await _create_session("installer")
    response = _client().get("/installer/event-log", cookies={"session": raw_token})
    body = response.text
    assert "unique-marker-0000" in body  # newest
    assert "unique-marker-0049" in body  # 50th
    assert "unique-marker-0050" not in body  # 51st excluded
    assert "unique-marker-0059" not in body


async def test_event_log_page_renders_load_more_when_more_rows_exist(
    session_repo: SessionRepo,
) -> None:
    await _ensure_event_log()
    base = datetime.now(UTC)
    for i in range(60):
        await _insert(summary=f"row-{i}", timestamp=base - timedelta(minutes=i))
    raw_token = await _create_session("installer")
    response = _client().get("/installer/event-log", cookies={"session": raw_token})
    body = response.text
    assert "installer-event-log-load-more" in body
    # Load-more link must preserve offset=50 for forward pagination.
    assert "offset=50" in body


async def test_event_log_page_omits_load_more_when_no_more_rows(
    session_repo: SessionRepo,
) -> None:
    await _ensure_event_log()
    base = datetime.now(UTC)
    for i in range(10):
        await _insert(summary=f"row-{i}", timestamp=base - timedelta(minutes=i))
    raw_token = await _create_session("installer")
    response = _client().get("/installer/event-log", cookies={"session": raw_token})
    assert "installer-event-log-load-more" not in response.text


# ── AC4 / AC7: filter pill pre-selection from URL ─────────────────────────────


async def test_event_log_page_with_type_filter_pre_selects_pills(
    session_repo: SessionRepo,
) -> None:
    await _ensure_event_log()
    raw_token = await _create_session("installer")
    response = _client().get(
        "/installer/event-log?type=DECISION,SYSTEM",
        cookies={"session": raw_token},
    )
    body = response.text
    # Active pills carry the ``--active`` CSS modifier + ``aria-pressed="true"``.
    # The hidden ``type`` input reflects the current filter set (sorted CSV)
    # so HTMX submits from date/keyword inputs preserve it.
    assert 'aria-pressed="true"' in body
    assert "event-log-filter-pill--active" in body
    assert 'name="type" value="DECISION,SYSTEM"' in body
    # Two pills are active.
    assert body.count("event-log-filter-pill--active") == 2
    # Each pill's toggle value reflects the post-click state. Clicking
    # DECISION (currently active) would remove it → toggle value is "SYSTEM".
    assert '"type": "SYSTEM"' in body
    # Clicking DEVICE (currently inactive) would add it → toggle value is the
    # sorted CSV "DECISION,DEVICE,SYSTEM".
    assert '"type": "DECISION,DEVICE,SYSTEM"' in body


# ── AC5 / AC20: window=24h ────────────────────────────────────────────────────


async def test_event_log_page_with_window_24h_renders(
    session_repo: SessionRepo,
) -> None:
    """Story 11.1 link contract: ?type=SYSTEM&window=24h renders pre-filtered."""
    await _ensure_event_log()
    base = datetime.now(UTC)
    await _insert(event_type="SYSTEM", summary="recent system event", timestamp=base)
    await _insert(
        event_type="SYSTEM",
        summary="old system event",
        timestamp=base - timedelta(days=5),
    )
    await _insert(event_type="DECISION", summary="decision event", timestamp=base)
    raw_token = await _create_session("installer")
    response = _client().get(
        "/installer/event-log?type=SYSTEM&window=24h",
        cookies={"session": raw_token},
    )
    assert response.status_code == 200
    body = response.text
    assert "recent system event" in body
    assert "old system event" not in body  # outside window
    assert "decision event" not in body  # wrong type
    # Window-derived filters render the form date inputs empty (with the
    # hidden ``window`` input carrying the relative vocabulary). Guards
    # against ``_filter_until_to_iso`` freezing the window's ``now`` boundary
    # into "yesterday" in the ``to`` input on re-render — see review P5.
    import re  # noqa: PLC0415

    assert re.search(r'name="from"\s+value=""', body) is not None
    assert re.search(r'name="to"\s+value=""', body) is not None
    assert 'name="window" value="24h"' in body


# ── AC20: device_id link contract ─────────────────────────────────────────────


async def test_event_log_page_with_device_id_filter_applies_correctly(
    session_repo: SessionRepo,
) -> None:
    await _ensure_event_log()
    base = datetime.now(UTC)
    await _insert(summary="batt event", device_id="batt-1", timestamp=base)
    await _insert(summary="inv event", device_id="inv-1", timestamp=base)
    await _insert(summary="null-device event", device_id=None, timestamp=base)
    raw_token = await _create_session("installer")
    response = _client().get(
        "/installer/event-log?device_id=batt-1",
        cookies={"session": raw_token},
    )
    body = response.text
    assert "batt event" in body
    assert "inv event" not in body
    assert "null-device event" not in body  # AC18: NULL excluded


# ── AC6: keyword filter ───────────────────────────────────────────────────────


async def test_event_log_page_with_keyword_filter(
    session_repo: SessionRepo,
) -> None:
    await _ensure_event_log()
    await _insert(summary="peak limit hit")
    await _insert(summary="battery dispatched")
    raw_token = await _create_session("installer")
    response = _client().get(
        "/installer/event-log?q=peak",
        cookies={"session": raw_token},
    )
    body = response.text
    assert "peak limit hit" in body
    assert "battery dispatched" not in body


# ── AC8: result count caption ─────────────────────────────────────────────────


async def test_event_log_page_renders_result_count_when_filter_active(
    session_repo: SessionRepo,
) -> None:
    await _ensure_event_log()
    for _ in range(3):
        await _insert(event_type="DECISION")
    for _ in range(2):
        await _insert(event_type="SYSTEM")
    raw_token = await _create_session("installer")
    response = _client().get(
        "/installer/event-log?type=DECISION",
        cookies={"session": raw_token},
    )
    body = response.text
    assert "Showing 3 of 3 events" in body


async def test_event_log_page_omits_result_count_when_no_filter(
    session_repo: SessionRepo,
) -> None:
    await _ensure_event_log()
    for _ in range(3):
        await _insert()
    raw_token = await _create_session("installer")
    response = _client().get("/installer/event-log", cookies={"session": raw_token})
    assert "Showing" not in response.text or "of" not in response.text
    # More precise: the result count caption class isn't rendered.
    assert "installer-event-log-result-count" not in response.text


# ── AC19: empty state ─────────────────────────────────────────────────────────


async def test_event_log_page_empty_result_filter_active_renders_no_match_copy(
    session_repo: SessionRepo,
) -> None:
    await _ensure_event_log()
    await _insert(event_type="DECISION", summary="something")
    raw_token = await _create_session("installer")
    response = _client().get(
        "/installer/event-log?type=SYSTEM",
        cookies={"session": raw_token},
    )
    assert "No events match this filter" in response.text


async def test_event_log_page_empty_result_no_filter_renders_no_events_yet_copy(
    session_repo: SessionRepo,
) -> None:
    await _ensure_event_log()
    raw_token = await _create_session("installer")
    response = _client().get("/installer/event-log", cookies={"session": raw_token})
    assert "No events yet" in response.text


# ── Auth gates ────────────────────────────────────────────────────────────────


async def test_event_log_page_unauthenticated_redirects_to_login(
    session_repo: SessionRepo,
) -> None:
    await _ensure_event_log()
    response = _client().get("/installer/event-log")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/login")


async def test_event_log_page_homeowner_rejected_redirects_to_homeowner_home(
    session_repo: SessionRepo,
) -> None:
    await _ensure_event_log()
    raw_token = await _create_session("homeowner")
    response = _client().get("/installer/event-log", cookies={"session": raw_token})
    assert response.status_code == 302
    # Homeowner gets redirected to their home (302), not 200.


# ── AC12: UTC ISO + Brussels fallback on every row ────────────────────────────


async def test_event_log_page_renders_data_utc_attribute_for_every_row(
    session_repo: SessionRepo,
) -> None:
    await _ensure_event_log()
    await _insert(timestamp=datetime(2026, 5, 12, 14, 32, tzinfo=UTC))
    raw_token = await _create_session("installer")
    response = _client().get("/installer/event-log", cookies={"session": raw_token})
    body = response.text
    assert 'data-utc="2026-05-12T14:32:00+00:00"' in body


async def test_event_log_page_renders_brussels_fallback_textcontent_for_every_row(
    session_repo: SessionRepo,
) -> None:
    """AC12 Layer A: Brussels-locale string is present in the server-rendered HTML.

    In mid-May Europe/Brussels is CEST (UTC+2) so 14:32 UTC → 16:32 CEST.
    """
    await _ensure_event_log()
    await _insert(timestamp=datetime(2026, 5, 12, 14, 32, tzinfo=UTC))
    raw_token = await _create_session("installer")
    response = _client().get("/installer/event-log", cookies={"session": raw_token})
    body = response.text
    assert "2026-05-12 16:32 CEST / UTC+2" in body


# ── Filter form HTMX wiring ───────────────────────────────────────────────────


async def test_event_log_page_filter_form_is_htmx_bound(
    session_repo: SessionRepo,
) -> None:
    """AC4-AC7: the filter form uses hx-get + hx-target wrapper + hx-push-url."""
    await _ensure_event_log()
    raw_token = await _create_session("installer")
    response = _client().get("/installer/event-log", cookies={"session": raw_token})
    body = response.text
    assert 'hx-get="/fragments/installer/event-log-list"' in body
    assert 'hx-target="#event-log-list-wrapper"' in body
    assert 'hx-push-url="true"' in body
    # Keyword input has the 300ms debounce.
    assert "input changed delay:300ms" in body


# ── AC22 #49: page route DB query count bounded ───────────────────────────────


@pytest.mark.asyncio
async def test_event_log_page_db_query_count_bounded(
    session_repo: SessionRepo,
) -> None:
    """AC22 #49: page render does <=5 DB queries (auth + 1-2 application).

    Forbidden-table substring assertion is dropped for this route (event-log
    IS the surface). The bound check ensures no N+1 or runaway query path.
    """
    from tests.utils.db_query_counter import count_queries  # noqa: PLC0415

    await _ensure_event_log()
    raw_token = await _create_session("installer")
    client = _client()
    # Warm-up: amortize one-time imports + caches.
    client.get("/installer/event-log", cookies={"session": raw_token})

    async with count_queries() as counter:
        response = client.get("/installer/event-log", cookies={"session": raw_token})

    assert response.status_code == 200
    assert counter.count <= 5, f"too many queries on page render: {counter.queries}"
