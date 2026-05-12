# Story 11.1: Implement installer dashboard with health indicator, per-device rows, peak tracker, and deterministic anomaly notice

Status: review
_Status: ready-for-dev → in-progress → review by bmad-dev-story on 2026-05-12._
_Status: ready-for-dev set by bmad-create-story on 2026-05-12._

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

> **Type:** Epic 11 opener — installer **operational monitoring** surface. Read-only against the StateStore + `peak_intervals` table + `event_log` table. **No degradation logic** lives in this story (that runs in Epic 7/8); 11.1 only renders.
> **A2-triggered:** **Yes — single trigger match on T1 (lifecycle / state-machine behavior).** The anomaly notice carries a deterministic dismiss/elevation state machine: a dismissed notice MUST re-appear automatically when severity increases (WARN → FAIL) OR when a new anomaly type appears that was not present in the dismissed signature. This is not optional UI polish — it is a structural invariant called out in the epic AC text ("Dismissed anomaly notices reappear on severity increase or new anomaly type appearing") and in the UX spec §"Anomaly Notice (Installer Dashboard)" (line 1250) plus Journey Flow 5 (line 920). The state machine has explicit transitions, signature comparison invariants, and per-render side effects. R1–R7 rendered below per the A1 enforcement contract.
> **Prerequisite (soft):** Epic 10 complete (10.1–10.4 all done 2026-05-12). Epic 11 consumes the dashboard infrastructure established by 10.1 (CSS bundle + base.html static mount + HTMX polling pattern), the `peak_intervals` table from 9.0d, the `event_log` from Epic 6, and the StateStore.component_states / operating_mode contract from Epic 5 + 7.
> **Sibling stories:** 11.2 (event log UI with pagination, filtering, search, note entry — consumes the "View in event log" pre-filter links defined here); 11.3 (homeowner credential management). Both are downstream of 11.1's URL contracts but do NOT block 11.1 delivery.

## Story

As an installer,
I want a monitoring dashboard showing overall system health, per-device connectivity, peak consumption against the configured limit, and a single deterministic anomaly notice when action is needed,
so that I can assess site health in a single view without noise, without false alarms, and without navigating multiple pages.

## Acceptance Criteria

**Given** the installer is authenticated and `StateStore` carries a current `SystemSnapshot` (or the cold-start default snapshot before the first ControlLoop tick), and `peak_intervals` / `event_log` tables exist (post-migration `0001`),

**When** this story is implemented,

**Then** the following acceptance criteria must hold:

---

### Layout, render path, and performance

1. **AC1 — Installer dashboard shell.** `GET /installer/dashboard` already exists at `src/open_ems/web/routes/installer.py:17-30` and currently renders `dashboard.html` with `dashboard_role="installer"`. Today the `else` branch in `dashboard.html` shows: `<p>Full dashboard UI implemented in Epic 11.</p>` (see `dashboard.html:56-58`). This story **replaces** that placeholder with the real installer shell. The shell renders **synchronously** on first GET (no client-side bootstrap dance — the page is usable when HTMX is disabled, just without live updates) and contains five HTMX-bound `<section>` slots that refresh on a load + every-10s cadence (mirroring the homeowner `dashboard-stack` pattern at `dashboard.html:9-40`):

   1. `#installer-anomaly-notice` — anomaly notice (see AC7/AC8); ABOVE the stat cards per UX spec line 1256 + epic line 2290 (`"appears inline above the stat cards"`). May render empty (no anomaly active or notice dismissed).
   2. `#installer-health-indicator` — system health indicator (AC3)
   3. `#installer-device-rows` — per-device rows (AC4)
   4. `#installer-peak-tracker` — peak tracker (AC5)
   5. `#installer-event-log-preview` — recent event log preview (AC6)

   Each section: `hx-get="/fragments/installer/<name>"`, `hx-trigger="load, every 10s"`, `hx-swap="outerHTML"`. Use the EXACT same HTMX attribute spelling as the homeowner shell so existing `htmx:configRequest` CSRF wiring carries over without changes.

   The installer shell sits in a new top-level wrapper `<section class="installer-dashboard">` (NOT `dashboard-stack` — that class is the homeowner pattern). The wrapper is the structural anchor for AC10 #2 (DB-query-count assertion at dashboard render time).

   **The route signature does NOT change:** `installer_dashboard(request, _user=Depends(require_installer))` continues to return `dashboard.html` with `dashboard_role="installer"`. The Jinja `{% if dashboard_role == "installer" %}` branch in `dashboard.html` (currently empty placeholder per `dashboard.html:56-58`) is filled in. **No new top-level route.**

2. **AC2 — Sidebar navigation.** The installer shell includes the fixed-width sidebar specified by UX spec §"Sidebar Navigation (Installer)" (line 1280-1289). In v1, only the **Dashboard** item is wired (active). The remaining items — Devices, Event Log, Settings, Setup — are rendered as **disabled/placeholder** anchors with `aria-disabled="true"` and `tabindex="-1"`, linking to `#` (no navigation) since:
   - Event Log → Story 11.2 (next)
   - Devices/Settings → future Epic 11.x or Epic 12
   - Setup → already exists at `/installer/setup/...` (Epic 9). Wire this one LIVE.

   Sidebar anatomy per UX spec: 220px fixed width on desktop, collapses to hamburger on ≤640px (Alpine.js `x-show` toggle — reuse the existing Alpine bundle from base.html). Site name at top reads from `Settings.project_name` if available, else literal `"OPEN-EMS"`. Active item gets `4px accent left-border + --color-accent-subtle background` per UX spec line 1286 — CSS additions land in `open-ems.css`.

   **Mobile collapse pattern:** `x-data="installerSidebar()"` factory with `open: boolean`. Hamburger button is the toggle. Default state `open=false` on mobile; `open=true` on desktop via a CSS-only media-query reset that ignores Alpine's `x-show` until the viewport is mobile-sized. The factory lives in `dashboard.html`'s `{% block scripts %}` alongside existing factories.

3. **AC11 — Performance: 2-second render budget.** GET `/installer/dashboard` renders in under 2 seconds under normal load (epic AC line 2268). Mirror Story 10.1 AC16's enforcement pattern: a 10-iteration timing test with mean < 2000ms (generous ceiling; the route is template + four DB queries at most). The shell itself is template-only — DB I/O happens in the fragment endpoints, which each have their own performance budget (AC3 / AC4 / AC5 / AC6 all derive from a single short query each).

---

### Section semantics

4. **AC3 — System health indicator (direct mapping, no interpretation layer).** New fragment endpoint `GET /fragments/installer/health-indicator` returns a `<section id="installer-health-indicator">` rendered from a single `StateStore.get_snapshot()` read. The displayed value is derived **exactly** from `SystemSnapshot.operating_mode` via the table:

   | `SystemOperatingMode` | Display |
   |---|---|
   | `normal` | `NORMAL` |
   | `degraded` | `DEGRADED` |
   | `conservative` | `DEGRADED` |
   | `fail_safe` | `FAILED` |

   This mapping is identical to the existing `OPERATING_MODE_GLOBAL_STATE` table at `src/open_ems/core/state.py:80-87` (which maps to `GlobalState` enum values `NORMAL` / `DEGRADED` / `FAILED` — already the contract). The fragment context reads `snapshot.global_state.value` directly; **no second derivation layer** is introduced. Structural enforcement test asserts that the four operating modes produce exactly three distinct display strings (`{normal} → NORMAL`, `{degraded, conservative} → DEGRADED`, `{fail_safe} → FAILED`) — an exhaustive parametrize over `SystemOperatingMode` ensures no future enum member ships without a display mapping.

   The displayed value is wrapped in a semantic element (`<span class="health-indicator__badge health-indicator__badge--{status}">`) so styling can branch on the three-way value without the route re-deriving anything. CSS classes: `--normal`, `--degraded`, `--failed`.

   **Aria-live:** `aria-live="polite"` on the badge so screen readers announce health transitions. **Aria-label:** `"System health: {NORMAL|DEGRADED|FAILED}"` on the section root for non-visual users.

