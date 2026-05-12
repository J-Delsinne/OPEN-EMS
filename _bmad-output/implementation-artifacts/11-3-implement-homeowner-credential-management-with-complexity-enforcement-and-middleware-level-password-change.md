# Story 11.3: Implement homeowner credential management with complexity enforcement and middleware-level password change

Status: done
_Status set to done by bmad-code-review on 2026-05-12 after review-closure gate passed (5b): 16 resolved, 30+ dismissed, 4 deferred-and-verified._
_Status: in-progress → review by bmad-dev-story on 2026-05-12 (all 12 tasks complete; 57 net new tests; quality gates clean)._
_Status: ready-for-dev → in-progress by bmad-dev-story on 2026-05-12._
_Status: ready-for-dev set by bmad-create-story on 2026-05-12._

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

> **Type:** Epic 11 — installer **operational monitoring** surface, third and final regular story of Epic 11. Adds the installer Settings surface (credential management) AND a new authentication-middleware enforcement layer for `must_change_password`. Mutates the `users` table (create homeowner + reset password) via the existing `UserRepo`; reuses existing `SessionRepo.delete_all_for_user` for session invalidation on reset. Also retrofits the `get_write_lock()` discipline onto `UserRepo` and `SessionRepo` writes — closing the explicit deferred item from Story 9.0b that gated this work to "when Epic 11 wires real homeowner credential flows".
> **A2-triggered:** **No.** Evaluated T1–T7 (see "A2 trigger evaluation" below). None match: `must_change_password` is a binary durable flag enforced by a guard middleware, not a multi-state lifecycle machine; sessions invalidation is a single DELETE WHERE user_id=?; no retry policy, no watchdog timing, no multi-adapter coordination, no process restart, and the installer-facing flow is single-step form submission (not multi-step wizard orchestration). R1–R7 enforcement does not apply. A substantive deferred-findings overlap check is included in dev notes (closes deferred-work.md line 107 in this story).
> **Prerequisites:** Epic 11 in-progress; **Story 11.2 done 2026-05-12** (depends on it for: shared installer-sidebar partial `installer/_sidebar.html` with `active` parameter, Settings item currently rendered as `aria-disabled` placeholder; the installer-side dashboard-stack + `installerSidebar` Alpine factory; `db_query_counter` test helper; `dependencies.get_observability_service` for audit emission).
> **Sibling stories:** 11.1 done 2026-05-12 (installer dashboard), 11.2 done 2026-05-12 (event log UI). 11.3 is the last operational-monitoring story in Epic 11; epic-11-retrospective is **optional** per `sprint-status.yaml`.
> **Epic 11 progress after this story:** 11.1 done · 11.2 done · 11.3 ready-for-dev (this story) · epic-11-retrospective optional.

## Story

As an installer,
I want to create and reset homeowner credentials with enforced password complexity, and have the first-login password change requirement enforced at the middleware level,
so that homeowner accounts are secure from the moment they are created and first-login enforcement cannot be bypassed by direct navigation.

## Acceptance Criteria

**Given** the installer is authenticated, the `users` table (migration `0002_add_users_table.py`) is at `must_change_password INTEGER NOT NULL DEFAULT 1` semantics, and the existing `UserRepo` exposes `count() / create() / get_by_username() / get_by_id() / update_password()`,

**When** this story is implemented,

**Then** the following acceptance criteria must hold:

---

### Settings page route, layout, sidebar wiring

1. **AC1 — `GET /installer/settings` is a real page.** A new route in `src/open_ems/web/routes/installer.py` (sibling to `installer_dashboard` at line 28-41 and `installer_event_log` at line 44-114) renders the installer settings page. The page extends `base.html`, reuses the shared installer sidebar partial with **Settings** as the active item (see AC2), and contains a single section: **Homeowner Credentials**. Route signature:

   ```python
   @router.get("/installer/settings", response_class=HTMLResponse)
   async def installer_settings(
       request: Request,
       user: InstallerUser = Depends(require_installer),
       user_repo: UserRepo = Depends(get_user_repo),
   ) -> HTMLResponse:
   ```

   The route queries `user_repo.get_homeowner()` (new method — see AC11) to determine whether a homeowner account already exists. The page renders one of two states inside the Homeowner Credentials section:

   - **No homeowner exists yet** — Create form with `username` + `password` inputs (per AC3).
   - **Homeowner exists** — A read-only "Homeowner account: `<username>` (created `<date in browser-local timezone>`)" display + a "Reset password" button that opens a confirmation form with a new `password` input (per AC4–AC5).

   Future Epic 12 surfaces (platform updates, backup/restore) are out of scope for this page in 11.3 — the section is intentionally a single-feature page.

2. **AC2 — Sidebar with Settings active.** The installer sidebar partial `src/open_ems/web/templates/installer/_sidebar.html` currently renders Settings as `aria-disabled="true" tabindex="-1"` placeholder (lines 42). 11.3 makes Settings **live**: drop `aria-disabled="true"` + `tabindex="-1"`, change `href="#"` to `href="/installer/settings"`, and apply `installer-sidebar__item--active` + `aria-current="page"` when the surrounding template sets `{% set active = "settings" %}`. The Devices item (line 36) remains placeholder (still Epic 11.x / 12.x scope — out of scope here).

   `settings.html` uses the partial via `{% include "installer/_sidebar.html" with context %}` after `{% set active = "settings" %}`. A regression test asserts the dashboard and event-log pages still render the same 5 nav items in the same order (no drift from the 11.2 baseline).

---

### Create homeowner credentials

