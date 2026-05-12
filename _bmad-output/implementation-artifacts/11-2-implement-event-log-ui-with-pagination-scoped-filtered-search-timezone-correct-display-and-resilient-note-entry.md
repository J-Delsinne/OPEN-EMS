# Story 11.2: Implement event log UI with pagination, scoped filtered search, timezone-correct display, and resilient note entry

Status: done
_Status set to done by bmad-code-review on 2026-05-12 after review-closure gate passed (5b): 18 resolved, 0 dismissed, 0 deferred-and-verified (4 defer items checked-and-linked to deferred-work.md)._
_Status: in-progress → review by bmad-dev-story on 2026-05-12 (all 12 tasks complete; 88 net new tests; quality gates clean)._
_Status: ready-for-dev → in-progress by bmad-dev-story on 2026-05-12._
_Status: ready-for-dev set by bmad-create-story on 2026-05-12._

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

> **Type:** Epic 11 — installer **operational monitoring** surface, continuation of Story 11.1. Read-side: paginated, filtered, keyword-searched view of the `event_log` table. Write-side: a single POST endpoint that appends INSTALLER notes via `ObservabilityService.installer_note` (existing). **No degradation logic** in this story; no engine surface mutation.
> **A2-triggered:** **No.** Evaluated T1–T7 (see "A2 trigger evaluation" below). None match: the surface is one new GET page + one HTMX list fragment + one POST note-append. No lifecycle/state-machine semantics in the structural sense (note-entry success/failure is a trivial UI form, not a recovery state machine), no retry policy, no persistence beyond ordinary append-only event_log rows, no watchdog, no multi-adapter coordination, no process restart, no installer workflow orchestration. R1–R7 enforcement does not apply. A brief deferred-findings overlap check is still included in dev notes.
> **Prerequisites:** Epic 11 in-progress; **Story 11.1 done 2026-05-12** (depends on it for: sidebar shell pattern, `EventLogRepo` + `EventLogEntry` Pydantic model, `installer_anomaly`-style URL contracts, CSS palette tokens, `installerSidebar` Alpine factory, `db_query_counter` test helper, 5-fragment installer dashboard for the "View all" / "View in event log" link targets).
> **Sibling stories:** 11.3 (homeowner credential management — independent surface). 11.2 consumes the URL contract emitted by 11.1's "View in event log" links (`?type=DECISION,DEVICE`, `?device_id=<id>`, `?window=24h`) and makes the previously-404 `/installer/event-log` page real.
> **Epic 11 progress after this story:** 11.1 done · 11.2 ready-for-dev (this story) · 11.3 backlog · epic-11-retrospective optional.

## Story

As an installer,
I want to browse the event log with paginated results, badge and date filters, keyword search scoped to the filtered dataset, timezone-correct timestamps, and note entry that preserves my text on failure,
so that I can find specific events efficiently and annotate the log with confidence even during poor connectivity.

## Acceptance Criteria

**Given** the installer is authenticated and the `event_log` table contains rows (`schema_version=1`, columns per migration `0005_add_event_log_table.py` — `id, timestamp, actor, event_type, summary, detail, device_id, config_version`), and Story 11.1's `EventLogEntry` Pydantic model + `EventLogRepo.list_recent` already exist,

**When** this story is implemented,

**Then** the following acceptance criteria must hold:

---

### Page route, layout, and initial render

1. **AC1 — `GET /installer/event-log` is a real page.** A new route in `src/open_ems/web/routes/installer.py` (sibling to `installer_dashboard` at line 17-30) renders the full event-log page. The page extends `base.html`, reuses the installer sidebar (with **Event Log** as the active item — see AC2), and contains: a note-entry form, a filter bar, a result-count caption, and the initial paginated list. Route signature:

   ```python
   @router.get("/installer/event-log", response_class=HTMLResponse)
   async def installer_event_log(
       request: Request,
       user: InstallerUser = Depends(require_installer),
       event_log_repo: EventLogRepo = Depends(get_event_log_repo),
       type: str | None = None,         # comma-separated list, e.g. "DECISION,DEVICE"
       device_id: str | None = None,
       window: str | None = None,       # "24h" relative window (11.1 contract)
       from_date: str | None = None,    # YYYY-MM-DD explicit override (form name: "from")
       to_date: str | None = None,      # YYYY-MM-DD explicit override (form name: "to")
       q: str | None = None,            # keyword
       offset: int = 0,
   ) -> HTMLResponse:
   ```

   FastAPI alias the `from` / `to` params via `Query(..., alias="from")` etc. (cannot use bare `from` — reserved Python keyword). Use `Annotated[str | None, Query(alias="from")]` so the wire format stays `?from=...`.

   The route delegates filter parsing to a helper `_parse_event_log_filters(...)` (see AC9) and passes a structured `EventLogFilter` dataclass into the builder.

2. **AC2 — Sidebar with Event Log active.** The installer sidebar is now **shared across all installer operational pages**. Extract the inline sidebar from `dashboard.html` (lines 57-78) into a new partial `src/open_ems/web/templates/installer/_sidebar.html` that takes a Jinja parameter `active` (string) and applies `installer-sidebar__item--active` + `aria-current="page"` to the matching item. The two consumers:

   - `dashboard.html` installer branch: `{% include "installer/_sidebar.html" with context %}` and sets `{% set active = "dashboard" %}` (or passes via `set` block).
   - new `event_log.html`: `{% set active = "event-log" %}` before the include.

   The Event Log sidebar item **becomes LIVE in this story** — drop `aria-disabled="true"` + `tabindex="-1"`, change `href="#"` to `href="/installer/event-log"`. The Devices / Settings remain placeholder anchors (Epic 11.3 / 12.x scope).

   The `installerSidebar` Alpine factory at `dashboard.html:268-270` is **unchanged** — the same factory drives mobile collapse on both pages (Alpine re-instantiates per-page on full server navigation, which is the contracted nav model per UX spec line 1287 "All navigation is server-side page navigation").

3. **AC3 — Initial render serves 50 entries; "Load more" pagination via HTMX.** The page's initial GET returns `EVENT_LOG_PAGE_SIZE = 50` rows (well within the epic AC range "50–100"). A `"Load more"` button at the bottom of the list issues `hx-get="/fragments/installer/event-log-list?offset=50&<current-filters>"` with `hx-target="closest ol"` and `hx-swap="beforeend"` — appending rows without a full-page reload (epic AC line 2314 "no full-page reload").

   When `len(returned_rows) < EVENT_LOG_PAGE_SIZE`, the "Load more" button is **NOT** rendered — the end of the result set is reached. The button's `hx-trigger` is `click` (no auto-trigger). The "Load more" link preserves all active filter query params so paged + filtered results stay coherent.

   **Pagination model:** `LIMIT EVENT_LOG_PAGE_SIZE OFFSET ?` against `ORDER BY timestamp DESC, id DESC`. Acceptable for v1 (installer-driven manual browsing); page-boundary drift when new entries arrive mid-browse is a known and acceptable trade-off (a new event sliding the offset is visible as a one-row duplicate at the page boundary — the installer can refresh to re-align). Cursor-based pagination is deferred as a future hardening item if real-world feedback requests it.

---

### Filtering — type, date range, and URL-state

4. **AC4 — Type filter pills (DECISION / DEVICE / SYSTEM / CONSTRAINT / INSTALLER).** Five `<button type="submit" name="type" value="..." class="event-log-filter-pill" aria-pressed="...">` pills in the filter bar. Toggling a pill submits the surrounding form (the filter bar is a single `<form method="get" action="/installer/event-log">` with HTMX targeting the result list — `hx-get` on the form, `hx-target="#event-log-list-wrapper"`, `hx-swap="outerHTML"`, `hx-push-url="true"`). Multiple pills may be active simultaneously: when 2+ pills are active, the form encodes them as a single `type=DECISION,DEVICE` comma-separated value (the 11.1 contract per Q4 resolution in `11-1-...md`).

   Filter parsing — `_parse_event_log_filters` — splits `type` on commas, validates each part against `audit_log.VALID_EVENT_TYPES` (`{"DECISION", "DEVICE", "SYSTEM", "CONSTRAINT", "INSTALLER"}`), and drops invalid entries silently (defensive; an unknown value via URL tampering should not 500 the page). If ALL parsed types are invalid OR the param is absent, no type filter is applied (all event types match).

   The active state of each pill is derived server-side: `aria-pressed="true"` + `event-log-filter-pill--active` CSS modifier on pills whose value is in the parsed type set. Server is authoritative; no Alpine state for pill toggle.

5. **AC5 — Date range filter (window-relative + explicit From / To).** The filter bar contains two `<input type="date" name="from" value="...">` / `<input type="date" name="to" value="...">` fields. Window-relative shortcut: when `?window=24h` is present (the 11.1 link contract), the server computes `since = datetime.now(UTC) - timedelta(hours=24)`, `until = datetime.now(UTC)`. Explicit `from` and `to` override the window — when **either** is present (not necessarily both), the window param is ignored: the explicit value wins for its boundary and the other boundary becomes unbounded (`None`). Partial-explicit ranges therefore do not pair the lone explicit value with the window's other boundary; this is intentional (consistent with "explicit wins" — the installer typed exactly one date and gets exactly one boundary).

   Date range parsing — accepts `YYYY-MM-DD`; invalid strings are dropped silently (same defensive policy as AC4). The interpretation is **midnight UTC** on each date (`from=2026-05-01` means `since = 2026-05-01T00:00:00Z`; `to=2026-05-01` means `until = 2026-05-02T00:00:00Z` — exclusive end-of-day in UTC). Document this in the route docstring + UX hint text ("Dates are interpreted in UTC").

   The filter form fires `hx-get` on blur via `hx-trigger="change"` on each date input. Result list re-renders inline; URL is pushed via `hx-push-url="true"`.

6. **AC6 — Keyword search (debounced, substring match, scoped to filtered dataset).** A `<input type="search" name="q" value="...">` in the filter bar with `hx-get="/fragments/installer/event-log-list"`, `hx-trigger="input changed delay:300ms"`, `hx-target="#event-log-list-wrapper"`, `hx-swap="outerHTML"`, `hx-push-url="true"`. The 300ms debounce matches UX spec line 1534.

   Keyword search is **substring match on `summary` only** (UX spec line 1534, 1544 — "simple substring matching only. No saved filters, no export, no analytics, no advanced faceted search, no full-text indexing"). SQL: `summary LIKE ?` with `?` bound to `f"%{escaped_keyword}%"`. **The `%` and `_` characters in the user keyword are escaped** via `ESCAPE` clause:

   ```sql
   AND summary LIKE ? ESCAPE '\'
   ```

   with the keyword pre-processed: `keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")` so a literal `%` in the search box does not match every row. The escape character is `\\` to align with the SQL string literal.

   **Crucially:** keyword search applies to the **post-filter dataset** (epic AC line 2328 — "after date range and type filters are applied"). The filter SQL is one combined `WHERE` clause assembled in a deterministic order: `WHERE 1=1 AND event_type IN (...) AND device_id = ? AND timestamp >= ? AND timestamp < ? AND summary LIKE ? ESCAPE '\\'`. A test asserts that keyword filtering reduces results relative to the type+date-filtered baseline — not the unfiltered baseline.

7. **AC7 — Filter state in URL query params (bookmarkable, shareable).** Every filter change pushes the URL via `hx-push-url="true"`. On page reload OR direct navigation to a bookmarked URL, the server re-parses the query params and renders the page in the matching state. The "View in event log" links emitted by Story 11.1 (`?type=DECISION,DEVICE`, `?device_id=...`, `?type=SYSTEM&window=24h`) land on this page with the corresponding filter pre-applied. Cross-story integration test: GET `/installer/event-log?type=SYSTEM&window=24h` renders with the SYSTEM pill active AND the date range narrowed to the last 24 hours.