5. **AC4 — Per-device rows (component state and capability badge as visually distinct elements).** New fragment endpoint `GET /fragments/installer/device-rows`. Returns a `<section id="installer-device-rows">` containing one `<div class="device-row">` per **role** in the canonical order `(grid_meter, inverter, battery, ev_charger)` (matching `ALL_DEVICE_ROLES` at `src/open_ems/core/state.py:35-40` order, but with `grid_meter` and `inverter` displayed FIRST because they are the required roles per `REQUIRED_DEVICE_ROLES`).

   Each row renders TWO visually distinct elements (epic AC line 2276-2279 — **never conflated**):

   - **Component state** (`ACTIVE` / `DEGRADED` / `ERROR` / `UNAVAILABLE`): derived directly from `snapshot.component_states[role]` (a `ComponentState` enum at `src/open_ems/core/state.py:52-60`). Mapping:
     - `ComponentState.active` → `ACTIVE`
     - `ComponentState.degraded` (does not currently exist — see note) → `DEGRADED`
     - `ComponentState.error` → `ERROR`
     - `ComponentState.unavailable` → `UNAVAILABLE`
     - `ComponentState.stale` → `UNAVAILABLE` (stale data treats as unavailable for the installer surface — same observability semantic, different rendering nuance handled below)
     - `ComponentState.pending` / `idle` → `UNAVAILABLE` (never observed in practice; mapped defensively)

     **Note (CONFIRM-BEFORE-IMPLEMENT):** the current `ComponentState` enum at `state.py:52-60` does NOT contain a `degraded` member — it has `idle / pending / active / error / unavailable / stale`. Either (a) `error` is the installer-facing `DEGRADED` equivalent (most likely, given `derive_component_state` at `state.py:96-104` returns `ComponentState.error` for `DegradedDeviceState` with any reason except `"unavailable"`), OR (b) a new `degraded` member is needed. **AC4 task #1 is a 30-minute investigation:** confirm whether the epic AC's "DEGRADED" component-state label maps to `ComponentState.error` (display rename only) or requires adding a new enum member. The story file's R6 (source-of-truth ownership) is gated on this answer.

   - **Capability badge** (`FULL` / `REDUCED` / `UNSUPPORTED` / `UNKNOWN`): derived from `DeviceRegistryEntry.last_capability_status` (a `CapabilityStatusLiteral` at `src/open_ems/storage/repositories/device_repo.py:33`, values `"full" | "reduced" | "unsupported"`). For rows where no `DeviceRegistryEntry` exists with the role assigned (e.g., the installer wizard has not been completed), the badge reads `UNKNOWN`. The mapping is:
     - `"full"` → `FULL`
     - `"reduced"` → `REDUCED`
     - `"unsupported"` → `UNSUPPORTED`
     - row not found OR `last_capability_status is None` → `UNKNOWN`

   **Rendering invariants (the AC's "never conflated" structural rule):**
   - Component state and capability badge MUST be rendered as **two separate DOM elements** with **distinct CSS classes** (`device-row__component-state` and `device-row__capability-badge` — never the same element). A structural test asserts this: parse the rendered HTML and verify each device row contains BOTH `device-row__component-state` AND `device-row__capability-badge` as distinct CSS classes on distinct DOM nodes.
   - The two values are **independently combinable**: a REDUCED device may be ACTIVE; a DEGRADED-error device may be FULL (epic AC line 2278-2279 — explicit decoupling).
   - **No conditional CSS class merges them** (no `class="state-{component}--cap-{capability}"`). Each element styles independently.
   - **Distinct positions:** the badges live in different `display: flex` columns within `.device-row`. The component state is on the LEFT (after the role label), the capability badge on the RIGHT. The structural test asserts CSS-class presence (the visual position is a CSS concern but the test catches the "same element" regression).

   **Data flow for AC4:**
   - `StateStore.get_snapshot()` → component_states[role] (no DB)
   - `DeviceRepo.list_all()` → all registered devices, filter by `role`, take the first (single-device-per-role contract from Epic 9). DB read.

   **`data_age_seconds[role]`** is rendered as a small caption under the component state (e.g. `"Updated 4s ago"` for `data_age_seconds < 60`, `"Updated 2 min ago"` for `≥ 60`, `"Updated long ago"` for `None`). Reuse the homeowner card's `_stale_caption` helper at `state_serialization.py:506-513` — or, if signature differs, write a sibling helper `_device_row_age_caption(age_seconds: int | None)`.

   Each device row carries a `"View in event log"` link with `?device_id=<id>` query param (URL contract for Story 11.2 — the linked view will pre-filter; until then the link is rendered but lands on the placeholder event-log page). The `device_id` is the `device_registry.device_id` of the row's registered device; for `UNKNOWN`-capability rows (no registry entry), the link is OMITTED.

6. **AC5 — Peak tracker.** New fragment endpoint `GET /fragments/installer/peak-tracker`. Returns a `<section id="installer-peak-tracker">` with two displayed values:

   - **Current-month highest 15-minute interval** in kW. Read via `EnergyRepo.get_current_monthly_peak_kw()` (already exists at `src/open_ems/storage/repositories/energy_repo.py:41-51`). Returns `0.0` if no rows exist yet for the current UTC month.
   - **Configured peak limit** in kW. Read via the `ActiveConstraintsProvider` (the `get_active_constraints_optional` dependency from `dependencies.py:113-126`); display the `peak_limit_kw` field. If constraints are unavailable (pre-installer-wizard-complete), display "Not configured".

   The relationship is rendered as **plain language** per epic AC line 2282 ("highest recorded 15-minute interval this month (from `peak_intervals` table) vs. configured peak limit, in kW with a plain-language label"). UX spec line 5l-657 + Linear reference (line 199, 243) call for "structured, compact, professional" copy — examples:
   - `"Peak this month: 12.4 kW · Limit: 25.0 kW"` (under limit)
   - `"Peak this month: 24.8 kW · Limit: 25.0 kW (approaching)"` (within 10% of limit)
   - `"Peak this month: 28.1 kW · Limit: 25.0 kW (exceeded)"` (over limit — also drives the AC7 anomaly)
   - `"Peak this month: 0.0 kW · No data yet"` (no peak_intervals row in current month)
   - `"Peak this month: 12.4 kW · Limit: not configured"` (constraints unavailable)

   **The "approaching" threshold is 90% of the limit** (i.e., `peak_kw >= 0.9 * limit_kw`). This is identical to the anomaly trigger in AC7 — single source of truth via a module-level constant `_PEAK_APPROACH_RATIO = 0.9` in `state_serialization.py`. **Update**: same constant used by anomaly detection.

   `<dt>` / `<dd>` semantic pairs render the two values. Numbers are rounded to one decimal (`f"{kw:.1f}"`). Monospace styling on the numeric values per UX spec line 523 ("Monospace is used in the installer view only: event log timestamps, peak tracker").

   **DB I/O for AC5:** one indexed SELECT on `peak_intervals` (the COALESCE-MAX query at `energy_repo.py:43-51`) + one read on `ActiveConstraintsProvider` (in-memory after Story 9.0b hydration). Both sub-millisecond on Pi 4.

7. **AC6 — Recent event log preview (5 entries with "View all" link).** New fragment endpoint `GET /fragments/installer/event-log-preview`. Returns a `<section id="installer-event-log-preview">` with the **5 most recent** `event_log` rows ordered by `timestamp DESC`. Each row renders:

   - **Timestamp** in the browser's local timezone with UTC offset shown. **Critical contract from AC6 + epic line 2384:** server emits the UTC ISO-8601 string; the client converts to local. v1 implementation: the server emits both forms — UTC ISO-8601 in `data-utc` attribute, and a server-side formatted local-timezone fallback (using `Europe/Brussels` as the v1 default — call out in dev notes that this is a v1 cheat; Story 11.2 will replace with client-side Intl.DateTimeFormat). The display string format is `"2026-05-12 14:32 CEST / UTC+2"`.
   - **Badge** — one of `DECISION` / `DEVICE` / `SYSTEM` / `CONSTRAINT` / `INSTALLER` rendered as a CSS-class-tagged span (`.event-badge.event-badge--{type}`). The five badge types match UX spec line 660. The exact pixel styling is in scope for the CSS additions (AC13).
   - **Plain-language summary** — the `event_log.summary` text column, rendered safely (Jinja autoescape). Truncated to 80 chars in the preview (the full text is visible in the Story 11.2 event-log page); truncation appends `…`.

   A **`"View all"`** link at the bottom of the section links to `/installer/event-log` with **no pre-filter** (epic AC line 2285 "a `"View all"` link opens the full event log with no pre-filter"). This page is not implemented until 11.2; the link is rendered as a placeholder anchor that 404s today (acceptable — confirmed against epic intent that 11.1 ships before 11.2). **Dev notes call this out explicitly** so the 404 is not flagged as a bug in review.

   **EventLogRepo needs a new method:** `list_recent(limit: int = 5) -> list[EventLogEntry]`. The current `EventLogRepo` at `event_log_repo.py` has `append`, `prune_expired`, and the 10.4 `count_peak_limiting_applied_decisions` — no read-back method exists. Add `list_recent`:

   ```python
   async def list_recent(self, *, limit: int = 5) -> list[EventLogEntry]:
       """Return the N most recent event_log rows, ordered by timestamp DESC.

       Used by Story 11.1's installer dashboard preview section. Story 11.2 will
       add the paginated/filtered surface; ``list_recent`` is the read-only
       constant-limit fast path for the dashboard.
       """
       async with self._conn.execute(
           "SELECT id, schema_version, timestamp, actor, event_type,"
           "       summary, detail, device_id, config_version"
           " FROM event_log"
           " ORDER BY timestamp DESC, id DESC"
           " LIMIT ?",
           (limit,),
       ) as cursor:
           rows = await cursor.fetchall()
       return [_row_to_event_log_entry(row) for row in rows]
   ```

   `EventLogEntry` is a NEW frozen Pydantic model in `event_log_repo.py` (or a new `core/event_log.py` module — colocate with the rest of the event_log surface). Fields mirror the table columns. The `detail` column is parsed as JSON (or left as raw string for templates — choose one and document; the preview only needs `summary` + `timestamp` + `event_type` so the `detail` JSON parse can be lazy/optional).

   **The secondary `ORDER BY id DESC`** is load-bearing for ties (two events emitted in the same millisecond — possible under fast adapter polling); without it row order is undefined and the test would be flaky.

   **DB I/O for AC6:** one indexed query on `event_log` ordered by timestamp DESC. The `event_log` table has an existing index on `timestamp` from migration `0006_*` (verify; if missing, this is a flag for a future hardening sweep but doesn't block 11.1 since v1 retention caps at 90 days).

---

### Anomaly notice — single, deterministic, severity-ordered (the A2-triggering state machine)

8. **AC7 — Anomaly detection (read-only over StateStore snapshot).** New module `src/open_ems/services/installer_anomaly.py` exposes a **pure-functional** anomaly detection helper:

   ```python
   from dataclasses import dataclass
   from typing import Literal

   Severity = Literal["WARN", "FAIL"]
   AnomalyType = Literal[
       "DEVICE_ERROR",          # any role in component_states is ERROR
       "DEVICE_UNAVAILABLE",    # required role (grid_meter, inverter) is UNAVAILABLE
       "SYSTEM_DEGRADED",       # operating_mode is degraded or conservative
       "SYSTEM_FAILED",         # operating_mode is fail_safe
       "PEAK_APPROACHING",      # snapshot-derived: peak signal close to limit (see note)
   ]

   @dataclass(frozen=True)
   class AnomalySignature:
       """Set of anomaly facts active at a single snapshot evaluation.

       Used for the dismiss/elevation state machine: re-display triggers on
       higher severity OR a new AnomalyType that was not in the dismissed
       signature. Frozen for hashability + dict-key use across renders.
       """
       severity: Severity
       anomaly_types: frozenset[AnomalyType]
       summary: str  # plain-language aggregated description
       link_query: str  # query-string suffix for the "View in event log" link

   def detect_anomaly_from_snapshot(snapshot: SystemSnapshot) -> AnomalySignature | None:
       """Return the AnomalySignature for the current snapshot, or None if no
       anomaly is active. Reads ONLY snapshot.operating_mode +
       snapshot.component_states + snapshot.active_ev_override. Does NOT
       perform any I/O — the StateStore-only invariant from epic AC line 2291
       is enforced structurally by the function signature (SystemSnapshot
       in → AnomalySignature | None out).
       """
   ```

   **AC7 sub-contract — single, severity-ordered, aggregating:**
   - **At most one anomaly notice is shown** (epic line 2289). When multiple anomaly types are simultaneously active, ONE `AnomalySignature` is returned with `anomaly_types` containing all active types and `summary` aggregating in plain language.
   - **Severity ordering:** if ANY active anomaly type maps to `FAIL` severity, the signature carries `severity="FAIL"`. Otherwise `"WARN"`. Mapping:
     - `SYSTEM_FAILED` → `FAIL`
     - `DEVICE_UNAVAILABLE` (required role missing) → `FAIL`
     - `SYSTEM_DEGRADED` → `WARN`
     - `DEVICE_ERROR` → `WARN`
     - `PEAK_APPROACHING` → `WARN`
   - **Summary aggregation examples:**
     - `severity=WARN`, types={DEVICE_ERROR} → `"1 device degraded"`
     - `severity=WARN`, types={DEVICE_ERROR, SYSTEM_DEGRADED} → `"System degraded · 1 device degraded"`
     - `severity=FAIL`, types={SYSTEM_FAILED, DEVICE_UNAVAILABLE} → `"System in safe mode · 1 required device unavailable"`
     - `severity=WARN`, types={DEVICE_ERROR, DEVICE_ERROR} (two devices) → `"2 devices degraded"` (count by role; aggregate within type)

     The exact wording table lives as `_ANOMALY_SUMMARIES` in `installer_anomaly.py`. Single-source-of-truth — never inline anywhere else.
   - **Link target:** the `link_query` field is the query string to append to `/installer/event-log` for the "View in event log" link in the rendered notice. Examples:
     - `SYSTEM_FAILED` only → `?type=SYSTEM&window=24h`
     - `DEVICE_ERROR` only → `?type=DEVICE&window=24h`
     - mixed → `?type=SYSTEM,DEVICE&window=24h` (multi-type pre-filter; Story 11.2 honors this)

     The `&window=24h` suffix is the "time window" element from epic line 2290.

   **PEAK_APPROACHING anomaly source:** the peak limit check requires comparing snapshot device telemetry (e.g., `GridMeterState.grid_power_kw`) to `ActiveConstraints.peak_limit_kw`. But the AC says anomaly detection MUST read ONLY from the snapshot (line 2291). Two paths reconcile:
   - **Path A (preferred for v1):** OMIT `PEAK_APPROACHING` from the snapshot-derived anomaly types. The peak tracker (AC5) already shows the "approaching" / "exceeded" labels directly. Anomaly notice covers the SystemOperatingMode + device-state surface only.
   - **Path B (deferred):** add `peak_limit_kw` and `peak_approaching_flag` to `SystemSnapshot` (computed by ControlLoop on each tick) so the anomaly detector can read both from the snapshot. This is a cross-cutting Epic 5/7 change and OUT OF SCOPE for 11.1.

   **Decision:** v1 ships with **Path A** — PEAK_APPROACHING is rendered in the peak tracker label only, NOT in the anomaly notice. The anomaly notice covers `SYSTEM_FAILED` / `SYSTEM_DEGRADED` / `DEVICE_ERROR` / `DEVICE_UNAVAILABLE`. The `AnomalyType` literal omits `PEAK_APPROACHING`; the four-member set is canonical for 11.1.

   **Structural enforcement of "StateStore-only":** the function signature `(snapshot: SystemSnapshot) -> AnomalySignature | None` makes I/O impossible without changing the signature. A test asserts that the function is called from the fragment route with NO repository, NO settings, NO database connection arguments — pure StateStore consumer. Mirror Story 10.1's `test_headline_ignores_device_state_per_ac14` pattern (the structural-discipline test).

9. **AC8 — Dismiss + elevation state machine (A2-trigger T1 surface).** The anomaly notice is **dismissible per session** (epic AC line 2292). A dismissed notice **reappears automatically** when:

   - the active `AnomalySignature.severity` is **higher** than the dismissed signature's severity (`WARN < FAIL`, total order), OR
   - the active signature's `anomaly_types` contains a member NOT present in the dismissed signature's `anomaly_types` (set-difference is non-empty).

   **Persistence model — per-session, in-memory.** A new module-level dictionary `_DISMISSED_ANOMALIES: dict[str, AnomalySignature] = {}` keyed by `session_id` lives in `installer_anomaly.py`. **No DB persistence.** Process restart clears all dismissals — this is the correct semantic (a new process is a new operational view of the world). The dict is bounded in practice by the active installer-session count (typically 1–2 — single installer browsing on laptop + phone).

   **API contracts:**

   ```python
   def evaluate_dismiss_state(
       current: AnomalySignature | None,
       dismissed: AnomalySignature | None,
   ) -> Literal["render", "suppress", "no_anomaly"]:
       """Return whether the notice should render given the current and
       previously-dismissed signatures. Pure-functional; no I/O.

       - "no_anomaly" — current is None (no anomaly active)
       - "render" — current is non-None AND (dismissed is None OR current
         elevates over dismissed)
       - "suppress" — current is non-None AND dismissed covers current
         (dismissed.severity >= current.severity AND
          current.anomaly_types <= dismissed.anomaly_types)
       """

   def dismiss_signature(session_id: str, signature: AnomalySignature) -> None:
       """Store the dismissed signature for the session. Called by the POST
       dismiss endpoint. Overwrites any prior dismissed signature for the
       session."""

   def get_dismissed_signature(session_id: str) -> AnomalySignature | None:
       """Return the dismissed signature for the session, or None."""

   def clear_dismissed_signature(session_id: str) -> None:
       """Best-effort clear (called on logout if a session_id deletion hook
       exists; otherwise garbage-collected on dict-key pruning)."""
   ```

   **Elevation table — total order:**

   | dismissed severity | current severity | dismissed types | current types | result |
   |---|---|---|---|---|
   | WARN | WARN | {DEVICE_ERROR} | {DEVICE_ERROR} | **suppress** |
   | WARN | WARN | {DEVICE_ERROR} | {DEVICE_ERROR, SYSTEM_DEGRADED} | **render** (new type) |
   | WARN | FAIL | any | any | **render** (severity elevated) |
   | FAIL | WARN | any superset of current | any | **suppress** (de-escalation does NOT re-display) |
   | FAIL | FAIL | {SYSTEM_FAILED} | {SYSTEM_FAILED, DEVICE_UNAVAILABLE} | **render** (new type) |
   | None | anything non-None | — | — | **render** (first anomaly this session) |
   | any | None | — | — | **no_anomaly** (clear) |

   **Critical edge case — anomaly clears and re-appears with same signature:**
   - At T0: current = `(WARN, {DEVICE_ERROR})`; installer dismisses.
   - At T1: anomaly clears (`current=None`); notice is suppressed naturally because there is no current anomaly. The dismissed signature is NOT cleared from the dict (we keep it for future comparison).
   - At T2: same anomaly returns (`current = (WARN, {DEVICE_ERROR})`). Per the elevation table this is **suppress** (no elevation).

   **Should the re-appearance re-display?** The epic AC is ambiguous on this. Reading line 2292 carefully: "a dismissed notice reappears automatically if severity increases OR a new anomaly type appears that was not present when dismissed". The literal reading is: re-display ONLY on elevation. A clear-and-return-with-same-signature does NOT re-display. **This is the v1 behavior.**

   _Rationale call-out in dev notes:_ this is the conservative behavior — the user dismissed the notice acknowledging "I know about DEVICE_ERROR"; a transient clear-and-recur of the same signature is the same fact, not new information. If installer feedback identifies this as a surprise after deployment, revisit in an Epic 11 follow-up.

   **POST /actions/dismiss-anomaly route:**

   ```python
   @router.post("/actions/dismiss-anomaly", response_class=HTMLResponse)
   async def dismiss_installer_anomaly(
       request: Request,
       user: InstallerUser = Depends(require_installer),
       store: StateStore = Depends(get_state_store),
   ) -> HTMLResponse:
       """Dismiss the currently-active anomaly notice for this session.

       Reads the current AnomalySignature from the snapshot and stores it as
       the dismissed signature for the session. Returns an empty section so
       the HTMX outerHTML swap collapses the notice. Idempotent: re-dismissing
       when no anomaly is active is a no-op that still returns the empty
       section.
       """
       snapshot = store.get_snapshot()
       current = detect_anomaly_from_snapshot(snapshot)
       if current is not None:
           dismiss_signature(user.session_id, current)
       # Return the empty section so the outerHTML swap collapses the notice.
       return _templates.TemplateResponse(
           request,
           "fragments/installer/anomaly-notice.html",
           {"signature": None, "csrf_token": user.csrf_token},
       )
   ```

   The route lives in `web/routes/actions.py` (same module as 10.2's `/actions/ev-override` and 10.3's `/actions/set-strategy`). Mirrors their CSRF + role-gated patterns.

   **GET /fragments/installer/anomaly-notice** reads the snapshot, calls `detect_anomaly_from_snapshot`, calls `evaluate_dismiss_state(current, get_dismissed_signature(user.session_id))`, and renders one of three template branches:
   - `"render"` → full notice with badge + summary + "View in event log" link + "Dismiss" button
   - `"suppress"` OR `"no_anomaly"` → empty `<section id="installer-anomaly-notice" hx-...></section>` (keeps the HTMX-binding for next poll, but with no content)

   **HTMX swap semantics:** the Dismiss button uses `hx-post="/actions/dismiss-anomaly"` with `hx-target="#installer-anomaly-notice"` and `hx-swap="outerHTML"`. The 10s self-refresh on the section continues — if the next refresh observes an elevation, the empty section is replaced by the new full notice. This is the polling-stops-after-outerHTML-swap pattern landed in Story 10.4 AC9 (the swapped-in fragment carries `hx-get` + `hx-trigger="every 10s"` on its own root section).

10. **AC9 — Pre-filter event-log links.** The "View in event log" links from:
    - The anomaly notice — link target `/installer/event-log?<link_query>` (the AnomalySignature.link_query field)
    - Per-device rows — link target `/installer/event-log?device_id=<id>` (one device's row links to events for that device only)
    - The health indicator — link target `/installer/event-log?type=SYSTEM&window=24h` (when status is not NORMAL)

    The `/installer/event-log` page itself is **OUT OF SCOPE** for 11.1 — Story 11.2 owns it. 11.1 emits the link URLs per the documented contract; the target page renders as a placeholder until 11.2 lands. **The query-string format is the contract** — 11.2's URL parser MUST honor exactly the keys 11.1 emits (`type`, `device_id`, `window`).

---

### Cross-cutting structural enforcement

11. **AC10 — All state reads come from StateStore + peak_intervals + event_log; ZERO additional DB queries for anomaly detection.**

    **Test #1 (structural — AC10 main rule):** `test_anomaly_detection_consumes_only_snapshot` patches `EnergyRepo.get_current_monthly_peak_kw`, `EventLogRepo.list_recent`, and `DeviceRepo.list_all` with `side_effect=AssertionError("anomaly detection must NOT trigger DB I/O")`. Calls `detect_anomaly_from_snapshot(some_snapshot)`. Asserts the function returns its `AnomalySignature | None` result without triggering any of the mocks. This is the structural-discipline test for the StateStore-only contract.

    **Test #2 (route-level — measures actual DB load):** `test_installer_dashboard_render_db_query_count_bounded` instruments the aiosqlite connection (custom row factory + execute hook, OR a SQL-capture context manager) and asserts that GET `/installer/dashboard` (the shell route) triggers **zero** DB queries (template-only). The fragment endpoints — when subsequently fetched by HTMX — trigger:
    - `/fragments/installer/health-indicator` — 0 queries (snapshot read only)
    - `/fragments/installer/device-rows` — 1 query (DeviceRepo.list_all)
    - `/fragments/installer/peak-tracker` — 1 query (EnergyRepo.get_current_monthly_peak_kw) + 0 for constraints (in-memory provider)
    - `/fragments/installer/event-log-preview` — 1 query (EventLogRepo.list_recent)
    - `/fragments/installer/anomaly-notice` — 0 queries (snapshot + dismiss-dict only)

    **Test #3 (no caching shenanigans):** `test_anomaly_dismiss_dict_is_module_level_not_request_scoped` asserts that two consecutive GETs to `/fragments/installer/anomaly-notice` for the same session preserve the dismissed state — verifies the dict is process-wide, not request-scoped, not client-cookie-stored.

12. **AC12 — Cross-story constraint compliance.** The cross-story rules from epic line 2377-2386 are honored by structural enforcement:
    - **No degradation logic in the dashboard layer** — verified by AC10 test #2 (zero DB queries during anomaly detection beyond what AC says is allowed). Anomaly summaries are derived from `SystemOperatingMode` + `component_states` only.
    - **`SystemOperatingMode` maps directly** — verified by AC3 exhaustive parametrize test over all four enum values.
    - **Component state and capability badge always visually distinct** — verified by AC4 DOM-parse test.
    - **Only one anomaly notice at a time** — verified by AC7 unit test that asserts the function returns `AnomalySignature | None`, never a list. The notice template emits a single `<aside>` element; a structural test asserts that no second `.installer-anomaly-notice` element can render.
    - **Dismissed anomaly notices reappear on severity increase or new anomaly type** — verified by AC8 elevation-table tests (one test per row of the elevation table).
    - **All event log timestamps stored in UTC; rendered in browser-local timezone with UTC offset shown** — verified by AC6 test that asserts both the UTC ISO-8601 and the local-timezone formatted string are present in the rendered HTML.
    - **"View in event log" links from dashboard pre-filter** — verified by AC9 tests that GET-render the anomaly + device-row + health-indicator fragments and assert link URLs match the contracted query-string format.

13. **AC13 — CSS additions for the installer surface.** New CSS classes added to `src/open_ems/web/static/open-ems.css`. The existing design tokens (`--color-accent`, `--color-accent-subtle`, `--color-pass`, `--color-degraded-bg`, `--color-text-secondary`, `--color-warn`, `--color-fail`) cover the installer palette; **no new tokens added**. New classes:

    - `.installer-dashboard` — top-level wrapper; `display: grid; grid-template-columns: 220px 1fr` on desktop; CSS-grid collapses to single column on `≤640px`.
    - `.installer-sidebar` — fixed 220px sidebar. Items use the existing token set; the active item gets `border-left: 4px solid var(--color-accent); background: var(--color-accent-subtle)`.
    - `.health-indicator__badge.health-indicator__badge--{normal|degraded|failed}` — three variants. `--normal` uses `--color-pass`; `--degraded` uses `--color-warn`; `--failed` uses `--color-fail`. Background-color rules; text-color contrasts via existing token pairings.
    - `.device-row` — flex layout (label · component-state · capability-badge · age-caption · "View in event log"). `.device-row__component-state` and `.device-row__capability-badge` are independent span classes — each carries its own background-color rule based on the value-class suffix (`--active`, `--degraded`, `--error`, `--unavailable` for component state; `--full`, `--reduced`, `--unsupported`, `--unknown` for capability).
    - `.installer-peak-tracker__value` — monospace via `font-family: var(--font-mono)` (already in the bundle for Story 10.2's data-correlation attributes; if not, fall back to the system monospace stack `font-family: ui-monospace, monospace`).
    - `.installer-event-log-preview__row` — flex row with timestamp · badge · summary · device-context. `.event-badge.event-badge--{DECISION|DEVICE|SYSTEM|CONSTRAINT|INSTALLER}` — five badge variants; each maps to a color from the existing token set (DECISION → `--color-text-secondary`; DEVICE → `--color-accent`; SYSTEM → `--color-text-secondary`; CONSTRAINT → `--color-warn`; INSTALLER → `--color-accent-subtle` with stronger text).
    - `.installer-anomaly-notice` — full-width banner above the stat cards. Two severity variants `.installer-anomaly-notice--warn` (background `--color-warn-subtle` or amber-tinted token; foreground `--color-text-primary`) and `.installer-anomaly-notice--fail` (background `--color-fail-subtle`; bold border-left). **NOT degraded-mode slate** per UX spec line 1256 (explicit forbidden state).
    - `[x-cloak] { display: none !important; }` — already defined from Story 10.3.

    All additions are appended to the existing `open-ems.css`. **No new CSS file.** The total bundle size grows by ~150 lines. The static-files servability test from Story 10.1 (`test_static_css_bundle_is_servable_at_expected_url`) continues to pass without modification.

14. **AC14 — Tests (unit + integration).**

    **Unit tests (target ~30 new):**

    Anomaly detection (`tests/unit/services/test_installer_anomaly.py`):
    1. `test_detect_anomaly_normal_snapshot_returns_none`
    2. `test_detect_anomaly_operating_mode_degraded_returns_warn_system_degraded`
    3. `test_detect_anomaly_operating_mode_conservative_returns_warn_system_degraded`
    4. `test_detect_anomaly_operating_mode_fail_safe_returns_fail_system_failed`
    5. `test_detect_anomaly_required_role_unavailable_returns_fail_device_unavailable`
    6. `test_detect_anomaly_optional_role_unavailable_does_not_promote_to_fail` _(unavailable EV charger is NOT a FAIL on its own)_
    7. `test_detect_anomaly_optional_role_error_returns_warn_device_error`
    8. `test_detect_anomaly_multiple_active_aggregates_summary_and_takes_highest_severity`
    9. `test_detect_anomaly_link_query_for_system_failed_is_type_system_window_24h`
    10. `test_detect_anomaly_link_query_for_mixed_types_is_multi_type_filter`
    11. `test_evaluate_dismiss_state_no_anomaly_returns_no_anomaly`
    12. `test_evaluate_dismiss_state_no_dismissed_returns_render`
    13. `test_evaluate_dismiss_state_same_signature_returns_suppress`
    14. `test_evaluate_dismiss_state_severity_elevation_returns_render`
    15. `test_evaluate_dismiss_state_new_type_returns_render`
    16. `test_evaluate_dismiss_state_de_escalation_returns_suppress` _(FAIL→WARN does NOT re-display)_
    17. `test_evaluate_dismiss_state_clear_and_return_same_signature_returns_suppress` _(the conservative-behavior test from AC8 dev notes)_
    18. `test_anomaly_summary_aggregates_count_by_device_error_type` _("2 devices degraded")_
    19. `test_anomaly_detection_consumes_only_snapshot` _(AC10 structural test #1 — patches DB repos to assert-not-called)_
    20. `test_anomaly_type_literal_covers_all_documented_types` _(exhaustive — guards against future enum members shipping without a summary entry)_

    Health indicator (`tests/unit/web/test_installer_dashboard_fragments.py`):
    21. `test_health_indicator_maps_operating_mode_normal_to_NORMAL`
    22. `test_health_indicator_maps_operating_mode_degraded_to_DEGRADED`
    23. `test_health_indicator_maps_operating_mode_conservative_to_DEGRADED`
    24. `test_health_indicator_maps_operating_mode_fail_safe_to_FAILED`
    25. `test_health_indicator_exhaustive_over_system_operating_mode_enum` _(parametrize over `list(SystemOperatingMode)` to guard against future enum members)_

    Device rows:
    26. `test_device_rows_render_component_state_and_capability_badge_as_distinct_dom_elements` _(parse HTML, assert each .device-row has BOTH .device-row__component-state AND .device-row__capability-badge as separate elements with distinct classes — AC4 structural)_
    27. `test_device_rows_decoupling_reduced_capability_active_state_renders_both`
    28. `test_device_rows_unknown_capability_when_no_registry_entry_for_role`
    29. `test_device_rows_no_event_log_link_for_unknown_capability_rows`

    Peak tracker:
    30. `test_peak_tracker_under_limit_renders_simple_label`
    31. `test_peak_tracker_approaching_limit_at_90_percent_renders_approaching_label`
    32. `test_peak_tracker_exceeded_limit_renders_exceeded_label`
    33. `test_peak_tracker_no_data_renders_no_data_message`
    34. `test_peak_tracker_constraints_unavailable_renders_not_configured`

    Event log preview:
    35. `test_event_log_preview_returns_5_most_recent_ordered_by_timestamp_desc_then_id_desc`
    36. `test_event_log_preview_renders_utc_iso_in_data_utc_attribute_and_local_timezone_text`
    37. `test_event_log_preview_summary_truncated_to_80_chars_with_ellipsis`
    38. `test_event_log_preview_view_all_link_has_no_filter`

    Dismiss state machine + route:
    39. `test_post_dismiss_anomaly_stores_current_signature_in_session_dict`
    40. `test_post_dismiss_anomaly_returns_empty_section_with_outerhtml_swap`
    41. `test_post_dismiss_anomaly_idempotent_when_no_anomaly_active`
    42. `test_get_fragment_after_dismiss_returns_empty_section`
    43. `test_get_fragment_after_dismiss_re_renders_on_severity_elevation`
    44. `test_get_fragment_after_dismiss_re_renders_on_new_anomaly_type`
    45. `test_anomaly_dismiss_dict_is_module_level_not_request_scoped` _(AC10 test #3)_

    Dashboard route + structural:
    46. `test_installer_dashboard_redirects_homeowner_to_their_home` _(mirror Story 10.1 pattern)_
    47. `test_installer_dashboard_redirects_unauthenticated_to_login`
    48. `test_installer_dashboard_shell_renders_five_htmx_bound_sections` _(mirror Story 10.2's homeowner equivalent — anomaly + health + device-rows + peak + event-log-preview)_
    49. `test_installer_dashboard_route_responds_under_2_seconds` _(AC11; 10-iteration timing test, mean < 2000ms — generous since 10.1's 200ms ceiling applies to the simpler homeowner shell)_
    50. `test_installer_dashboard_render_db_query_count_bounded` _(AC10 test #2 — shell route itself triggers 0 DB queries)_

    Integration (`tests/integration/web/test_installer_dashboard_e2e.py`):
    51. `test_e2e_normal_snapshot_no_anomaly_notice_health_normal`
    52. `test_e2e_failed_snapshot_renders_fail_anomaly_with_link_to_event_log_filter`
    53. `test_e2e_dismiss_and_elevate_round_trip` _(POST dismiss; next GET returns empty; mutate StateStore to elevate; next GET returns full notice)_
    54. `test_e2e_sidebar_setup_link_navigates_to_existing_setup_page`
    55. `test_installer_dashboard_a11y_placeholder` _(xfail+skipif per Story 9.x precedent — a11y test scaffolding for future audit pass)_

15. **AC15 — Quality gates.** All of the following MUST hold at story completion:
    - `uv run pytest` → at least `1628 + N` passed where `N ≥ 50` (the new tests above; total target `≥ 1678 passed`). Baseline 1628 from Story 10.4 (1598 + the ≥30 net new from 10.4). Allowed: same xfailed + skipped counts (no new xfail except the one a11y placeholder in AC14 #55).
    - `uv run ruff check .` → clean across all source files (new module + new route + new template + new test files).
    - `uv run ruff format --check .` → clean.
    - `uv run mypy src` → clean across ~101 source files (the existing ~99 + new `installer_anomaly.py` + the new `event_log_repo.list_recent` may bump the count).
    - `uv.lock` → unchanged (no new Python dependencies; reuse htmx + alpine + fastapi already pinned).
    - No migration in this story (the `event_log` and `peak_intervals` tables already exist).

---

## Developer Context Section

This section is the comprehensive guide for the dev agent. It encodes **everything the agent needs to know that is not obvious from the AC text alone** — including the regressions to avoid, the patterns to follow, and the cross-story interactions to honor.

### Foundation patterns to mirror from completed stories

**Pattern 1 — HTMX-bound fragment section + Alpine.js factory wiring (from Story 10.1 + 10.2 + 10.3 + 10.4):**

- Shell template (`dashboard.html`) defines the section skeleton with `hx-get`, `hx-trigger="load, every 10s"`, `hx-swap="outerHTML"`. The fragment endpoint returns the FULL `<section>` element — outerHTML swap replaces both the polling binding AND the content. The swapped-in fragment carries its own `hx-get` / `hx-trigger` so polling continues after the first swap.
- For interactive sections (the anomaly-notice's Dismiss button), wire an Alpine.js factory in `dashboard.html`'s `{% block scripts %}` if local UI state is needed. For 11.1 the dismiss flow is purely server-side (POST → re-render), so **no Alpine state is required** for the anomaly notice. The sidebar mobile-collapse Alpine factory (`installerSidebar`) is the only Alpine.js addition; it lives next to the existing 10.x factories.

**Pattern 2 — Fragment route + builder pattern (from `web/routes/fragments.py` + `state_serialization.py`):**

The 10.x homeowner fragment routes follow this shape:
1. Route function declared in `routes/fragments.py` (will become `routes/installer_fragments.py` for 11.1 — or extend the existing module; **decision: extend** — a single fragments module is simpler, and the existing module already mixes homeowner + the weekly summary which shares nothing semantically with the homeowner cards).
2. Pure-functional builder in `state_serialization.py` takes the typed inputs (SystemSnapshot, etc.) and returns a context `dict`. **No I/O in the builder.**
3. Route does the I/O (`store.get_snapshot()`, `await energy_repo.read_...`, etc.), passes to the builder, renders the template.

For 11.1, add `build_installer_health_indicator_context`, `build_installer_device_row_context`, `build_installer_peak_tracker_context`, `build_installer_event_log_preview_context`, and `build_installer_anomaly_notice_context` to `state_serialization.py`. Each is pure-functional over its typed inputs. The structural enforcement of "anomaly detection reads only from snapshot" is encoded in the `build_installer_anomaly_notice_context(snapshot: SystemSnapshot, dismissed: AnomalySignature | None) -> dict` signature.

**Pattern 3 — Test naming honesty (from Story 9-Y-a, 10.3 review-cycle):**

Test names MUST describe what the test ACTUALLY asserts. Examples from this story:
- `test_detect_anomaly_link_query_for_mixed_types_is_multi_type_filter` ← describes what it tests (multi-type filter), not just "test_link_query".
- `test_anomaly_dismiss_dict_is_module_level_not_request_scoped` ← describes the structural invariant under test.

Avoid generic names like `test_dismiss_works` — they fail the naming-honesty rule.

**Pattern 4 — Defensive `.get(...)` fallbacks on label-table reads (from Story 10.1 review patch #2):**

Where a mapping table is keyed by an enum (e.g., `_ANOMALY_SUMMARIES`), the **production code path** uses `.get(key, default)` to degrade gracefully on future enum additions. The exhaustiveness test catches the missing-entry case at TEST time; production must not 500 on a missing entry. Mirror the `_STRATEGY_LABELS.get(...)` pattern at `state_serialization.py:299-301`.

**Pattern 5 — Defensive truncation of summaries (from Story 10.4):**

The event-log preview truncates `summary` at 80 chars. Use Python's `[:80] + "…"` rather than a custom whitespace-aware truncator (the latter has off-by-one risks and is over-engineered for v1).

### Files to read in full before starting

**Required reads (failure to read these is the primary cause of review-cycle bugs):**

1. `src/open_ems/core/state.py` — `SystemOperatingMode`, `SystemSnapshot`, `ComponentState`, `OPERATING_MODE_GLOBAL_STATE` mapping at line 80-87. The `ComponentState` enum at lines 52-60 does NOT have a `degraded` member — AC4 task #1 is the investigation to confirm whether `error` is the installer-facing "DEGRADED" or whether a new member must be added.
2. `src/open_ems/web/routes/installer.py` — the existing installer route (only 30 lines). The replacement does NOT change the route signature; the `dashboard.html` template branch is what changes.
3. `src/open_ems/web/templates/dashboard.html` — the existing shell. The homeowner branch is the structural reference for the new installer branch. Pay attention to lines 9-40 (the `.dashboard-stack` with 5 sections + the weekly-summary trigger OUTSIDE the stack — the 10.4 pattern). The new installer wrapper is `.installer-dashboard` (NOT `.dashboard-stack`).
4. `src/open_ems/web/routes/fragments.py` — the homeowner fragment-route pattern. The installer fragments will follow exactly the same shape: short route function + builder call + TemplateResponse.
5. `src/open_ems/web/state_serialization.py` — the builder pattern. The `_OVERRIDE_FAILURE_REASONS` table is the model for `_ANOMALY_SUMMARIES`. The structural-discipline `build_homeowner_headline_context` reads only `operating_mode` + `active_strategy` — the new `build_installer_anomaly_notice_context` reads only `snapshot` + the dismissed signature.
6. `src/open_ems/storage/repositories/energy_repo.py` — `get_current_monthly_peak_kw` at line 41-51 already exists and is the peak tracker's data source. **DO NOT add a new method** for the same value.
7. `src/open_ems/storage/repositories/event_log_repo.py` — the existing repo has NO `list_recent` method. AC6 adds it. The detail JSON parsing is handled at write time (json.dumps); the read side returns the raw string and lets callers parse if needed.
8. `src/open_ems/storage/repositories/device_repo.py` — the `DeviceRegistryEntry` Pydantic model at lines 53-119. The `last_capability_status` field at line 65 is the AC4 capability-badge source. The `role` field at line 70 is the join key.
9. `src/open_ems/web/dependencies.py` — the auth dependencies. `require_installer` is what gates the new routes; `get_state_store`, `get_active_constraints_optional`, `get_settings_dep` are the existing dependency-injection wiring. **A new `get_event_log_repo` dependency** parallel to `get_energy_repo` may be cleaner than constructing `EventLogRepo()` inline in each route — confirm via codebase grep what the existing 10.x precedent is.
10. `src/open_ems/web/templates/fragments/homeowner/status-headline.html` — the canonical interactive-fragment template. The installer anomaly-notice template will mirror this shape but with a Dismiss button instead of strategy buttons.

### Files to read on-demand

- `src/open_ems/core/state_store.py` — only if the snapshot reading path is unclear. The snapshot is read-only from the route's perspective; no mutator is invoked by 11.1.
- `src/open_ems/web/app.py` — only the `app.include_router(...)` block at lines 688-695 changes (the actions router is already included for 10.2/10.3 — the dismiss POST lands in the existing actions router).
- `src/open_ems/web/templates/dashboard.html` `{% block scripts %}` — for adding the `installerSidebar` Alpine factory.

### What NOT to change

- **`SystemSnapshot`, `StateStore`** — no changes. 11.1 is purely a read consumer.
- **`OPERATING_MODE_GLOBAL_STATE` mapping** — no changes. AC3 reuses it as-is.
- **The homeowner-side templates and routes** — completely untouched. The shared `dashboard.html` shell has a `{% if dashboard_role == "homeowner" %} ... {% elif dashboard_role == "installer" %} ... {% endif %}` branch; the homeowner branch is preserved verbatim.
- **`derive_component_state` / `derive_global_state`** at `state.py:96-127` — unchanged. AC3 and AC4 are pure consumers.
- **The `peak_intervals` table schema** — unchanged. The existing `EnergyRepo.get_current_monthly_peak_kw` query is the only API used.
- **Migrations** — no new migration for 11.1. The existing `event_log`, `peak_intervals`, and `device_registry` tables already provide all needed columns.

### Library / framework constraints

- **Python:** 3.13 (project lockfile-pinned).
- **FastAPI:** existing pin. No new FastAPI features required.
- **HTMX:** 1.9.x already in `base.html` (line 28). No new HTMX version. The `hx-trigger="load, every 10s"` + `hx-swap="outerHTML"` pattern is the canonical pattern carried from 10.x.
- **Alpine.js:** 3.13.10 already in `static/alpine.min.js` (SHA256 pinned per Story 10.2 AC19). The `installerSidebar` factory uses standard Alpine `x-data` + `x-show` + `x-on:click`. No new Alpine plugins.
- **Pydantic:** v2 (project lockfile-pinned). The new `AnomalySignature` is a `@dataclass(frozen=True)` — NOT a Pydantic model, because the dict-key semantics (hashable frozenset of types + severity) is cleaner with the stdlib dataclass. The new `EventLogEntry` IS a Pydantic frozen model (consistent with the rest of the repo entries like `DeviceRegistryEntry`).
- **structlog:** existing pin. New INFO emissions from the dismiss route: `anomaly_dismissed` with fields `session_id`, `severity`, `anomaly_types` (sorted tuple for log readability), `component="installer_dashboard"`.
- **No new dependencies.** `uv.lock` unchanged.

### Web research — current-version specifics

- **HTMX 1.9.12 + `hx-trigger="every 10s"`** — well-documented; no version-specific gotchas. The Story 10.3 review-cycle DEFERRED finding D1 (polling-stops-after-outerHTML-swap on the homeowner cards) is irrelevant here because the installer-side fragments self-refresh by carrying `hx-get`/`hx-trigger` on their own root section (the 10.4 pattern that lands the fix on a per-section basis).
- **Alpine.js 3.13.10** — the `x-cloak` directive requires the CSS `[x-cloak] { display: none !important; }` rule (already added in Story 10.3). The new `installerSidebar` factory uses standard idioms.
- **FastAPI** — standard `APIRouter` + `Depends` + `TemplateResponse`. No new feature.
- **Jinja2 autoescape** — automatic in `Jinja2Templates(directory=...)`. Event-log `summary` strings are user-authored (INSTALLER badge entries from Story 6.3) — autoescape protects against XSS. Add a regression test that submits a `<script>` payload via INSTALLER note (from a Story 11.2 path; 11.1 only renders) and asserts the rendered preview does NOT contain the raw tag — defer to 11.2 since the write path is owned there.

### Previous-story intelligence (recent stories whose patterns directly inform 11.1)

**Story 10.4 (Weekly summary, done 2026-05-12):**
- The `hx-trigger="every 60s"` on the swapped-in `weekly-summary.html` fragment is the canonical fix for the polling-stops-after-outerHTML-swap pattern. **11.1's anomaly-notice fragment MUST carry `hx-get` + `hx-trigger="every 10s"` on its root section** — this is the same pattern.
- The `test_homeowner_dashboard_render_does_not_query_weekly_summary_table` structural-discipline test at `tests/unit/web/test_dashboard_route.py:155-176` is the direct template for 11.1's AC10 test #2.
- AC13 #1 structural assertion ("trigger lives OUTSIDE `.dashboard-stack`") is the inspiration for 11.1's AC10 test that asserts the installer-dashboard wrapper does NOT use the `.dashboard-stack` class.

**Story 10.3 (Strategy selector, done 2026-05-12):**
- The `_active_strategy` writer-lock contract carry-over (from 10.1 deferred D1) was honored by 10.3. **11.1 does NOT touch StateStore mutators** so this contract is not load-bearing here, but the principle (lock-free reads, atomic snapshot reads) applies — the anomaly detection's `store.get_snapshot()` is lock-free and produces an immutable view, eliminating the read-tear class entirely.
- The exhaustive enum-coverage test pattern (e.g., test_strategy_selector_option_for_every_energy_strategy_member) is the model for AC14 #20 and AC14 #25.

**Story 10.2 (EV override, done 2026-05-12):**
- The frozen Pydantic + model-validator pattern is the model for `EventLogEntry`.
- The `_OVERRIDE_FAILURE_REASONS` exhaustiveness test (grep over policy_guard.py source for literal reason strings) is the inspiration for AC14 #20 (anomaly types covered by summary table).
- The standalone fragment template pattern (`ev-card.html` NOT `_card.html include`) is the model for `installer/anomaly-notice.html` and the four other installer fragment templates.

**Story 10.1 (Homeowner dashboard, done 2026-05-11):**
- AC16's 10-iteration timing test (`test_homeowner_dashboard_route_responds_quickly` at `tests/unit/web/test_dashboard_route.py:99-122`) is the direct template for AC14 #49. The ceiling is relaxed to 2000ms for the installer dashboard (epic AC line 2268) vs. 200ms for the homeowner shell.
- The route signature `installer_dashboard(request, _user=Depends(require_installer))` does NOT change — the existing route at `routes/installer.py:17-30` already renders `dashboard.html`. **The story changes the template branch, not the route.** This is intentional: the route surface is stable; only the rendered content evolves.

**Story 9-X (Adapter wiring, done 2026-05-11):**
- The `_DeferredOCPPAdapter` proxy at lifespan step 4 is the cold-start surface that the installer dashboard observes: during the gap between lifespan-yield and the first OCPP BootNotification, `ev_charger` is a `DegradedDeviceState` from the proxy. The AC4 device-row rendering MUST handle this case — verified by AC14 #28 (UNKNOWN capability when no registry entry).

**Story 9-Y-a (E2E fail-abort coverage, done 2026-05-11):**
- The naming-honesty contract — every test name must describe what it actually asserts — is enforced for all AC14 tests. The R7 deferred-findings triage section of 11.1 will cite this story when carrying forward any 10.x review-cycle items that overlap.

### Git intelligence — recent commit patterns

Recent commits (`git log --oneline -8`):
- `0a595a9 10.4` — weekly summary story
- `71bf9a1 10.3` — strategy selector
- `efd986f 10.2` — EV status card
- `4ac50e1 10.1` — homeowner dashboard layout
- `fd3bfe6 9.5.3` — handoff guide round 3

**Patterns from recent work that 11.1 should follow:**
- One commit per story (per the project's git history). 11.1 should land in ONE commit titled `11.1` after the dev cycle + review cycle complete.
- Migration numbering: 11.1 does NOT introduce a migration (no schema changes). Next migration available is `0014` if 11.2 needs one.
- File-naming convention: `tests/unit/<module>/test_<name>.py`. New test files for 11.1:
  - `tests/unit/services/test_installer_anomaly.py` (NEW)
  - `tests/unit/web/test_installer_dashboard_fragments.py` (NEW)
  - `tests/unit/web/test_installer_dashboard_route.py` (NEW — or extend `test_dashboard_route.py`; **decision: NEW** to keep the installer-vs-homeowner test surfaces cleanly separable).
  - `tests/integration/web/test_installer_dashboard_e2e.py` (NEW)
  - `tests/unit/storage/test_event_log_repo.py` (extend if exists, else NEW) for the `list_recent` method.

---

## A1 Mandatory Orchestration Artifacts (A2-triggered: T1)

The seven artifacts below are MANDATORY because Story 11.1 is A2-triggered on T1 (the anomaly-notice dismiss/elevation state machine). Each section is substantive — not a placeholder. Same enforcement class as missing acceptance criteria.

### R1 — Composition-risk analysis

Story 11.1 converges the following operational domains into one read-only surface:

1. **StateStore snapshot consumption** (Epic 5 + 7) — the dashboard reads `snapshot.operating_mode`, `snapshot.component_states`, `snapshot.active_ev_override` (for completeness; not load-bearing here), and per-role device slots. **Risk:** a torn snapshot read between fields is impossible because `SystemSnapshot` is frozen and replacement is atomic (Story 10.2/10.3 invariant carried forward). **Convergence with anomaly state machine:** the anomaly signature derives from the snapshot's `operating_mode` + `component_states` ONLY — a consistent point-in-time view, no cross-field tear possible. **Confirmed safe.**

2. **DeviceRegistry consumption** (Epic 9) — the per-device row's capability badge reads from `DeviceRepo.list_all()`. **Risk:** the DB query is a separate transaction from the StateStore snapshot read — the two views may show DIFFERENT roles for the same device (the installer just reassigned a role in another tab). **Convergence with the per-device row contract:** the row aligns by `role`, not by `device_id`. If the installer reassigns the inverter role from device A to device B between the snapshot read and the device-registry read, the row may show component_state from device A (the snapshot still has it as the inverter) and capability badge from device B (the registry now has it as the inverter). **Mitigation:** the row is keyed by `role` in the snapshot; the registry lookup uses the SAME `role`. The mismatch window is bounded by the 10s HTMX poll cadence — the next refresh aligns both reads. **Acceptable for v1** — note in dev notes. A future refactor that snapshots the device-registry into `SystemSnapshot` would close the window structurally.

3. **EnergyRepo (peak_intervals) consumption** (Epic 8.1, Epic 9.0d) — the peak tracker reads `get_current_monthly_peak_kw()`. **Risk:** monthly-peak hydrate from DB vs. tracker's in-memory `_monthly_peak_kw` divergence. **Already addressed** by Story 9.0d's restart-hydration design. **Not load-bearing for 11.1** — the dashboard reads the DB authoritatively; the in-memory tracker is the engine's view, not the dashboard's.

4. **EventLogRepo consumption** (Epic 6, Story 6.2) — the preview reads the 5 most recent rows. **Risk:** the event_log is append-only (`update`/`delete` are forbidden at `event_log_repo.py:54-58`), so the read returns a consistent prefix of recent events. **No tear possible.** The DECISION-type entries from RetryPolicy.`_emit_success_audit` may be in-flight (transaction committed but not yet visible to the next SELECT in another connection); aiosqlite's WAL mode handles this correctly — the dashboard sees the latest committed row set, no half-rows.

5. **Anomaly-dismiss session dict** (NEW in 11.1) — module-level `_DISMISSED_ANOMALIES: dict[str, AnomalySignature]`. **Risk:** memory leak if a session dismisses, never logs out, and the process runs for months. **Mitigation:** dict size is bounded by active session count (typically 1–2 for OPEN-EMS's installer-on-laptop deployment context); a long-running session that has dismissed an anomaly carries one stale `AnomalySignature` (~100 bytes). Process restart clears the dict. **Acceptable** for v1. If multi-tenant or many-session deployments emerge, a TTL eviction policy (LRU by session-creation-timestamp) is the structural fix.

6. **Concurrent dismiss-and-poll race** — the installer dismisses the notice; the next GET (from the 10s poll) races against the POST. **Resolution:** POST writes the dict under no lock (Python's GIL serializes single-dict writes); the next GET reads the dict under no lock. The read sees either the pre-dismiss state (notice still shown — re-clicks Dismiss → idempotent) or the post-dismiss state (notice suppressed). Both outcomes are correct. **No invariant violated.**

### R2 — State-transition table

The anomaly-notice dismiss/elevation state machine (A2-trigger T1 surface):

**States** (per session):
- **`S0` — no_dismissed_signature, no_current_anomaly** — initial state, no anomaly active, no dismiss recorded. Render: empty section.
- **`S1` — no_dismissed_signature, current_anomaly_active** — first time the anomaly is seen this session. Render: full notice.
- **`S2` — dismissed_signature_recorded, no_current_anomaly** — installer dismissed; anomaly subsequently cleared. Render: empty section. Dismissed signature retained for future comparison.
- **`S3` — dismissed_signature_recorded, current_anomaly_active_covered_by_dismissed** — anomaly is active but covered by the dismissed signature (no elevation). Render: empty section.
- **`S4` — dismissed_signature_recorded, current_anomaly_active_elevates_over_dismissed** — anomaly is active AND elevates over the dismissed signature (higher severity OR new type). Render: full notice. **The active dismissed signature is NOT cleared automatically** — only re-cleared by the installer dismissing again (which overwrites with the new elevated signature).

**Transitions:**

| From | Trigger | To | Side effects |
|---|---|---|---|
| S0 | snapshot now has anomaly | S1 | none (next GET renders full notice) |
| S1 | installer clicks Dismiss | S2 (if anomaly clears) or S3 (if anomaly still active) | POST writes session_id → current_signature to dict; logger.info `anomaly_dismissed` (severity, anomaly_types, session_id, component="installer_dashboard"); empty `<section>` returned via outerHTML swap |
| S1 | snapshot clears anomaly | S0 | next GET renders empty section |
| S2 | snapshot has new anomaly | S3 (if covered) or S4 (if elevated) | next GET render branches on elevate vs. covered |
| S3 | anomaly elevates (severity OR new type) | S4 | next GET renders full notice |
| S3 | snapshot clears anomaly | S2 | dismissed signature retained; next GET renders empty |
| S3 | installer clicks Dismiss | S3 (idempotent — POST records current signature, which already covers itself) | logger.info `anomaly_dismissed_idempotent` (or same `anomaly_dismissed` event with `is_idempotent=true` field) |
| S4 | installer clicks Dismiss | S3 | POST overwrites dict entry with new elevated signature; subsequent GETs return to S3 |
| S4 | snapshot clears anomaly | S2 | dismissed signature retained (the one that was first elevated over); next GET renders empty |
| S4 | snapshot de-escalates (FAIL → WARN, same types) | S3 | the dismissed signature still covers; next GET renders empty per "de-escalation does not re-display" rule (AC8) |

**Audit trail:** every dismiss action emits a structlog INFO. No event_log row is emitted (the dismiss is per-session UI state, not a system event). **A future Epic 11 hardening sub-story** may add an INSTALLER-type event_log row for dismiss actions if installer-feedback identifies audit-trail value, but v1 does NOT.

### R3 — Impossible-state analysis (with cold-start coverage)

**Impossible states and the structural invariants that prevent each:**

1. **Two anomaly notices simultaneously rendered.** Prevented by: the `detect_anomaly_from_snapshot` function returns `AnomalySignature | None` (single value, not a list). The template renders a single `<aside class="installer-anomaly-notice">` element. A structural test (AC14 #48 + AC12 fifth bullet) asserts that the rendered HTML contains AT MOST ONE matching element.

2. **A dismissed notice that severity-elevates but stays suppressed.** Prevented by: the `evaluate_dismiss_state` function evaluates `dismissed.severity < current.severity` as a strict total order; elevation ALWAYS triggers `"render"`. Structural test AC14 #14.

3. **A dismissed notice that gains a new anomaly type and stays suppressed.** Prevented by: `evaluate_dismiss_state` uses `current.anomaly_types <= dismissed.anomaly_types` (subset check). If `current.anomaly_types - dismissed.anomaly_types` is non-empty, returns `"render"`. Structural test AC14 #15.

4. **A `DEGRADED` device that incorrectly maps to `ACTIVE` component state.** Prevented by: `derive_component_state` at `state.py:96-104` maps `DegradedDeviceState` with `reason != "unavailable"` to `ComponentState.error` (the installer-facing "DEGRADED" — confirmed by AC4 task #1 investigation). Structural test AC14 #26 asserts the mapping holds.

5. **A `REDUCED` capability badge that overrides the component state in the rendered DOM.** Prevented by: the two badges live in distinct CSS classes on distinct DOM elements. Structural test AC14 #26 asserts the DOM-element distinctness.

6. **An anomaly notice that renders with `severity="FAIL"` but contains only WARN-mapping anomaly types.** Prevented by: the `severity` field is derived deterministically in `detect_anomaly_from_snapshot`: `"FAIL"` iff any active type maps to FAIL in the severity-mapping table. Structural test AC14 #8 covers the multi-type case.

7. **A new `EnergyStrategy`, `SystemOperatingMode`, or `ComponentState` enum member that ships without an entry in the relevant rendering table.** Prevented by: exhaustiveness tests (AC14 #20, #25). **Production code uses `.get(key, default)`** so the route does not 500 on the missing entry; the exhaustiveness test catches it at TEST time before merge.

8. **A dismissed signature persists across process restart.** Prevented by: the `_DISMISSED_ANOMALIES` dict is module-level (process-scoped). Process restart re-initializes the module → empty dict → all dismissals cleared. This is the **correct semantic** — a new process is a fresh view of the operational state; the installer's previous dismissals are not carried forward.

**Cold-start coverage (mandatory for A2-triggered stories):**

The system's state from process boot until the first successful ControlLoop tick is the **cold-start window**. During this window:

- `StateStore.get_snapshot()` returns the default snapshot built at construction time (`state_store.py:79-93`): `operating_mode=degraded` (per the constructor's default at line 67), all device slots `None`, all `component_states` set to `ComponentState.unavailable`, `active_ev_override=None`, `sequence_id=0`.
- **Dashboard render during cold-start:**
  - **Health indicator:** `DEGRADED` (from `operating_mode=degraded` → `GlobalState.degraded` → `"DEGRADED"`).
  - **Per-device rows:** all four roles render `UNAVAILABLE` component state. Capability badges read `UNKNOWN` (the DeviceRepo may be empty if the installer has not run the setup wizard; otherwise the badges are whatever the registry says, irrespective of liveness).
  - **Peak tracker:** `get_current_monthly_peak_kw()` returns `0.0` (the COALESCE-MAX query at `energy_repo.py:45` returns `0.0` for an empty `peak_intervals` table). Renders `"Peak this month: 0.0 kW · No data yet"`.
  - **Event log preview:** may render 0 to 5 entries depending on what's been logged during startup (some bootstrap INFO entries may exist; otherwise empty). The fragment template handles the empty case with a calm placeholder `"No events yet — system is starting up."`.
  - **Anomaly notice:** `detect_anomaly_from_snapshot` evaluates `operating_mode=degraded` → `SYSTEM_DEGRADED` anomaly type → returns `AnomalySignature(severity="WARN", anomaly_types={SYSTEM_DEGRADED}, summary="System is starting up", link_query="?type=SYSTEM&window=24h")`. **Cold-start-specific summary text** — the default `"System degraded"` would alarm an installer watching the dashboard come up. The summary table includes a specific entry for the cold-start case: `if snapshot.sequence_id == 0: return AnomalySignature(severity="WARN", ..., summary="System is starting up")`. Once `sequence_id > 0` (first publish completes), the summary reverts to the normal "System degraded" copy.
- **Invariants that temporarily relax:** the "all data reads come from real adapters" invariant is in-effect but trivially satisfied (no real adapters polled yet → all UNAVAILABLE → consistent with the contract).
- **Normal-operation marker:** the first publish() call from the ControlLoop produces `sequence_id=1` and replaces the default snapshot. From this point forward, the dashboard reflects real adapter telemetry. The cold-start window is bounded by `control_loop_interval_seconds` (default 10s from Settings) — within ≤10 seconds of process boot, the dashboard shows real state.

**Process-restart-with-dismissed-anomaly behavior:** the `_DISMISSED_ANOMALIES` dict is empty at boot. The installer's previously-dismissed anomalies are NOT restored. If the system is still in the same state (e.g., a device is still erroring), the installer sees the anomaly notice again — they re-dismiss if they've already accepted it. **This is the v1 contract** — process restart is a structural event that warrants re-acknowledgment of operational anomalies. Document explicitly in dev notes.

### R4 — Cancellation ownership map

The dashboard's async surface is narrow — there are only three `await` points per request:

| Async operation | CancelledError owner | Audit emission | Resource cleanup | Reporting semantics |
|---|---|---|---|---|
| `StateStore.get_snapshot()` (called in 4 of 5 fragment routes) | FastAPI cancels the request task on client disconnect. `get_snapshot()` is synchronous (no await), so cancellation cannot interrupt it mid-call. | none | none | not applicable — synchronous, no in-flight state |
| `DeviceRepo.list_all()` (AC4) | FastAPI cancels the request task; aiosqlite cancels the in-flight query at the cursor level. | none | aiosqlite cursor closes automatically via `async with` | the partial result is discarded; no DB state mutated (read-only query) |
| `EnergyRepo.get_current_monthly_peak_kw()` (AC5) | same as above | none | same as above | same as above |
| `EventLogRepo.list_recent()` (AC6) | same as above | none | same as above | same as above |
| `POST /actions/dismiss-anomaly` body parse + state write | FastAPI cancels the request task; the dict write is atomic (single statement under GIL). | `logger.info("anomaly_dismissed", ...)` is emitted INSIDE the dict-write critical section — non-blocking by structlog default config. If cancelled BEFORE the dict write, the dismiss does not take effect; the installer re-clicks. If cancelled AFTER the dict write but BEFORE the response is rendered, the dismiss took effect but the installer's HTMX client may see a disconnect — the next poll re-fetches and reflects the new state. | none — dict write is in-process | cancellation mid-dismiss leaves the dict in a clean state (either pre- or post-write); the response may not arrive at the client |

**No background tasks introduced by 11.1.** The dashboard is request/response only. **No RetryPolicy involvement** — dismisses are not retried; they are user-driven actions. **No PolicyGuard involvement** — dismisses don't dispatch device commands.

**Why this is not the "interesting" cancellation surface that A2-T2 anticipates:** T2 is about retry policies + cancellation propagation through in-flight device dispatches. 11.1 has no device dispatch surface. The cancellation ownership map is small because the work surface is small.

### R5 — Before-first-successful-cycle lifecycle review

Trace from process start to first successful normal dashboard render:

**Step 0 (`t=0`) — process boot.** `python -m open_ems` or `uvicorn open_ems.web.app:create_app` starts. Lifespan begins.

**Step 1 (`t=0` to `t≈100ms`) — lifespan startup steps.** `init_database()`, Alembic migrations run, `StateStore(system_clock_status=..., ..., operating_mode=SystemOperatingMode.degraded, active_strategy=EnergyStrategy.maximize_self_consumption)` constructed (default snapshot at sequence_id=0). FastAPI routes registered. Static files mounted.

**Step 2 (`t≈100ms`) — first GET `/installer/dashboard` arrives.** If the installer is browsing during startup. The route renders `dashboard.html` with `dashboard_role="installer"` — pure template, no DB I/O at this level. The browser then dispatches 5 HTMX polls (one per fragment).

**Step 3 (`t≈110ms` to `t≈150ms`) — fragment endpoints respond:**
- `/fragments/installer/health-indicator` → `DEGRADED` (sequence_id=0 default).
- `/fragments/installer/device-rows` → all four roles `UNAVAILABLE`. Capability badges depend on whether the device_registry has any rows. If the installer has never run the setup wizard, the registry is empty and all badges read `UNKNOWN`.
- `/fragments/installer/peak-tracker` → `"Peak this month: 0.0 kW · No data yet"` (or whatever the configured limit reads — pre-wizard, the constraints provider may return None → "Not configured").
- `/fragments/installer/event-log-preview` → 0 to 5 entries from startup logging. The placeholder `"No events yet — system is starting up."` if empty.
- `/fragments/installer/anomaly-notice` → `WARN` notice with cold-start summary `"System is starting up"` (per R3 #6 — special-case summary on `sequence_id == 0`).

**Step 4 (`t≈10s`) — first ControlLoop tick completes.** `StateStore.publish(...)` produces `sequence_id=1`. The snapshot reflects real adapter polling results.

**Step 5 (`t≈10s` to `t≈20s`) — first post-tick HTMX poll cycle.** Each fragment refreshes from the new snapshot. If all required devices are connected and healthy, the operating_mode transitions to `normal` (per the engine's degradation matrix at `control_loop.py`); health indicator shows `NORMAL`; anomaly notice clears (`detect_anomaly_from_snapshot` returns None); device rows show real component states + capability badges; peak tracker shows actual data (still 0.0 until the first 15-min interval rolls over, which is up to 15 minutes from boot).

**Normal-operation marker:** the dashboard transitions to "fully operational" view when:
- `operating_mode == normal` AND
- all required component states (`grid_meter`, `inverter`) are `active` AND
- the anomaly notice is empty.

**Temporarily-relaxed invariants during cold-start:**
- "Anomaly summary text is operationally meaningful" — relaxed: the cold-start summary `"System is starting up"` is a UX-only message, not a real operational fact. The structural invariant ("anomaly detection reads from snapshot only") is NOT relaxed — the special-case summary is still derived from `snapshot.sequence_id == 0` which is a snapshot field.
- "Peak tracker shows a meaningful value" — relaxed: `0.0` is a non-meaningful placeholder until the first 15-min interval lands.
- "Event-log preview is informative" — relaxed: may be empty for the first ~10s of process life.

**No process-restart-as-part-of-workflow** — the dashboard does NOT trigger restart. The cold-start window is a passive observation, not an active orchestration.

### R6 — Source-of-truth ownership per datum

Every piece of state 11.1 reads:

| Datum | Owner (single source of truth) | Read path | Drift risk |
|---|---|---|---|
| `SystemOperatingMode` (for health indicator AC3 + anomaly AC7) | `StateStore` (`state_store.py:_active_strategy`-style invariant — read inside the writer lock during publish) | `store.get_snapshot().operating_mode` (lock-free, immutable snapshot) | **none** — atomic snapshot replacement; consumers cannot tear |
| `component_states[role]` (AC4) | `StateStore.publish` (`state_store.py:209-211`) | `store.get_snapshot().component_states[role]` | **none** — same atomic snapshot |
| `active_ev_override` (read in passing if needed; not load-bearing for 11.1) | `StateStore` (mutators at `state_store.py:126`, `compare_and_set_ev_override` at `state_store.py:177`) | `store.get_snapshot().active_ev_override` | **none** |
| `data_age_seconds[role]` (for the per-device row age caption) | `StateStore.publish` (`state_store.py:212-214`) | `store.get_snapshot().data_age_seconds[role]` | **none** |
| `DeviceRegistryEntry` per role (for AC4 capability badge) | `device_registry` table (DB) | `DeviceRepo.list_all()` filtered by role | **Yes (acceptable)** — separate transaction from the snapshot read; misalignment window bounded by 10s poll cadence. **Documented in R1 #2.** A future structural fix would snapshot the role→capability_status map into `SystemSnapshot` at publish time. |
| `peak_intervals.avg_power_kw` (AC5 peak tracker) | `peak_intervals` table (Story 8.1, Story 9.0d hydrate) | `EnergyRepo.get_current_monthly_peak_kw()` | **none** — read-only; latest committed value |
| `active_constraints.peak_limit_kw` (AC5 configured limit) | `active_constraints` table (Story 9.3), hydrated by `ActiveConstraintsProvider` | `get_active_constraints_optional()` → `constraints.peak_limit_kw` | **none** — provider is the single in-memory authoritative view |
| `event_log` rows (AC6 preview) | `event_log` table (Epic 6) | `EventLogRepo.list_recent(limit=5)` | **none** — append-only; reads return consistent prefix |
| **`_DISMISSED_ANOMALIES[session_id]` (AC8 — NEW in 11.1)** | `installer_anomaly._DISMISSED_ANOMALIES` module-level dict | `get_dismissed_signature(session_id)` | **none** — single owner; Python GIL serializes single-key dict writes |
| `AnomalySignature` (current) | derived from `SystemSnapshot` — NOT persisted | `detect_anomaly_from_snapshot(snapshot)` | **none** — pure-functional, no caching |

**Drift risks called out explicitly:**

- **R1 #2 above** — the per-device row aligns role from the snapshot with capability from the registry. The 10s misalignment window is acceptable for v1; a future structural fix would land the registry snapshot into `SystemSnapshot`.
- **No other drift risks identified.** The pattern of "StateStore for live state, DB repos for historical/configuration data, in-process dict for session UI state" is clean.

### R7 — Deferred-findings triage (overlap with 11.1 scope)

Scanning `_bmad-output/implementation-artifacts/deferred-work.md` for items whose component or invariant overlaps Story 11.1:

| Bracket reference | Item | Classification |
|---|---|---|
| `[src/open_ems/web/routes/fragments.py:73-85` + `src/open_ems/web/templates/dashboard.html:10-15]` | HTMX-error UX when `/fragments/homeowner/status-headline` 500s (Story 10.1 deferred D3) | **safe-during-this-story** — the homeowner-side gap is unchanged by 11.1, but the installer side faces the same risk. **Recommendation:** add an analogous `hx-target-error` pattern OR a try/except in the installer fragment routes that returns a calm "Status unavailable" placeholder. **Decision: defer to a future Epic 11 hardening sweep** (consistent with the 10.1 deferred's own classification "Decision-tier UX pattern — better bundled with the Epic 11 installer-monitoring UX pass"). 11.1 does NOT add the error-boundary code because the AC text does not contract it and the patch would be cross-cutting (both fragments.py modules would need the same pattern). Log to deferred-work.md at story completion if not addressed in cycle. |
| `[src/open_ems/services/protocol_adapter_factory.py:1808-1815]` | `_outcome_reachable` silent reduce on missing model (Story 9.4 deferred W3 — "Defer to Epic 11 operational-monitoring work where structured-log volume + format is being designed holistically") | **acceptable-post-this-story** — the deferred classification explicitly names "Epic 11 operational-monitoring work" as the future home. 11.1 establishes the installer dashboard as the operational-monitoring surface; the structured-log volume + format design that W3 anticipates is owned by a future story (likely 11.2 or a 11.x hardening sub-story). **No action in 11.1.** |
| `[src/open_ems/web/routes/setup.py` + `device_repo.py]` (no delete-pending-entry route — Story 9.1 deferred) | "Spec doesn't contract a delete endpoint; revisit when Epic 11 considers installer-side device-registry management" | **acceptable-post-this-story** — 11.1 is installer **monitoring** only; device-registry management is out of scope. The deferred classification anticipates Epic 11.x. **No action in 11.1.** |
| `[src/open_ems/storage/repositories/deployment_validation_repo.py:170,188,...]` | P19 repo transaction-discipline cleanup (Story 9.4 retro deferred) — "Defer to Epic 11 storage-hardening sweep" | **acceptable-post-this-story** — 11.1 does not touch the storage layer beyond adding `event_log_repo.list_recent`. The new method follows the existing read-side pattern (no `BEGIN IMMEDIATE` needed for SELECT-only). The storage-hardening sweep itself is a separate sub-story. **No action in 11.1 beyond ensuring `list_recent` does NOT introduce a new instance of the pre-commit-before-BEGIN-IMMEDIATE pattern (read-only — safe).** |
| **No 10.x deferred items** carry forward to 11.1 with a `must-resolve-in-this-story` classification. The 10.1 deferred `_active_strategy writer-lock contract` was resolved by 10.3. The 10.2 deferred items were bundled into 10.3. The 10.3 deferred D1 (polling-stops-after-outerHTML-swap systemic from 10.1) was resolved on the weekly-summary surface by 10.4 — for the installer dashboard, the resolution is the same pattern (each fragment carries its own `hx-get` + `hx-trigger` on its root section per AC1). **Confirmed: no must-resolve items.** | | |

**Triage summary:**
- **must-resolve-in-this-story:** 0 items
- **safe-during-this-story:** 1 item (HTMX-error UX — defer to follow-up unless trivial in cycle)
- **acceptable-post-this-story:** 3 items (all explicitly scoped to future Epic 11.x work)

The deferred-work.md scan is complete. No overlapping finding blocks 11.1 ready-for-dev.

---

## Project Context Reference

Project artifacts authoritative at story creation time:
- `_bmad-output/planning-artifacts/prd.md` — FR18 (system health indicator), FR19 (peak consumption), FR23 (homeowner credential management, Story 11.3), NFR-P5 (event log query performance, Story 11.2)
- `_bmad-output/planning-artifacts/architecture.md` — `services/audit_log.py` (Story 6.1), `engine/control_loop.py` (Story 8.1 + 9.0d hydrate), `web/templates/installer/dashboard.html` mentioned at line 889
- `_bmad-output/planning-artifacts/ux-design-specification.md` — §"Anomaly Notice (Installer Dashboard)" line 1250; §"Sidebar Navigation (Installer)" line 1280; Journey Flow 5 line 874; Installer dashboard structure line 657
- `_bmad-output/planning-artifacts/epics.md` — Epic 11 starts at line 2249; Story 11.1 AC text starts at line 2257

Stories whose surface 11.1 reads from (no mutation, read-only consumer):
- Epic 5 (5.1) — `StateStore.publish` and `SystemSnapshot` contract
- Epic 7 (7.1) — `SystemOperatingMode` derivation and degradation matrix
- Epic 6 (6.1, 6.2) — `event_log` append-only semantics
- Story 8.1 + 9.0d — `peak_intervals` write + hydrate
- Story 9.3 — `active_constraints.peak_limit_kw`
- Story 9.1 + 9.2 — `device_registry` capability classification

Patterns from Stories 10.1 / 10.2 / 10.3 / 10.4 — all done within the last 7 days; their dev notes are the foremost reference for the HTMX + Alpine + StateStore-driven fragment pattern used here.

---

## Open questions saved for Jordan (post-implementation review or pre-dev sync)

1. **AC4 task #1 — `ComponentState` enum mapping for "DEGRADED" label:** the epic AC says the installer-facing per-device-row label is `DEGRADED`, but the existing `ComponentState` enum at `state.py:52-60` does not have a `degraded` member — only `error`, `unavailable`, `stale`. Confirm whether the rendering should map `ComponentState.error` → display `"DEGRADED"` (the most likely intent given `derive_component_state` returns `error` for any `DegradedDeviceState` with a non-"unavailable" reason), OR whether a new `ComponentState.degraded` member should be added. **Recommendation: rename at the display layer only** (map `error` → `"DEGRADED"` in the installer fragment context); the engine-internal name `error` stays. This is the v1 path. Confirm before merge.

2. **AC7 PEAK_APPROACHING anomaly path A vs path B:** v1 ships with **Path A** (peak-approaching is rendered in the peak tracker label only, NOT as an anomaly type). The epic AC line 2290 lists "device communication failure", "peak limit approached or breached", "constraint enforcement failure", "system restart" as anomaly triggers — but the strict "StateStore-only" rule at line 2291 contradicts the peak-limit trigger (which needs to read constraints + telemetry). Confirm Path A is the intended v1 interpretation.

3. **AC8 dismiss persistence — really per-session?** The epic line 2292 says "dismissible per session". v1 implements this as a module-level Python dict keyed by `session_id`. Process restart clears all dismissals. **Confirm** — alternative interpretations would be: dismiss-until-clear (re-clears on `current = None`), dismiss-forever-this-installer (DB-backed per-user state). The per-session in-memory model is the simplest and matches the literal AC text; confirm it is the intended UX.

4. **AC9 "View in event log" link target — pre-filter URL contract:** 11.1 emits the URL contract (`?type=...&device_id=...&window=...`); 11.2 will implement the consuming page. Confirm the query-string keys are correct (specifically: `type=DECISION,DEVICE` for multi-type vs. `type=DECISION&type=DEVICE` repeated-key syntax). 11.1 uses **comma-separated single-key** because it's more compact and matches the URL aesthetic of UX spec line 1538 ("filter state is carried in URL query params").

5. **AC10 test #2 — DB-query-count instrumentation:** is there an existing aiosqlite query-capture context-manager pattern in the codebase? Quick grep before writing — Story 10.4's tests may already have an analogous pattern (the `test_homeowner_dashboard_render_does_not_query_weekly_summary_table` uses `patch(..., side_effect=AssertionError)` which is a one-method assertion, not a generic counter). The 11.1 test #2 needs a counter, not just an assertion. If no existing pattern, implement a small helper using the `aiosqlite.Connection._execute` interception or a custom row factory. ~30-line helper.

---

## Resolved decisions (2026-05-12, pre-dev sync with Jordan)

All five open questions resolved with the recommended v1 paths:

1. **ComponentState mapping for "DEGRADED" label** — **display-layer rename only**. Map `ComponentState.error` → display string `"DEGRADED"` in the installer fragment context. Engine-internal enum name `error` stays.
2. **PEAK_APPROACHING anomaly path** — **Path A**. `AnomalyType` literal is `{SYSTEM_FAILED, SYSTEM_DEGRADED, DEVICE_ERROR, DEVICE_UNAVAILABLE}` (4 members). Peak tracker label carries `"(approaching)"`/`"(exceeded)"` suffix; anomaly notice does NOT include peak-limit triggers.
3. **Dismiss persistence** — **module-level `_DISMISSED_ANOMALIES: dict[str, AnomalySignature]`** keyed by `session_id`. Process restart clears (correct semantic).
4. **"View in event log" URL query-string format** — **comma-separated single-key** (`?type=DECISION,DEVICE&window=24h`).
5. **DB-query-count instrumentation** — **new helper**: a small aiosqlite-execute-interception context manager in `tests/utils/db_query_counter.py` (or `tests/conftest.py` if it stays small enough).

## Tasks / Subtasks

### Task 1: `EventLogEntry` model + `EventLogRepo.list_recent` (AC6)
- [x] 1.1 Define `EventLogEntry` frozen Pydantic model in `event_log_repo.py` (mirrors `DeviceRegistryEntry` style)
- [x] 1.2 Implement `EventLogRepo.list_recent(limit: int = 5) -> list[EventLogEntry]` ordered by `timestamp DESC, id DESC` (id is load-bearing tie-breaker)
- [x] 1.3 Write unit tests in `tests/unit/storage/repositories/test_event_log_repo.py` (extended): 7 new tests — ordering, limit honored, ties broken by id, empty table returns `[]`, optional NULL columns, frozen-model check, zero-limit guard

### Task 2: Core `installer_anomaly` module (AC7 + AC8)
- [x] 2.1 Created `src/open_ems/services/installer_anomaly.py` with `Severity` Literal, `AnomalyType` Literal (4 members per Q2), `AnomalySignature` frozen dataclass (hashable frozenset of types)
- [x] 2.2 Defined `_SEVERITY_MAPPING` (anomaly type → severity) and aggregated-summary logic in `_build_anomaly_summary`
- [x] 2.3 Implemented `detect_anomaly_from_snapshot(snapshot: SystemSnapshot) -> AnomalySignature | None` — pure-functional, snapshot-only I/O footprint
- [x] 2.4 Implemented cold-start special-case: `if snapshot.sequence_id == 0: summary = "System is starting up"` (per R5)
- [x] 2.5 Implemented `evaluate_dismiss_state(current, dismissed) -> Literal["render", "suppress", "no_anomaly"]` per the R2 elevation table
- [x] 2.6 Implemented module-level `_DISMISSED_ANOMALIES: dict[str, AnomalySignature]` + accessor functions (`dismiss_signature`, `get_dismissed_signature`, `clear_dismissed_signature`, `_reset_dismiss_state_for_tests`)
- [x] 2.7 Wrote `tests/unit/services/test_installer_anomaly.py`: 28 tests covering detection per operating mode, multi-type aggregation, severity ordering, link_query format, dismiss-state machine elevation table rows, exhaustiveness over the AnomalyType literal, structural-discipline (DB repos not called), frozen-dataclass invariant

### Task 3: Installer fragment builders in `state_serialization.py`
- [x] 3.1 `build_installer_health_indicator_context(snapshot: SystemSnapshot) -> dict` (AC3) — direct SystemOperatingMode → display mapping with defensive `.get()` fallback
- [x] 3.2 `build_installer_device_row_context(snapshot, registry_entries) -> dict` (AC4 — produces 4 rows in canonical order: grid_meter, inverter, battery, ev_charger; component_state + capability_badge as STRUCTURALLY DISTINCT dict keys)
- [x] 3.3 `build_installer_peak_tracker_context(current_month_peak_kw, peak_limit_kw) -> dict` (AC5 — derives "approaching" / "exceeded" / "no data" / "not configured" labels with the 0.9 approach ratio)
- [x] 3.4 `build_installer_event_log_preview_context(entries) -> dict` (AC6 — server-side Europe/Brussels timezone formatting via stdlib zoneinfo; emits `data-utc` + display-string pair per row; 80-char summary truncation)
- [x] 3.5 `build_installer_anomaly_notice_context(snapshot, dismissed) -> dict` (AC7 + AC8 — calls `detect_anomaly_from_snapshot` + `evaluate_dismiss_state`)
- [x] 3.6 Builder unit tests covered by the fragment route tests in Task 4 (each route exercises one builder end-to-end with assertions on the rendered HTML)

### Task 4: Fragment routes in `web/routes/fragments.py`
- [x] 4.1 Added `get_event_log_repo` and `get_device_repo` dependencies in `web/dependencies.py` (mirror `get_energy_repo` pattern)
- [x] 4.2 `GET /fragments/installer/health-indicator` (require_installer gate)
- [x] 4.3 `GET /fragments/installer/device-rows`
- [x] 4.4 `GET /fragments/installer/peak-tracker`
- [x] 4.5 `GET /fragments/installer/event-log-preview`
- [x] 4.6 `GET /fragments/installer/anomaly-notice` (calls `evaluate_dismiss_state` with `get_dismissed_signature(user.session_id)`)

### Task 5: `POST /actions/dismiss-anomaly` route in `web/routes/actions.py`
- [x] 5.1 Route handler: require_installer + read snapshot, call `detect_anomaly_from_snapshot`, store via `dismiss_signature(user.session_id, current)`, return empty anomaly-notice section via outerHTML swap
- [x] 5.2 Idempotent on no-current-anomaly (returns empty section unconditionally)
- [x] 5.3 Tests included in `test_installer_dashboard_fragments.py` (dismiss + dismiss-then-elevate round-trip + idempotent no-anomaly)

### Task 6: Installer fragment templates (5 NEW HTML files)
- [x] 6.1 `web/templates/fragments/installer/health-indicator.html`
- [x] 6.2 `web/templates/fragments/installer/device-rows.html`
- [x] 6.3 `web/templates/fragments/installer/peak-tracker.html`
- [x] 6.4 `web/templates/fragments/installer/event-log-preview.html`
- [x] 6.5 `web/templates/fragments/installer/anomaly-notice.html` (carries `hx-get` + `hx-trigger="every 10s"` on root section per 10.4 polling-after-swap pattern)

### Task 7: Dashboard.html installer branch + sidebar
- [x] 7.1 Replaced placeholder `<p>Full dashboard UI implemented in Epic 11.</p>` with installer shell (5 HTMX-bound sections + sidebar)
- [x] 7.2 Added `installerSidebar` Alpine factory to `{% block scripts %}` (`open: false` state for mobile hamburger)
- [x] 7.3 Tests in `tests/unit/web/test_installer_dashboard_route.py` (new file): redirect homeowner→/homeowner/dashboard, redirect unauth→/login, shell renders 5 HTMX-bound sections, route under 2s
- [x] 7.4 Test: shell route triggers 0 DB queries on the dashboard's data surface (peak_intervals/event_log/device_registry); uses the new `tests/utils/db_query_counter.py` helper

### Task 8: CSS additions to `web/static/open-ems.css`
- [x] 8.1 `.installer-dashboard` grid (220px + 1fr; collapses to single column ≤640px)
- [x] 8.2 `.installer-sidebar` + active-item left-border + disabled-link styling + mobile hamburger
- [x] 8.3 `.health-indicator__badge--{normal,degraded,failed}` 3-variant
- [x] 8.4 `.device-row` grid + `.device-row__component-state--{active,degraded,unavailable}` + `.device-row__capability-badge--{full,reduced,unsupported,unknown}` (DISTINCT classes — AC4 invariant)
- [x] 8.5 `.installer-peak-tracker__value` monospace
- [x] 8.6 `.installer-event-log-preview__row` + `.event-badge--{decision,device,system,constraint,installer}` 5-variant
- [x] 8.7 `.installer-anomaly-notice--{warn,fail}` 2-variant (NOT degraded-mode slate per UX spec line 1256)
- [x] 8.8 Added missing palette tokens (`--color-pass`, `--color-pass-bg`, `--color-warn`, `--color-warn-bg`, `--color-fail`, `--color-fail-bg`, `--color-accent-subtle`) — see Completion Notes for divergence from the dev-notes "no new tokens" claim

### Task 9: Integration tests in `tests/integration/web/test_installer_dashboard_e2e.py`
- [x] 9.1 Normal snapshot → no anomaly notice, health=NORMAL
- [x] 9.2 Failed snapshot → FAIL anomaly notice with `?type=SYSTEM&window=24h` link
- [x] 9.3 Dismiss + elevate round-trip: POST dismiss → next GET returns empty → publish FAIL snapshot → next GET returns full FAIL notice
- [x] 9.4 Sidebar Setup link present in shell
- [x] 9.5 A11y placeholder (xfail+skipif per Story 9.x precedent)

### Task 10: Test infrastructure + quality gates
- [x] 10.1 Added `tests/utils/db_query_counter.py` aiosqlite execute-interception helper (per Q5 resolution)
- [x] 10.2 `uv run ruff check .` → clean (10 auto-fixes applied)
- [x] 10.3 `uv run ruff format --check .` → clean (5 files reformatted)
- [x] 10.4 `uv run mypy src` → 103 source files clean
- [x] 10.5 `uv run pytest` → 1716 passed (1628 baseline + 88 net new; target was ≥1678), 2 skipped (1 baseline + 1 new a11y), 8 xfailed (7 baseline + 1 new a11y placeholder)
- [x] 10.6 `uv.lock` unchanged (no new Python dependencies)

## Dev Agent Record

### Implementation Plan

Built bottom-up: storage layer → service module → builders → routes → templates → shell → CSS → tests. Each layer landed before the consumer above it so smoke tests at each step caught issues early.

The pure-functional discipline of `detect_anomaly_from_snapshot` and the five `build_installer_*_context` functions is the structural enforcement of the AC10 "StateStore-only for anomaly detection" rule — the function signatures `(SystemSnapshot, ...) -> dict` encode the contract; the AC10 #1 test patches all three DB repos with `side_effect=AssertionError` and asserts none are invoked during detection.

### Debug Log

1. **Initial test failure: `RuntimeError("Database already initialized")`** — `tests/unit/web/test_installer_dashboard_fragments.py` declared both `initialized_db` and `session_repo` fixtures on the same test functions; the `session_repo` fixture already calls `init_database`, and the second call hit the guard. Fix: dropped the redundant `initialized_db` parameter from all 5 affected tests; the `session_repo` fixture handles DB init alone.

2. **`ActiveConstraints(actor="installer", ...)` rejected by pydantic `extra_forbidden`** — initial fixture passed `actor` as a kwarg, but the real `ActiveConstraints` model has no `actor` field (the audit-actor metadata lives in `config_audit_log`, not in the immutable constraint snapshot). Fix: removed `actor` from the kwargs.

3. **`_StubProvider` failed `isinstance(provider, ActiveConstraintsProvider)` check** — the dependency at `dependencies.py:113-126` does an isinstance check before calling `.get()`. Fix: made `_StubProvider` subclass `ActiveConstraintsProvider` (init skipped to avoid the parent's lock/state setup).

4. **mypy `Incompatible types in assignment` on the device-row builder** — mypy's flow-scope leaked `entry: DeviceRegistryEntry` from the first for-loop (lines 699-704) into the second loop (line 713), where `role_to_entry.get(role)` returns `DeviceRegistryEntry | None`. Fix: renamed the inner loop variable to `row_entry`.

5. **Jinja autoescape encodes `&` as `&amp;`** in href attributes. The anomaly-notice link assertion needed to accept both forms.

### Completion Notes

**All 15 ACs satisfied.** Key implementation decisions:

- **AC8 elevation table is enforced structurally** by `evaluate_dismiss_state`: the function returns `Literal["render", "suppress", "no_anomaly"]` and 9 transition-table rows are covered by unit tests. The de-escalation rule (FAIL → WARN with covered types stays suppressed) is documented in the function docstring and tested explicitly.

- **AC10 #2 (zero DB queries on shell render)** is verified by the new `tests/utils/db_query_counter.py` helper — it patches `aiosqlite.Connection.execute` and counts calls. The shell route does trigger ~2-3 queries for session auth (sessions + users tables), but the strict "no peak_intervals / event_log / device_registry reads" rule is asserted via substring check on the captured SQL.

- **AC4 distinct DOM elements** are verified both structurally (the builder returns separate `component_state_display` and `capability_badge_display` dict keys) and via DOM parse (the test asserts both CSS classes are present and that no class merges them).

- **Cold-start path (R5)** — `detect_anomaly_from_snapshot` checks `snapshot.sequence_id == 0` and overrides the summary to "System is starting up". Without this, the default StateStore snapshot (operating_mode=degraded by default per the constructor) would show "System is operating with reduced functionality" during the ~10s lifespan-to-first-tick window, alarming any installer watching the dashboard come up.

- **Path A (PEAK_APPROACHING omitted from anomalies)** — the `AnomalyType` literal has 4 members, NOT 5. The peak tracker label carries `(approaching)` / `(exceeded)` suffix; the anomaly notice covers SystemOperatingMode + device-state anomalies only. This preserves the strict AC7 "StateStore-only" rule.

- **`tests/utils/db_query_counter.py` is the new generic helper** for "this code path must not trigger DB I/O" assertions. Future stories can reuse it for analogous structural-discipline tests.

- **Divergence from "no new tokens" dev-notes claim:** the create-story dev notes asserted "the existing design tokens cover the installer palette; no new tokens added". The reality at implementation time was that `--color-pass`, `--color-fail`, `--color-warn`, `--color-accent-subtle` did NOT exist in `open-ems.css` (only the homeowner-focused palette of accent + degraded slate). I added these tokens at the top of the new installer CSS block. They are scoped to the installer surface only — the homeowner palette continues to avoid them per UX spec §"degraded stays slate, never amber". The dev-notes mismatch was a foreseen risk in the story (the architecture doc references Tailwind which is NOT used; the actual palette is the hand-authored bundle from Story 10.1 Path B).

- **`test_event_log_preview_renders_5_most_recent`** asserts the `event-0` through `event-4` substrings appear and `event-5`/`event-6` do not. This catches both the limit (5) and the ordering (newest-first by timestamp) in one test.

- **Story 11.2 contract emitted**: the URL formats `?device_id=...`, `?type=SYSTEM&window=24h`, `?type=DEVICE,SYSTEM&window=24h` are the contract Story 11.2's event-log page will parse. Comma-separated single-key format per Q4.

**Status (Story 11.1):** ready-for-dev → in-progress → review.

**No deferred findings introduced.** Three pre-existing deferred items from R7 (HTMX-error UX, structured-log volume design, repo transaction discipline) remain in `deferred-work.md` with their existing classifications.

### Awaits

A7 review-closure gate via `bmad-code-review`. Would be the **seventh** proof-of-A7-enforcement target after 9-Y-a, 9-X, 10.1, 10.2, 10.3, 10.4 — and the **first** Epic 11 story to complete.

## File List

### New files
- `src/open_ems/services/installer_anomaly.py` — anomaly detection + dismiss state machine (NEW module)
- `src/open_ems/web/templates/fragments/installer/health-indicator.html`
- `src/open_ems/web/templates/fragments/installer/device-rows.html`
- `src/open_ems/web/templates/fragments/installer/peak-tracker.html`
- `src/open_ems/web/templates/fragments/installer/event-log-preview.html`
- `src/open_ems/web/templates/fragments/installer/anomaly-notice.html`
- `tests/utils/__init__.py` — new package
- `tests/utils/db_query_counter.py` — aiosqlite query-count context manager
- `tests/unit/services/test_installer_anomaly.py` — 28 tests
- `tests/unit/web/test_installer_dashboard_route.py` — 5 tests
- `tests/unit/web/test_installer_dashboard_fragments.py` — 22 tests
- `tests/integration/web/test_installer_dashboard_e2e.py` — 5 tests (4 + 1 a11y skip)

### Modified files
- `src/open_ems/storage/repositories/event_log_repo.py` — added `EventLogEntry` frozen Pydantic model + `list_recent` method
- `src/open_ems/web/dependencies.py` — added `get_event_log_repo` + `get_device_repo` dependencies
- `src/open_ems/web/routes/fragments.py` — added 5 installer fragment routes
- `src/open_ems/web/routes/actions.py` — added `POST /actions/dismiss-anomaly`
- `src/open_ems/web/state_serialization.py` — added 5 `build_installer_*_context` builders + display-mapping tables
- `src/open_ems/web/templates/dashboard.html` — replaced installer branch placeholder with full shell + `installerSidebar` Alpine factory
- `src/open_ems/web/static/open-ems.css` — Story 11.1 installer surface CSS (~330 new lines including new palette tokens)
- `tests/unit/storage/repositories/test_event_log_repo.py` — added 7 tests for `list_recent` + `EventLogEntry`

## Change Log

- 2026-05-12: Story 11.1 implementation complete. Status: ready-for-dev → in-progress → review. 88 net new tests (1716 total passing); ruff + format + mypy clean; uv.lock unchanged. First Epic 11 story delivered.

## Story Completion Status

**Status:** ready-for-dev

**A2-triggered:** True (T1 — anomaly notice dismiss/elevation state machine)

**A1 enforcement:** all seven R1–R7 artifacts authored and substantive per the workflow contract.

**Completion note:** Ultimate context engine analysis completed — comprehensive developer guide created. The story is unblocked for `bmad-dev-story`. Five open questions saved for Jordan (post-cycle or pre-sync). Awaits A7 review-closure gate via `bmad-code-review` (would be the seventh proof-of-enforcement story after 9-Y-a, 9-X, 10.1, 10.2, 10.3, 10.4 — and the first Epic 11 story).