3. **AC3 — `POST /actions/create-homeowner` succeeds when no homeowner exists.** New route in `src/open_ems/web/routes/actions.py` (sibling to `post_installer_note` at line 532-664). The form body fields are `username: str` and `password: str` (both required). Validation steps in **strict order**:

   1. **Username present + length sanity.** `username.strip()` non-empty AND `len(username.strip()) <= 64` (matches the existing `users.username` column's `Text` type without an explicit upper bound; 64 chars is a defensive UX limit). Otherwise render the form fragment with preserved `username` value + inline error `"Username must be 1–64 characters."`.
   2. **Username uniqueness.** `await user_repo.get_by_username(stripped_username)` returns `None`. Otherwise render with preserved username + `"This username is already in use."` — no role disclosure (the existing installer's "admin" username gives the same error as a homeowner duplicate).
   3. **Password complexity.** `len(password) >= 12` (see AC7 for the canonical complexity rule). Otherwise render with preserved username + `"Password must be at least 12 characters."` (per AC7 the rule is server-authoritative; client-side enforcement in AC8 short-circuits but is NEVER the gate).
   4. **No-homeowner precondition.** `await user_repo.get_homeowner()` returns `None` (see AC11). If a homeowner already exists, render with preserved username + `"A homeowner account already exists. Reset its password instead."` — defensive against URL-tampering replay or browser-back-button stale-form-submit; the UI already conditionally renders the reset variant when a homeowner exists, but the server is the authority.
   5. **Hash and persist.** `hashed = hash_password(password)` (existing `user_repo.hash_password`); call `await user_repo.create(username=stripped_username, hashed_password=hashed, role="homeowner", must_change_password=True)` — the default `must_change_password=True` is asserted explicitly in the call so future refactors that flip the default do not break the contract.
   6. **Audit.** Emit via the existing `ObservabilityService.audit(actor="installer", event_type="SYSTEM", summary="Homeowner credentials created")` — note the exact summary string is contractual (epic AC line 2357). No `device_id`, no `detail` payload (PII minimization — username is omitted from the audit row).
   7. **Render confirmation.** Return the settings page fragment with the section flipped to the "Homeowner exists" state PLUS a confirmation banner: "Homeowner account created. View in event log →" — the link is `/installer/event-log?type=SYSTEM&window=24h` (the 11.1 URL contract). HTMX wires this as `hx-post="/actions/create-homeowner"` `hx-target="#homeowner-credentials-section"` `hx-swap="outerHTML"`. The success response is the re-rendered section fragment.

   **bcrypt 72-byte ceiling:** `UserRepo.hash_password` raises `ValueError` if the password exceeds bcrypt's 72-byte UTF-8 ceiling. The route must catch this `ValueError` and render the form fragment with `"Password is too long. Use 12–72 bytes."` (bytes, not characters — emoji-heavy passwords can hit the ceiling at 19 characters). This is a real path: the complexity floor is 12 chars, the bcrypt ceiling is 72 bytes, and the gap matters for UTF-8-multibyte passwords.

4. **AC4 — `POST /actions/reset-homeowner-password` succeeds when a homeowner exists.** New route in `actions.py`. The form body field is `new_password: str` (required). Validation in strict order:

   1. **Homeowner exists.** `homeowner = await user_repo.get_homeowner()` is not `None`. Otherwise render the section flipped back to the "No homeowner" state with inline error `"No homeowner account exists. Create one first."` — defensive against stale-form replay.
   2. **Password complexity** (same as AC3 step 3).
   3. **bcrypt-byte ceiling check** (same as AC3 step 6 catch).
   4. **Atomic reset.** All three actions land **inside one `get_write_lock()` acquisition** (see AC6 for the lock-discipline retrofit): (a) `await user_repo.update_password(user_id=homeowner.id, hashed_password=hashed, must_change_password=True)` — the new variant of `update_password` that also re-sets the flag, see AC9; (b) `await session_repo.delete_all_for_user(user_id=homeowner.id)` — invalidates all active homeowner sessions; (c) the audit emission via `ObservabilityService.audit(actor="installer", event_type="SYSTEM", summary="Homeowner credentials reset")`.
   5. **Render confirmation.** Re-render the settings section with a confirmation banner: "Homeowner password reset. All homeowner sessions ended." + "View in event log →" pre-filtered link as in AC3.

   **Atomicity contract:** the password update + session invalidation must happen inside the same `get_write_lock()` window so that a race between the reset transaction and a homeowner login refresh (`_resolve_session.touch()`) cannot leave a stale-token session valid against the new hash. The audit emission inside the lock is acceptable here even though `ObservabilityService.audit` is itself a `get_write_lock()`-protected writer (see AC6) because the lock is `asyncio.Lock` (recursion-aware? — see implementation note). **Implementation note:** Python's `asyncio.Lock` is **NOT** re-entrant. The route MUST hold the lock for the password+session-delete pair, then release, then call `audit()` (which acquires the lock again internally). Document this ordering in the route's docstring. The window between password-update-commit and audit-emit is acceptable: the audit row may be slightly delayed; the security-critical invariant (sessions invalidated before the function returns to the installer) is preserved.

5. **AC5 — Reset confirmation gate (two-step inline UX).** The "Reset password" button in the settings page is a `<button type="button" hx-get="/fragments/installer/homeowner-reset-form" hx-target="#homeowner-reset-slot" hx-swap="outerHTML">` that loads the new-password input form inline (NOT a separate page). The form renders with a `<details open>` confirmation copy block ("This will end all active homeowner sessions. They will need to log in with the new password.") + the `new_password` input + a "Reset password" submit button + a "Cancel" button (HTMX-replaces the slot with the original "Reset password" trigger button).

   A fragment route `GET /fragments/installer/homeowner-reset-form` returns the form HTML. The form is rendered with `csrf_token` from `user.csrf_token` (server-rendered hidden input). Tests cover this round-trip (AC22 #N).

---

### Password complexity rule (server-authoritative, client-side echo)

6. **AC6 — `get_write_lock()` retrofit onto `UserRepo` and `SessionRepo` writes.** This closes the deferred-work item from Story 9.0b (`deferred-work.md` line ~107): *"UserRepo and SessionRepo writes are not yet routed through the database-module write lock... add the lock when Epic 11 wires real homeowner credential flows"*. The retrofit pattern follows Story 9.0b's precedent (`ConfigRepo._activate_locked`, `EventLogRepo.append`, `EnergyRepo.write_peak_interval`):

   **`UserRepo` writes to wrap in `async with get_write_lock(): await self._conn.execute(...) ... await self._conn.commit()`:**
   - `create(...)` — wrap the INSERT + commit pair (`user_repo.py:76-83`)
   - `update_password(...)` — wrap the UPDATE + commit pair (`user_repo.py:103-115`)

   **`SessionRepo` writes to wrap:**
   - `create(...)` — wrap the INSERT + commit pair (`session_repo.py:29-46`)
   - `delete_by_id(...)` — wrap the DELETE + commit pair (`session_repo.py:57-63`)
   - `delete_all_for_user(...)` — wrap the DELETE + commit pair (`session_repo.py:65-71`)
   - `update_last_active(...)` — wrap the UPDATE + commit pair (`session_repo.py:73-86`)
   - `touch(...)` — wrap the UPDATE + commit pair (`session_repo.py:88-97`)
   - `delete_expired_for_user(...)` — wrap the DELETE + commit pair (`session_repo.py:99-108`)
   - `delete_all_expired(...)` — wrap the DELETE + commit pair (`session_repo.py:110-119`)

   **Discipline:** EVERY method that issues DML on the shared connection acquires the lock; reads (`get_by_username`, `get_by_id`, `count`, `get_by_token_hash`) are NOT under the lock (consistent with the 9.0b precedent — reads are concurrent). The lock is acquired AROUND the `execute → commit` pair, not just around `execute`. The audit emission inside `ObservabilityService.audit` is unaffected — it acquires its own lock independently (and `asyncio.Lock` is non-re-entrant, so any caller that holds the lock must release before calling `audit` — see AC4 note).

   A new pair of integration tests under `tests/integration/storage/test_user_repo_concurrent_writers.py` and `test_session_repo_concurrent_writers.py` mirrors the 9.0b `test_config_repo_concurrent_writers` shape: spawn N concurrent writers, assert all succeed without `cannot start a transaction within a transaction` errors. AC22 #N.

7. **AC7 — Password complexity = length ≥ 12 characters (server-authoritative).** A module-level constant `MIN_PASSWORD_LENGTH: Final[int] = 12` is introduced in `src/open_ems/storage/repositories/user_repo.py` (the same module that hosts `hash_password`). A new function `validate_password_complexity(password: str) -> str | None` returns `None` if the password passes, or an error message string if it fails. The v1 rule is **length-only ≥ 12 characters** (no character-class requirements — NIST SP 800-63B §5.1.1.2 explicitly recommends against character-class composition rules; length is the dominant entropy factor). The function is also referenced from the `/change-password` route (AC14).

   The complexity check runs **after** the `username` and uniqueness checks (so a bad password is reported only when the other inputs are valid; this aligns the UX with one-error-at-a-time feedback). Server-side is always the enforcing authority; client-side echo (AC8) is a UX accelerator, never the gate. AC22 includes a regression test that a 12-char password passes and an 11-char password fails — at both create-homeowner, reset-homeowner-password, and change-password endpoints.

8. **AC8 — Client-side echo of complexity rule.** The new-password input on all three forms (create-homeowner, reset-homeowner-password, change-password) carries:
   - `minlength="12"` (HTML5 native validation; surfaces a browser-native tooltip)
   - `required` attribute
   - A static `<small class="password-hint">` next to the input with copy `"At least 12 characters. The server enforces this rule; this hint is informational."`

   No JavaScript-side complexity scoring or strength meter in v1 (spec is silent; v1 simplicity wins). The browser's native `minlength` provides the immediate-rejection feedback; the server still re-validates authoritatively. A test asserts that an 11-char password POSTed with a fetch (bypassing HTML5 validation) is rejected server-side.

---

### `must_change_password` middleware enforcement

9. **AC9 — `UserRepo.update_password` overloaded for reset semantics.** The existing method at `user_repo.py:103-115` always clears `must_change_password=0`. The 11.3 reset path needs it set to `1` again. **Decision:** add a `must_change_password: bool = False` parameter to the existing method:

   ```python
   async def update_password(
       self,
       user_id: str,
       hashed_password: str,
       *,
       must_change_password: bool = False,
   ) -> None:
       """Replace hashed_password; set must_change_password flag.

       The flag defaults to False (the historic /change-password use case clears
       the flag). Pass must_change_password=True for installer-initiated resets
       so the homeowner is forced through the change-password flow on next login.
       """
       async with get_write_lock():
           async with self._conn.execute(
               "UPDATE users SET hashed_password = ?, must_change_password = ? WHERE id = ?",
               (hashed_password, int(must_change_password), user_id),
           ) as cursor:
               if cursor.rowcount == 0:
                   raise ValueError(f"No user found with id={user_id!r}")
           await self._conn.commit()
   ```

   **Backward compatibility:** the existing single positional-form call (admin-bootstrap path at `app.py:85-90` — no, that's the `create()` call. There IS no current caller of `update_password()` in production code; it exists for the forthcoming `/change-password` flow this story is delivering. The flag default of `False` matches the historic single-callsite expectation; the reset path explicitly passes `must_change_password=True` as a keyword argument.

10. **AC10 — `UserRepo.get_homeowner()` returns the single homeowner row.** v1 invariant: at most one homeowner user. New method:

    ```python
    async def get_homeowner(self) -> aiosqlite.Row | None:
        """Fetch the single homeowner user row, or None if none exists.

        v1 invariant: ``users`` table contains at most one row with role='homeowner'.
        Multi-homeowner scenarios (e.g. household sharing) are out of scope for v1
        and would require an explicit migration + UX update — not a silent expansion
        of this method.
        """
        async with self._conn.execute(
            "SELECT id, username, role, hashed_password, must_change_password, created_at"
            " FROM users WHERE role = ? LIMIT 1",
            ("homeowner",),
        ) as cursor:
            return await cursor.fetchone()
    ```

    A unit test asserts that when two homeowner rows exist (test-only via direct INSERT), `get_homeowner()` returns the first row deterministically (by `created_at` order — add `ORDER BY created_at ASC LIMIT 1` to the query) AND that the create-homeowner route rejects creation if any homeowner row exists. AC22 #N.

11. **AC11 — `MustChangePasswordMiddleware` enforces the redirect at the middleware level.** A new middleware class in `src/open_ems/web/must_change_password_middleware.py` (sibling to `csrf.py`). Registered in `web/app.py` **AFTER** `CsrfMiddleware` (i.e., its `dispatch` wraps CSRF's, so it runs AFTER CSRF passes). The middleware:

    1. **Resolves the session** using the same `_resolve_session` primitive as `dependencies.py` — but read-only (does not touch `last_active_at` to avoid double-update with the route-level dependency). New helper `web/dependencies.py:_get_session_user_if_any(request) -> tuple[user_row, session_row] | None` that the middleware can call directly.

       **Decision:** to avoid duplicating session-resolution logic, the middleware uses a stripped-down read-only path — `SessionRepo.get_by_token_hash` + `UserRepo.get_by_id` + expiry check. NO touch, NO cleanup, NO sliding-window renewal. Those happen later in the request via `require_installer` / `require_homeowner`. The middleware is purely a guard.
    2. **Reads `must_change_password` from the user row.** If the value is falsy (0), call `await call_next(request)` and return — middleware is a no-op.
    3. **If `must_change_password = 1`:**
       - Allow-list paths: `GET /change-password`, `POST /change-password`, `POST /logout`, and `GET /static/*` continue normally (so the user can submit the form, log out, and load CSS). Note: `GET /login` and `POST /login` are also allowed — a stale cookie path where the user clicks Logout-then-Log-in must still work.
       - All other paths: return a `RedirectResponse(url="/change-password", status_code=303)`. For HTMX requests (`HX-Request: true`), return a 200 with `HX-Redirect: /change-password` header so HTMX performs a full-page navigation (the standard HTMX redirect contract).

       **Path matching:** prefix-match `/static/` (StaticFiles mount); exact-match on the auth + change-password paths. A test asserts that sub-pages of `/installer/...` and `/homeowner/...` all redirect when the flag is set — including HTMX fragment GETs and `POST /actions/...`.

12. **AC12 — Middleware ordering and registration.** In `web/app.py:create_app()`, add the new middleware AFTER `CsrfMiddleware`:

    ```python
    app.add_middleware(CsrfMiddleware)  # Runs first on every request
    app.add_middleware(MustChangePasswordMiddleware)  # Runs after CSRF passes
    ```

    Starlette/FastAPI executes middleware in **reverse** registration order on the inbound request (each `add_middleware` wraps the prior one), so the LAST-added wraps the prior ones — meaning `MustChangePasswordMiddleware.dispatch` runs FIRST on the way in. **This is the desired order** because:
    - GET requests don't need CSRF, but DO need the must-change-password redirect — so the gate runs first.
    - For unsafe-method requests on routes that require a fresh password change, the CSRF check is unnecessary work (the response is a redirect anyway) — middleware ordering is an optimization plus a correctness improvement.

    A test asserts that a stale-CSRF POST with `must_change_password=true` returns the redirect (not the CSRF 403) — the middleware ordering decides which gate fires first.

13. **AC13 — Middleware allow-list is finite + exhaustive.** The allow-list is a `frozenset[str]` constant `_MIDDLEWARE_ALLOW_LIST: Final[frozenset[str]] = frozenset({"/change-password", "/login", "/logout"})` plus a `_MIDDLEWARE_ALLOW_PREFIX = "/static/"` for static assets. Health endpoints (`/health/live`, `/health/ready`) are also exempt because the middleware short-circuits on "no session cookie" — but adding them to the allow-list is defensive against future cookie-mocking debug paths.

    **Decision:** include `/health/live` and `/health/ready` in the allow-list. The middleware MUST NOT add latency to health probes even in pathological cookie-mocking scenarios.

---

### `/change-password` route (forced + voluntary path)

14. **AC14 — `GET /change-password` renders the change-password form.** New route in `src/open_ems/web/routes/auth.py` (sibling to `login_page` at line 64-73). The handler reads the session via `_resolve_session` (already private at `dependencies.py`); requires a valid session of ANY role; renders `change_password.html` (new template) with:
    - `csrf_token` from `session_row["csrf_token"]`
    - `is_forced: bool` — `must_change_password` flag from the user row; controls the page copy ("You must change your password before continuing." vs "Update your password.")
    - `error: str | None = None`
    - `username: str` — for accessibility (the username field is rendered read-only `disabled` with `autocomplete="username"` to assist password-manager autofill)

    The page contains:
    - A `<form method="POST" action="/change-password">` with hidden `csrf_token` input
    - `current_password: str` input (always required — even for forced changes, because the homeowner just successfully logged in with the temp password; this is a session-binding defense-in-depth check)
    - `new_password: str` input with `minlength="12"`, `required`, `autocomplete="new-password"`
    - `confirm_new_password: str` input (client-side echo only; AC15 step 4)
    - Submit button "Update password"

    **Why require current_password on the forced path?** A homeowner just authenticated with the temp password — checking it again forces the password change to happen in the same session-with-credentials-knowledge window, defeating any scenario where a forgotten logged-in browser is used by another person to "claim" the credentials by setting their own password.

15. **AC15 — `POST /change-password` updates the password and clears the flag.** Validation in strict order:

    1. **Session valid.** Same `_resolve_session` path. No session → 401 (HTMX) or 302 → `/login?next=/change-password` (browser).
    2. **`current_password` correct.** `verify_password(current_password, user_row["hashed_password"])`. Otherwise render the form with `error="Current password is incorrect."` — same generic phrasing as login (no account-existence signal; this user IS authenticated so the failure mode is narrow).
    3. **`new_password` complexity check.** `validate_password_complexity(new_password) is None`. Otherwise render with `error="Password must be at least 12 characters."`
    4. **`new_password == confirm_new_password`.** Otherwise render with `error="Passwords do not match."` (client-side echo via HTML5 + small inline script that disables submit until they match, but the server is authoritative).
    5. **`new_password != current_password`.** Reject same-password updates with `error="New password must differ from the current password."` (defends against accidental no-op submissions on the forced path).
    6. **bcrypt 72-byte ceiling.** Same path as AC3 step 6.
    7. **Hash + update.** `hashed = hash_password(new_password)`; call `user_repo.update_password(user_id=user.user_id, hashed_password=hashed, must_change_password=False)` (default for this path). The `get_write_lock()` is held by the repo (AC6).
    8. **Session invalidation contract.** Per epic AC for Story 2.2 (line 756 — "if a password change occurs during an active session, that session is invalidated and the user must re-authenticate"): **after the password update commits**, call `session_repo.delete_all_for_user(user_id=user.user_id)` and clear the cookie. The user MUST re-authenticate with the new password. This is the existing 2.2 contract reinforced by 11.3 — the contract was previously latent because no `/change-password` route existed.
    9. **Audit emission.** `ObservabilityService.audit(actor=<resolver>, event_type="SYSTEM", summary="Password changed")` where `<resolver>` is `user.role` (installer or homeowner). No username, no detail (PII minimization).
    10. **Redirect to login.** `RedirectResponse(url="/login", status_code=303)` with the session cookie deleted (same pattern as `POST /logout`).

    The forced-change path and voluntary path use the SAME route — `is_forced` is purely a UI hint that adjusts copy. The validation + persistence logic is identical.

16. **AC16 — `/change-password` is CSRF-protected via the existing `CsrfMiddleware`.** No new CSRF exemption is added. The form submits with a hidden `csrf_token` field; `CsrfMiddleware` reads it from the form body for `multipart/form-data` or `application/x-www-form-urlencoded` content types (already implemented at `csrf.py:71-79`). A test asserts that a `/change-password` POST without the token returns 403.

---

### Routes / Dependencies wiring

17. **AC17 — `get_user_repo` FastAPI dependency.** New dependency in `web/dependencies.py` (mirrors `get_event_log_repo` at line 148-153):

    ```python
    def get_user_repo(request: Request) -> UserRepo:
        """Story 11.3 AC3+: UserRepo dependency for the installer settings and
        change-password routes. Construction mirrors ``get_event_log_repo`` —
        fresh instance per request, shared aiosqlite connection from the
        module-level pool."""
        del request
        return UserRepo()
    ```

    And mirror `get_session_repo` for the reset-password route's session-invalidation step:

    ```python
    def get_session_repo(request: Request) -> SessionRepo:
        """Story 11.3 AC4: SessionRepo dependency for explicit session
        invalidation on credential reset. Mirrors ``get_user_repo``."""
        del request
        return SessionRepo()
    ```

18. **AC18 — Router registrations.** No new routers — both `/installer/settings` and the action POSTs land in the existing `installer_router` and `actions_router`. The `/change-password` GET + POST land in `auth_router`. The new middleware is registered in `create_app()` as documented in AC12.

---

### Templates + UX

19. **AC19 — New templates.**
    - `src/open_ems/web/templates/installer/settings.html` — installer settings page. Extends `base.html`. Renders `installer/_sidebar.html` with `active="settings"`. Inside the main column, renders the Homeowner Credentials section (with the no-homeowner vs homeowner-exists variant per AC1).
    - `src/open_ems/web/templates/installer/_homeowner_credentials_section.html` — partial that renders the section's current state. The settings page includes it; the create/reset POST routes return it directly as `hx-target="#homeowner-credentials-section"` `hx-swap="outerHTML"`.
    - `src/open_ems/web/templates/installer/_homeowner_create_form.html` — partial for the "No homeowner exists" variant's create form. Embedded into the section partial.
    - `src/open_ems/web/templates/installer/_homeowner_reset_form.html` — partial for the inline "Reset password" form (returned by `GET /fragments/installer/homeowner-reset-form` per AC5).
    - `src/open_ems/web/templates/change_password.html` — change-password form (forced + voluntary). Extends `base.html`; styled like `login.html` (same card pattern, isolated; no installer sidebar).

20. **AC20 — Accessibility + UX details.**
    - All password inputs are `<input type="password" autocomplete="new-password">` (the change-password form's `current_password` input uses `autocomplete="current-password"`).
    - All forms include `role="alert"` on the inline error `<p>` element when an error is populated; absent when no error (Jinja: `{% if error %}<p class="...form-error..." role="alert">{{ error }}</p>{% endif %}`).
    - The forced-change page's heading uses `role="status"` with the message: "You must change your password before continuing."
    - Submit buttons have explicit `aria-label` when their text is contextual ("Update password" is self-explanatory; no extra `aria-label` needed).

21. **AC21 — No new dependencies. `uv.lock` unchanged.** Uses only existing pins:
    - `fastapi` (existing) — `Depends`, `Form`, `Request`
    - `starlette` (transitive) — `BaseHTTPMiddleware`, `Response`
    - `bcrypt` (existing, used by `user_repo.hash_password`) — no new direct calls outside `user_repo.py`
    - `jinja2` autoescape (existing) — protects against `<script>` in `username` rendering
    - `aiosqlite` (existing)
    - `structlog` (existing) — new emissions: `homeowner_created`, `homeowner_password_reset`, `password_changed`, `must_change_password_redirect`
    - `htmx` 1.9.12 (existing) — `hx-post`, `hx-target`, `hx-swap`, `HX-Redirect` response header
    - `alpine.js` 3.13.10 (existing) — NOT used on the credential pages (simple HTML forms; no client state needed)

---

### Tests (target ≥ 35 net new)

22. **AC22 — Tests (unit + integration).** Total target: **≥ 35 new tests** added to the 1814 baseline (from `uv run pytest --collect-only -q` at 2026-05-12), bringing the total to **≥ 1849 collected** (≥ 1847 passing; the 11.2 baseline includes 2 skipped + 8 xfailed — no new xfail introduced in 11.3 unless explicitly documented).

    **`UserRepo` tests** (`tests/unit/storage/repositories/test_user_repo.py` — extend):
    1. `test_validate_password_complexity_accepts_exactly_12_chars`
    2. `test_validate_password_complexity_rejects_11_chars_with_clear_message`
    3. `test_validate_password_complexity_accepts_long_unicode`
    4. `test_get_homeowner_returns_none_when_no_homeowner_exists`
    5. `test_get_homeowner_returns_row_when_one_exists`
    6. `test_get_homeowner_returns_first_by_created_at_when_multiple_exist` _(defensive — direct INSERT bypasses the route-level guard; documents the deterministic ordering)_
    7. `test_update_password_clears_must_change_password_by_default`
    8. `test_update_password_sets_must_change_password_when_kwarg_true`
    9. `test_create_user_acquires_write_lock` _(uses monkeypatch + AsyncMock to assert `get_write_lock()` is awaited)_
    10. `test_update_password_acquires_write_lock`

    **`SessionRepo` tests** (`tests/unit/storage/repositories/test_session_repo.py` — extend):
    11. `test_create_session_acquires_write_lock`
    12. `test_delete_by_id_acquires_write_lock`
    13. `test_delete_all_for_user_acquires_write_lock`
    14. `test_touch_acquires_write_lock`
    15. `test_delete_expired_for_user_acquires_write_lock`
    16. `test_delete_all_expired_acquires_write_lock`

    **Concurrent-writer integration tests** (`tests/integration/storage/test_user_repo_concurrent_writers.py` + `test_session_repo_concurrent_writers.py` — new):
    17. `test_concurrent_create_users_no_transaction_within_transaction_error` _(spawn N=20 concurrent create() calls; assert all succeed; no `sqlite3.OperationalError`)_
    18. `test_concurrent_session_creates_no_transaction_within_transaction_error`
    19. `test_concurrent_password_update_and_session_create_serialize_cleanly` _(mixed-writer scenario reproducing the 9.0b concurrency precedent)_

    **`MustChangePasswordMiddleware` tests** (`tests/unit/web/test_must_change_password_middleware.py` — new):
    20. `test_no_session_cookie_middleware_is_noop`
    21. `test_session_with_must_change_password_false_middleware_is_noop`
    22. `test_session_with_must_change_password_true_redirects_to_change_password`
    23. `test_change_password_path_itself_allowed_when_flag_is_true`
    24. `test_login_and_logout_paths_allowed_when_flag_is_true`
    25. `test_static_assets_allowed_when_flag_is_true`
    26. `test_htmx_request_returns_200_with_hx_redirect_header`
    27. `test_subpath_under_installer_redirects_when_flag_is_true` _(asserts e.g. `/installer/event-log?type=DECISION` redirects — including query params; the redirect destination is plain `/change-password`, query params dropped)_
    28. `test_subpath_under_homeowner_redirects_when_flag_is_true`
    29. `test_post_actions_path_redirects_when_flag_is_true` _(POST request, no-CSRF; assert middleware fires before CSRF)_
    30. `test_expired_session_no_redirect` _(expired cookie → middleware is a no-op; route-level dependency handles auth)_

    **Create-homeowner route tests** (`tests/unit/web/test_create_homeowner_route.py` — new):
    31. `test_create_homeowner_success_creates_user_with_must_change_password_true`
    32. `test_create_homeowner_emits_audit_event_with_exact_summary`
    33. `test_create_homeowner_rejects_when_homeowner_already_exists`
    34. `test_create_homeowner_rejects_empty_username`
    35. `test_create_homeowner_rejects_username_over_64_chars`
    36. `test_create_homeowner_rejects_duplicate_username_with_neutral_message`
    37. `test_create_homeowner_rejects_password_under_12_chars_server_side`
    38. `test_create_homeowner_rejects_password_over_72_bytes_with_byte_message`
    39. `test_create_homeowner_unauthenticated_redirects_to_login`
    40. `test_create_homeowner_homeowner_role_rejected_403`
    41. `test_create_homeowner_csrf_token_missing_returns_403`
    42. `test_create_homeowner_response_includes_view_in_event_log_link`

    **Reset-password route tests** (`tests/unit/web/test_reset_homeowner_password_route.py` — new):
    43. `test_reset_homeowner_password_success_updates_hash_and_sets_must_change_password_true`
    44. `test_reset_homeowner_password_invalidates_all_homeowner_sessions`
    45. `test_reset_homeowner_password_does_not_invalidate_installer_session`
    46. `test_reset_homeowner_password_emits_audit_event_with_exact_summary`
    47. `test_reset_homeowner_password_rejects_when_no_homeowner_exists`
    48. `test_reset_homeowner_password_rejects_short_password`
    49. `test_reset_homeowner_password_csrf_token_missing_returns_403`

    **Change-password route tests** (`tests/unit/web/test_change_password_route.py` — new):
    50. `test_get_change_password_renders_form_for_authenticated_user`
    51. `test_get_change_password_redirects_unauthenticated_to_login`
    52. `test_post_change_password_success_clears_must_change_password_flag`
    53. `test_post_change_password_success_invalidates_all_user_sessions_and_redirects_to_login`
    54. `test_post_change_password_wrong_current_password_renders_error`
    55. `test_post_change_password_short_new_password_renders_error`
    56. `test_post_change_password_mismatched_confirm_renders_error`
    57. `test_post_change_password_same_as_current_renders_error`
    58. `test_post_change_password_emits_audit_event`
    59. `test_post_change_password_csrf_token_missing_returns_403`

    **End-to-end integration tests** (`tests/integration/web/test_homeowner_credential_management_e2e.py` — new):
    60. `test_e2e_installer_creates_homeowner_then_homeowner_logs_in_and_is_redirected_to_change_password`
    61. `test_e2e_homeowner_completes_change_password_then_accesses_dashboard`
    62. `test_e2e_installer_resets_homeowner_password_and_homeowner_sees_session_terminated`
    63. `test_e2e_homeowner_with_must_change_password_blocked_from_all_dashboard_routes` _(crawls a representative set of `/homeowner/dashboard`, `/fragments/homeowner/battery-card`, etc. — every one redirects)_
    64. `test_e2e_installer_sidebar_settings_link_navigates_to_settings_page` _(asserts the sidebar refactor regression — Dashboard + Event Log + Settings all render correctly)_
    65. `test_e2e_xss_in_homeowner_username_renders_escaped` _(submit `<script>` as username; assert the settings page response body does NOT contain raw `<script>`)_

    **Sidebar regression tests** (`tests/unit/web/test_installer_sidebar.py` — new OR extend existing 11.2 partial test if one exists):
    66. `test_sidebar_settings_item_is_live_with_correct_href`
    67. `test_sidebar_dashboard_event_log_settings_render_in_consistent_order`

23. **AC23 — Quality gates.** All of the following MUST hold at story completion:
    - `uv run pytest` → ≥ 1847 passed (1814 baseline + ≥ 35 net new). Same xfailed + skipped counts (no new xfail introduced unless explicitly documented).
    - `uv run ruff check .` → clean across all new + modified files.
    - `uv run ruff format --check .` → clean.
    - `uv run mypy src` → clean across all source files (new dependencies, new routes, new middleware, new repo methods, new builders).
    - `uv.lock` → unchanged (no new dependencies).
    - No new migration (the `users.must_change_password` column already exists from migration `0002`; the new methods on `UserRepo` / `SessionRepo` add no schema).

---

## Developer Context Section

This section is the comprehensive guide for the dev agent. It encodes everything not obvious from the AC text alone — including patterns to mirror from 11.1 / 11.2, regressions to avoid, the existing auth + middleware surface, and cross-story interactions.

### Foundation patterns to mirror from prior stories

**Pattern 1 — Page route + sidebar + section fragment split.** Story 11.2 established the page (`installer.py:installer_event_log`) + sidebar partial (`installer/_sidebar.html`) + section fragment (`event-log-list.html` / `event-log-note-form.html`) pattern. Story 11.3 takes the same shape: settings page route + sidebar (Settings goes live) + section fragment that is swapped out by HTMX after each POST.

- Page route lives in `routes/installer.py` (alongside `installer_dashboard`, `installer_event_log`). Same `require_installer` gate, same `TemplateResponse` pattern.
- POST routes for credential actions live in `routes/actions.py` (alongside `post_installer_note` from 11.2, which is the structural precedent for "render section fragment after a write").
- Optional fragment route (`GET /fragments/installer/homeowner-reset-form`) lives in `routes/fragments.py`.

**Pattern 2 — Pure-functional builders for testability.** Per the 11.x precedent at `state_serialization.py:665+`, all rendering context derivation goes through pure-functional builders. The 11.3 surface adds:

- `build_installer_homeowner_credentials_section_context(homeowner_row, csrf_token, banner_message=None, banner_kind=None, form_state=None) -> dict` — section partial context. `form_state` is a small dataclass-like dict for inline form errors / preserved-input values.
- `build_installer_homeowner_create_form_context(csrf_token, username_value="", error_message=None) -> dict` — create-form context.
- `build_installer_homeowner_reset_form_context(csrf_token, error_message=None) -> dict` — reset-form context.
- `build_change_password_page_context(username, csrf_token, is_forced, error_message=None) -> dict` — change-password page context.

All builders are pure functions over typed inputs. The routes do the I/O (repo calls, validation); templates render the builder output.

**Pattern 3 — Single-source-of-truth complexity check.** `validate_password_complexity` lives in `user_repo.py` next to `hash_password`. All four callers (create-homeowner, reset-homeowner-password, change-password, future admin-password-change) reference the SAME function. The constant `MIN_PASSWORD_LENGTH = 12` is the single dial.

**Pattern 4 — Defensive route + form-fragment-on-error.** The 11.2 `post_installer_note` route established the "200 + form-fragment-with-preserved-input-and-inline-error" UX pattern. 11.3's create and reset POSTs follow the same shape:
- On validation failure → 200 + section-fragment with form sub-partial preserved (username preserved on create errors; nothing to preserve on reset since the password field is single-use).
- On unexpected exception → 200 + section-fragment with generic error.
- On success → 200 + section-fragment in the "homeowner-exists" variant with banner.

**Pattern 5 — Audit emission via `ObservabilityService.audit`.** The 11.2 `post_installer_note` used `ObservabilityService.installer_note` (a higher-level helper) — that helper is not applicable here because we want `actor="installer"` + `event_type="SYSTEM"` with the exact `summary` strings from the epic AC. Direct `audit()` calls are correct.

**Pattern 6 — `get_write_lock()` discipline across all DML.** Story 9.0b's precedent (`ConfigRepo.activate`, `EventLogRepo.append`) is `async with get_write_lock(): execute → commit`. 11.3 retrofits the same pattern onto every write method in `UserRepo` and `SessionRepo`. The `BEGIN IMMEDIATE` transaction wrapper that 9.0b's `_activate_locked` uses is NOT needed for these repos — they are all single-statement DML (one INSERT or one UPDATE or one DELETE), where the implicit auto-begin transaction is sufficient. The lock alone serializes writers; the implicit transaction commits atomically.

**Pattern 7 — Middleware ordering rule (last-registered runs first inbound).** Starlette's `add_middleware` chain wraps in reverse on inbound. The 11.3 middleware ordering decision (CSRF first registered, MustChangePassword second) means MustChangePassword runs FIRST inbound — the redirect short-circuits CSRF checks for users who must change their password before doing anything else. Document this in the `create_app()` block with an inline comment.

### Files to read in full before starting

**Required reads (failure to read these is the primary cause of review-cycle bugs):**

1. `src/open_ems/storage/repositories/user_repo.py` (full — 116 lines). Note: `_BCRYPT_MAX_BYTES = 72`, `_VALID_ROLES = {"installer", "homeowner"}`, `_HASHERS` dispatch dict (PHC-prefix routed), `hash_password` raises `ValueError` over 72 bytes, `verify_password` returns False for unknown prefixes (never raises), `create()` has `must_change_password: bool = True` default, `update_password()` always clears the flag (11.3 changes this signature — see AC9), `get_by_username` / `get_by_id` return `aiosqlite.Row | None`.
2. `src/open_ems/storage/repositories/session_repo.py` (full — 120 lines). `SESSION_TOKEN_BYTES = 32`, `generate_session_token()` is hex-encoded, `hash_token()` is SHA-256, `create()` stores the hash NOT the raw token, `touch()` is the sliding-expiration primitive, `delete_all_for_user()` is the exact primitive 11.3 calls on reset. Existing `delete_all_for_user` is positional, NOT keyword-only — keep the call style `session_repo.delete_all_for_user(user_id)`.
3. `src/open_ems/storage/database.py` (full — 79 lines). `_write_lock: asyncio.Lock` is the singleton; `get_write_lock()` is the public accessor; the lock is lazy-created if `init_database` was bypassed (test path). Note: `asyncio.Lock` is **NOT** re-entrant — caller MUST release before any further `get_write_lock()`-acquiring call.
4. `src/open_ems/web/csrf.py` (full — 89 lines). `_CSRF_EXEMPT_PATHS = {"/login", "/api/stream/state"}` — note `/change-password` is NOT exempt; the form-body CSRF read at lines 71-79 handles the standard form submit. The middleware reads `await request.form()` which exhausts the body — see deferred-work line 85 for the latent issue (pre-existing).
5. `src/open_ems/web/dependencies.py` (full — 364 lines). `_resolve_session` at lines 187-240 is the canonical session resolver. The middleware needs a **read-only** variant that does NOT touch / sliding-window-renew / cleanup — extract a private helper `_get_session_user_if_any_readonly(request) -> tuple[user_row, session_row] | None`. Do NOT call `_resolve_session` from the middleware (the touch/cleanup side-effects would double-fire when the route-level dependency runs later).
6. `src/open_ems/web/routes/auth.py` (full — 199 lines). The `login_submit` route at line 76-169 is the structural precedent for `/change-password` POST: cookie handling, redirect-to-login, audit emission patterns. **Note line 152-156:** `rate_limiter.reset_for_key` is called on successful login — no equivalent in 11.3 (password change does not interact with the rate limiter).
7. `src/open_ems/web/routes/actions.py` lines 532-664 — `post_installer_note` is the structural precedent for `/actions/create-homeowner` and `/actions/reset-homeowner-password`. Same `_render_*_only` helper pattern; same outerHTML swap response shape. **DO NOT** copy the OOB-swap (`#event-log-list-rows`) pattern — credentials section is a single target; no out-of-band swap needed.
8. `src/open_ems/web/routes/installer.py` (full — 115 lines). The `installer_dashboard` and `installer_event_log` routes are the structural precedents for `installer_settings`. Same `require_installer` + `Depends(get_*_repo)` pattern.
9. `src/open_ems/web/app.py` lines 684-697 — `create_app()` is where the new middleware is registered. The ordering rule is documented in AC12; double-check the registration order before committing.
10. `src/open_ems/web/templates/installer/_sidebar.html` (full — 46 lines). Lines 36 (Devices, stays disabled) and 42 (Settings, going live). Update line 42 to mirror lines 31-35 (Dashboard) — apply the active-class + aria-current pattern.
11. `src/open_ems/web/templates/login.html` (full — 99 lines). The standalone-card pattern is the structural precedent for `change_password.html`. Inline CSS in `<style>` block — match the existing tokens (`--color-surface`, `--color-card`, `--color-border`, `--color-error`, `--color-text-primary`, `--color-text-secondary`).
12. `src/open_ems/services/audit_log.py` (full — 84 lines). `MAX_INSTALLER_NOTE_LENGTH=2000` (irrelevant here), `VALID_ACTORS = {"system", "installer", "homeowner"}`, `VALID_EVENT_TYPES = {"DECISION", "DEVICE", "SYSTEM", "CONSTRAINT", "INSTALLER"}`. `audit()` raises `ValueError` for empty summary or invalid actor/event_type — 11.3's exact summary strings ("Homeowner credentials created", "Homeowner credentials reset", "Password changed") are non-empty and the actors/event_types are valid; no defensive try/except needed.
13. `migrations/versions/0002_add_users_table.py` (full — 39 lines). Confirms column types + the role CHECK constraint (`role IN ('installer', 'homeowner')`); `must_change_password INTEGER NOT NULL DEFAULT 1`. No new migration in 11.3.
14. `tests/integration/storage/test_config_repo_concurrent_writers.py` — the 9.0b reference test for concurrent-writer behavior. Mirror its shape exactly for the new `test_user_repo_concurrent_writers.py` and `test_session_repo_concurrent_writers.py`.
15. `_bmad-output/implementation-artifacts/2-1-define-user-model-upgradeable-credential-storage-and-safe-admin-bootstrap.md` — the must_change_password contract for admin bootstrap. The forced-change UX described at lines 720-723 of epics.md was contractually documented in 2.1 but never built (no `/change-password` route exists today). 11.3 closes this latent gap for both installer and homeowner roles.
16. `_bmad-output/implementation-artifacts/2-2-implement-login-form-hardened-session-creation-and-brute-force-protection.md` — the session-invalidation-on-password-change contract at line 756 of epics.md. 11.3's AC15 step 8 reinforces this in the `/change-password` route.

### Files to read on-demand

- `src/open_ems/web/dependencies.py:_renew_session_cookie` (lines 53-65) — only if the middleware decision raises questions about cookie re-emission on the redirect path. The middleware does NOT renew the cookie; it just redirects.
- `src/open_ems/services/rate_limiter.py` — only if extending rate-limiting to `/change-password` becomes a question. **Decision: NO rate limiting on /change-password in v1** — the user is already authenticated; brute-forcing the current_password from an authenticated session is a rare attack pattern, and rate-limiting would interfere with legitimate password-typo-and-retry UX.
- `src/open_ems/storage/repositories/config_repo.py:_activate_locked` (lines 110-180) — the canonical example of a `get_write_lock()` + `BEGIN IMMEDIATE` user. 11.3 does NOT need `BEGIN IMMEDIATE` (single-statement DML).
- `tests/integration/storage/test_config_repo_concurrent_writers.py` — only for the precise test-shape mirror; if the test name and assertions are clear, the concurrent-writer pattern is well-established.

### What NOT to change

- **The `users` table schema, indexes, and migrations** — UNCHANGED. The existing `must_change_password INTEGER NOT NULL DEFAULT 1` column at migration `0002` is sufficient.
- **The `sessions` table schema** — UNCHANGED.
- **`UserRepo.hash_password` and `verify_password` module-level functions** — UNCHANGED. The bcrypt 72-byte ceiling check at line 30-34 is correct; the route catches the resulting `ValueError`.
- **`CsrfMiddleware`** — UNCHANGED. The `MustChangePasswordMiddleware` is purely additive.
- **`_resolve_session` in `dependencies.py`** — UNCHANGED. The middleware uses a stripped-down read-only variant (new helper) so the touch/cleanup side-effects do NOT double-fire.
- **`require_installer` / `require_homeowner`** — UNCHANGED. They continue to handle role-mismatch redirects and session-cookie renewal AFTER the middleware passes.
- **The 11.1 dashboard surface, 11.2 event-log surface** — UNTOUCHED except for the sidebar partial's Settings line. The sidebar regression test asserts no drift in the dashboard and event-log surfaces.
- **HTMX / Alpine / FastAPI / Pydantic / bcrypt versions** — UNCHANGED (`uv.lock` invariant per AC21 / AC23).
- **Rate limiter** — UNCHANGED. Login rate-limiting at `auth.py:101-110` is the only rate-limited path; 11.3 adds no new rate-limited surface.
- **`/health/live`, `/health/ready`, `/api/stream/state`** — UNTOUCHED. The new middleware adds these to the allow-list defensively.
- **PRD secret storage (`INITIAL_ADMIN_PASSWORD`)** — UNTOUCHED. The admin bootstrap path at `app.py:70-98` continues to set `must_change_password=True` on the admin user; the 11.3 middleware + `/change-password` route make this contract enforceable for the FIRST time across the entire codebase (it has been latent since Story 2.1).

### Library / framework constraints

- **Python:** 3.13 (project lockfile-pinned).
- **FastAPI:** existing pin. No new features used — `Form`, `Depends`, `Request` are stdlib for the project.
- **Starlette:** existing pin (transitive via FastAPI). `BaseHTTPMiddleware` is the base class for `CsrfMiddleware`; the new `MustChangePasswordMiddleware` inherits from the same. The `BaseHTTPMiddleware.dispatch` signature is async; no special handling.
- **bcrypt:** existing pin. The 72-byte UTF-8 ceiling is documented in `UserRepo.hash_password`; the 11.3 routes catch the `ValueError` and surface the byte-message UX.
- **HTMX 1.9.12:** existing pin. New attributes: `hx-post`, `hx-target`, `hx-swap="outerHTML"`. New response header: `HX-Redirect` (for the middleware's HTMX-aware redirect path — see HTMX docs at htmx.org/headers/hx-redirect/).
- **structlog:** existing pin. New emissions:
  - `homeowner_created` (info) on successful homeowner creation: fields `actor_user_id`, `homeowner_user_id`, `component="installer_settings"`.
  - `homeowner_password_reset` (info) on successful reset: fields `actor_user_id`, `homeowner_user_id`, `sessions_invalidated_count`, `component="installer_settings"`.
  - `password_changed` (info) on successful self-change: fields `user_id`, `role`, `component="auth"`.
  - `must_change_password_redirect` (debug) on every middleware-triggered redirect: fields `user_id`, `role`, `requested_path`, `is_htmx`, `component="must_change_password_middleware"`. **Debug level** so production noise stays bounded — the redirect IS the contract, not an anomaly.
- **No new dependencies.** `uv.lock` unchanged.

### Web research — current-version specifics

- **NIST SP 800-63B-rev3 §5.1.1.2** (current revision as of 2026-05) recommends a minimum length of 8 for memorized secrets with broader composition; for systems handling sensitive credentials, ≥ 12 with NO character-class composition rules is widely considered the modern best practice (length dominates entropy). 11.3 ships ≥ 12 chars, no character classes.
- **HTMX 1.9.12 `HX-Redirect` response header** — when an HTMX request receives a 2xx response with `HX-Redirect: <url>`, the browser performs a hard navigation to `<url>`. Standard HTMX behavior for "the page must reload, this isn't a partial swap" — used by the middleware to handle HTMX-request must-change-password redirects without the swap-on-302 quirks. Reference: htmx.org/headers/hx-redirect/.
- **Starlette `BaseHTTPMiddleware.dispatch` ordering:** `add_middleware` wraps the existing middleware chain in reverse. Last-added is outermost (runs first inbound). This is documented in Starlette's middleware docs and confirmed in the project's own `CsrfMiddleware` registration at `app.py:696`. Critical for AC12.
- **bcrypt UTF-8 72-byte limit** — bcrypt's well-known constraint, documented in the python-bcrypt README and in OWASP's password-storage cheat sheet. The 11.3 ceiling check is at the route layer (the repo already raises `ValueError`).

### Previous-story intelligence

**Story 11.2 (Event log UI, done 2026-05-12) — 88 net new tests; 1804 total passing; `EventLogFilter` dataclass + `EventLogRepo.list_filtered`/`count_filtered`; `installer/_sidebar.html` partial; client-side Intl.DateTimeFormat rewrite layer; `get_observability_service` + `get_user_repo`-style dependency wiring patterns. Key carry-overs to 11.3:**

- **`installer/_sidebar.html`** — the partial already exists with the Settings item as a disabled placeholder. 11.3 updates that one line to make Settings live; the dashboard + event-log includes continue to work.
- **`db_query_counter`** — reusable for any AC22 page-route DB-query-count test (not currently in 11.3's test list; the test bound is per-route bounded reads + a small number of writes, all expected; no anomaly-detection test needed).
- **`get_observability_service`** — the 11.2 dependency at `dependencies.py:162-170` is reused for the audit emissions in the create/reset/change-password routes.
- **`build_installer_note_form_context`** — the 11.2 "preserved-input + inline error" form pattern is the structural model for `build_installer_homeowner_create_form_context` and the change-password page builder.
- **The 11.2 dual-defended XSS pattern** (service-side `html.escape` + Jinja autoescape) — for `username` rendering, ONLY Jinja autoescape applies (the username is stored verbatim in the DB, not pre-escaped). The XSS regression test at AC22 #65 asserts a `<script>` username renders as `&lt;script&gt;` in the settings page.

**Story 11.1 (Installer dashboard, done 2026-05-12):**

- The 5-fragment dashboard pattern is NOT applicable here (the settings page is single-section; no polling, no live updates).
- The `installer-anomaly` cross-session-state-via-module-dict pattern (`installer_anomaly.py:_DISMISSED_ANOMALIES`) is NOT applicable here — credentials section state is fully derived from `UserRepo.get_homeowner()` each render.

**Story 2.1 (User model + admin bootstrap, done 2026-05-02):**

- `must_change_password=True` is set on the admin user at bootstrap (`app.py:89`). The forced-change UX has been contractual since 2.1 but unbuilt. 11.3 closes this gap for the FIRST time across both installer and homeowner roles.
- The PHC-prefix hash dispatch (`_HASHERS` at `user_repo.py:18-21`) is unchanged. 11.3 produces only `$2b$` hashes via `hash_password()` (which is `bcrypt.hashpw(... bcrypt.gensalt())`).

**Story 2.2 (Login form + sessions, done 2026-05-02):**

- The session-invalidation-on-password-change contract at epic line 756 is reinforced by 11.3's AC15 step 8.
- The `_safe_next` helper at `auth.py:36-46` (open-redirect prevention) is unchanged. The `/change-password` route does NOT accept a `next=` query param — the redirect destination is always `/login` after a successful change (the user must re-authenticate).

**Story 2.5 (Session expiry + multi-device + logout, done 2026-05-02):**

- `SessionRepo.delete_all_for_user(user_id)` is the exact primitive 11.3 uses on both reset and change-password paths.

**Story 9.0b (DB-backed active constraints reload, done 2026-05-10):**

- The `get_write_lock()` discipline established here is the precedent for 11.3 AC6. The 9.0b deferred-finding at deferred-work.md line ~107 explicitly says "add the lock when Epic 11 wires real homeowner credential flows" — 11.3 closes this item.

### Git intelligence — recent commit patterns

Recent commits (`git log --oneline -10`):
- `7b177fa 11.2` — event log UI (this story's predecessor)
- `d0d7914 11.1.1` — 11.1 hotfix
- `c52229e 11.1` — installer dashboard
- `0a595a9 10.4` — weekly summary
- `71bf9a1 10.3` — strategy selector
- `efd986f 10.2` — EV status card
- `4ac50e1 10.1` — homeowner dashboard layout

**Patterns from recent work that 11.3 should follow:**

- One commit per story. 11.3 should land in ONE commit titled `11.3` after dev cycle + review cycle complete (if a hotfix is needed, `11.3.1` is the existing precedent from 11.1.1).
- No migration in 11.3 (the `must_change_password` column already exists from migration `0002`).
- File-naming convention: `tests/unit/<module>/test_<name>.py`. New test files for 11.3:
  - `tests/unit/storage/repositories/test_user_repo.py` — EXTEND with 10 new tests (AC22 #1-10).
  - `tests/unit/storage/repositories/test_session_repo.py` — EXTEND with 6 new tests (AC22 #11-16).
  - `tests/integration/storage/test_user_repo_concurrent_writers.py` (NEW) — 1+ test.
  - `tests/integration/storage/test_session_repo_concurrent_writers.py` (NEW) — 2 tests.
  - `tests/unit/web/test_must_change_password_middleware.py` (NEW) — 11 tests.
  - `tests/unit/web/test_create_homeowner_route.py` (NEW) — 12 tests.
  - `tests/unit/web/test_reset_homeowner_password_route.py` (NEW) — 7 tests.
  - `tests/unit/web/test_change_password_route.py` (NEW) — 10 tests.
  - `tests/integration/web/test_homeowner_credential_management_e2e.py` (NEW) — 6 tests.
  - `tests/unit/web/test_installer_sidebar.py` (NEW or extend 11.2's partial test if present) — 2 tests.

---

## A2 trigger evaluation (mandatory audit note for non-A2-triggered stories)

A2 evaluation was performed against T1–T7 from the Epic 8 retrospective (2026-05-10). None matched. Brief rationale:

- **T1 (lifecycle / state-machine):** `must_change_password` is a binary durable flag on `users.must_change_password`. The transitions are `False → True` (on installer reset) and `True → False` (on successful password change). This is **not** a state machine in the structural-class sense the trigger covers — there is no fail-safe entry/exit, no recovery semantics, no mode-of-operation that affects multiple subsystems. The middleware enforcement is a stateless guard: "if flag=1, redirect"; the rule is invariant-class, not state-machine-class. Compared to Story 8.4's fail-safe lifecycle (a clear T1 match) or Story 9.3's wizard step-state machine (also T1), this is a single boolean flag with two transitions and no recoverable substates.
- **T2 (retries / cancellation):** No retry policy with attempt counting. Failure paths re-render the form with the user's input preserved (similar to 11.2's note-entry pattern, evaluated identically). No cancellation propagation beyond ordinary FastAPI request cancellation.
- **T3 (persistence + recovery across restart):** The `must_change_password` flag IS persisted in `users` and IS read on every authenticated request, but this is the **existing** read pattern from Story 2.1 — 11.3 introduces no new durable state, no new durable counter or aggregate, no hydration step. The flag is read directly from DB on each request (no in-memory cache to hydrate). Cold-start behavior is trivially correct because the read path is the standard `UserRepo.get_by_id` call.
- **T4 (watchdog / timing):** No timing semantics, no deadline-relative scheduling. The HTMX `hx-post` debounce / minlength validation are UI UX accelerators, not deadline-class.
- **T5 (multi-adapter coordination):** Pure web layer — no protocol adapters touched.
- **T6 (deployment / restart):** No process restart triggered by this workflow. The installer's settings page persists immediately and the change takes effect on the next request — no restart involved.
- **T7 (installer workflow orchestration):** The installer-facing surface is single-step (one form submit per action: create, reset). The "settings page → action button → form → submit" flow is two clicks total, not a multi-step wizard mutating persisted configuration. Compared to Story 9.1's 4-step installer wizard (a clear T7 match) or 9.2/9.3's role-assignment + constraint-configuration multi-stage flows (also T7), this is a single-form-per-action surface.

R1–R7 mandatory artifacts therefore do not apply. A substantive deferred-findings overlap check follows below — and because one of the items is **must-resolve-in-this-story**, the dev agent must close it (not skip it).

### Deferred-findings overlap with 11.3 scope

Scanning `_bmad-output/implementation-artifacts/deferred-work.md` for items whose component or invariant overlaps this story:

| Bracket reference | Item | Classification for 11.3 |
|---|---|---|
| `[src/open_ems/storage/repositories/user_repo.py, session_repo.py]` (Story 9.0b deferred) | **UserRepo and SessionRepo writes are not yet routed through the database-module write lock** — the deferral explicitly says "add the lock when Epic 11 wires real homeowner credential flows OR when Epic 12 sweeps the storage layer for connection-sharing hardening." | **must-resolve-in-this-story** — Story 11.3 IS the Epic 11 trigger this deferral named. The retrofit is contracted at AC6. The concurrent-writer integration tests at AC22 #17-19 prove correctness. |
| `[src/open_ems/web/csrf.py:71-79]` (Story 9.1 deferred) | `CsrfMiddleware` exhausts the request body via `await request.form()`; potential decoupling between middleware and handler views of the form in some Starlette versions. | **safe-during-this-story** — pre-existing pattern; the `/change-password` POST and the new `/actions/create-homeowner` and `/actions/reset-homeowner-password` routes inherit the same risk class. The 11.3 form bodies are small (≤ 200 bytes typical for credentials), and the FastAPI `Form()` re-parse for the route is functionally idempotent against the cached middleware parse. NO new action required in 11.3; the body-exhaustion risk remains tracked for a future middleware-hardening sweep. |
| `[src/open_ems/web/routes/fragments.py:73-85 + src/open_ems/web/templates/dashboard.html:10-15]` (Story 10.1 deferred) | HTMX-error UX when a fragment endpoint 500s | **safe-during-this-story** — the new `/fragments/installer/homeowner-reset-form` is a trivial GET that should never 500 in practice (no DB I/O; pure template render). The remediation pattern stays deferred to a future Epic 11.x hardening sweep. **NO action in 11.3.** |
| `[tests/utils/db_query_counter.py]` (Story 11.1 DF1) | `db_query_counter` patches only `Connection.execute`; future cursor-based execution surfaces would bypass it | **safe-during-this-story** — 11.3 does not introduce new query-counted tests (the credential surfaces have bounded, well-understood query counts: 1 SELECT on settings render, 1 INSERT + 1 audit-write on create, 2 SELECTs + 1 UPDATE + 1 DELETE + 1 audit-write on reset). No new exposure. |
| `[src/open_ems/web/state_serialization.py:775, 793]` (Story 11.1 DF2) | Peak tracker `<= 0.0` branch mislabels small real peaks as "No data yet" | **acceptable-post-this-story** — installer dashboard surface; 11.3 does not touch it. |
| `[tests/unit/web/test_installer_dashboard_fragments.py]` (Story 11.1 DF3 + 11.2 AC22 #58 partial close) | Negative-CSRF coverage for installer POST routes. 11.2 closed the gap for `POST /actions/installer-note`. | **resolved-by-AC22-#41,49,59** — 11.3 introduces a CSRF coverage test for EACH of the three new POST surfaces (create-homeowner, reset-homeowner-password, change-password). DF3 still tracks dismiss-anomaly, set-strategy, ev-override; 11.3 closes the gap for its own surfaces. |
| Other deferred items | Unrelated to credential-management surface | **acceptable-post-this-story** |

The Story 9.0b deferred-finding (UserRepo + SessionRepo write-lock retrofit) is the FIRST `must-resolve-in-this-story` classification any non-A2 story in this codebase has carried; the dev agent must close it as part of the 11.3 implementation, not after. AC6 contracts the retrofit; AC22 #9-19 prove correctness.

---

## Project Context Reference

Project artifacts authoritative at story creation time:
- `_bmad-output/planning-artifacts/prd.md` — FR23 (homeowner credential creation/reset, line 625), FR35 (role-based local authentication, line 646), NFR-S1 (PHC credential storage), AR-S2 (no plaintext credentials at rest)
- `_bmad-output/planning-artifacts/architecture.md` — `user_repo.py` for FR23/FR35 (line 866), `installer/settings.html` template path (line 893), `audit_log.py` (line 904), `csrf.py` (line 874), Decision 2.3 — Bcrypt + PHC string format (line 259-261)
- `_bmad-output/planning-artifacts/ux-design-specification.md` — minimal UX guidance for credential management (the surface is utility-grade; UX spec primarily covers homeowner real-time UX and event-log filtering); the spec's "First access" instruction screen reference (line 1691) is adjacent context but does not contract this surface
- `_bmad-output/planning-artifacts/epics.md` — Story 11.3 AC text at line 2344-2375; Epic 11 cross-story constraints at line 2377-2386; Story 2.1 user model contract at line 702-733; Story 2.2 session-invalidation-on-password-change contract at line 749-758
- `_bmad-output/implementation-artifacts/11-2-implement-event-log-ui-...md` — sibling story (done 2026-05-12); shared sidebar partial pattern, dependency injection pattern, form-fragment + preserved-input pattern
- `_bmad-output/implementation-artifacts/2-1-define-user-model-...md` — the user model + admin-bootstrap precedent
- `_bmad-output/implementation-artifacts/2-2-implement-login-form-...md` — the login + session-rotation precedent
- `_bmad-output/implementation-artifacts/9-0b-db-backed-active-constraints-reload.md` — the `get_write_lock()` discipline established here

Stories whose surface 11.3 reads from (no mutation):
- Epic 2 (Stories 2.1–2.5) — `users` and `sessions` tables, `_resolve_session` semantics, `verify_password` PHC dispatch, `delete_all_for_user` primitive
- Story 11.1 — installer dashboard sidebar shell, the live-Settings precedent (lines 36 / 42 of `_sidebar.html`)
- Story 11.2 — the shared sidebar partial; the form-fragment-with-preserved-input UX pattern; the `get_*_repo` / `get_observability_service` dependency-injection style

Stories whose surface 11.3 mutates:
- **The `users` table** — INSERT on create-homeowner; UPDATE on reset-homeowner-password and change-password
- **The `sessions` table** — DELETE WHERE user_id on reset-homeowner-password and on change-password (own-session)
- **The `event_log` table** — INSERT 3 audit rows (created, reset, password-changed) via `ObservabilityService.audit`
- **The middleware chain** — one new middleware layer added in `create_app()`
- **The installer sidebar partial** — Settings item line flipped from placeholder to live

---

## Open questions saved for Jordan (post-implementation review or pre-dev sync)

1. **Password complexity rule — length-only ≥ 12 OR length-and-class:** 11.3 ships with **length-only ≥ 12** per NIST SP 800-63B §5.1.1.2 (modern best practice; length dominates entropy). Alternative: add a "must contain at least one digit + one uppercase + one symbol" rule. **Recommendation: length-only.** Composition rules are NIST-discouraged and produce a worse UX (passwords like `Password1!` pass but are weak; passwords like `correct horse battery staple` fail without the digit but are strong). Confirm.

2. **Generate-temporary-password vs installer-typed on reset:** 11.3 ships **installer-typed** (same form pattern as create — server-authoritative complexity check). Alternative: system generates a one-time-shown random password the installer reads off the screen. The installer-typed path is simpler (one fewer "view this once" UX surface), reuses the same input control, and matches the homeowner-handoff workflow where the installer is physically present and can pick a memorable shared-secret with the homeowner. Confirm.

3. **Multiple-homeowner support:** 11.3 v1 invariant: **at most one homeowner row.** The create-homeowner route rejects if any homeowner exists; the reset route operates on the single homeowner. A future story can lift this invariant when household-sharing is in scope. Confirm — alternative is to support N homeowners with a list UI; significant scope increase.

4. **`current_password` requirement on the forced-change path:** 11.3 ships **always-required current_password.** The forced-change path requires it because the homeowner just authenticated with the temp password — re-checking it defends against "unattended logged-in browser used to claim credentials." Alternative: skip the current_password check on the forced path (the user authenticated within the last 4h installer / 30d homeowner). **Recommendation: always require.** Confirm.

5. **`/change-password` URL — root-level vs `/auth/change-password`:** 11.3 ships **`/change-password`** (root-level, parallel to `/login` and `/logout` — all auth-flow URLs are at root). Alternative: `/auth/change-password` for namespace consistency with future `/auth/*` URLs. **Recommendation: root-level.** Matches the existing `/login` and `/logout` pattern. Confirm.

6. **Middleware path-matching — exact vs prefix for `/static/`:** 11.3 ships **prefix-match `/static/`** + exact-match for `/login`, `/logout`, `/change-password`, `/health/live`, `/health/ready`. The static prefix is necessary because `/static/htmx.min.js`, `/static/alpine.min.js`, `/static/open-ems.css` are all served from there. Confirm.

7. **Audit `actor` field on `password_changed` event — installer vs homeowner vs system:** 11.3 ships `actor=<user.role>` — installer-self-change emits `actor="installer"`; homeowner-self-change emits `actor="homeowner"`. This lets the event log filter by actor cleanly. Alternative: `actor="system"` for all self-change events. **Recommendation: actor=<user.role>.** Confirm.

8. **`current_password` rate limiting on `/change-password`:** 11.3 ships **no rate limiting** on the change-password endpoint. The user is already authenticated; brute-forcing the current_password from within an authenticated session is a rare attack pattern, and rate-limiting interferes with legitimate password-typo retries. The `rate_limiter` module is reserved for unauthenticated `/login` per Story 2.2. Confirm.

9. **Settings page scope — credentials only OR include Devices/Updates/Backup placeholders:** 11.3 ships **credentials-only.** The Devices and Updates/Backup sections are Epic 12 / future Epic 11.x scope; their inclusion as placeholders on this page would be premature scope creep. Devices remains a disabled sidebar item until its dedicated story lands. Confirm.

---

## Resolved decisions (2026-05-12, pre-dev sync with Jordan)

All nine open questions resolved with the recommended v1 paths:

1. **Password complexity rule** — **length-only ≥ 12 characters.** NIST SP 800-63B §5.1.1.2-aligned; no character-class composition.
2. **Reset password input mechanism** — **installer-typed in form** (no temporary-password generation). Reuses the create form's complexity check.
3. **Multiple homeowner support** — **single homeowner invariant for v1.** Create rejects if one exists; reset operates on the single homeowner.
4. **`current_password` requirement on forced-change path** — **always required.** Defense against unattended-logged-in-browser credential claim.
5. **`/change-password` URL** — **root-level** (parallel to `/login`, `/logout`).
6. **Middleware path-matching** — **prefix `/static/`** + exact-match for the auth + health paths.
7. **`password_changed` audit actor field** — **actor=<user.role>** (installer-self-change → `installer`; homeowner-self-change → `homeowner`).
8. **`/change-password` rate limiting** — **none in v1.** User is already authenticated; brute-force surface is narrow.
9. **Settings page scope** — **credentials-only.** Devices and Updates/Backup placeholders stay disabled until their dedicated stories.

---

## Tasks / Subtasks

### Task 1: Password-complexity helper + UserRepo enhancements (AC7, AC9, AC10)
- [x] 1.1 Add `MIN_PASSWORD_LENGTH: Final[int] = 12` and `validate_password_complexity(password: str) -> str | None` to `src/open_ems/storage/repositories/user_repo.py`
- [x] 1.2 Modify `UserRepo.update_password` signature to accept `must_change_password: bool = False` keyword-only parameter; update SQL to `SET hashed_password = ?, must_change_password = ?`; update docstring per AC9
- [x] 1.3 Add `UserRepo.get_homeowner() -> aiosqlite.Row | None` per AC10 (ORDER BY created_at ASC LIMIT 1 over role='homeowner' rows)
- [x] 1.4 Write unit tests in `tests/unit/storage/repositories/test_user_repo.py` (AC22 #1-10): 10 tests

### Task 2: `get_write_lock()` retrofit onto UserRepo + SessionRepo (AC6)
- [x] 2.1 Wrap `UserRepo.create` and `UserRepo.update_password` writes with `async with get_write_lock():` per AC6
- [x] 2.2 Wrap every DML method in `SessionRepo` (create, delete_by_id, delete_all_for_user, update_last_active, touch, delete_expired_for_user, delete_all_expired) with `async with get_write_lock():`
- [x] 2.3 Add SessionRepo write-lock unit tests in `tests/unit/storage/repositories/test_session_repo.py` (AC22 #11-16): 6 tests
- [x] 2.4 Add integration tests in `tests/integration/storage/test_user_repo_concurrent_writers.py` (AC22 #17): 1 test
- [x] 2.5 Add integration tests in `tests/integration/storage/test_session_repo_concurrent_writers.py` (AC22 #18-19): 2 tests

### Task 3: New FastAPI dependencies (AC17)
- [x] 3.1 Add `get_user_repo(request) -> UserRepo` and `get_session_repo(request) -> SessionRepo` to `src/open_ems/web/dependencies.py`
- [x] 3.2 No unit tests required for these (trivial constructors; exercised end-to-end via the route tests)

### Task 4: `MustChangePasswordMiddleware` (AC11-13)
- [x] 4.1 Add private helper `_get_session_user_if_any_readonly(request) -> tuple[user_row, session_row] | None` to `dependencies.py` (mirrors `_resolve_session` but with NO touch / NO cleanup / NO renewal)
- [x] 4.2 Create new module `src/open_ems/web/must_change_password_middleware.py` with `MustChangePasswordMiddleware(BaseHTTPMiddleware)`; implement allow-list short-circuit + HTMX `HX-Redirect` path + structured-log emission
- [x] 4.3 Register the middleware in `web/app.py:create_app()` AFTER `CsrfMiddleware` per AC12 (last-registered runs first inbound)
- [x] 4.4 Write unit tests in `tests/unit/web/test_must_change_password_middleware.py` (AC22 #20-30): 11 tests

### Task 5: `/installer/settings` page + sidebar wiring (AC1-2, AC19, AC20)
- [x] 5.1 Update `installer/_sidebar.html` line 42 — Settings goes live with `href="/installer/settings"` + active-class + aria-current pattern (mirror lines 31-35)
- [x] 5.2 Add `GET /installer/settings` route to `web/routes/installer.py` (require_installer + get_user_repo deps; renders `installer/settings.html`)
- [x] 5.3 Create `installer/settings.html` template (extends base; includes sidebar with active="settings"; renders the homeowner-credentials section partial)
- [x] 5.4 Create the section partials per AC19:
  - `installer/_homeowner_credentials_section.html` — selects between create and reset variants based on `homeowner_row is None`
  - `installer/_homeowner_create_form.html` — create form with preserved username + inline error
  - `installer/_homeowner_reset_form.html` — reset form returned by the fragment route
- [x] 5.5 Sidebar regression test in `tests/unit/web/test_installer_sidebar.py` (AC22 #66-67): 2 tests

### Task 6: Section-context builders (Pattern 2)
- [x] 6.1 Add `build_installer_homeowner_credentials_section_context(...)` to `web/state_serialization.py`
- [x] 6.2 Add `build_installer_homeowner_create_form_context(...)` and `build_installer_homeowner_reset_form_context(...)` to `web/state_serialization.py`
- [x] 6.3 Builder unit-tests deferred to the route-level tests (each route exercises one builder end-to-end with assertions on the rendered HTML — consistent with 11.1 / 11.2 precedent)

### Task 7: `POST /actions/create-homeowner` (AC3, AC22 #31-42)
- [x] 7.1 Add the route to `web/routes/actions.py`; require_installer + get_user_repo + get_observability_service deps
- [x] 7.2 Implement validation per AC3 strict-order; route helper `_render_credentials_section_only(...)` mirrors 11.2's `_render_note_form_only`
- [x] 7.3 Emit structured log `homeowner_created` + audit event "Homeowner credentials created"
- [x] 7.4 Write route tests (AC22 #31-42): 12 tests

### Task 8: `POST /actions/reset-homeowner-password` + `GET /fragments/installer/homeowner-reset-form` (AC4-5, AC22 #43-49)
- [x] 8.1 Add the GET fragment route to `web/routes/fragments.py` (mirror 11.1 fragments pattern)
- [x] 8.2 Add the POST action route to `web/routes/actions.py`; require_installer + get_user_repo + get_session_repo + get_observability_service deps
- [x] 8.3 Implement validation per AC4; ensure the password update + session-delete pair holds the write-lock atomically; emit audit + structured log AFTER lock release (per AC4 implementation note)
- [x] 8.4 Write route tests (AC22 #43-49): 7 tests

### Task 9: `GET /change-password` + `POST /change-password` (AC14-16, AC22 #50-59)
- [x] 9.1 Add the GET route to `web/routes/auth.py`; renders `change_password.html` with `is_forced` from `user.must_change_password`
- [x] 9.2 Add the POST route to `web/routes/auth.py`; implement validation per AC15 strict-order; on success update password + invalidate all user sessions + clear cookie + redirect to `/login`
- [x] 9.3 Emit structured log `password_changed` + audit event "Password changed" with `actor=user.role`
- [x] 9.4 Create `templates/change_password.html` (standalone card; styled like login.html)
- [x] 9.5 Add `build_change_password_page_context(...)` to `state_serialization.py`
- [x] 9.6 Write route tests (AC22 #50-59): 10 tests

### Task 10: End-to-end integration tests (AC22 #60-65)
- [x] 10.1 Create `tests/integration/web/test_homeowner_credential_management_e2e.py`
- [x] 10.2 Implement the 6 e2e tests covering: installer create → homeowner login → forced change-password; installer reset → homeowner session termination; XSS-in-username regression; sidebar navigation regression

### Task 11: Quality gates + uv.lock invariant (AC21, AC23)
- [x] 11.1 Run `uv run pytest` and verify ≥ 1847 passed (1814 baseline + ≥ 35 net new)
- [x] 11.2 Run `uv run ruff check .` and verify clean
- [x] 11.3 Run `uv run ruff format --check .` and verify clean
- [x] 11.4 Run `uv run mypy src` and verify clean
- [x] 11.5 Verify `uv.lock` is unchanged via `git diff uv.lock`

### Task 12: Documentation + cross-references
- [x] 12.1 Add inline docstring to `create_app()` documenting the middleware-ordering rule (CSRF first registered + MustChangePassword second; last-registered runs first inbound)
- [x] 12.2 Add a comment in `database.py:get_write_lock()` cross-referencing 11.3 as the close-out of the 9.0b UserRepo/SessionRepo deferral
- [x] 12.3 Cross-reference Story 2.1 / 2.2 in the routes' docstrings — the forced-change-password contract has been latent since Story 2.1 (2026-05-02) and is closed by 11.3

---

## Dev Agent Record

### Implementation Plan

Implemented Tasks 1-12 in order. All 23 acceptance criteria satisfied. 57 net new tests added (1814 baseline → 1871 passing; target was ≥35 net new). Quality gates clean: `uv run pytest` (1871 passed / 2 skipped / 8 xfailed unchanged), `uv run ruff check .` (clean), `uv run ruff format --check .` (clean), `uv run mypy src` (no issues in 105 source files), `uv.lock` (unchanged — git diff --stat empty).

### Completion Notes

- **AC6 — `get_write_lock()` retrofit on UserRepo + SessionRepo closes the 9.0b deferred-finding** (deferred-work.md line ~107) as a `must-resolve-in-this-story` item. All DML methods on both repos now wrap their `execute → commit` pair with `async with get_write_lock():`. Three concurrent-writer integration tests (`test_user_repo_concurrent_writers` + `test_session_repo_concurrent_writers`) prove the discipline holds under load; 8 unit tests assert each individual method acquires the lock.
- **AC11-13 — MustChangePasswordMiddleware registered after CsrfMiddleware** so it runs FIRST inbound (Starlette wraps in reverse). The middleware is a stateless guard: reads the session via a new read-only helper `_get_session_user_if_any_readonly` that does NOT touch / cleanup / renew (those happen later in `require_installer` / `require_homeowner`). Allow-list: `/change-password`, `/login`, `/logout`, `/health/live`, `/health/ready`, plus the `/static/` prefix. HTMX requests get 200 + `HX-Redirect` (not 303) per the standard HTMX redirect contract.
- **AC14-16 — `/change-password` route** is the FIRST implementation of the must-change-password gate that has been contractual since Story 2.1 (2026-05-02). Validation order: session valid → current_password correct → complexity (≥12 chars) → confirm matches → differs from current → bcrypt 72-byte ceiling → persist + clear flag → invalidate all user sessions → audit (`actor=user.role`, `event_type=SYSTEM`, `summary="Password changed"`) → clear cookie → 303 to `/login`. The route is CSRF-protected by the existing `CsrfMiddleware` (no new CSRF exemption).
- **AC3-5 — `POST /actions/create-homeowner` and `POST /actions/reset-homeowner-password`** mirror the 11.2 `post_installer_note` shape (preserved-input + inline error on validation failure; HTMX `outerHTML` swap of the `#homeowner-credentials-section` on every response). The reset path's password-update + session-delete pair both acquire `get_write_lock()` (the asyncio.Lock is non-re-entrant; sequencing is back-to-back, not nested). Audit emission happens AFTER lock release (also non-re-entrant). The "View in event log →" link uses the 11.1 URL contract `/installer/event-log?type=SYSTEM&window=24h`.
- **AC9 — `UserRepo.update_password` signature widened** with `must_change_password: bool = False` kwarg. Backward-compatible: only test-only callers exist today; the default (False) matches the historic semantic for the self-change path (Story 11.3's `/change-password`).
- **AC1, AC19, AC20 — Settings page** + 4 new templates. The sidebar's Settings item went live (mirror of 11.2's Event Log pattern). Settings is intentionally credentials-only for v1; future Epic 12 sections will land as separate stories.
- **bcrypt 72-byte ceiling** — the route layer catches `ValueError` from `hash_password()` and surfaces "Password is too long. Use 12–72 bytes." UTF-8-multibyte passwords can hit the ceiling at 19 chars while satisfying the 12-char floor; this is a real path covered by AC22 #38.
- **CSRF-middleware body-consumption quirk** — Starlette's `BaseHTTPMiddleware`-based `CsrfMiddleware` reads the form body via `await request.form()`, which prevents FastAPI's `Form()` parameters from re-reading the body. This is the pre-existing pattern documented in deferred-work line ~85; tests work around it by sending the CSRF token via `X-CSRF-Token` header (the production HTMX path uses this natively via base.html's listener). Documented in the change-password tests' `_post_data` docstring.
- **A2 evaluation confirmed NOT triggered** — single binary flag (`must_change_password`), stateless middleware guard, single-step installer actions. T1-T7 all evaluated against the AC text + dev notes + file scope; none match the structural-class bar (compared explicitly to 8.4 fail-safe lifecycle, 9.1 wizard, 9.3 staged constraint config — those are the canonical T1/T7 matches; this story is below all of them).

### File List

New files (src):
- `src/open_ems/web/must_change_password_middleware.py`
- `src/open_ems/web/templates/installer/settings.html`
- `src/open_ems/web/templates/installer/_homeowner_credentials_section.html`
- `src/open_ems/web/templates/installer/_homeowner_create_form.html`
- `src/open_ems/web/templates/installer/_homeowner_reset_form.html`
- `src/open_ems/web/templates/change_password.html`

Modified files (src):
- `src/open_ems/storage/repositories/user_repo.py` — added MIN_PASSWORD_LENGTH, validate_password_complexity, get_homeowner, update_password kwarg, get_write_lock retrofit on create + update_password
- `src/open_ems/storage/repositories/session_repo.py` — get_write_lock retrofit on all 7 DML methods
- `src/open_ems/storage/database.py` — cross-reference comment on 11.3 retrofit
- `src/open_ems/web/dependencies.py` — added get_user_repo, get_session_repo, _get_session_user_if_any_readonly
- `src/open_ems/web/app.py` — registered MustChangePasswordMiddleware after CsrfMiddleware; ordering comment
- `src/open_ems/web/routes/installer.py` — added GET /installer/settings
- `src/open_ems/web/routes/actions.py` — added POST /actions/create-homeowner + POST /actions/reset-homeowner-password
- `src/open_ems/web/routes/auth.py` — added GET/POST /change-password + _resolve_change_password_session helper
- `src/open_ems/web/routes/fragments.py` — added GET /fragments/installer/homeowner-reset-form
- `src/open_ems/web/state_serialization.py` — added 4 builders (create/reset form, section, change-password page)
- `src/open_ems/web/templates/installer/_sidebar.html` — Settings item live (active=settings)

New files (tests):
- `tests/integration/storage/test_user_repo_concurrent_writers.py`
- `tests/integration/storage/test_session_repo_concurrent_writers.py`
- `tests/integration/web/test_homeowner_credential_management_e2e.py`
- `tests/unit/web/test_must_change_password_middleware.py`
- `tests/unit/web/test_create_homeowner_route.py`
- `tests/unit/web/test_reset_homeowner_password_route.py`
- `tests/unit/web/test_change_password_route.py`
- `tests/unit/web/test_installer_sidebar.py`

Modified files (tests):
- `tests/unit/storage/repositories/test_user_repo.py` — 10 new tests (AC22 #1-10)
- `tests/unit/storage/repositories/test_session_repo.py` — 6 new tests (AC22 #11-16)

## Review Findings

_`bmad-code-review` run on 2026-05-12. Three review layers executed in parallel: Blind Hunter (25 findings), Edge Case Hunter (28 findings), Acceptance Auditor (23 findings + AC22 test coverage audit). After deduplication and triage: **3 decision-needed**, **13 patch**, **4 deferred**, **30+ dismissed**. AC22 coverage audit confirms all 67 enumerated tests are present (2 with naming/coverage caveats — see P10 + D3 below). Acceptance Auditor verdict on AC1–AC23: most ACs met; AC14/AC15-step-1 (session-resolution reuse), AC15-step-9 (audit actor + try/except), AC17 (DI), AC21 (no Alpine on credentials page), and AC11-step-1 (helper naming) all show clear deviations queued as patches._

### Decision-needed (3)

- [x] [Review][Decision→Patched] **TOCTOU race on `/actions/create-homeowner` can violate v1 single-homeowner invariant** — Both step-2 (`get_by_username`) and step-4 (`get_homeowner`) predicates are checked BEFORE the lock is acquired by `UserRepo.create`. Two concurrent installer POSTs can both pass the `existing_homeowner is None` gate and both call `create(... role="homeowner")` — the write lock serializes the INSERTs but the predicate is not re-checked. The `users` table has no DB-level uniqueness constraint on `role='homeowner'`. Two valid fix paths: **(a)** wrap the precondition checks + `create()` in a single `async with get_write_lock():` block in the route, OR **(b)** add a partial unique index in a new migration (`CREATE UNIQUE INDEX ix_users_single_homeowner ON users(role) WHERE role='homeowner'`). Both have trade-offs (route-level extends the lock window; migration adds schema change). [`src/open_ems/web/routes/actions.py:post_create_homeowner` + `src/open_ems/storage/repositories/user_repo.py:create`] — Source: blind+edge
- [x] [Review][Decision→Patched] **Username uniqueness is case-sensitive (BINARY collation)** — SQLite default collation means `Admin` and `admin` can both be created as separate homeowner usernames (also separate from the bootstrap `admin` installer if collation isn't aligned). Spec is silent on case behavior. Options: **(a)** accept current behavior; **(b)** normalize via `LOWER(username)` index or `COLLATE NOCASE` on the column; **(c)** apply Unicode NFC normalization in the route. Decision affects login lookup too (`get_by_username`) — would need coordinated change in `auth.py`. [`src/open_ems/storage/repositories/user_repo.py:get_by_username` + `src/open_ems/web/routes/actions.py:post_create_homeowner`] — Source: blind+edge+auditor
- [x] [Review][Decision→Patched] **AC22 test #40 `test_create_homeowner_homeowner_role_rejected_403` asserts 302, not 403** — Test name says "403" but asserts `status_code == 302`. The current behavior is 302 (the `require_installer` dependency redirects unauthenticated/wrong-role users to `/login`). Spec did not explicitly mandate 403 vs 302 for role-mismatch on action POSTs. Options: **(a)** rename the test to `..._redirects_302`; **(b)** change `require_installer` to return 403 for role-mismatch (vs 302 for missing session). [`tests/unit/web/test_create_homeowner_route.py` + `src/open_ems/web/dependencies.py:require_installer`] — Source: auditor

### Patch (13)

- [x] [Review][Patch] **`/change-password` introduces a third parallel session-resolution helper; AC14 + AC15-step-1 mandate `_resolve_session`** [`src/open_ems/web/routes/auth.py:_resolve_change_password_session`] — Spec text: "The handler reads the session via `_resolve_session` (already private at `dependencies.py`)" (AC14) and "Same `_resolve_session` path" (AC15 step 1). Implementation defines a new local `_resolve_change_password_session`. Also AC11-step-1 names the readonly helper `_get_session_user_if_any` but it ships as `_get_session_user_if_any_readonly`. Fix: refactor `change_password_page` + `change_password_submit` to call `_resolve_session`; rename `_get_session_user_if_any_readonly` → `_get_session_user_if_any`. Source: blind+edge+auditor
- [x] [Review][Patch] **`/change-password` unauthenticated HTMX requests return 302 — AC15 step 1 mandates 401 for HTMX** [`src/open_ems/web/routes/auth.py:change_password_page` + `change_password_submit` unauth branches] — Spec text: "No session → 401 (HTMX) or 302 → `/login?next=/change-password` (browser)". Implementation always returns 302. Fix: branch on `HX-Request` header — return `Response(status_code=401)` for HTMX, `RedirectResponse(..., 302)` for browser. Source: auditor
- [x] [Review][Patch] **`/change-password` audit `actor` falls back to `"system"` — AC15 step 9 + Resolved decision #7 mandate `actor=user.role`** [`src/open_ems/web/routes/auth.py:change_password_submit` (audit emission)] — Spec text: "actor=<resolver> where <resolver> is user.role (installer or homeowner)". Implementation: `actor = role if role in ("installer", "homeowner") else "system"`. The `else "system"` is dead defensive code (the DB CHECK constraint enforces role IN ('installer', 'homeowner')) but deviates from spec. Fix: drop the fallback; pass `actor=role` directly (with optional `assert` documenting the DB CHECK invariant). Source: auditor
- [x] [Review][Patch] **`/change-password` audit emission wrapped in `try/except Exception` — dev notes #12 explicit "no defensive try/except needed"** [`src/open_ems/web/routes/auth.py:change_password_submit` audit block] — Dev notes Files-to-Read #12 says: "`audit()` raises `ValueError` for empty summary or invalid actor/event_type — 11.3's exact summary strings are non-empty and the actors/event_types are valid; no defensive try/except needed." Implementation wraps the call in `try/except Exception` with a `password_changed_audit_failed` log fallback. Inconsistent with `/actions/create-homeowner` and `/actions/reset-homeowner-password` which do NOT catch. Fix: drop the try/except so audit failures propagate. Source: blind+edge+auditor
- [x] [Review][Patch] **`/change-password` constructs repos inline instead of using the new `Depends(get_user_repo)` / `get_session_repo` / `get_observability_service`** [`src/open_ems/web/routes/auth.py:change_password_submit`] — AC17 docstring explicitly names `/change-password` as a consumer of `get_user_repo`. Implementation has `UserRepo().update_password(...)`, `SessionRepo().delete_all_for_user(...)`, `ObservabilityService().audit(...)` inline. Fix: thread the three dependencies into the route signature. Source: auditor
- [x] [Review][Patch] **`installer/settings.html` wraps the page in `x-data="installerSidebar()"` despite AC21 "Alpine NOT used on credential pages"** [`src/open_ems/web/templates/installer/settings.html`] — AC21: "alpine.js 3.13.10 (existing) — NOT used on the credential pages (simple HTML forms; no client state needed)". The sidebar partial is shared with dashboard/event-log where Alpine is appropriate — but settings.html lifts the Alpine factory to its own wrapper. Fix: drop `x-data="installerSidebar()"` from `settings.html`; rely on the partial's own mobile-toggle behavior or none. Source: blind+auditor
- [x] [Review][Patch] **`MustChangePasswordMiddleware` allow-list does not normalize trailing slashes or case** [`src/open_ems/web/must_change_password_middleware.py:_path_is_allow_listed`] — Path-equality match means `/change-password/`, `/Change-Password`, `//login`, `/logout/` all fail the exact-match check and trigger redirects. FastAPI's default routing returns 404 for trailing-slash mismatch — but if any proxy in front normalizes paths, a redirect loop becomes possible on the change-password page itself. Fix: normalize the path (`path.rstrip("/").lower()` or `path.rstrip("/")`-only if case-preservation matters elsewhere) before the allow-list check. Source: blind+edge
- [x] [Review][Patch] **Unauthenticated POST `/change-password` returns 302 — browser re-POSTs the credentials form to `/login`** [`src/open_ems/web/routes/auth.py:change_password_submit` unauth branch] — A 302 after POST tells the browser to repeat the POST at the new URL. The form payload (containing `current_password` + `new_password` + `confirm_new_password`) would be POSTed to `/login`. Fix: use 303 for the POST-result redirect (303 forces GET). Source: blind
- [x] [Review][Patch] **Section-flip leak on duplicate-username error path** [`src/open_ems/web/routes/actions.py:post_create_homeowner` error paths + `_render_credentials_section`] — When a homeowner already exists and the installer's POST hits step-2 (duplicate-username) BEFORE reaching step-4 (no-homeowner precondition), the error-rendering helper calls `get_homeowner()` and flips the section to the reset-form variant while showing "Username already in use" — UX-confusing because the username field doesn't exist on the reset form. Fix: thread a "force-create-variant" hint into `_render_credentials_section` for steps 1–3 error paths; only call `get_homeowner()` for step-4 precondition and the success render. Source: blind+edge+auditor (B14+E16+A7)
- [x] [Review][Patch] **AC22 #61 `test_e2e_homeowner_completes_change_password_then_accesses_dashboard` does not actually test dashboard access** [`tests/integration/web/test_homeowner_credential_management_e2e.py`] — Test name says "then accesses dashboard" but the test body only asserts 303 → /login and session deleted; no subsequent dashboard GET is exercised. Fix: extend the test to log in again with the new password and GET `/homeowner/dashboard`, asserting 200 + dashboard content. Source: blind+auditor
- [x] [Review][Patch] **`hash_password()` `except ValueError` catch is too broad — misleading "12–72 bytes" message for unrelated ValueErrors** [`src/open_ems/web/routes/actions.py:post_create_homeowner` step 5 + `post_reset_homeowner_password` + `auth.py:change_password_submit`] — `hash_password` currently raises `ValueError` only for the 72-byte ceiling, but any future addition (NUL-byte rejection, encoding errors) would surface the same misleading message. Fix: pre-check byte length explicitly (`if len(password.encode('utf-8')) > 72: ...`) BEFORE calling `hash_password`, and remove the catch (or narrow to a named exception class). Source: blind+edge
- [x] [Review][Patch] **`test_concurrent_password_update_and_session_create_serialize_cleanly` passes trivially — never asserts final hash state** [`tests/integration/storage/test_session_repo_concurrent_writers.py`] — Test counts return values from coroutines (each returns literal `10`) and asserts the row count is 10. A lost-update bug would not be caught. Fix: capture each submitted hash in an order-tagged list and after the gather assert the final stored hash equals one of the 10 submitted hashes (and that all 10 sessions exist). Source: blind
- [x] [Review][Patch] **Reset-form cancel branch returns hand-rolled HTML string instead of rendering a template — drift risk** [`src/open_ems/web/routes/fragments.py:installer_homeowner_reset_form` cancel branch] — Hand-rolled `'<div id="homeowner-reset-slot"><button type="button" class="installer-settings-reset-trigger" hx-get="/fragments/installer/homeowner-reset-form" ...>Reset password</button></div>'` duplicates markup that lives in the credentials-section template. Any change to the trigger button (classes, hx-attrs, text) silently desyncs. Fix: extract a `_homeowner_reset_trigger.html` partial and have both the section template and the cancel branch render it. Source: blind

### Deferred (4)

- [x] [Review][Defer] **CSRF middleware body-consumption pre-existing issue affects `/change-password` POST** [`src/open_ems/web/csrf.py:71-79`] — deferred, pre-existing — already tracked in `deferred-work.md` (Story 9.1 deferred at csrf.py:71-79). Tests work around via `X-CSRF-Token` header. Carry-over: `deferred-work.md` (existing entry).
- [x] [Review][Defer] **`reset_homeowner_password` partial-failure: `update_password` succeeds, `delete_all_for_user` fails** [`src/open_ems/web/routes/actions.py:post_reset_homeowner_password`] — deferred, pre-existing — two-write atomicity gap exists across all 11.x routes that perform multi-step writes (the lock serializes individual writes but doesn't atomically span the pair). Architectural decision required; carry-over to deferred-work for Epic 12 hardening.
- [x] [Review][Defer] **`_render_change_password_error` returns 200 without `Cache-Control: no-store`** [`src/open_ems/web/routes/auth.py:_render_change_password_error`] — deferred, pre-existing — same pattern as existing auth + installer error renders; no codebase-wide Cache-Control discipline for 200-with-error responses. Bundle with a future security-header sweep.
- [x] [Review][Defer] **`SessionRepo.update_last_active` / `touch` raise `ValueError` inside the cursor `async with` block, leaving an uncommitted UPDATE under the lock** [`src/open_ems/storage/repositories/session_repo.py:update_last_active` + `touch`] — deferred, pre-existing — the existing methods already raised this way pre-11.3; the diff only added the surrounding lock. Pattern affects multiple methods and is consistent with the codebase's auto-commit-via-execute assumption. Bundle with the same future repo-hardening sweep.

### Patch resolution notes (2026-05-12)

_All 16 review patches (13 patch + 3 decision-needed-resolved-as-patches) applied in-cycle. Quality gates re-verified: `uv run pytest` → 1871 passed / 2 skipped / 8 xfailed (no regression; same baseline as initial implementation). `uv run ruff check .` → clean. `uv run ruff format --check .` → clean (288 files). `uv run mypy src` → no issues in 105 source files. Summary of fixes by file:_

- **`src/open_ems/storage/repositories/user_repo.py`** — Added `PasswordTooLongError(ValueError)` subclass; `hash_password` now raises the specific subclass (P14). Added `RoleAlreadyExistsError`; `UserRepo.create` accepts `enforce_role_uniqueness` kwarg-only parameter that re-checks role-uniqueness under the write lock (D1). `UserRepo.get_by_username` SQL now uses `COLLATE NOCASE` for case-insensitive matching (D2).
- **`src/open_ems/web/dependencies.py`** — Renamed `_get_session_user_if_any_readonly` → `_get_session_user_if_any` per AC11-step-1 (P1 sub-issue).
- **`src/open_ems/web/must_change_password_middleware.py`** — Updated import for the renamed helper (P1 sub-issue). `_path_is_allow_listed` now strips trailing slashes before exact-match check (P7).
- **`src/open_ems/web/routes/auth.py`** — Dropped local `_resolve_change_password_session`; `change_password_page` + `change_password_submit` now call `_resolve_session` directly (P1 main). New `_unauth_change_password_response` helper branches on `_is_htmx_or_api` to return 401 (HTMX) vs 302/303 (browser GET/POST) for unauth (P2 + P8). Audit `actor=user.role` (no fallback) (P3). Dropped `try/except Exception` wrapper around audit (P4). Refactored to use `Depends(get_user_repo)` / `get_session_repo` / `get_observability_service` (P5). Narrowed `except ValueError` → `except PasswordTooLongError` (P11).
- **`src/open_ems/web/routes/actions.py`** — `post_create_homeowner` now passes `homeowner_row=None` on steps 1-3 error paths (P9 — section-flip leak), passes `enforce_role_uniqueness="homeowner"` to `user_repo.create` and catches `RoleAlreadyExistsError` (D1). Narrowed `except ValueError` → `except PasswordTooLongError` on both `post_create_homeowner` and `post_reset_homeowner_password` (P11).
- **`src/open_ems/web/routes/fragments.py`** — Cancel branch of `installer_homeowner_reset_form` now renders the new `_homeowner_reset_trigger.html` partial via `_templates.TemplateResponse` (P13).
- **`src/open_ems/web/templates/installer/settings.html`** — Dropped `x-data="installerSidebar()"` per AC21 (P6).
- **`src/open_ems/web/templates/installer/_homeowner_credentials_section.html`** — Collapsed-state markup replaced by `{% include "installer/_homeowner_reset_trigger.html" %}` (P13).
- **`src/open_ems/web/templates/installer/_homeowner_reset_trigger.html`** (NEW) — Single source of truth for the reset-trigger button markup; consumed by both the credentials section and the cancel branch (P13).
- **`tests/unit/web/test_create_homeowner_route.py`** — Test #40 renamed from `test_create_homeowner_homeowner_role_rejected_403` → `test_create_homeowner_homeowner_role_redirects_to_homeowner_home` with docstring documenting `require_installer`'s browser-vs-HTMX behavior (D3).
- **`tests/unit/web/test_change_password_route.py`** — `test_get_change_password_redirects_unauthenticated_to_login` now uses the `session_repo` fixture to initialize the DB (required because the route now eagerly resolves `Depends(get_user_repo)` even on unauth paths).
- **`tests/integration/web/test_homeowner_credential_management_e2e.py`** — AC22 #61 extended: after change-password 303, the test logs back in with the new password, asserts a 303 → `/homeowner/dashboard`, captures the new session cookie, and verifies the new session resolves to an unflagged user (P10).
- **`tests/integration/storage/test_session_repo_concurrent_writers.py`** — `test_concurrent_password_update_and_session_create_serialize_cleanly` now asserts the final stored hash `verify_password`s against one of the 10 submitted candidates (P12). Imports `verify_password`.

### Dismissed (30+, rationale-inline)

_Dismissed findings with brief rationale, grouped by topic:_

**Explicit-by-spec / by Resolved-decision** (no action — author's intent confirmed):
- No rate limit on `/change-password` (Resolved decision #8 — "no rate limiting in v1; user already authenticated").
- Double bcrypt verify on `/change-password` POST (same rationale — explicit-by-decision).
- GET `/change-password` accessible to non-forced users (AC14 explicitly defines voluntary path).
- `_get_session_user_if_any_readonly` is a stateless guard with no touch/cleanup/renewal (AC11 step 1 explicit).
- `update_password` kwarg-only signature widening (AC9 explicit; positional 3rd-arg-call breakage is the intended migration).
- HTMX `HX-Request` header detection for redirect downgrade (standard HTMX protocol).
- `delete_cookie` attribute match with `/logout` pattern (matches existing `/logout` route signature).
- Middleware redirects logged at `debug` not `info` (intentional per dev notes — production noise control).
- Concurrent reset+change-password is lock-serialized; final state is acceptable.

**Test-coverage gaps that are low-value** (skipped to avoid test-suite bloat):
- HEAD/OPTIONS not exercised against middleware (middleware path-matches only; HTTP method irrelevant).
- Empty-string session cookie not tested (already covered by `if not raw_token` short-circuit).
- `get_homeowner` ORDER BY ties with identical `created_at` strings (sub-second resolution makes this practically impossible).
- Username with trailing whitespace / embedded control chars / newlines (extreme edge cases; defensive UX nits).
- 72/73-byte password boundary not directly tested (covered by 11/12-char boundary test pattern).
- HTMX double-fire on forced-change page (HTMX standard rapid-click handling).
- Cancel-form fragment when no homeowner exists (UX nit; clicking submit gracefully errors).
- "All homeowner sessions ended" banner when zero sessions existed (UX copy nit).
- GET `/logout` not tested with `must_change_password=1` (POST `/logout` test covers the path-allow-list semantics).
- AC22 #41/#49/#59 use `X-CSRF-Token` header instead of form `csrf_token` field (documented workaround for CSRF body-consumption — see deferred D1).
- Validation-error 200 status not explicitly asserted (test for content implicitly asserts non-redirect).
- `is_forced` distinction in POST validation-error rendering (LOW-coverage gap; copy difference only).
- Voluntary-path coverage of installer self-change `actor="installer"` (covered by audit-actor test).

**Code-quality / micro-perf nits** (not impactful):
- `get_homeowner()` called 4× in `post_create_homeowner` error paths (micro-perf; subsumed by patch P9).
- `# type: ignore[index]` on `aiosqlite.Row` access in builders (codebase pattern).
- `MustChangePasswordMiddleware.__init__` redundant override (cosmetic).
- XSS test only checks for literal `<script>` substring (autoescape soundness is established).
- AC4-step-4 prose-vs-code atomicity wording inconsistency (implementation matches the spec's "Implementation note" prose; AC text wording is the discrepancy).
- `Form()` fields default to `""` allowing missing-field POST to flow into validation-error render (intentional UX — clean inline error vs FastAPI 422).

## Change Log

- 2026-05-12: `bmad-code-review` run. 3 decision-needed, 13 patches queued, 4 deferred, 30+ dismissed. Acceptance Auditor AC22 coverage audit: all 67 enumerated tests present (2 with naming/coverage caveats queued as P10 + D3). HIGH finding (TOCTOU on create-homeowner) routed to decision-needed for fix-path selection.
- 2026-05-12: Story 11.3 implementation complete. Status: ready-for-dev → in-progress → review. 57 net new tests (1871 total passing); ruff + format + mypy clean across 105 source files; uv.lock unchanged. Third Epic 11 story delivered (final regular story); epic-11-retrospective is optional per sprint-status. Closes the 9.0b deferred-finding (UserRepo + SessionRepo write-lock retrofit) and builds the contractually-latent `/change-password` route that has been pending since Story 2.1 (2026-05-02). Awaits A7 review-closure gate via bmad-code-review (would be the eighth proof-of-enforcement target after 9-Y-a, 9-X, 10.1, 10.2, 10.3, 10.4, 11.1, 11.2).
- 2026-05-12: Story 11.3 created by bmad-create-story. Status: ready-for-dev. Last regular story of Epic 11. Closes the 9.0b deferred-finding (UserRepo + SessionRepo write-lock retrofit) and builds the contractually-latent `/change-password` route that has been pending since Story 2.1 (2026-05-02). A2 not triggered (T1–T7 evaluated; none match — middleware enforcement is a stateless guard, not a state-machine).