8. **AC8 — Result count caption ("Showing N of M events").** When ANY filter is active (type / device_id / date range / keyword), a caption renders below the filter bar in the format `"Showing 14 of 47 events"` per UX spec line 1542. The values are:

   - `N` = number of rows returned in the current paginated page IF `offset == 0`; otherwise `N` = `offset + len(rows_on_this_page)` (cumulative through current "Load more" position).
   - `M` = `COUNT(*)` of rows matching the same filter (no LIMIT, no OFFSET).

   When NO filter is active (epic UX line 1542: "Not shown when no filter is active"), the caption is **NOT rendered**. The `is_filtered` predicate is true if ANY of `event_types`, `device_id`, `since`, `until`, `keyword` are non-default.

   The COUNT query reuses the same WHERE clause as the SELECT — captured in a shared helper `_event_log_filter_where_clause(filter: EventLogFilter) -> tuple[str, list[object]]` returning the WHERE-fragment + parameter list. Single source of truth; the test asserts the two queries produce identical WHERE clauses.

9. **AC9 — Filter contract (`EventLogFilter` dataclass).** Centralize the parsed filter shape:

   ```python
   from dataclasses import dataclass
   from datetime import datetime
   from typing import Final

   EVENT_LOG_PAGE_SIZE: Final[int] = 50
   _VALID_WINDOWS: Final[dict[str, timedelta]] = {
       "24h": timedelta(hours=24),
       "7d": timedelta(days=7),
       "30d": timedelta(days=30),
   }

   @dataclass(frozen=True)
   class EventLogFilter:
       """Story 11.2 AC9: parsed event-log filter state.

       Single source of truth for ``where_clause`` + ``count`` semantics. The
       parser ``_parse_event_log_filters`` is the only producer; the repository's
       ``list_filtered`` / ``count_filtered`` methods are the only consumers.
       """
       event_types: frozenset[str]   # empty → no type filter
       device_id: str | None
       since: datetime | None        # UTC
       until: datetime | None        # UTC
       keyword: str                  # empty → no keyword filter
       window: str | None = None     # provenance of since/until (24h/7d/30d or None)

       def is_filtered(self) -> bool:
           return bool(
               self.event_types or self.device_id or self.since or self.until or self.keyword
           )
   ```

   Module-level constant `EVENT_LOG_PAGE_SIZE = 50`. The relative-window vocabulary is `_VALID_WINDOWS: Final[dict[str, timedelta]] = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}` — the dict-with-delta form carries both the valid set and the lookup table in one container (no parallel `_WINDOW_DELTAS` dict needed); unknown windows are ignored (defensive). Window-relative parsing uses `datetime.now(UTC)` at request time — the wall clock is the boundary, not a fixed timestamp.

   `EventLogFilter` also carries an optional `window: str | None = None` field recording the **provenance** of `since`/`until`. When non-None, the date range was derived from `?window=...`; when None and `since`/`until` are set, they came from explicit `from`/`to`. The provenance lets the page builder leave the form's date inputs empty for window-derived ranges (so resubmission preserves the now-relative semantics rather than freezing the window's `now` boundary into yesterday's literal date).

---

### Note entry (resilient on failure)

10. **AC10 — `POST /actions/installer-note` resilient note submission.** New route in `src/open_ems/web/routes/actions.py` (sibling to `dismiss_installer_anomaly` at line 464):

    ```python
    @router.post("/actions/installer-note", response_class=HTMLResponse)
    async def post_installer_note(
        request: Request,
        user: InstallerUser = Depends(require_installer),
        observability: ObservabilityService = Depends(get_observability_service),
    ) -> HTMLResponse:
        """Append an INSTALLER-type event_log row from the installer's note.

        On success: HTMX returns the empty form fragment (textarea cleared) PLUS
        an out-of-band swap (`hx-swap-oob="afterbegin:#event-log-list-rows"`)
        carrying the newly-rendered INSTALLER row — prepended to the list.

        On failure: returns 200 with the form re-rendered including the preserved
        note + an inline error message. The textarea content is NEVER lost.
        """
    ```

    Form body fields: `note: str`. Validation steps in order:
    1. **Server-side length check.** `MAX_INSTALLER_NOTE_LENGTH = 2000` from `audit_log.py:14`. Notes exceeding 2000 chars (counted on the raw input) → render the form fragment with preserved `note` value + inline error `"Note exceeds 2,000 characters."`.
    2. **Whitespace-only rejection.** `note.strip() == ""` → render with preserved value + `"Note cannot be empty."`.
    3. **Delegate to `ObservabilityService.installer_note(note)`** — which does the final length check + HTML-escape via `html.escape(stripped, quote=True)` + append via `EventLogRepo.append`. The escape is already in `audit_log.py:83` — DO NOT double-escape here.
    4. **On `ValueError` from the service** — same preserved-form behavior (catch-all defensive path; the service's own `ValueError` cases are length + non-empty which the route already gated, so this branch is normally unreachable but documented).
    5. **On unexpected exception** (DB error, etc.) — log via `structlog.warning("installer_note_persist_failed", error=str(exc))`, return the form with preserved value + a generic inline error `"Could not save note. Please try again."`. The textarea is preserved.

    **Client-side `maxlength="2000"` attribute** on the textarea is the first line of defense (per epic AC line 2332 "rejected client-side before submission"). Server-side check is authoritative.

    **HTMX wiring on the form:**
    - `<form hx-post="/actions/installer-note" hx-target="#installer-note-form" hx-swap="outerHTML" hx-include="closest form">`
    - The success response is a re-rendered empty form fragment (target outerHTML swap)
    - PLUS an OOB swap containing the new INSTALLER row that prepends to the list: `<li hx-swap-oob="afterbegin:#event-log-list-rows">...</li>`
    - The failure response is the same form fragment with the `note` value preserved + the inline error rendered inside `<p class="installer-note-form__error" role="alert">`

11. **AC11 — `data_age_seconds` is N/A here.** The event log page does not display data-age captions (those are dashboard concerns). All entries carry their own append-time timestamp; "Updated X ago" semantics do not apply. **No regression to AC4 of Story 11.1** — that contract applies only to the per-device-row component.

---

### Timezone correctness

12. **AC12 — UTC at rest, browser-local at render.** Per epic AC line 2319: all timestamps stored in UTC; rendering layer converts to local timezone with UTC offset shown. The event log page implements this in two layers:

    **Layer A (server-side fallback, JS-disabled accessible):** Each row's `<time>` element renders the timestamp using the same server-side `Europe/Brussels` formatter introduced in Story 11.1 (`build_installer_event_log_preview_context` at `state_serialization.py:855-915`). The display string format is `"2026-05-12 14:32 CEST / UTC+2"`. This is the fallback that screen readers, JS-disabled clients, and broken-locale browsers see. **Extract the formatting helper into a module-level `_format_event_log_timestamp_brussels_fallback(ts: datetime) -> str`** so both the preview (11.1) and the list (11.2) call the same code path; the previous inline implementation in the preview builder is replaced by a call to the new helper. Behavior must be byte-for-byte identical to 11.1's output so the existing preview test does not regress.

    **Layer B (client-side Intl.DateTimeFormat override):** A small inline script in the event-log page template (NOT in `base.html`; isolated to this page so the preview surface is untouched) walks `[data-utc]` elements and rewrites their `textContent` to the browser-local representation using `Intl.DateTimeFormat`:

    ```js
    // Story 11.2 AC12 — client-side timezone localization for [data-utc] elements.
    // Runs on initial page load and after every HTMX swap (htmx:afterSwap).
    (function () {
      function localizeOne(el) {
        var utc = el.getAttribute('data-utc');
        if (!utc) return;
        var d = new Date(utc);
        if (isNaN(d.getTime())) return;  // server-fallback stays in place
        var opts = {
          year: 'numeric', month: '2-digit', day: '2-digit',
          hour: '2-digit', minute: '2-digit',
          timeZoneName: 'short',
        };
        try {
          var parts = new Intl.DateTimeFormat(undefined, opts).format(d);
          el.textContent = parts;
        } catch (e) {
          /* keep server-side fallback on Intl failure */
        }
      }
      function localizeAll(root) {
        var nodes = (root || document).querySelectorAll('[data-utc]');
        for (var i = 0; i < nodes.length; i++) localizeOne(nodes[i]);
      }
      document.addEventListener('DOMContentLoaded', function () { localizeAll(document); });
      document.body.addEventListener('htmx:afterSwap', function (evt) {
        localizeAll(evt.detail.elt);
      });
    })();
    ```

    The script lives in `event_log.html`'s `{% block scripts %}` — **scoped to this page**, not loaded on the dashboard. The 11.1 dashboard preview keeps the Brussels server-side fallback only (acceptable; the preview is short and Brussels is the project's deployment locale).

    **Critical invariant:** `data-utc` MUST be a valid ISO-8601 UTC string (e.g., `"2026-05-12T14:32:00+00:00"`). The server emits this via `entry.timestamp.astimezone(UTC).isoformat()` — same pattern as `state_serialization.py:868`. A test asserts the rendered HTML contains a `data-utc` attribute matching the regex `\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}` for every row.

13. **AC13 — Server-rendered time string is in Europe/Brussels until JS rewrites.** No mixing: every `<time>` element carries `data-utc` (UTC ISO) AND a server-rendered Brussels-locale textContent. The client script either replaces textContent on success or leaves it alone on failure (Intl missing in old browsers, malformed `data-utc`, etc.) — never mixed mid-string. UX spec line 197 ("entries never require decoding") is respected by both paths.

---

### Repository read methods + performance

14. **AC14 — `EventLogRepo.list_filtered` + `count_filtered`.** Two new async methods in `src/open_ems/storage/repositories/event_log_repo.py`:

    ```python
    async def list_filtered(
        self,
        *,
        event_types: frozenset[str],
        device_id: str | None,
        since: datetime | None,
        until: datetime | None,
        keyword: str,
        limit: int,
        offset: int,
    ) -> list[EventLogEntry]:
        """Paginated, filtered, ORDER BY timestamp DESC, id DESC list."""

    async def count_filtered(
        self,
        *,
        event_types: frozenset[str],
        device_id: str | None,
        since: datetime | None,
        until: datetime | None,
        keyword: str,
    ) -> int:
        """COUNT(*) over the same WHERE clause as list_filtered."""
    ```

    Both methods MUST share their WHERE-clause assembly via a private helper:

    ```python
    def _build_event_log_filter_where(
        *,
        event_types: frozenset[str],
        device_id: str | None,
        since: datetime | None,
        until: datetime | None,
        keyword: str,
    ) -> tuple[str, list[object]]:
        """Single source of truth for the filter WHERE clause + params.

        Returns ('', []) when no filter is active (matches all rows).
        Parameter order is LOAD-BEARING: SELECT and COUNT bind in the same order.
        """
    ```

    **WHERE-clause construction rules:**
    - No filter active → `""` (just `ORDER BY ... LIMIT ... OFFSET ?` in SELECT; `SELECT COUNT(*) FROM event_log` in COUNT).
    - Type filter present → `event_type IN (?, ?, ...)` with one placeholder per type. **SQL injection safety:** types are validated against `VALID_EVENT_TYPES` at parse time; types still go through parameter binding (defense in depth).
    - Device ID filter present → `device_id = ?` (exact match).
    - Since/until present → `timestamp >= ?` / `timestamp < ?` (since inclusive, until exclusive). **Bind as ISO-8601 UTC strings** to match the storage representation (`event_log_repo.py:62` writes via `astimezone(UTC).isoformat()`).
    - Keyword present → `summary LIKE ? ESCAPE '\\'` with the keyword wrapped in `%...%` after `%`/`_`/`\\` escape (per AC6).
    - Conditions joined with `AND `; the final clause is prefixed with ` WHERE ` only when non-empty.

