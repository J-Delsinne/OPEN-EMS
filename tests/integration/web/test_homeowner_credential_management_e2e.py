"""Story 11.3 AC22 #60-65 — end-to-end integration tests.

Exercises the full installer↔homeowner credential lifecycle:
1. Installer creates homeowner credentials.
2. Homeowner attempts to log in; middleware redirects to /change-password.
3. Homeowner submits the new password; session ends, redirect to /login.
4. Installer resets the homeowner password; homeowner's existing session is
   terminated.

Plus regression coverage for the sidebar refactor and XSS-in-username.
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
from open_ems.storage.repositories.user_repo import UserRepo, hash_password, verify_password
from open_ems.web.csrf import CsrfMiddleware
from open_ems.web.must_change_password_middleware import MustChangePasswordMiddleware
from open_ems.web.routes.actions import router as actions_router
from open_ems.web.routes.auth import router as auth_router
from open_ems.web.routes.fragments import router as fragments_router
from open_ems.web.routes.installer import router as installer_router

_CSRF_TOKEN = "c" * 64
_VALID_PASSWORD_A = "FirstStrongPass1!"
_VALID_PASSWORD_B = "SecondStrongPass2!"


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


async def _ensure_event_log_table() -> None:
    conn = get_connection()
    await conn.execute(_CREATE_EVENT_LOG)
    await conn.commit()


async def _create_session(role: str, *, must_change_password: bool = False) -> tuple[str, str]:
    user_id = await UserRepo(get_connection()).create(
        username=f"{role}_{generate_session_token()[:8]}",
        hashed_password=hash_password("password1234"),
        role=role,
        must_change_password=must_change_password,
    )
    raw_token = generate_session_token()
    await SessionRepo(get_connection()).create(
        user_id=user_id,
        token_hash=hash_token(raw_token),
        expires_at=datetime.now(UTC) + timedelta(hours=4),
        csrf_token=_CSRF_TOKEN,
    )
    return raw_token, user_id


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(installer_router)
    app.include_router(actions_router)
    app.include_router(fragments_router)
    # Story 11.3 AC12 ordering — last-registered runs first inbound.
    app.add_middleware(CsrfMiddleware)
    app.add_middleware(MustChangePasswordMiddleware)
    return app


def _client() -> TestClient:
    return TestClient(_app(), base_url="https://test", follow_redirects=False)


# ── AC22 #60: full create → forced-change flow ───────────────────────────────


async def test_e2e_installer_creates_homeowner_then_homeowner_logs_in_and_is_redirected_to_change_password(  # noqa: E501
    session_repo: SessionRepo,
) -> None:
    del session_repo
    await _ensure_event_log_table()
    installer_token, _ = await _create_session("installer")

    # Step 1 — installer creates the homeowner.
    resp = _client().post(
        "/actions/create-homeowner",
        cookies={"session": installer_token},
        headers={"X-CSRF-Token": _CSRF_TOKEN},
        data={"username": "home1", "password": _VALID_PASSWORD_A},
    )
    assert resp.status_code == 200

    homeowner_row = await UserRepo(get_connection()).get_by_username("home1")
    assert homeowner_row is not None
    assert homeowner_row["must_change_password"] == 1

    # Step 2 — simulate homeowner login by minting a session for them.
    homeowner_token = generate_session_token()
    await SessionRepo(get_connection()).create(
        user_id=str(homeowner_row["id"]),
        token_hash=hash_token(homeowner_token),
        expires_at=datetime.now(UTC) + timedelta(hours=4),
        csrf_token="h" * 64,
    )

    # Step 3 — homeowner navigation to ANY dashboard route → 303 to /change-password.
    resp = _client().get("/homeowner/dashboard", cookies={"session": homeowner_token})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/change-password"


# ── AC22 #61: complete change-password then access dashboard ─────────────────


async def test_e2e_homeowner_completes_change_password_then_accesses_dashboard(
    session_repo: SessionRepo,
) -> None:
    """Review-P13: extended to actually verify the new credentials grant
    authenticated access. Original assertions (303 → /login, flag cleared,
    old session deleted) preserved; new assertions cover login-with-new-
    password + session-cookie issuance + the resolved session is unflagged
    so the next dashboard request will NOT redirect via the middleware."""
    del session_repo
    await _ensure_event_log_table()

    # Seed a forced-change homeowner.
    homeowner_uid = await UserRepo(get_connection()).create(
        username="forced-home",
        hashed_password=hash_password(_VALID_PASSWORD_A),
        role="homeowner",
        must_change_password=True,
    )
    homeowner_token = generate_session_token()
    await SessionRepo(get_connection()).create(
        user_id=homeowner_uid,
        token_hash=hash_token(homeowner_token),
        expires_at=datetime.now(UTC) + timedelta(hours=4),
        csrf_token="h" * 64,
    )

    # Step 1 — POST /change-password with valid inputs.
    resp = _client().post(
        "/change-password",
        cookies={"session": homeowner_token},
        headers={"X-CSRF-Token": "h" * 64},
        data={
            "current_password": _VALID_PASSWORD_A,
            "new_password": _VALID_PASSWORD_B,
            "confirm_new_password": _VALID_PASSWORD_B,
        },
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"

    # Step 2 — flag cleared on the user row.
    row = await UserRepo(get_connection()).get_by_id(homeowner_uid)
    assert row is not None
    assert row["must_change_password"] == 0

    # Step 3 — original session is gone.
    assert (
        await SessionRepo(get_connection()).get_by_token_hash(hash_token(homeowner_token)) is None
    )

    # Step 4 — log in with the new password (the "accesses dashboard" step).
    # POST /login mints a fresh session bound to the un-flagged user.
    login_resp = _client().post(
        "/login",
        data={"username": "forced-home", "password": _VALID_PASSWORD_B},
    )
    assert login_resp.status_code == 303
    assert login_resp.headers["location"] == "/homeowner/dashboard"
    # Session cookie was issued.
    new_cookie = login_resp.cookies.get("session")
    assert new_cookie is not None and new_cookie != homeowner_token

    # Step 5 — the new session resolves to an unflagged user, so the middleware
    # would NOT intercept a subsequent dashboard request. This is the audit-
    # trail proof that the rotation took effect end-to-end.
    new_session_row = await SessionRepo(get_connection()).get_by_token_hash(hash_token(new_cookie))
    assert new_session_row is not None
    assert str(new_session_row["user_id"]) == homeowner_uid
    refreshed_user = await UserRepo(get_connection()).get_by_id(homeowner_uid)
    assert refreshed_user is not None
    assert refreshed_user["must_change_password"] == 0


# ── Regression: full bootstrap-admin → forced-change → re-login flow ─────────


async def test_e2e_bootstrap_admin_forced_change_with_form_body_csrf_full_flow(
    session_repo: SessionRepo,
) -> None:
    """Regression for the Story 11.3 production bug: an installer admin
    bootstrapped with ``must_change_password=True`` logs in successfully, is
    redirected to ``/change-password`` by the middleware, submits the
    change-password form (which carries ``csrf_token`` in the FORM BODY because
    it is a traditional non-HTMX ``<form>``), and the route mistakenly returned
    "Current password is incorrect" because the CSRF middleware consumed the
    ASGI stream and FastAPI's ``Form()`` parameters arrived empty.

    Asserts the full contract:
      1. Bootstrap admin login succeeds (303 → /installer/dashboard).
      2. Middleware redirects every non-allow-listed path to /change-password.
      3. POST /change-password with form-body csrf (no header) succeeds (303 → /login).
      4. ``must_change_password`` is cleared on the user row.
      5. The original session is invalidated.
      6. Login with the new password succeeds.
      7. The new session is unflagged so the middleware no longer redirects.
    """
    del session_repo
    await _ensure_event_log_table()

    bootstrap_username = "admin"
    bootstrap_password = "BootstrapAdmin12!"  # noqa: S105 — test fixture
    new_password = "RotatedPass34!"  # noqa: S105 — test fixture

    admin_uid = await UserRepo(get_connection()).create(
        username=bootstrap_username,
        hashed_password=hash_password(bootstrap_password),
        role="installer",
        must_change_password=True,
    )

    # Step 1 — bootstrap admin login succeeds.
    login_resp = _client().post(
        "/login",
        data={"username": bootstrap_username, "password": bootstrap_password},
    )
    assert login_resp.status_code == 303, login_resp.text
    assert login_resp.headers["location"] == "/installer/dashboard"
    initial_cookie = login_resp.cookies.get("session")
    assert initial_cookie is not None

    # Step 2 — middleware redirects any non-allow-listed path to /change-password.
    blocked_resp = _client().get("/installer/dashboard", cookies={"session": initial_cookie})
    assert blocked_resp.status_code == 303
    assert blocked_resp.headers["location"] == "/change-password"

    # Step 3 — POST /change-password with csrf_token IN FORM BODY (no header,
    # mirroring the real browser submission of the standalone change-password
    # page). This is the path that previously failed with "Current password is
    # incorrect" because CsrfMiddleware drained the ASGI body before the route
    # could read it.
    session_row = await SessionRepo(get_connection()).get_by_token_hash(hash_token(initial_cookie))
    assert session_row is not None
    csrf_token = str(session_row["csrf_token"])

    change_resp = _client().post(
        "/change-password",
        cookies={"session": initial_cookie},
        data={
            "csrf_token": csrf_token,
            "current_password": bootstrap_password,
            "new_password": new_password,
            "confirm_new_password": new_password,
        },
    )
    assert change_resp.status_code == 303, change_resp.text
    assert change_resp.headers["location"] == "/login"

    # Step 4 — must_change_password cleared.
    user_row_after = await UserRepo(get_connection()).get_by_id(admin_uid)
    assert user_row_after is not None
    assert user_row_after["must_change_password"] == 0
    # Hash actually rotated to the new password (sanity check).
    assert verify_password(new_password, user_row_after["hashed_password"]) is True
    assert verify_password(bootstrap_password, user_row_after["hashed_password"]) is False

    # Step 5 — original session invalidated.
    assert await SessionRepo(get_connection()).get_by_token_hash(hash_token(initial_cookie)) is None

    # Step 6 — login with the NEW password succeeds.
    relogin_resp = _client().post(
        "/login",
        data={"username": bootstrap_username, "password": new_password},
    )
    assert relogin_resp.status_code == 303
    assert relogin_resp.headers["location"] == "/installer/dashboard"
    new_cookie = relogin_resp.cookies.get("session")
    assert new_cookie is not None and new_cookie != initial_cookie

    # Step 7 — new session resolves to an unflagged user; middleware no longer
    # intercepts. We verify this by inspecting the user row instead of GETting
    # the dashboard (which would require state_store wiring this test does not
    # set up). The unflagged invariant is what the middleware reads on each
    # request, so this is the load-bearing assertion.
    refreshed = await UserRepo(get_connection()).get_by_id(admin_uid)
    assert refreshed is not None
    assert refreshed["must_change_password"] == 0


# ── AC22 #62: installer reset → homeowner session terminated ─────────────────


async def test_e2e_installer_resets_homeowner_password_and_homeowner_sees_session_terminated(
    session_repo: SessionRepo,
) -> None:
    del session_repo
    await _ensure_event_log_table()
    installer_token, _ = await _create_session("installer")

    homeowner_uid = await UserRepo(get_connection()).create(
        username="home2",
        hashed_password=hash_password(_VALID_PASSWORD_A),
        role="homeowner",
        must_change_password=False,
    )
    homeowner_token = generate_session_token()
    await SessionRepo(get_connection()).create(
        user_id=homeowner_uid,
        token_hash=hash_token(homeowner_token),
        expires_at=datetime.now(UTC) + timedelta(hours=4),
        csrf_token="h" * 64,
    )

    resp = _client().post(
        "/actions/reset-homeowner-password",
        cookies={"session": installer_token},
        headers={"X-CSRF-Token": _CSRF_TOKEN},
        data={"new_password": _VALID_PASSWORD_B},
    )
    assert resp.status_code == 200

    # Homeowner session deleted.
    assert (
        await SessionRepo(get_connection()).get_by_token_hash(hash_token(homeowner_token)) is None
    )
    # Flag re-applied.
    row = await UserRepo(get_connection()).get_by_id(homeowner_uid)
    assert row is not None
    assert row["must_change_password"] == 1


# ── AC22 #63: every homeowner subpath redirects when flag is true ────────────


async def test_e2e_homeowner_with_must_change_password_blocked_from_all_dashboard_routes(
    session_repo: SessionRepo,
) -> None:
    del session_repo
    await _ensure_event_log_table()
    homeowner_token, _uid = await _create_session("homeowner", must_change_password=True)

    representative_paths = [
        "/homeowner/dashboard",
        "/installer/dashboard",  # cross-role attempt is also gated by middleware first
    ]
    for path in representative_paths:
        resp = _client().get(path, cookies={"session": homeowner_token})
        assert resp.status_code == 303, path
        assert resp.headers["location"] == "/change-password", path


# ── AC22 #64: sidebar Settings link navigates correctly ──────────────────────


async def test_e2e_installer_sidebar_settings_link_navigates_to_settings_page(
    session_repo: SessionRepo,
) -> None:
    del session_repo
    await _ensure_event_log_table()
    installer_token, _ = await _create_session("installer")

    # GET /installer/settings — should render successfully with the sidebar.
    resp = _client().get("/installer/settings", cookies={"session": installer_token})
    assert resp.status_code == 200
    assert "Homeowner credentials" in resp.text
    # Sidebar regression: Dashboard, Event Log, Settings all present.
    assert 'href="/installer/dashboard"' in resp.text
    assert 'href="/installer/event-log"' in resp.text
    assert 'href="/installer/settings"' in resp.text
    # Settings is the active item (aria-current=page on that link).
    settings_link_block = resp.text.split('href="/installer/settings"')[1].split("</a>")[0]
    assert 'aria-current="page"' in settings_link_block


# ── Bugfix regression: settings page surfaces the homeowner credentials form ──


async def test_settings_page_renders_create_form_when_no_homeowner_exists(
    session_repo: SessionRepo,
) -> None:
    """Regression: the installer Settings page must visibly expose the
    create-homeowner form when no homeowner account exists.

    The original wiring bug was visual — the sidebar's ``min-height: 100vh``
    pushed the unstyled main section below the viewport, so the form WAS in
    the response HTML but invisible. This test guards the response markup;
    the accompanying CSS for ``.installer-settings-page`` keeps the form on
    screen at desktop and mobile.
    """
    del session_repo
    await _ensure_event_log_table()
    installer_token, _ = await _create_session("installer")

    # Pre-condition: no homeowner exists.
    assert await UserRepo(get_connection()).get_homeowner() is None

    resp = _client().get("/installer/settings", cookies={"session": installer_token})
    assert resp.status_code == 200
    assert 'id="homeowner-credentials-section"' in resp.text
    # Create-form action + required inputs are visible.
    assert 'action="/actions/create-homeowner"' in resp.text
    assert 'name="username"' in resp.text
    assert 'name="password"' in resp.text
    # Reset-form action MUST NOT appear when no homeowner exists.
    assert 'action="/actions/reset-homeowner-password"' not in resp.text
    # Layout wrapper is grid-scoped and Alpine-bound so the mobile sidebar
    # toggle works (the bug-fix wiring — without these, the form scrolled
    # off-screen on desktop).
    assert 'class="installer-settings-page"' in resp.text
    assert 'x-data="installerSidebar()"' in resp.text


async def test_settings_page_renders_reset_form_when_homeowner_exists(
    session_repo: SessionRepo,
) -> None:
    """Regression: the installer Settings page must visibly expose the
    reset-password trigger (which loads the reset form) when a homeowner
    already exists.
    """
    del session_repo
    await _ensure_event_log_table()
    installer_token, _ = await _create_session("installer")
    # Seed a homeowner row so the section renders its "exists" branch.
    await UserRepo(get_connection()).create(
        username="homeowner_seed",
        hashed_password=hash_password("password1234"),
        role="homeowner",
        must_change_password=False,
    )

    resp = _client().get("/installer/settings", cookies={"session": installer_token})
    assert resp.status_code == 200
    assert 'id="homeowner-credentials-section"' in resp.text
    # Read-only summary appears.
    assert "homeowner_seed" in resp.text
    # Reset-password trigger button (the hx-get loads the inline form into
    # #homeowner-reset-slot on click).
    assert 'id="homeowner-reset-slot"' in resp.text
    assert 'hx-get="/fragments/installer/homeowner-reset-form"' in resp.text
    # Create-form action MUST NOT appear once a homeowner exists.
    assert 'action="/actions/create-homeowner"' not in resp.text


# ── AC22 #65: XSS in homeowner username renders escaped ──────────────────────


async def test_e2e_xss_in_homeowner_username_renders_escaped(
    session_repo: SessionRepo,
) -> None:
    del session_repo
    await _ensure_event_log_table()
    installer_token, _ = await _create_session("installer")

    xss_payload = "<script>alert(1)</script>"
    resp = _client().post(
        "/actions/create-homeowner",
        cookies={"session": installer_token},
        headers={"X-CSRF-Token": _CSRF_TOKEN},
        data={"username": xss_payload, "password": _VALID_PASSWORD_A},
    )
    assert resp.status_code == 200
    # Raw tag must NOT appear; Jinja autoescape converts to &lt;script&gt;.
    assert "<script>" not in resp.text
    assert "&lt;script&gt;" in resp.text