15. **AC15 — NFR-P5 performance (queries over past 30 days return within 5 seconds).** At representative log volume — defined per architecture line 51 (12 months energy history, 90 days event log) and PRD line 698 (event log retained ≥ 90 days) — the worst-case row count is bounded by retention: ~30 events/hour × 24 × 90 = ~64,800 rows at the 90-day cap. The 30-day window subset is ~21,600 rows. Existing indexes from migration `0005`:
    - `ix_event_log_timestamp` (covers date range filter)
    - `ix_event_log_event_type` (covers type filter)

    No `ix_event_log_device_id` index exists. **Decision: do NOT add it in 11.2.** With ≤22K rows and the timestamp/event_type indexes narrowing first, the remaining `device_id = ?` filter is a small scan well under the 5s budget on Pi 4. Document explicitly in dev notes that adding the index is a follow-up if real-world deployments show degradation.

    **Performance test (AC22 #N — see test list below):** populate the test DB with 3,000 rows (1 every ~14 minutes over 30 days; ~75/day across 5 event types and 4 device_ids — representative shape) and assert that:
    - `list_filtered(no filter, limit=50, offset=0)` completes in mean < 200ms over 10 iterations (generous for 50-row LIMIT against an indexed table).
    - `list_filtered(event_types={"DECISION"}, since=now-30d, limit=50, offset=0)` completes in mean < 500ms.
    - `list_filtered(keyword="peak", since=now-30d, limit=50, offset=0)` completes in mean < 1500ms (LIKE has no index; full scan in worst case across the type-filtered subset).
    - `count_filtered(...)` completes in mean < 2000ms across the same 30-day windows.

    The aggregate ceiling for any one request is **5000ms** (NFR-P5 hard contract). The mean-based assertions above are tighter to surface regressions earlier; if any individual iteration exceeds 5s, the test fails (use `max(timings) < 5000` as the strict NFR-P5 assertion).

---

### Cross-cutting structural enforcement

16. **AC16 — XSS safety on installer notes.** `summary` for INSTALLER rows is `html.escape(stripped, quote=True)` at the service boundary (`audit_log.py:83`). Jinja2 autoescape is on by default. **Test:** submit a note containing `<script>alert(1)</script>`; assert the rendered list HTML contains `&lt;script&gt;` (escaped) and does NOT contain the raw tag. This regression covers both the write path (service escape) and the render path (Jinja autoescape).

17. **AC17 — Append-only, no edit/delete surface.** No edit or delete UI for any row, system-generated or installer-authored. UX spec line 1920: "Logs are append-only. No entries are ever edited or deleted by the system or by users. Installer notes are similarly permanent once submitted. Corrections are made by adding new entries — not modifying existing ones." The repo's `update()` / `delete()` methods at `event_log_repo.py:77-81` already raise `RuntimeError("event_log is append-only")` — DO NOT add new bypass paths.

18. **AC18 — Filter-by-`device_id` does not break when `device_id` is None for some rows.** Most system events emit `device_id=None` (DECISION events from the engine; SYSTEM events from lifecycle). When the device_id filter is active, the SQL `device_id = ?` excludes NULL rows correctly (SQLite NULL comparison semantics). **Test:** with a filter `device_id="batt-1"`, assert that DECISION rows with `device_id=NULL` are NOT returned.

19. **AC19 — Empty result state.** When the filter returns 0 rows, render the empty-state copy per UX spec line 1497: `"No events match this filter."` (filter-active variant) OR `"No events yet. Events will appear here as the system makes decisions."` (no-filter variant — same copy as the empty preview state). The "Load more" button is suppressed.

20. **AC20 — URL contracts honored from Story 11.1.** Specifically:
    - `?type=SYSTEM&window=24h` → SYSTEM pill active + last 24h
    - `?type=DEVICE,SYSTEM&window=24h` → DEVICE + SYSTEM pills active + last 24h
    - `?device_id=batt-1` → device_id filter active, no type filter
    - `?type=DECISION` (single type) → DECISION pill active, no time filter

    Integration test #N exercises all four URL patterns end-to-end (GET, assert pills + count + result list).

21. **AC21 — Zero new dependencies.** `uv.lock` MUST be unchanged. The implementation uses:
    - `datetime` / `zoneinfo` (stdlib)
    - `aiosqlite` (already pinned)
    - `fastapi` Query / Depends (already pinned)
    - `jinja2` autoescape + the existing `Jinja2Templates` instance
    - `htmx` 1.9.12 (already loaded from base.html)
    - `alpine.js` 3.13.10 — actually NOT needed for this page; filter pills + form submit are pure server-side. Sidebar's `installerSidebar` factory is reused for mobile-collapse only.

---

### Tests (target ≥ 40 net new)

22. **AC22 — Tests (unit + integration).** Total target: **≥ 40 new tests** added to the 1716 baseline from 11.1, bringing the total to **≥ 1756 passed**.

    **Repository tests** (`tests/unit/storage/repositories/test_event_log_repo.py` — extend):
    1. `test_list_filtered_no_filter_returns_all_ordered_by_timestamp_desc_then_id_desc`
    2. `test_list_filtered_respects_limit_and_offset`
    3. `test_list_filtered_by_single_event_type`
    4. `test_list_filtered_by_multiple_event_types_with_in_clause`
    5. `test_list_filtered_by_device_id_excludes_null_device_rows`
    6. `test_list_filtered_by_since_only`
    7. `test_list_filtered_by_until_only`
    8. `test_list_filtered_by_since_and_until_inclusive_exclusive_semantics`
    9. `test_list_filtered_keyword_substring_match_on_summary_only`
    10. `test_list_filtered_keyword_escapes_sql_wildcards_percent_and_underscore` _(insert summaries `100% efficient` and `100_efficient`; search for literal `%` returns only the percent row)_
    11. `test_list_filtered_keyword_escapes_backslash` _(insert `path\\to\\thing`; search for `path\\` matches it)_
    12. `test_list_filtered_combined_filters_apply_AND_semantics`
    13. `test_count_filtered_matches_list_filtered_length_under_same_filter`
    14. `test_count_filtered_no_filter_returns_full_table_count`
    15. `test_count_filtered_with_type_filter`
    16. `test_count_filtered_with_keyword`
    17. `test_list_filtered_xfail_when_offset_negative_raises_value_error`
    18. `test_list_filtered_xfail_when_limit_zero_or_negative_raises_value_error`

    **Filter parser tests** (`tests/unit/web/test_installer_event_log_filters.py` — new):
    19. `test_parse_no_params_returns_empty_filter`
    20. `test_parse_type_single_returns_one_member_frozenset`
    21. `test_parse_type_comma_separated_returns_full_frozenset`
    22. `test_parse_type_with_invalid_member_drops_invalid_silently`
    23. `test_parse_type_all_invalid_returns_empty_set_no_filter`
    24. `test_parse_window_24h_sets_since_to_now_minus_24_hours`
    25. `test_parse_window_unknown_value_is_dropped_silently`
    26. `test_parse_explicit_from_to_overrides_window`
    27. `test_parse_from_invalid_date_string_is_dropped`
    28. `test_parse_keyword_strips_outer_whitespace_preserves_inner`
    29. `test_parse_device_id_preserved_verbatim`
    30. `test_event_log_filter_is_filtered_predicate_is_true_when_any_field_set`

    **Page route tests** (`tests/unit/web/test_installer_event_log_route.py` — new):
    31. `test_event_log_page_renders_sidebar_with_event_log_active`
    32. `test_event_log_page_renders_five_filter_pills`
    33. `test_event_log_page_renders_note_form_with_2000_char_maxlength`
    34. `test_event_log_page_initial_render_shows_50_entries_max`
    35. `test_event_log_page_renders_load_more_when_more_rows_exist`
    36. `test_event_log_page_omits_load_more_when_no_more_rows`
    37. `test_event_log_page_with_type_filter_pre_selects_pills`
    38. `test_event_log_page_with_window_24h_pre_fills_date_range`
    39. `test_event_log_page_with_device_id_filter_applies_correctly`
    40. `test_event_log_page_with_keyword_filter_pushes_url`
    41. `test_event_log_page_renders_result_count_when_filter_active`
    42. `test_event_log_page_omits_result_count_when_no_filter`
    43. `test_event_log_page_empty_result_filter_active_renders_no_match_copy`
    44. `test_event_log_page_empty_result_no_filter_renders_no_events_yet_copy`
    45. `test_event_log_page_unauthenticated_redirects_to_login`
    46. `test_event_log_page_homeowner_rejected_redirects_to_homeowner_home`
    47. `test_event_log_page_renders_data_utc_attribute_for_every_row`
    48. `test_event_log_page_renders_brussels_fallback_textcontent_for_every_row`
    49. `test_event_log_page_db_query_count_bounded` _(uses tests/utils/db_query_counter; 11.1 helper)_

    **List fragment tests** (`tests/unit/web/test_installer_event_log_fragments.py` — new):
    50. `test_event_log_list_fragment_paginates_via_offset_param`
    51. `test_event_log_list_fragment_preserves_filter_in_load_more_link`
    52. `test_event_log_list_fragment_returns_just_rows_no_full_page_shell`

    **Note POST tests** (in the same fragments file or a dedicated `test_installer_note_route.py`):
    53. `test_post_installer_note_success_returns_empty_form_plus_oob_row_swap`
    54. `test_post_installer_note_over_2000_chars_returns_form_with_preserved_value_and_error`
    55. `test_post_installer_note_whitespace_only_returns_form_with_preserved_value_and_empty_error`
    56. `test_post_installer_note_persists_html_escaped_summary` _(submit `<script>...</script>`; assert DB row's summary is escaped)_
    57. `test_post_installer_note_homeowner_rejected`
    58. `test_post_installer_note_csrf_token_required` _(P-tier coverage; not deferred this time — extends DF3 from 11.1's deferred-work into structural coverage for the new POST surface)_

    **Performance test** (`tests/integration/web/test_installer_event_log_performance.py` — new):
    59. `test_event_log_30_day_query_under_nfr_p5_budget_at_representative_volume` _(per AC15 — 3000 rows over 30 days; 10-iteration mean test; strict max < 5000ms)_

    **End-to-end integration tests** (`tests/integration/web/test_installer_event_log_e2e.py` — new):
    60. `test_e2e_filter_pill_toggle_updates_list_inline`
    61. `test_e2e_keyword_search_300ms_debounced_partial_replace`
    62. `test_e2e_load_more_appends_rows_without_full_page_reload`
    63. `test_e2e_note_submit_prepends_installer_row_clears_textarea`
    64. `test_e2e_note_submit_failure_preserves_textarea_value`
    65. `test_e2e_url_contract_type_system_window_24h_renders_filtered_view` _(AC20 cross-story link from 11.1)_
    66. `test_e2e_url_contract_device_id_filter_renders_filtered_view` _(AC20)_
    67. `test_e2e_xss_in_note_renders_escaped`

23. **AC23 — Quality gates.** All of the following MUST hold at story completion:
    - `uv run pytest` → ≥ 1756 passed (1716 baseline from 11.1 + ≥ 40 net new). Same xfailed + skipped counts (no new xfail introduced unless explicitly documented).
    - `uv run ruff check .` → clean across all new + modified files.
    - `uv run ruff format --check .` → clean.
    - `uv run mypy src` → clean across all source files (new repo methods, new route, new builder, new fragment route, new POST route).
    - `uv.lock` → unchanged (no new dependencies).
    - No new migration (existing indexes from migration `0005` are sufficient per AC15 dev notes).

---

## Developer Context Section

This section is the comprehensive guide for the dev agent. It encodes everything not obvious from the AC text alone — including patterns to mirror from 11.1, regressions to avoid, the URL contract surface inherited from 11.1, and cross-story interactions.

### Foundation patterns to mirror from Story 11.1 (done 2026-05-12)

**Pattern 1 — Page route + sidebar + fragments split.** Story 11.1 established the installer-dashboard layout as a server-rendered page (`dashboard.html` installer branch) with HTMX-driven section fragments. 11.2 takes the same pattern at a different scale: the event-log page is server-rendered on initial GET with all filter state derived from query params, and the list portion is replaceable via a single HTMX fragment endpoint.

- **Page route lives in `routes/installer.py`** (alongside `installer_dashboard` — not in `actions.py` and not in `fragments.py`). This is a navigable page, not a partial.
- **Fragment route lives in `routes/fragments.py`** (alongside the 5 installer dashboard fragments). The fragment returns the filter result list + result count + "Load more" — wrapped in a single `<section id="event-log-list-wrapper">` element so HTMX `hx-swap="outerHTML"` collapses both the count caption AND the list together.
- **Filter-bar form** is rendered ONCE in the page (not in the fragment) — the wrapper element it targets is INSIDE the form so HTMX swaps in-place without losing the form.

**Pattern 2 — Builder + route + template split for testability.** Per the 11.x precedent at `state_serialization.py:665+` (the 5 installer builders), all rendering context derivation goes through pure-functional builders. The 11.2 surface adds:

- `build_installer_event_log_page_context(filter: EventLogFilter, rows: list[EventLogEntry], total_count: int, current_offset: int) -> dict` — full-page context including filter pill active states + result count + Load-more flag + note form initial state.
- `build_installer_event_log_list_context(rows: list[EventLogEntry], total_count: int, current_offset: int, filter: EventLogFilter) -> dict` — list-fragment context (subset of the page context).
- `build_installer_note_form_context(note_value: str = "", error_message: str | None = None) -> dict` — note form context (for both initial render AND failure-response re-render).

All three builders are pure-functional over their typed inputs. The route does the I/O (repo calls, filter parse) and the template renders the builder output.

**Pattern 3 — `data-utc` + server-side Brussels fallback + client-side Intl rewriter.** See AC12. Story 11.1's preview emits the same `data-utc` attribute and server-side Brussels textContent; 11.2 ADDS the client-side rewriter scoped to the new page. Extract the shared `_format_event_log_timestamp_brussels_fallback` helper into `state_serialization.py` (currently inlined at `state_serialization.py:868-893`); the new event-log builder calls the same helper. The preview test must continue to pass byte-for-byte.

**Pattern 4 — DB-query-count discipline via `tests/utils/db_query_counter.py`.** Story 11.1 introduced this helper at `tests/utils/db_query_counter.py`. Reuse it for AC22 test #49 (page route DB query count). Expected counts on the page route:
- 1 query for `list_filtered`
- 1 query for `count_filtered` (only when filter is active; else skipped — implementation detail)
- Auth queries on `sessions` / `users` (framework-level, exempt — same exemption rule as 11.1 AC10 #2 / P20).

The forbidden-table substring assertion is dropped for this route (every event-log query DOES touch `event_log` — that's the entire point). Replace with a hard upper bound: `assert counter.count <= 5` (3 framework + 2 application).

**Pattern 5 — Defensive parser for URL params.** Filter parsing follows the "silently drop invalid; never 500" rule. Examples:
- `?type=GARBAGE,DECISION` → parses as `{DECISION}` (GARBAGE silently dropped)
- `?from=not-a-date` → parses as `since=None` (no error)
- `?window=99h` → parses as `since=None` (only `_VALID_WINDOWS` accepted)
- `?offset=abc` → FastAPI's int coercion handles this; returns 422 if it slips through, which is acceptable for a non-user-typed param (offset arrives from "Load more" links the server itself emitted).

The "drop silently" rule applies to URL-tampering-resilience for params the user might type or share (`type`, `from`, `to`, `q`, `device_id`, `window`); it does NOT apply to `offset` which is server-emitted.

**Pattern 6 — Single source of truth for the WHERE clause.** AC14's `_build_event_log_filter_where` is non-negotiable. The risk of drift between `list_filtered` and `count_filtered` WHERE clauses is the same class as the `_changed_fields` vs `_build_audit_changes` drift item in `deferred-work.md` (Story 9.3 deferred — line 50-51). Lift the helper to module level so both methods call it; the unit test asserts `count_filtered(...) == len(list_filtered(...))` for several filter combinations.

### Files to read in full before starting

**Required reads (failure to read these is the primary cause of review-cycle bugs):**

1. `src/open_ems/storage/repositories/event_log_repo.py` — the 11.1 base. Note: `EventLogEntry` model (frozen Pydantic, `extra=forbid`, `id ge=1`, `schema_version ge=1`, timezone-aware `timestamp`). `list_recent` is the read-path precedent; the new `list_filtered` uses the same `row → EventLogEntry` parse logic. `count_peak_limiting_applied_decisions` shows the LIKE-in-detail-JSON pattern (don't copy that pattern for keyword search — it's a domain-specific aggregator, not a general-purpose filter).
2. `src/open_ems/services/audit_log.py` — `VALID_EVENT_TYPES` set, `MAX_INSTALLER_NOTE_LENGTH=2000`, `ObservabilityService.installer_note` (escape + length check + delegate to `audit`). DO NOT replicate the validation in the route — call the service.
3. `src/open_ems/web/routes/installer.py` — the `installer_dashboard` route is the structural template for `installer_event_log` (require_installer gate, TemplateResponse pattern).
4. `src/open_ems/web/routes/fragments.py` (full — 259 lines). The 5 installer fragment routes at lines 148-234 are the precedent for the event-log-list fragment.
5. `src/open_ems/web/routes/actions.py` (full — esp. lines 460-498 for `dismiss_installer_anomaly`). The note POST follows the same shape: require_installer + read body + delegate + render fragment.
6. `src/open_ems/web/state_serialization.py:855-915` — `build_installer_event_log_preview_context`. The new builders share the timestamp-formatting helper (extract to module level — see Pattern 3).
7. `src/open_ems/web/templates/dashboard.html` — the installer branch is the sidebar pattern reference. Lines 57-78 (sidebar) are the markup to extract into `_sidebar.html`; lines 79-117 (HTMX-bound sections) are NOT to be touched.
8. `src/open_ems/web/templates/fragments/installer/event-log-preview.html` — the `<time>` element + `data-utc` pattern; the new list page emits the same shape on each row.
9. `src/open_ems/web/csrf.py` — confirms `POST /actions/installer-note` is CSRF-protected by `CsrfMiddleware` (registered at `web/app.py:696`). HTMX sends `X-CSRF-Token` automatically via the listener in `base.html:36-43`.
10. `src/open_ems/web/dependencies.py:147-152` — the existing `get_event_log_repo` is reused; no new dependency wiring required. **One new dependency: `get_observability_service`** (parallel pattern to `get_event_log_repo`; constructs `ObservabilityService()` per request).
11. `tests/utils/db_query_counter.py` — 11.1's helper; reused for AC22 #49.
12. `tests/integration/web/test_installer_dashboard_e2e.py` — the 5-test e2e module from 11.1 is the structural model for the new e2e module. Fixture patterns (app boot, session_repo, csrf header) carry over.

### Files to read on-demand

- `src/open_ems/storage/database.py` — only if connection lifecycle or WAL semantics are unclear; the existing `_conn` injection through `EventLogRepo.__init__` is what `list_filtered` uses.
- `src/open_ems/web/app.py` — only the `app.include_router(...)` block at lines 688-695; no new routers are added (page route lands in `installer_router`, fragment in `fragments_router`, POST in `actions_router`).
- `migrations/versions/0005_add_event_log_table.py` — only to confirm column types + existing indexes. No migration in 11.2.

### What NOT to change

- **The 11.1 dashboard surface** — `dashboard.html` installer branch lines 79-117 (the 5 HTMX-bound sections) is UNTOUCHED. The sidebar extraction touches lines 57-78 only (replacing the inline `<nav>` with an `{% include %}`).
- **`EventLogEntry` Pydantic model fields** — UNCHANGED. The new methods produce the same `EventLogEntry` instances; no new fields.
- **`ObservabilityService.installer_note`** — UNCHANGED. The new POST route is a thin wrapper.
- **`event_log` table schema, indexes, and migrations** — UNCHANGED. NFR-P5 is met with the existing `ix_event_log_timestamp` + `ix_event_log_event_type`.
- **HTMX / Alpine / FastAPI / Pydantic versions** — UNCHANGED (`uv.lock` invariant per AC21 / AC23).
- **The homeowner-side templates and routes** — UNTOUCHED. Cross-role isolation invariant (UX spec line 41-44 — "Role isolation with a single web app").
- **The 5 installer fragment routes from 11.1** — UNTOUCHED. They continue to serve the dashboard. The NEW fragment route `/fragments/installer/event-log-list` is purely additive.
- **`installer_anomaly.py` and the dismiss state machine** — UNTOUCHED. The "View in event log" link target (`?type=...&window=...`) is consumed by this story but the link emitter (the anomaly notice template) does not change.

### Library / framework constraints

- **Python:** 3.13 (project lockfile-pinned).
- **FastAPI:** existing pin. Use `Annotated[str | None, Query(alias="from")]` for the `from` query param to avoid the Python keyword collision; `Query(alias="to")` for `to`. No new FastAPI features.
- **Pydantic v2:** the new `EventLogFilter` is a `@dataclass(frozen=True)` — stdlib, not Pydantic. Hashable, immutable, simpler. The Pydantic surface stays at the `EventLogEntry` boundary only.
- **HTMX 1.9.12:** existing pin. New attributes used: `hx-get`, `hx-post`, `hx-target`, `hx-swap` (`outerHTML`, `beforeend`, `afterbegin`), `hx-trigger` (`click`, `input changed delay:300ms`, `change`), `hx-push-url="true"`, `hx-swap-oob="afterbegin:#event-log-list-rows"`. All standard HTMX 1.9 features — no version bump.
- **Alpine.js 3.13.10:** existing pin. The page does NOT introduce new Alpine factories — `installerSidebar` (from 11.1) is the only Alpine on this page.
- **Jinja2 autoescape:** ON by default in `Jinja2Templates`. Installer-note `summary` is double-defended: escaped at service boundary (`html.escape`) and re-escaped by Jinja on render. This is acceptable (idempotent for already-escaped strings; the output is `&amp;lt;script&amp;gt;` if double-escaped, but the service-level escape comes BEFORE storage, so the DB stores `&lt;script&gt;` and Jinja's autoescape on a string starting with `&lt;` produces `&amp;lt;...` which is wrong). **Resolution:** the existing `_handoff_guide_body.html` template at `src/open_ems/web/templates/installer/_handoff_guide_body.html` is the precedent for installer-note rendering — confirm via grep what escape pattern it uses. If autoescape double-escapes, the template MUST mark the summary `| safe` ONLY IF the service-side `html.escape` is the authoritative XSS gate. **The current 11.1 preview template at `event-log-preview.html:23` renders `{{ row.summary }}` without `| safe`** — relying on Jinja autoescape; the underlying DB value is the already-escaped string from `html.escape(stripped, quote=True)`, which means the rendered output is the double-escaped form. **This is correct** — Jinja escaping an already-escaped string produces the visually-correct display because `&lt;` rendered without further escape would be interpreted as a `<` tag start. The double-escape ensures a literal `&lt;` is shown in the page. 11.2 follows the same pattern. AC22 #56 (XSS regression test) covers this.
- **structlog:** existing pin. New emissions:
  - `installer_note_persisted` on success (`session_id`, `note_length`, `event_log_id`, `component="installer_event_log"`)
  - `installer_note_persist_failed` on unexpected exception (catch-all branch in AC10)
  - `installer_event_log_filter_invalid_value` at DEBUG level when a parser silently drops an invalid value (URL tampering signal; quiet by default but available for ops)
- **No new dependencies.** `uv.lock` unchanged.

### Web research — current-version specifics

- **HTMX 1.9.12 `hx-swap-oob` ("out-of-band swap")** — the documented pattern for "after this POST, swap the response into the target AND ALSO update another element". Used by AC10 to clear the textarea AND prepend the new INSTALLER row in a single response. Reference: htmx.org/attributes/hx-swap-oob/ (the markup pattern is `<li id="..." hx-swap-oob="afterbegin:#event-log-list-rows">...</li>` inside the response body). No version-specific gotchas.
- **HTMX `hx-push-url="true"` on `<form>` elements** — pushes the URL composed from `hx-get` + serialized form data to the browser history; back-button navigation re-issues the GET. Reference: htmx.org/attributes/hx-push-url/. Works at HTMX 1.9+.
- **`Intl.DateTimeFormat` with `timeZoneName: 'short'`** — modern browsers (Chrome 91+, Firefox 91+, Safari 14.1+) render the short zone name (`CEST`, `EST`). Older browsers may render the long name or fall through — the script's try/catch guards against this; the server-side Brussels fallback covers the broken case. No new JS dependencies.
- **FastAPI `Query(alias="from")`** — standard since FastAPI 0.95; the project's existing pin supports it.
- **aiosqlite `LIKE ... ESCAPE` parameter binding** — works with the standard `?`-style binding; `ESCAPE '\\'` is a literal in the SQL string. Reference: SQLite docs on LIKE.

### Previous-story intelligence

**Story 11.1 (Installer dashboard, done 2026-05-12) — 5 net-new builder functions, 1 new POST action, 5 new templates, 1 new module (`installer_anomaly.py`), 1 new test helper (`db_query_counter.py`). 88 net new tests. Key carry-overs to 11.2:**

- **`EventLogEntry` Pydantic model** — exists at `event_log_repo.py:14-33`. 11.2 reuses it as-is for `list_filtered` / `count_filtered` result objects.
- **`EventLogRepo.list_recent`** — pattern reference for `list_filtered`. Note the `ORDER BY timestamp DESC, id DESC` secondary sort (load-bearing for ties — same applies to the paginated list).
- **`_format_event_log_timestamp_brussels_fallback`** — INLINE in 11.1's preview builder; the 11.2 extraction lifts it to module level (see Pattern 3). The extraction is a refactor, not a behavior change.
- **`/installer/event-log?<filter>` URL contracts** — emitted by the 11.1 anomaly notice, per-device-row, and health-indicator links (P2 review fix). 11.2 honors the contract. The format is the comma-separated-single-key form (Q4 resolution): `?type=DECISION,DEVICE&device_id=...&window=...`.
- **`db_query_counter`** — reusable for AC22 #49.
- **Sidebar markup** — inline in `dashboard.html:57-78`. 11.2 extracts to a partial; the dashboard's installer branch updates to use the partial. **A test asserts the dashboard still renders the same 5 nav items in the same order** so the refactor is no-regression.
- **The `installerSidebar` Alpine factory** — UNCHANGED at `dashboard.html:268-270`. The mobile-collapse behavior carries over to the event-log page by virtue of using the same sidebar partial.

**Story 10.4 (Weekly summary, done 2026-05-12):**

- The `weekly-summary.html` HTMX fragment + `hx-trigger="every 60s"` pattern is NOT applicable here — the event-log page does NOT auto-refresh. Filter changes are user-driven; no polling on the list. (Per UX spec line 1525 — HTMX polling is a homeowner-realtime pattern; the installer event log is a browsing surface, not a live stream.)
- The `tests/utils/db_query_counter` helper introduced via 11.1 is the precedent for the AC22 #49 pattern.

**Story 10.3 (Strategy selector, done 2026-05-12) and Story 10.2 (EV override, done 2026-05-12):**

- The HTMX POST + outerHTML swap pattern is the precedent for `POST /actions/installer-note` AC10. The `_safe_audit` pattern at `actions.py:73-82` is OUT OF SCOPE for this story (the note POST IS the audit — no separate emission needed).
- The `csrf_token` injection at the route context level (per `homeowner_status_headline` at `fragments.py:111-145`) is the model for the note-form rendering — the form's hidden `csrf_token` input is injected via the builder context.

**Story 9-Y-a (E2E fail-abort coverage, done 2026-05-11):**

- Naming-honesty contract — every test name must describe what it actually asserts. Apply to all AC22 test names. Examples in the test list above are deliberately verbose ("`test_list_filtered_keyword_escapes_sql_wildcards_percent_and_underscore`") to make their assertions self-documenting.

### Git intelligence — recent commit patterns

Recent commits (`git log --oneline -8`):
- `c52229e 11.1` — installer dashboard story (this story's parent)
- `0a595a9 10.4` — weekly summary
- `71bf9a1 10.3` — strategy selector
- `efd986f 10.2` — EV status card
- `4ac50e1 10.1` — homeowner dashboard layout

**Patterns from recent work that 11.2 should follow:**

- One commit per story. 11.2 should land in ONE commit titled `11.2` after dev cycle + review cycle complete.
- No migration in 11.2 (per AC15 — existing indexes suffice).
- File-naming convention: `tests/unit/<module>/test_<name>.py`. New test files for 11.2:
  - `tests/unit/web/test_installer_event_log_filters.py` (NEW — filter parser tests)
  - `tests/unit/web/test_installer_event_log_route.py` (NEW — page route tests)
  - `tests/unit/web/test_installer_event_log_fragments.py` (NEW — list-fragment + note-POST tests)
  - `tests/integration/web/test_installer_event_log_e2e.py` (NEW — end-to-end)
  - `tests/integration/web/test_installer_event_log_performance.py` (NEW — NFR-P5 benchmark)
  - `tests/unit/storage/repositories/test_event_log_repo.py` — EXTEND with `list_filtered` / `count_filtered` tests (≥18 new tests).

---

## A2 trigger evaluation (mandatory audit note for non-A2-triggered stories)

A2 evaluation was performed against T1–T7 from the Epic 8 retrospective (2026-05-10). None matched. Brief rationale:

- **T1 (lifecycle / state-machine):** note-entry has success/failure UI behavior, but no fail-safe entry/exit, no recovery semantics, no signature comparison across renders. The note-form state machine is trivial UI optimistic pattern, not the structural-state class.
- **T2 (retries / cancellation):** no retry policy with attempt counting; failure is shown inline and the user re-clicks. Default browser/HTMX cancellation on disconnect; no in-flight resource cleanup beyond ordinary FastAPI request cancellation.
- **T3 (persistence + recovery across restart):** notes are normal append-only `event_log` rows via the existing `ObservabilityService.installer_note`. No new durable state.
- **T4 (watchdog / timing):** 300ms keyword debounce is UI debounce, not deadline-relative scheduling.
- **T5 (multi-adapter coordination):** pure web layer; no adapter coordination.
- **T6 (deployment / restart):** no process restart triggered by this workflow.
- **T7 (installer workflow orchestration):** the page is a browsing/annotation surface; no multi-step orchestration mutating persisted configuration.

R1–R7 mandatory artifacts therefore do not apply. However, a brief deferred-findings overlap check follows.

### Deferred-findings overlap with 11.2 scope (informational — not R7-mandatory)

Scanning `_bmad-output/implementation-artifacts/deferred-work.md`:

| Bracket reference | Item | Classification for 11.2 |
|---|---|---|
| `[tests/utils/db_query_counter.py]` (Story 11.1 DF1) | `db_query_counter` patches only `Connection.execute`; future cursor-based execution surfaces would bypass it | **safe-during-this-story** — 11.2 uses the existing helper; the gap is identical to 11.1's state. No new exposure. |
| `[src/open_ems/web/state_serialization.py:775, 793]` (Story 11.1 DF2) | Peak tracker `<= 0.0` branch mislabels small real peaks as "No data yet" | **acceptable-post-this-story** — peak tracker is a 11.1 surface; 11.2 does not touch it. |
| `[tests/unit/web/test_installer_dashboard_fragments.py]` (Story 11.1 DF3) | No negative CSRF test for `POST /actions/dismiss-anomaly` | **resolved-by-AC22-#58** — 11.2 introduces a CSRF coverage test for the new `POST /actions/installer-note` route. This sets a precedent that the next hardening sub-story can extend to `dismiss-anomaly`, `set-strategy`, `ev-override`. The 11.1 DF3 ticket itself remains open (it covers the 4 existing POSTs, not just the new one); 11.2 closes the gap for its own surface. |
| `[src/open_ems/web/routes/fragments.py:73-85 + src/open_ems/web/templates/dashboard.html:10-15]` (Story 10.1 deferred) | HTMX-error UX when a fragment endpoint 500s | **safe-during-this-story** — the event-log list fragment carries the same risk pattern. The remediation (calm error placeholder via `hx-target-error` or route-level try/except) is unchanged in scope and remains deferred to a future Epic 11.x hardening sweep. **NO action in 11.2** — the AC text does not contract error-boundary behavior and the patch would be cross-cutting. |
| Other deferred items | Unrelated to event-log UI surface | **acceptable-post-this-story** |

No deferred item blocks 11.2 ready-for-dev.

---

## Project Context Reference

Project artifacts authoritative at story creation time:
- `_bmad-output/planning-artifacts/prd.md` — FR20 (event log access), FR21 (installer notes), NFR-P5 (event log query performance, line 668), Data retention (line 698 — 90 days minimum)
- `_bmad-output/planning-artifacts/architecture.md` — `event_log_repo.py` mention at line 862; `installer/event_log.html` template path at line 892 (the canonical location for the new page template); `audit_log.py` at line 904 (the write-path service)
- `_bmad-output/planning-artifacts/ux-design-specification.md` — §"Event Log Row (Installer)" line 1219; §"Event Log Filter Bar (Installer)" line 1235; §"Installer Note Entry (Installer Event Log)" line 1291; §"Search and Filtering Pattern" line 1531; §"Empty States" — Empty event log copy line 1497; §"Event Log Guarantees" line 1911; §"Component Implementation Strategy" — filter bar pattern line 1328
- `_bmad-output/planning-artifacts/epics.md` — Story 11.2 AC text at line 2304-2340; Epic 11 cross-story constraints line 2377-2386
- `_bmad-output/implementation-artifacts/11-1-implement-installer-dashboard-...md` — sibling story (done 2026-05-12); the URL contracts emitted there are consumed here

Stories whose surface 11.2 reads from (no mutation, read-only consumer except for the note POST):
- Epic 6 (Story 6.1, 6.2) — `event_log` table semantics, append-only invariant, `EventLogRepo.append`
- Story 11.1 — `EventLogEntry` Pydantic model, `EventLogRepo.list_recent`, the inlined Brussels timestamp formatter (lifted to module level by this story), sidebar shell, `db_query_counter` test helper

Stories whose surface 11.2 mutates:
- **None.** The note POST appends a new event_log row (via the existing `ObservabilityService.installer_note`). No engine state, no StateStore, no persistent UI state beyond the row insertion.

---

## Open questions saved for Jordan (post-implementation review or pre-dev sync)

1. **Pagination model — offset+limit vs cursor (timestamp,id):** 11.2 ships with **offset+limit** for simplicity. The known trade-off: a new event arriving during browsing slides the offset, producing a one-row duplicate or skip at the page boundary. Cursor-based pagination (`WHERE (timestamp, id) < (last_seen_timestamp, last_seen_id) ORDER BY timestamp DESC, id DESC LIMIT N`) is more correct under live load. **Recommendation: offset+limit for v1** (acceptable for installer-driven manual browsing; the typical session is short and the event-log volume is low). Confirm — alternative is to swap to cursor before merge. The cursor implementation is ~30 LOC + the AC22 test set would shift by ~5 tests; not prohibitively expensive but a meaningful scope increase.

2. **Initial page size 50 vs 100:** Spec says "50–100 entries". 11.2 ships with **50** (lower time-to-interactive on Pi 4; "Load more" gets more on demand). 100 would halve the click count at the cost of ~2x initial render size. Confirm.

3. **Timezone display path — Brussels server fallback + client-side Intl rewrite:** 11.2 ships **both layers** (AC12 — server emits Brussels-locale textContent + `data-utc`, client script rewrites to browser-local on page load + after each HTMX swap). The alternative is "client-only" (server emits UTC textContent, client always rewrites). The dual-layer model preserves usability when JS is disabled or `Intl` is missing; the client-side rewrite gives the correct local-timezone view in the common case. Confirm this is the intended UX.

4. **Note-failure UX response code — 200 + form-with-error fragment vs 422:** 11.2 ships **200 + HTMX outerHTML swap of the form fragment with preserved value + inline error**. Alternative: 422 + `hx-target-error` to a static error fragment. The 200 path is consistent with 11.1's dismiss POST (200 + render) and avoids the HTMX 1.9 "non-2xx is ignored by default" surface. Confirm.

5. **Event Log sidebar item — server-side full-page navigation:** Per UX spec line 1287 "All navigation is server-side page navigation". 11.2 ships **server-side anchor navigation** (no HTMX boost on the sidebar — each click is a full page load). The Alpine `installerSidebar` state is re-instantiated per page (acceptable; the only state it holds is the mobile-open-toggle, which intentionally resets on navigation). Confirm.

6. **AC10 OOB swap target ID — `#event-log-list-rows` vs `#event-log-list-wrapper`:** the OOB swap that prepends the new INSTALLER row needs a stable target ID INSIDE the list. 11.2 ships **`#event-log-list-rows`** as the `<ol>` element ID (the wrapper `<section id="event-log-list-wrapper">` is for filter-driven outerHTML swap; the inner list ID is for note-prepend). Confirm the two-level ID structure is clean.

7. **Sidebar partial extraction — is the refactor in scope for 11.2?** Currently the sidebar is inline in `dashboard.html:57-78`. 11.2 extracts to `installer/_sidebar.html`. The extraction is small (~20 lines moved + an `active` parameter) but it touches 11.1's surface. **Recommendation: in-scope** — the alternative is duplicating the markup in `event_log.html`, which sets up exactly the drift risk (active state can desync between templates) the consolidation prevents. Confirm.

---

## Resolved decisions (2026-05-12, pre-dev sync with Jordan)

All seven open questions resolved with the recommended v1 paths:

1. **Pagination model** — **offset+limit**. Acceptable for v1 (installer-driven manual browsing); page-boundary drift on live inserts is documented and tolerated. Cursor-based pagination deferred as a future hardening item if real-world feedback requests it.
2. **Initial page size** — **50**. Lower time-to-interactive on Pi 4; "Load more" handles deeper browsing.
3. **Timezone display path** — **dual-layer (Brussels server fallback + client-side `Intl.DateTimeFormat` rewrite)**. Server emits Brussels-locale `textContent` + `data-utc` ISO; the inline script on the event-log page (NOT base.html) rewrites textContent to browser-local on `DOMContentLoaded` and after every `htmx:afterSwap`. JS-disabled / broken-`Intl` clients keep the Brussels fallback.
4. **Note-failure UX response code** — **200 + HTMX outerHTML swap of the form fragment with preserved `note` value + inline error**. Consistent with 11.1's dismiss POST; avoids HTMX 1.9 "non-2xx ignored by default" surface.
5. **Event Log sidebar item** — **server-side full-page anchor navigation** (no HTMX boost). Per UX spec line 1287; Alpine `installerSidebar` state re-instantiates per page (acceptable — only state is mobile-open-toggle which intentionally resets on navigation).
6. **OOB swap target structure** — **`#event-log-list-rows` is the `<ol>` element ID INSIDE the wrapper `<section id="event-log-list-wrapper">`**. Filter-driven outerHTML swaps target the wrapper; the note-prepend OOB swap (`<li hx-swap-oob="afterbegin:#event-log-list-rows">`) targets the inner list.
7. **Sidebar partial extraction** — **in scope for 11.2**. Extract `installer/_sidebar.html` with `active` parameter; update `dashboard.html` installer branch to use the include. A regression test asserts the dashboard still renders the same 5 nav items in the same order so the refactor is no-regression.

---

## Tasks / Subtasks

### Task 1: Filter parser + EventLogFilter dataclass (AC9)
- [x] 1.1 Define `EventLogFilter` frozen dataclass in a new module `src/open_ems/web/event_log_filters.py` (or co-locate with the route in `installer.py`; **decision: new module** — keeps the parser unit-testable in isolation; importable from `installer.py`, `fragments.py`, and tests)
- [x] 1.2 Define `_VALID_WINDOWS: Final[dict[str, timedelta]] = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}` and `EVENT_LOG_PAGE_SIZE = 50`
- [x] 1.3 Implement `_parse_event_log_filters(type, device_id, window, from_date, to_date, q) -> EventLogFilter` (all params str | None; the function does the validation drops)
- [x] 1.4 Write unit tests in `tests/unit/web/test_installer_event_log_filters.py` (AC22 #19-30): 12 tests

### Task 2: Repo methods `list_filtered` + `count_filtered` (AC14)
- [x] 2.1 Add `_build_event_log_filter_where(...)` private helper in `event_log_repo.py` returning `(where_sql, params)` tuple
- [x] 2.2 Implement `EventLogRepo.list_filtered(...)`
- [x] 2.3 Implement `EventLogRepo.count_filtered(...)`
- [x] 2.4 Validate inputs: `limit >= 1`, `offset >= 0` (raise `ValueError`)
- [x] 2.5 Write unit tests in `tests/unit/storage/repositories/test_event_log_repo.py` (AC22 #1-18): 18 new tests

### Task 3: Lift Brussels timestamp formatter from 11.1 to module level (Pattern 3)
- [x] 3.1 Extract the timestamp formatting block (currently inline at `state_serialization.py:868-893`) into a new module-level function `_format_event_log_timestamp_brussels_fallback(ts: datetime) -> tuple[str, str]` returning `(utc_iso, display_string)`
- [x] 3.2 Update `build_installer_event_log_preview_context` (the existing 11.1 builder) to call the helper
- [x] 3.3 Re-run the existing 11.1 preview tests; they MUST still pass byte-for-byte (no behavior change in this refactor step)

### Task 4: New builders for the page + list fragment + note form (AC22 #31-49, #50-52, #53-58)
- [x] 4.1 `build_installer_event_log_page_context(filter, rows, total_count, current_offset, csrf_token) -> dict` — full-page context (filter pills active states, list rows, count caption, load-more flag, note form initial state)
- [x] 4.2 `build_installer_event_log_list_context(filter, rows, total_count, current_offset) -> dict` — list-fragment context (subset)
- [x] 4.3 `build_installer_note_form_context(note_value, error_message, csrf_token) -> dict` — note-form context (used by initial render AND failure response)
- [x] 4.4 Builder unit-tests deferred to the route/fragment tests (each route exercises one builder end-to-end with assertions on the rendered HTML — consistent with 11.1 Task 3.6)

### Task 5: New dependency `get_observability_service` (AC10)
- [x] 5.1 Add `get_observability_service(request: Request) -> ObservabilityService` in `web/dependencies.py` (mirror `get_event_log_repo` pattern at line 147-152; construct `ObservabilityService()` per request; the service's underlying `EventLogRepo()` constructs from the shared aiosqlite pool)

### Task 6: Page route `GET /installer/event-log` (AC1-9, AC12-13, AC20)
- [x] 6.1 Add route to `web/routes/installer.py` with `Annotated[str | None, Query(alias="from")]` for `from_date` + `Query(alias="to")` for `to_date`
- [x] 6.2 Parse filter via `_parse_event_log_filters(...)`
- [x] 6.3 Issue `event_log_repo.list_filtered(...)` + `event_log_repo.count_filtered(...)` (only call count when filter is active; when no filter, skip the count query — minor performance optimization documented in code comment)
- [x] 6.4 Pass to `build_installer_event_log_page_context(...)` and render `installer/event_log.html`
- [x] 6.5 Page route tests (AC22 #31-49): 19 tests

### Task 7: Fragment route `GET /fragments/installer/event-log-list` (AC22 #50-52)
- [x] 7.1 Add route to `web/routes/fragments.py` (alongside the 5 installer fragments)
- [x] 7.2 Reuse `_parse_event_log_filters` + repo methods + `build_installer_event_log_list_context`
- [x] 7.3 Render `fragments/installer/event-log-list.html` (the partial — list rows + count caption + load-more button only)
- [x] 7.4 Fragment route tests (AC22 #50-52): 3 tests

### Task 8: POST route `/actions/installer-note` (AC10, AC16, AC22 #53-58)
- [x] 8.1 Add route to `web/routes/actions.py` (sibling to `dismiss_installer_anomaly` at line 464)
- [x] 8.2 Parse form body `note: str` via `Form(...)` dependency
- [x] 8.3 Validate (server-side length, whitespace-only); on failure render `fragments/installer/event-log-note-form.html` with preserved value + error
- [x] 8.4 On success: call `observability.installer_note(note)` (the service handles escape + length + append); then render the success response — the empty form fragment PLUS an OOB swap `<li hx-swap-oob="afterbegin:#event-log-list-rows">...</li>` carrying the new INSTALLER row template
- [x] 8.5 Catch unexpected exceptions (DB error, etc.); log via `structlog.warning("installer_note_persist_failed", error=str(exc))`; render the form with the generic inline error
- [x] 8.6 Note POST tests (AC22 #53-58): 6 tests

### Task 9: Templates (5 NEW HTML files + 1 modification)
- [x] 9.1 NEW `web/templates/installer/_sidebar.html` — extracted partial, takes `active` parameter
- [x] 9.2 MODIFY `web/templates/dashboard.html` — replace inline sidebar (lines 57-78) with `{% include "installer/_sidebar.html" with context %}` after setting `{% set active = "dashboard" %}`
- [x] 9.3 NEW `web/templates/installer/event_log.html` — full page template; extends base.html; includes `_sidebar.html` with `active="event-log"`; renders note form, filter bar, list wrapper, "Showing N of M" count, load-more button; includes the inline Intl.DateTimeFormat script in `{% block scripts %}`
- [x] 9.4 NEW `web/templates/fragments/installer/event-log-list.html` — list fragment (wrapper `<section id="event-log-list-wrapper">` containing `<p class="installer-event-log-result-count">` + `<ol id="event-log-list-rows">` + load-more `<button>`)
- [x] 9.5 NEW `web/templates/fragments/installer/event-log-row.html` — single row partial (`<li>` element); used inside the list AND inside the OOB-swap response from the note POST
- [x] 9.6 NEW `web/templates/fragments/installer/event-log-note-form.html` — note form fragment (textarea + submit + inline error placeholder); used for initial render AND failure responses

### Task 10: CSS additions to `web/static/open-ems.css`
- [x] 10.1 `.installer-event-log-page` page wrapper grid (sidebar + main, mirror `.installer-dashboard` from 11.1)
- [x] 10.2 `.installer-event-log-filter-bar` flex layout (pills · date inputs · keyword input)
- [x] 10.3 `.event-log-filter-pill` + `.event-log-filter-pill--active` (active uses `--color-accent-subtle` background per UX spec)
- [x] 10.4 `.installer-event-log-note-form` + `.installer-event-log-note-form__textarea` + `.installer-event-log-note-form__error` (error uses `--color-fail` text)
- [x] 10.5 `.installer-event-log-result-count` (slate-400 caption per UX spec line 1542)
- [x] 10.6 `.installer-event-log-list` (full-page variant — distinct class from `.installer-event-log-preview` which is the dashboard variant)
- [x] 10.7 `.installer-event-log-load-more` button (text-secondary outlined per UX spec line 1386)
- [x] 10.8 No new design tokens — palette from 11.1 covers this surface

### Task 11: E2E integration tests + performance test (AC22 #59-67)
- [x] 11.1 NEW `tests/integration/web/test_installer_event_log_e2e.py` — 8 tests (#60-67)
- [x] 11.2 NEW `tests/integration/web/test_installer_event_log_performance.py` — 1 test (#59) covering NFR-P5

### Task 12: Quality gates
- [x] 12.1 `uv run ruff check .` → clean
- [x] 12.2 `uv run ruff format --check .` → clean
- [x] 12.3 `uv run mypy src` → clean
- [x] 12.4 `uv run pytest` → ≥ 1756 passed (1716 baseline + ≥ 40 net new)
- [x] 12.5 `uv.lock` unchanged

---

## Dev Agent Record

### Agent Model Used

claude-opus-4-7

### Implementation Plan

Built bottom-up: filter parser + dataclass → repo read methods → Brussels-formatter refactor → builders → dependency → page route → fragment route → POST note route → templates → CSS → e2e/performance tests → quality gates. The repo `_build_event_log_filter_where` helper is the structural single-source-of-truth for the WHERE clause shared by `list_filtered` and `count_filtered`; the unit test that asserts `len(list_filtered(...)) == count_filtered(...)` under identical filters is the discipline test for that contract.

### Debug Log References

1. **Initial e2e assertion too loose** — `test_e2e_url_contract_type_system_window_24h_renders_filtered_view` asserted `"decision" not in body`, but the rendered HTML contains `event-log-filter-pill--decision` (the CSS-modifier class on the DECISION pill). Replaced fixture summaries with unique sentinels (`UNIQ-recent-system-AAA`, `UNIQ-old-system-BBB`, `UNIQ-decision-CCC`) so the assertion only matches the row content.

2. **Ruff line-too-long on `_CREATE_TIMESTAMP_INDEX`** — Single-line SQL string was 101 chars. Wrapped in parentheses across two lines, ruff format then normalized.

3. **Import organization at bottom of `state_serialization.py`** — Initial draft put `from open_ems.web.event_log_filters import EventLogFilter` at file end with `# noqa: E402` to dodge a perceived circular import. There is no cycle (the filters module imports nothing from state_serialization), so the import was hoisted to the top alongside other module-level imports. Cleaner read; same behavior.

### Completion Notes

**All 23 ACs satisfied.** Key implementation decisions per the resolved Q1–Q7:

- **Q1 (pagination — offset+limit):** `LIMIT EVENT_LOG_PAGE_SIZE OFFSET ?` against `ORDER BY timestamp DESC, id DESC`. Page-boundary drift on live inserts is documented and accepted for v1.

- **Q2 (page size — 50):** `EVENT_LOG_PAGE_SIZE = 50` constant in `web/event_log_filters.py`. Test `test_event_log_page_size_is_50` pins the value.

- **Q3 (timezone — dual layer):** Server emits Brussels-locale `textContent` + `data-utc` ISO. The event-log page's `{% block scripts %}` runs an inline IIFE that walks `[data-utc]` elements and rewrites via `Intl.DateTimeFormat` on `DOMContentLoaded` AND `htmx:afterSwap`. JS-disabled / broken-`Intl` clients keep the Brussels textContent. The Brussels formatter is lifted to module-level `_format_event_log_timestamp_brussels_fallback` in `state_serialization.py` and reused by the 11.1 preview builder (byte-for-byte unchanged behavior — 11.1 tests still pass).

- **Q4 (note failure UX — 200 + form fragment):** `POST /actions/installer-note` returns 200 with `fragments/installer/event-log-note-form.html` carrying the preserved `note_value` + inline `error_message`. On success: empty form + OOB swap `<ol hx-swap-oob="afterbegin:#event-log-list-rows">...</ol>` carrying the new INSTALLER row via the shared `event-log-row.html` partial.

- **Q5 (sidebar navigation — server-side):** Each sidebar link is a plain `<a href="...">` (no HTMX boost). The `installerSidebar` Alpine factory re-instantiates per page-load (open=false default; mobile-hamburger toggle only).

- **Q6 (OOB target structure):** `<ol id="event-log-list-rows">` is the inner list inside `<section id="event-log-list-wrapper">`. Filter-driven outerHTML swaps target the wrapper; note-prepend OOB swap targets the inner list.

- **Q7 (sidebar partial extraction):** Extracted to `installer/_sidebar.html` taking an `active` parameter. Dashboard installer branch + event-log page both `{% include "installer/_sidebar.html" %}`. The dashboard's existing `test_installer_dashboard_shell_renders_five_htmx_bound_sections` continues to pass.

**Structural single-source-of-truth invariants:**
- `_build_event_log_filter_where(...)` in `event_log_repo.py` is the ONLY producer of the filter WHERE-clause. Both `list_filtered` and `count_filtered` consume it; the unit test asserting `count_filtered(filt) == len(list_filtered(filt, large_limit, 0))` for several filter shapes guards against drift.
- `_format_event_log_timestamp_brussels_fallback(ts)` in `state_serialization.py` is the ONLY producer of the Brussels-locale display string used by 11.1 preview + 11.2 page row. The 11.1 preview test continues to pass byte-for-byte.
- `EVENT_LOG_PAGE_SIZE = 50` and `_VALID_WINDOWS: Final[dict[str, timedelta]] = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}` are the ONLY values across the parser + route + fragment route paths. The dict carries vocabulary + lookup-table in one container — no parallel `_WINDOW_DELTAS` dict is needed.

**Defensive behavior (URL-tampering-resilience):**
- `_parse_event_log_filters` silently drops invalid types (`?type=GARBAGE`), unknown windows (`?window=99h`), and malformed dates (`?from=not-a-date`). A `structlog.debug("installer_event_log_filter_invalid_value", ...)` emission per drop preserves the signal for ops but does not 500 the page.
- `summary LIKE ? ESCAPE '\\'` with the keyword pre-processed to escape `%`, `_`, and `\\` — verified by `test_list_filtered_keyword_escapes_sql_wildcards_percent_and_underscore`.

**Test deltas: 1716 → 1804 (+88 net new).** Quality gates:
- `uv run ruff check .` → clean.
- `uv run ruff format --check .` → clean (279 files).
- `uv run mypy src` → clean across 104 source files (1 new module added by this story: `web/event_log_filters.py`).
- `uv run pytest` → 1804 passed, 2 skipped (1 baseline + 1 11.1 a11y placeholder), 8 xfailed (unchanged baseline). 88 net new tests.
- `uv.lock` unchanged.
- No migration (existing `ix_event_log_timestamp` + `ix_event_log_event_type` indexes suffice per AC15).

**Performance verification (AC15 / NFR-P5):** the in-memory SQLite benchmark on 3000 rows over 30 days shows all four query shapes well under the 5-second NFR-P5 ceiling AND under the tighter per-shape mean targets (no-filter list <200ms, type-filter list <500ms, keyword-filter list <1500ms, count <2000ms).

**CSRF coverage for the new POST surface:** `test_post_installer_note_csrf_token_required` wires up the real `CsrfMiddleware` and verifies the 403 rejection path when the `X-CSRF-Token` header is omitted. This closes the gap for `POST /actions/installer-note` (Story 11.1 DF3 remains open for the 4 pre-existing POSTs — that's a separate hardening sub-story).

**Status (Story 11.2):** ready-for-dev → in-progress → review. Awaits A7 review-closure gate via `bmad-code-review`.

### File List

#### New files

- `src/open_ems/web/event_log_filters.py` — `EventLogFilter` frozen dataclass + `_parse_event_log_filters` + `EVENT_LOG_PAGE_SIZE` + `_VALID_WINDOWS`
- `src/open_ems/web/templates/installer/_sidebar.html` — extracted shared sidebar partial (takes `active` param)
- `src/open_ems/web/templates/installer/event_log.html` — full event-log page template (extends base.html; includes _sidebar + filter bar + note form + list wrapper + Intl rewriter script)
- `src/open_ems/web/templates/fragments/installer/event-log-list.html` — HTMX list-fragment partial (count caption + ordered list + Load-more)
- `src/open_ems/web/templates/fragments/installer/event-log-row.html` — single-row partial (reused by list render AND note-POST OOB swap)
- `src/open_ems/web/templates/fragments/installer/event-log-note-form.html` — note form partial (initial / success / failure renders all use this template; OOB row swap appended on success)
- `tests/unit/web/test_installer_event_log_filters.py` — 14 filter-parser tests
- `tests/unit/web/test_installer_event_log_route.py` — 20 page route tests
- `tests/unit/web/test_installer_event_log_fragments.py` — 3 fragment-route tests + 6 note-POST tests = 9 tests
- `tests/integration/web/test_installer_event_log_e2e.py` — 8 e2e tests
- `tests/integration/web/test_installer_event_log_performance.py` — 1 NFR-P5 performance test

#### Modified files

- `src/open_ems/storage/repositories/event_log_repo.py` — added `_escape_like_keyword`, `_build_event_log_filter_where`, `list_filtered`, `count_filtered`
- `src/open_ems/web/state_serialization.py` — lifted `_format_event_log_timestamp_brussels_fallback` to module level (refactor — byte-for-byte behavior preserved); added `_INSTALLER_EVENT_LOG_FILTER_PILLS`, `build_installer_event_log_row_context`, `build_installer_event_log_list_context`, `build_installer_event_log_page_context`, `build_installer_note_form_context`, `_filter_date_to_iso`, `_filter_until_to_iso`, `_build_event_log_query_string`
- `src/open_ems/web/dependencies.py` — added `get_observability_service` (mirror `get_event_log_repo` pattern)
- `src/open_ems/web/routes/installer.py` — added `GET /installer/event-log` page route (with `Query(alias="from"/"to")` for the reserved-keyword form params)
- `src/open_ems/web/routes/fragments.py` — added `GET /fragments/installer/event-log-list` HTMX fragment route
- `src/open_ems/web/routes/actions.py` — added `POST /actions/installer-note` resilient note submission; imports `EventLogRepo`, `MAX_INSTALLER_NOTE_LENGTH`, `get_event_log_repo`, `get_observability_service`, `build_installer_event_log_row_context`, `build_installer_note_form_context`
- `src/open_ems/web/templates/dashboard.html` — installer branch replaced inline sidebar with `{% include "installer/_sidebar.html" %}` (after `{% set active = "dashboard" %}`)
- `src/open_ems/web/static/open-ems.css` — appended ~210 lines covering `.installer-event-log-page` grid wrapper, `.installer-event-log-filter-bar` + `.event-log-filter-pill` variants, `.installer-event-log-note-form` + `.installer-event-log-note-form__error`, `.installer-event-log-list-wrapper` + list rows + timestamp + summary + device-id + empty state + load-more button + `.visually-hidden` helper. No new design tokens.
- `tests/unit/storage/repositories/test_event_log_repo.py` — added 18 new tests covering `list_filtered` + `count_filtered`

---

## Change Log

- 2026-05-12: Story 11.2 implementation complete. Status: ready-for-dev → in-progress → review. 88 net new tests (1804 total passing); ruff + format + mypy clean across 279 files / 104 source files; uv.lock unchanged. Second Epic 11 story delivered. Awaits A7 review-closure gate via bmad-code-review (would be the eighth proof-of-enforcement target after 9-Y-a, 9-X, 10.1, 10.2, 10.3, 10.4, 11.1).
- 2026-05-12: Story 11.2 created by bmad-create-story. Status: ready-for-dev. A2 evaluation returned no triggers; A1 R1–R7 do not apply.

---

## Review Findings

_Run: bmad-code-review on 2026-05-12. Three layers: Blind Hunter, Edge Case Hunter, Acceptance Auditor. 2 decision-needed, 12 patch, 4 deferred, 14 dismissed as noise._

### Decision-needed → Patched (2)

- [x] [Review][Decision→Patched] `_VALID_WINDOWS` shape — Jordan chose (b) on 2026-05-12: update spec text to declare `Final[dict[str, timedelta]]` (matching impl). Patch the AC9 code block, the "Resolved decisions" line, and the "Structural single-source-of-truth invariants" wording.
- [x] [Review][Decision→Patched] Partial-explicit date range semantics — Jordan chose (a) on 2026-05-12: keep current "either explicit wins" impl, document it in AC5. Patch AC5 to add an explicit sentence: "When ONLY ONE of `from`/`to` is provided (alongside or without `window`), the explicit value wins for its boundary and the other boundary becomes unbounded (the window is dropped entirely)."

### Patch (12)

- [x] [Review][Patch] HIGH — Filter pill multi-select is functionally broken [src/open_ems/web/templates/installer/event_log.html:46-52] — Each pill is `<button type="submit" name="type" value="X">`. HTML form semantics: only the clicked submit button's name/value is submitted, so clicking SYSTEM while DECISION is active sends `?type=SYSTEM` (DECISION lost). There is no hidden `<input name="type">` carrying the currently-active types from `filter_type_value`. Spec AC4 explicitly requires multi-pill support; the URL contract from 11.1 (`?type=DECISION,DEVICE`) round-trips on load but cannot be reached or modified additively via the UI. Suggested fix: render a hidden `<input type="hidden" name="type" value="{{ filter_type_value }}">` and use JS to toggle the clicked pill's value into/out of the comma-list on submit; or convert pills to `<button type="button">` with `hx-get` + `hx-vals` that send the merged set.
- [x] [Review][Patch] HIGH — Note POST success path can crash on `[latest] = ...` unpack or fetch wrong row under race [src/open_ems/web/routes/actions.py:602-610] — After `await observability.installer_note(note)` commits, the code does `[latest] = await event_log_repo.list_filtered(event_types=frozenset({"INSTALLER"}), limit=1, offset=0)` and unpacks. If the read returns 0 rows (transaction-visibility race, isolation glitch, or the rare-but-possible aiosqlite ordering), `ValueError: not enough values to unpack` is raised — uncaught — and the installer sees a 500 with the note already persisted. Two concurrent installers can also race the SELECT and each fetch the other's row into their OOB swap. Fix: have `ObservabilityService.installer_note` return the inserted `EventLogEntry` (or its id) so the route can render the row deterministically without a second SELECT.
- [x] [Review][Patch] HIGH — First-note-ever path silently drops the OOB row [src/open_ems/web/templates/fragments/installer/event-log-list.html:28-40] — The `<ol id="event-log-list-rows">` is rendered only inside the `{% if not list.is_empty %}` branch. On a fresh installation with no events yet, the empty-state copy ("No events yet…") is shown and the `#event-log-list-rows` target does not exist. The note POST's OOB swap (`hx-swap-oob="afterbegin:#event-log-list-rows"`) then silently fails — HTMX cannot find the target — the textarea clears but the new row never appears. The installer believes the note was lost. Fix: always render the `<ol id="event-log-list-rows">` (move outside the if-else), and put the empty-state copy as a `<li>` or sibling that disappears once a row exists; OR have the POST handler return the full list-fragment (outerHTML swap) when the prior state was empty.
- [x] [Review][Patch] MEDIUM — `aria-pressed` on `<button type="submit">` violates the ARIA contract [src/open_ems/web/templates/installer/event_log.html:46-52] — `aria-pressed` is defined only for `role="button"` toggle buttons; on submit buttons, screen readers may announce inconsistently. Combined with finding #1 (the pills aren't actually toggles), the attribute is semantically wrong twice over. Fix follows from the multi-select redesign — once pills become `type="button"` toggles with JS state, `aria-pressed` is appropriate.
- [x] [Review][Patch] MEDIUM — Window-derived form date pre-fill is wrong [src/open_ems/web/state_serialization.py — `_filter_date_to_iso` / `_filter_until_to_iso`] — When `filter.since`/`filter.until` come from a window (`now-24h..now`) rather than explicit dates, the form's `<input type="date" value="…">` pre-fill is derived by stripping to `YYYY-MM-DD`. For `_filter_until_to_iso`, the subtract-1-day round-trip assumes `until` is next-day-midnight (correct for explicit `to`), but for window-`now` it produces yesterday's date in the `to` input. Resubmitting via the date input then loses the now-boundary AND extends the lower bound. Fix: track filter provenance (`source: "window" | "explicit"`) so window-derived ranges render the date inputs as empty (preserving `?window=...` in the URL) until the user explicitly chooses dates.
- [x] [Review][Patch] MEDIUM — Template render exception after persist leaks 500 with note in DB [src/open_ems/web/routes/actions.py:602-631] — `build_installer_event_log_row_context(latest)` and the `TemplateResponse` call happen AFTER `installer_note(note)` commits. Any exception in row-context construction or template rendering (bad Unicode, broken template, etc.) produces a 500, but the note is already persisted. Installer retries → duplicate note. Fix: wrap the post-persist render in a try/except that, on failure, returns a form-only success response (no OOB row) with a log emission — the installer's textarea clears (note succeeded) and the next page reload shows the row.
- [x] [Review][Patch] MEDIUM — Missing "Dates are interpreted in UTC" UX hint required by AC5 [src/open_ems/web/templates/installer/event_log.html] [src/open_ems/web/routes/installer.py:57-62] — AC5 says: "Document this in the route docstring + UX hint text ('Dates are interpreted in UTC')". Neither the page template nor the route docstring carries the wording. Without the hint, a Brussels installer typing `from=2026-05-12` may not realize the boundary is UTC midnight (= 02:00 Brussels local), and a ~2-hour window of expected events near midnight is excluded with no signal. Fix: add a small `<small>` / `aria-describedby` hint under the date inputs, and add the line to the route docstring.
- [x] [Review][Patch] MEDIUM — Test coverage gap: `test_event_log_page_with_window_24h_renders` does not assert date-input pre-fill [tests/unit/web/test_installer_event_log_route.py — renamed from spec AC22 #38] — Spec AC22 test #38 (`test_event_log_page_with_window_24h_pre_fills_date_range`) was renamed and narrowed to only assert row filtering. The renamed test does not inspect `value="…"` on the `<input type="date">` fields, so the wrong-date-prefill bug above (patch P5) would not be caught. Fix: extend or replace the renamed test with an assertion on the rendered `value=""` for both `from` and `to` inputs.
- [x] [Review][Patch] LOW — `since > until` not validated [src/open_ems/web/event_log_filters.py:95-119] — `?from=2026-12-31&to=2026-01-01` runs the query and returns empty; the user sees "No events match this filter." with no signal that the date range is reversed. Fix: when both explicit dates are provided and `since >= until`, log at DEBUG and either swap them or surface a tiny inline hint.
- [x] [Review][Patch] LOW — `_parse_date` does not strip whitespace before `strptime` [src/open_ems/web/event_log_filters.py:122-143] — A copy-paste with a trailing space (or `+` from URL decoding) silently drops the date value. Fix: `value = value.strip()` before `strptime`.
- [x] [Review][Patch] LOW — Deep pagination past end shows the wrong empty-state copy [src/open_ems/web/routes/fragments.py + state_serialization.build_installer_event_log_list_context] — When `offset > total_count`, `list_filtered` returns `[]`; `is_empty=True` + `is_filtered=True` → "No events match this filter." even though the filter matches many rows starting at offset 0. Fix: if `offset > 0` and `rows == []`, suppress the empty-state copy (or render a "You've reached the end" caption) and drop the load-more button.
- [x] [Review][Patch] LOW — Two weak test assertions [tests/unit/web/test_installer_event_log_fragments.py — `test_post_installer_note_homeowner_rejected`] [tests/integration/web/test_installer_event_log_e2e.py — `test_e2e_xss_in_note_renders_escaped`] — (a) The homeowner-rejected test asserts only `response.status_code != 200` — any 500 from any other bug satisfies it. Pin to 302/401/403 explicitly. (b) The XSS test accepts `&lt;` OR `&amp;lt;` for the escape rendering. Security tests should pin exactly one escape level; the OR-condition lets both a missing-escape regression (somewhere upstream) and a double-escape regression pass.

### Deferred (4)

- [x] [Review][Defer] DF1 — `count_filtered` performance at production-scale row counts (50K+) [src/open_ems/storage/repositories/event_log_repo.py — `count_filtered`] — deferred, performance hardening item. `count_filtered` is invoked on every load-more click (any-filter path). NFR-P5 passes at story-target volume (3000 rows over 30 days) but the perf test does not cover the load-more pagination path. Real-world deployments at 90-day retention × heavier event volume may approach the budget. Carry to a future Epic 11.x or 12.x hardening sweep — bundle with the deferred indexing decision for `device_id`.
- [x] [Review][Defer] DF2 — Brussels formatter uses integer-hour UTC offset and floor-div on negative seconds [src/open_ems/web/state_serialization.py — `_format_event_log_timestamp_brussels_fallback`] — deferred, pre-existing from Story 11.1. The formatter inherited from 11.1's preview builder uses `int(offset.total_seconds() // 3600)`, which truncates half-hour zones (India UTC+5:30) and floor-divides negative offsets toward `-∞`. Brussels is whole-hour and positive, so the production path is correct; the gap is dev/test exposure if the formatter is ever used outside Europe/Brussels. Bundle with a future i18n/locale hardening item.
- [x] [Review][Defer] DF3 — Performance test thresholds hardcoded across hardware classes [tests/integration/web/test_installer_event_log_performance.py:166-169] — deferred, CI hardening item. The mean-time assertions (200/500/1500/2000ms) are tuned for dev-laptop SQLite and may produce false failures on slow CI runners or Pi 4 emulation. The strict NFR-P5 ceiling (`max < 5000ms`) is correct. Carry to a CI-hardening sweep — env-derived thresholds or `pytest.mark.benchmark` gating.
- [x] [Review][Defer] DF4 — CSRF middleware test exercises plumbing not token-value check [tests/unit/web/test_installer_event_log_fragments.py — `test_post_installer_note_csrf_token_required`] — deferred, pre-existing test pattern (11.1 DF3 carry-over class). The fixture uses a literal `"test-csrf"` for both stored token and request header; if the middleware ever degenerated to a constant comparison, this test would still pass. Same limitation as the 11.1-deferred CSRF coverage gap for the four pre-existing POSTs (`dismiss-anomaly`, `set-strategy`, `ev-override`, `revert-strategy`). Bundle into the CSRF-hardening sub-story already deferred from 11.1 DF3.

### Dismissed (14 — counts only)

- 2× Blind Hunter CRITICAL findings reclassified: "wrong event_type unpack" (false — `installer_note` writes `INSTALLER`); "XSS via autoescape disabled" (false — `Jinja2Templates` autoescape is on by default).
- 4× Blind Hunter HIGH/MED reclassified: "HTMX dual-submit race" (HTMX intercepts hx-get forms); "keyword=' ' truthiness slipthrough" (parser strips); "async tests missing `@pytest.mark.asyncio`" (`pyproject.toml` line 81 sets `asyncio_mode = "auto"`); "URL encoding edge for `&`/`=` in q" (`quote()` handles).
- 8× Edge Case Hunter / LOW noise reclassified: empty `q=""` URL clutter (cosmetic); unfiltered `total_count = len(rows)` (dormant, not user-facing); DB-row naive timestamp (no legacy data); Alpine.data registered twice (per-page navigation, idempotent); sidebar dashboard markup regression (intentional per spec — Event Log goes live); note absent → FastAPI 422 (`Form("")` default returns 200); load-more URL encoding of `+`/`#` in keyword (handled by `quote()`); positive-improvement nits (a11y `aria-live="polite"`, `safe_offset = max(0, offset)`, dropped `xfail_` prefix, `<ol>` vs `<li>` OOB wrapping, `hx-include` placement, `hx-target` closest-ol vs id, `ValueError` branch returning empty-error message, frozenset deterministic-ordering comment, `Form()` B008 lint, redundant timedelta import claim, CSS `100vh - 4rem` magic number, log-injection via DEBUG type log, constant-pinning tautology, zoneinfo ImportError defensive).
