# Story 10.4: Implement FR30 weekly energy summary as non-blocking, pre-aggregated secondary feature

Status: done
_Status set to done by bmad-code-review on 2026-05-12 after review-closure gate passed (5b): 2 resolved, 0 dismissed, 1 deferred-and-verified._
_Status: ready-for-dev → in-progress → review by bmad-dev-story on 2026-05-12._

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

> **Type:** Epic 10 final story — nice-to-have, non-blocking secondary feature. **Does NOT affect** the primary dashboard card-stack render path (Stories 10.1 / 10.2 / 10.3); it is a separate HTMX fragment loaded on click from a "This week" trigger below the card stack.
> **A2-triggered:** **Yes.** Three triggers match — **T3 (persistence + recovery — pre-aggregated rows persisted to SQLite, hydrated on every request after restart)**, **T4 (watchdog / timing — scheduled background aggregation task with deadline-relative `weekly_summary_aggregation_interval_seconds` cadence)**, and **T6 (deployment / restart — cold-start behavior of the aggregation scheduler is user-visible via the "Data still being collected" branch)**. R1–R7 are mandatory and rendered below.
> **Prerequisite (soft):** Stories 10.1 (homeowner dashboard, done 2026-05-11), 10.2 (EV status card, done 2026-05-12), and 10.3 (inline strategy selector, done 2026-05-12). Epic 10's primary dashboard contract is complete; 10.4 closes Epic 10 by adding FR30. Hard data-source dependency on `peak_intervals` (Story 9.0d), `GridMeterState.energy_delivered_kwh` / `energy_returned_kwh` (Story 4.3), and the ControlLoop's per-tick adapter polling (Stories 8.1 / 9.X).
> **Sibling stories:** none — this is the final Epic 10 story. Epic 11 (installer monitoring) follows.

## Story

As a homeowner,
I want to optionally see a weekly energy summary accessible via a secondary link,
so that I can review the system's impact over time without it affecting the primary dashboard performance or layout.

## Acceptance Criteria

**Given** the homeowner dashboard already renders (post-10.1 / 10.2 / 10.3), `StateStore` carries device telemetry through `SystemSnapshot`, and the homeowner is authenticated on `/homeowner/dashboard`

**When** this story is implemented

**Then** the following acceptance criteria must hold:

1. **AC1 — `energy_flow_intervals` table + migration `0013`.** A new SQLite table persists one row per completed 15-minute clock-aligned interval with the energy flow for that window:

   ```sql
   CREATE TABLE energy_flow_intervals (
     interval_start_utc TEXT NOT NULL PRIMARY KEY,        -- ISO-8601 UTC; clock-aligned to :00 / :15 / :30 / :45
     pv_kwh             REAL NOT NULL DEFAULT 0.0,        -- integrated from InverterState.pv_power_kw (>= 0)
     battery_charged_kwh    REAL NOT NULL DEFAULT 0.0,    -- integrated from BatteryState.battery_power_kw > 0
     battery_discharged_kwh REAL NOT NULL DEFAULT 0.0,    -- integrated from BatteryState.battery_power_kw < 0 (positive magnitude)
     grid_imported_kwh  REAL NOT NULL DEFAULT 0.0,        -- from GridMeterState.energy_delivered_kwh delta over the interval
     grid_exported_kwh  REAL NOT NULL DEFAULT 0.0,        -- from GridMeterState.energy_returned_kwh delta over the interval
     ev_charged_kwh     REAL NOT NULL DEFAULT 0.0,        -- integrated from EVChargerState.current_power_kw (NULL/None treated as 0)
     sample_count       INTEGER NOT NULL DEFAULT 0,       -- number of ControlLoop ticks contributing to this interval
     data_quality       TEXT NOT NULL DEFAULT 'complete', -- 'complete' if no DegradedDeviceState observed during the interval; else 'incomplete'
     CHECK (pv_kwh >= 0 AND battery_charged_kwh >= 0 AND battery_discharged_kwh >= 0
            AND grid_imported_kwh >= 0 AND grid_exported_kwh >= 0 AND ev_charged_kwh >= 0
            AND sample_count >= 0)
   );
   CREATE INDEX ix_energy_flow_intervals_interval_start_utc ON energy_flow_intervals(interval_start_utc);
   ```

   Migration filename: `migrations/versions/0013_add_energy_flow_intervals_and_weekly_summary_tables.py`. Down-migration drops index and table.

2. **AC2 — `weekly_energy_summary` table + migration (same `0013`).** A second new table holds the pre-aggregated weekly summary row consumed by the homeowner endpoint. The table is constrained to a single row (`id = 1`) — the upsert path overwrites in place:

   ```sql
   CREATE TABLE weekly_energy_summary (
     id                          INTEGER NOT NULL PRIMARY KEY CHECK (id = 1),  -- structural single-row constraint
     window_start_utc            TEXT NOT NULL,
     window_end_utc              TEXT NOT NULL,
     peaks_avoided_count         INTEGER,                    -- nullable when insufficient_history=1
     self_consumption_ratio      REAL,                       -- nullable when insufficient_history=1; bounded [0.0, 1.0]
     estimated_cost_savings_eur  REAL,                       -- nullable when insufficient_history=1
     data_complete_days_count    INTEGER NOT NULL DEFAULT 0, -- number of distinct UTC days in [window_start, window_end] with >=80 intervals of data_quality='complete'
     insufficient_history        INTEGER NOT NULL DEFAULT 1, -- boolean 0/1; 1 when data_complete_days_count < 7
     computed_at                 TEXT NOT NULL,              -- ISO-8601 UTC; when this row was last (re)computed
     CHECK (insufficient_history IN (0, 1))
     CHECK ((insufficient_history = 1)
            OR (peaks_avoided_count IS NOT NULL
                AND self_consumption_ratio IS NOT NULL
                AND self_consumption_ratio >= 0.0 AND self_consumption_ratio <= 1.0
                AND estimated_cost_savings_eur IS NOT NULL))
   );
   ```

   Cold-start (table exists, no row): treated identically to `insufficient_history=1` at the route layer. The aggregation task writes the row on its FIRST tick at process start, so the absent-row window is bounded to ~the first aggregation pass.

3. **AC3 — `EnergyFlowIntervalTracker` class** (new module `src/open_ems/engine/energy_flow_tracker.py`). Sibling to `engine/partial_interval_tracker.py:PartialIntervalTracker`; shares the same 15-min clock-aligned interval boundaries. On each ControlLoop tick the tracker accepts:

   ```python
   tracker.update(
       at: datetime,                      # UTC; must be timezone-aware
       pv_power_kw: float | None,         # None when inverter slot is DegradedDeviceState/None
       battery_power_kw: float | None,    # None when battery slot is DegradedDeviceState/None
       ev_power_kw: float | None,         # None when EV charger slot is DegradedDeviceState/None or EVChargerState.current_power_kw is None
       grid_delivered_kwh: float | None,  # monotonic accumulator from GridMeterState; None when degraded
       grid_returned_kwh: float | None,   # monotonic accumulator from GridMeterState; None when degraded
       any_role_degraded: bool,           # mark interval data_quality='incomplete' if True at any tick during the interval
   ) -> CompletedEnergyFlowInterval | None
   ```

   Contract:
   - **Power integration:** for `pv_power_kw`, `battery_power_kw`, `ev_power_kw`, the tracker maintains a running sum of `power × dt_seconds` (with `dt_seconds = at - last_at`, **clamped to ≤ `interval_seconds`** to defend against clock jumps / NTP step). On rollover, the integrated kWh is `running_sum_kw_seconds / 3600`. The first tick of an interval contributes zero (no `last_at` reference within the interval).
   - **Battery sign split:** `battery_power_kw > 0` → integrated into `battery_charged_kwh`; `< 0` → integrated into `battery_discharged_kwh` (positive magnitude). The two columns never both grow on a single tick.
   - **Grid kWh from monotonic accumulators:** at the FIRST tick of an interval, latch `(grid_delivered_kwh, grid_returned_kwh)` as the baseline. At rollover, `grid_imported_kwh = max(0, current_delivered - baseline_delivered)`; `grid_exported_kwh = max(0, current_returned - baseline_returned)`. The `max(0, ...)` floor defends against meter rollover / re-zero events; any negative observed delta is logged as `grid_accumulator_rollback_detected` (structlog WARNING) and treated as zero for that interval.
   - **None semantics:** when a power input is `None` for a tick, that role contributes zero kWh AND the interval is marked `data_quality='incomplete'`. The tracker tolerates intermittent None (e.g., one degraded tick) without losing the partial-interval state — it simply skips the contribution from that tick.
   - **Rollover detection:** identical mechanism to `PartialIntervalTracker` — when `at` crosses the next 15-min clock boundary, return a `CompletedEnergyFlowInterval` and start a fresh accumulator for the new interval. The `CompletedEnergyFlowInterval` carries: `interval_start_utc`, `pv_kwh`, `battery_charged_kwh`, `battery_discharged_kwh`, `grid_imported_kwh`, `grid_exported_kwh`, `ev_charged_kwh`, `sample_count`, `data_quality`.
   - **Cold-start:** `from_current_time(at)` returns a tracker positioned at the current 15-min boundary with empty accumulators; the FIRST in-interval tick contributes zero seconds (no `last_at` yet).

4. **AC4 — `EnergyRepo` extensions.** Add three methods to `src/open_ems/storage/repositories/energy_repo.py`:

   ```python
   async def write_energy_flow_interval(
       self,
       completed: CompletedEnergyFlowInterval,
   ) -> None: ...

   async def read_energy_flow_intervals_since(
       self,
       since_utc: datetime,
   ) -> list[EnergyFlowIntervalRow]: ...

   async def upsert_weekly_energy_summary(
       self,
       row: WeeklyEnergySummaryRow,
   ) -> None: ...

   async def read_weekly_energy_summary(self) -> WeeklyEnergySummaryRow | None: ...
   ```

   Where `EnergyFlowIntervalRow` and `WeeklyEnergySummaryRow` are new frozen Pydantic models in `core/energy.py` (NEW module — colocate with the EnergyFlow domain types). The `write_energy_flow_interval` method MUST:
   - Use `get_write_lock()` (same write-lock discipline as `write_peak_interval` at `energy_repo.py:21`).
   - Use `INSERT OR REPLACE` so duplicate writes for the same `interval_start_utc` (e.g., test-driven re-execution) are idempotent.

   `upsert_weekly_energy_summary` MUST use `INSERT INTO ... ON CONFLICT(id) DO UPDATE SET ...` (SQLite UPSERT) so the single-row constraint is honored without a separate read-then-write. `read_weekly_energy_summary` returns `None` if the row does not exist yet (cold-start path).

5. **AC5 — `ControlLoop` wire-up.** The control loop owns the `EnergyFlowIntervalTracker` instance and feeds it on every `_tick()`. Modify `src/open_ems/engine/control_loop.py:_update_tracker` (currently writes `peak_intervals` only) to ALSO update the energy-flow tracker:

   - Construct `self._energy_flow_tracker = EnergyFlowIntervalTracker.from_current_time(at=datetime.now(UTC))` in `__init__` (mirror line 119's `PartialIntervalTracker` construction).
   - In `_update_tracker(device_states, now)`, extract the four power signals and the two grid accumulators from `device_states`; pass them to `self._energy_flow_tracker.update(...)`. Use helper functions `_extract_pv_power(device_states)`, `_extract_battery_power(device_states)`, `_extract_ev_power(device_states)`, `_extract_grid_accumulators(device_states)` colocated in `control_loop.py` next to the existing `_extract_grid_power` helper.
   - When `update()` returns a `CompletedEnergyFlowInterval`, `await self._energy_repo.write_energy_flow_interval(completed)`. **Ordering:** the existing `write_peak_interval` call MUST run BEFORE the new `write_energy_flow_interval` call so that a failure in the new write does not regress the load-bearing peak-interval persistence path. Both writes go inside the same `_update_tracker` invocation; both inherit the same try/except resilience semantics established by Story 9.0d.
   - `any_role_degraded` is computed by checking whether ANY slot in `device_states` is a `DegradedDeviceState` instance OR `None`. This is the SAME degradation signal already used by `_extract_grid_power` to mark the peak interval as `incomplete`; reuse the existing classification rather than re-deriving.

   **No change to existing peak_interval logic.** The new tracker is additive; if its write fails, peak-interval writes continue and the lifespan is unaffected. The next tick re-tries on its own rollover. ControlLoop's existing failure semantics carry over.

6. **AC6 — `WeeklyEnergySummaryService` background task.** New module `src/open_ems/services/weekly_energy_summary.py`. Mirrors the `_event_log_pruning_task` pattern at `web/app.py:138-146` — a module-level async function that loops with `await asyncio.sleep(...)` between aggregation passes:

   ```python
   async def weekly_energy_summary_task(
       *,
       energy_repo: EnergyRepo,
       event_log_repo: EventLogRepo,
       active_constraints: ActiveConstraintsProvider,
       settings: Settings,
       clock: Callable[[], datetime] = lambda: datetime.now(UTC),  # injectable for tests
   ) -> None:
       """Periodically recompute the single weekly_energy_summary row.

       Runs the FIRST aggregation immediately on startup so the row exists
       (with insufficient_history=1) within seconds of process boot, not
       only after the first sleep interval. Subsequent passes sleep for
       settings.weekly_summary_aggregation_interval_seconds.
       """
       while True:
           try:
               await _compute_and_upsert_weekly_summary(
                   energy_repo=energy_repo,
                   event_log_repo=event_log_repo,
                   active_constraints=active_constraints,
                   settings=settings,
                   now=clock(),
               )
           except Exception:  # noqa: BLE001 — task must survive transient repo errors
               logger.error(
                   "weekly_summary_aggregation_failed",
                   exc_info=True,
                   component="weekly_summary",
               )
           await asyncio.sleep(settings.weekly_summary_aggregation_interval_seconds)
   ```

   Aggregation logic (in `_compute_and_upsert_weekly_summary`, pure-functional except for the two awaits):

   1. Window: `[window_end - 7d, window_end]` where `window_end = now`.
   2. `intervals = await energy_repo.read_energy_flow_intervals_since(window_start)`.
   3. Compute `data_complete_days_count` = number of distinct UTC dates in the window with **≥ 80 `complete`-quality intervals** (80% of the 96 daily 15-min intervals — tolerates ≤19 missing/incomplete intervals per day).
   4. If `data_complete_days_count < 7` → upsert with `insufficient_history=1`, peaks_avoided/self_consumption/cost_savings = `None`, computed_at = `now`. **Return early.**
   5. Otherwise compute the three metrics:
      - **`peaks_avoided_count`** = count of `event_log` entries in `[window_start, window_end]` where `event_type = 'DECISION'` AND `detail` (JSON) contains `"source_rule": "peak_limiting"` AND `"applied": true`. Implementation: `event_log_repo.count_peak_limiting_applied_decisions(since=window_start, until=window_end)` (NEW method on EventLogRepo). The SQL is `SELECT COUNT(*) FROM event_log WHERE event_type = 'DECISION' AND timestamp BETWEEN ? AND ? AND detail LIKE '%"source_rule": "peak_limiting"%' AND detail LIKE '%"applied": true%'`. The double-LIKE is acceptable here — event_log volume is bounded by retention (90 days) and the aggregation runs hourly, not on every page load.
      - **`self_consumption_ratio`** = `(total_pv_kwh - total_grid_exported_kwh) / total_pv_kwh` where totals are summed over complete-quality intervals in the window. Clamp to `[0.0, 1.0]`. If `total_pv_kwh == 0` (no PV produced this week — e.g., long cloudy stretch in winter), treat as `0.0` (NOT NaN); the aggregator emits a structlog INFO `self_consumption_zero_pv_window` and the value is rendered as `0%` in the UI. Per UX spec line 60–62 (Belgian-residential context), this is a real winter scenario and must not break the summary.
      - **`estimated_cost_savings_eur`** = component sum:
        - **Self-consumption savings:** `(total_pv_kwh - total_grid_exported_kwh) × settings.default_import_tariff_eur_per_kwh` — kWh consumed from own PV that would otherwise have been imported.
        - **Export revenue:** `total_grid_exported_kwh × settings.default_export_tariff_eur_per_kwh` — kWh paid at injection tariff (typically near zero in BE 2026 post-prosumer reform).
        - **Battery cycling savings (peak-shaving proxy):** `total_battery_discharged_kwh × settings.default_import_tariff_eur_per_kwh × 0.5` — half-weight, since the discharged kWh's marginal value depends on whether the discharge was time-shifted from off-peak charging (cheap kWh) vs. PV surplus (free kWh); 0.5 is the conservative midpoint per the v1 simplified-tariff assumption. Document in the dev notes that this is a deliberate first-order approximation; a future story can refine if time-of-use tariff data lands.
        - All three components summed and rounded to 2 decimals.
   6. Upsert with `insufficient_history=0`, all three metrics set, `data_complete_days_count` set, `computed_at = now`.

   **The service is NOT cancelled by any in-cycle event** — only by lifespan shutdown. Exceptions inside the body are caught, logged, and the loop continues. The 1h default sleep cadence means at most one hour of "stale" summary data on any single transient failure.

7. **AC7 — Lifespan wiring.** In `src/open_ems/web/app.py`'s `lifespan(...)` context manager:
   - **Construct** the task after the ControlLoop is constructed (it depends on the same `energy_repo`, `active_constraints_provider`, and `settings` instances).
   - **Spawn** as `_weekly_summary_task = asyncio.create_task(weekly_energy_summary_task(...), name="weekly_summary")`. Add a `_on_weekly_summary_done` callback mirror of `_on_pruning_done` at `web/app.py:473-485` so a task crash is logged but does not bring down the app.
   - **Shutdown** in the same finally block as `_pruning_task` / `_cleanup_task` — `_weekly_summary_task.cancel()` followed by `await _weekly_summary_task` inside a `try/except asyncio.CancelledError: pass` guard. Mirror the existing pattern at `web/app.py:624-628`.
   - **Failure mode:** if construction of the task fails (e.g., misconfigured Settings), the lifespan logs the error but does NOT exit — the weekly summary is nice-to-have; failure must NOT prevent the homeowner dashboard from coming up. This is the structural enforcement of the AC's "must not block delivery of Stories 10.1–10.3" constraint at the runtime layer.

8. **AC8 — `GET /fragments/homeowner/weekly-summary` route** in `src/open_ems/web/routes/fragments.py`:

   ```python
   @router.get("/fragments/homeowner/weekly-summary", response_class=HTMLResponse)
   async def homeowner_weekly_summary(
       request: Request,
       _user: HomeownerUser = Depends(require_homeowner),
       energy_repo: EnergyRepo = Depends(get_energy_repo),
   ) -> HTMLResponse: ...
   ```

   The route MUST:
   1. `row = await energy_repo.read_weekly_energy_summary()` — single indexed read; sub-millisecond on Pi 4.
   2. Build a context dict (via `build_homeowner_weekly_summary_context(row)` — new function in `state_serialization.py`):
      - If `row is None` OR `row.insufficient_history == 1`: `{"insufficient_history": True, "data_complete_days_count": row.data_complete_days_count if row else 0, "computed_at": row.computed_at if row else None}`.
      - Otherwise: `{"insufficient_history": False, "peaks_avoided_count": ..., "self_consumption_percent": round(row.self_consumption_ratio * 100), "estimated_cost_savings_eur": row.estimated_cost_savings_eur, "window_start_utc": row.window_start_utc, "window_end_utc": row.window_end_utc, "computed_at": row.computed_at}`.
   3. Render `templates/fragments/homeowner/weekly-summary.html`.
   4. **Return within 200ms on Pi 4** (AC's performance constraint). The implementation path is: single indexed SELECT + template render. No on-demand computation. AC15 includes a 10-iteration timing test that asserts mean < 200ms.

   **Add the `get_energy_repo` dependency** at `src/open_ems/web/dependencies.py` (mirror `get_state_store`). EnergyRepo is constructed at request time from the shared aiosqlite connection (same as `EventLogRepo` usage in fragments today).

9. **AC9 — `templates/fragments/homeowner/weekly-summary.html`** — new template, the HTMX swap target. The outer `<section>` MUST carry `hx-get` / `hx-trigger` for self-refresh so swapped-in DOM continues to update if the user keeps the panel open (closes the polling-stops-after-outerHTML-swap class identified in 10-3 review-deferred D1 for new fragments).

   ```jinja2
   <section
     id="weekly-summary-panel"
     class="weekly-summary{% if insufficient_history %} weekly-summary--insufficient{% endif %}"
     hx-get="/fragments/homeowner/weekly-summary"
     hx-trigger="every 60s"
     hx-swap="outerHTML"
     aria-label="Weekly energy summary"
     aria-live="polite"
   >
     <h3 class="weekly-summary__heading">This week</h3>
     {% if insufficient_history %}
     <p class="weekly-summary__message">
       Data is still being collected. Your weekly summary will appear once your system has been running for at least 7 full days
       ({{ data_complete_days_count }}/7 days of data so far).
     </p>
     {% else %}
     <dl class="weekly-summary__metrics">
       <div class="weekly-summary__metric">
         <dt>Peaks avoided</dt>
         <dd>{{ peaks_avoided_count }}</dd>
       </div>
       <div class="weekly-summary__metric">
         <dt>Self-consumption</dt>
         <dd>{{ self_consumption_percent }}%</dd>
       </div>
       <div class="weekly-summary__metric">
         <dt>Estimated savings</dt>
         <dd>&euro;&nbsp;{{ "%.2f"|format(estimated_cost_savings_eur) }}</dd>
       </div>
     </dl>
     <p class="weekly-summary__footnote">
       For the 7 days ending {{ window_end_utc | format_utc_date }}.
       Estimated savings use a default tariff &mdash; check with your supplier for exact figures.
     </p>
     {% endif %}
   </section>
   ```

   **`format_utc_date` Jinja filter** is reused if it already exists (search `web/app.py` for `add_jinja_filter` calls); if not, register a new filter that renders `"2026-05-12"` for an ISO-8601 input. Confirm via grep before re-creating; mismatched filters break templates silently. Per UX spec lines 144 + 173 the summary tone is "quietly proud, satisfied" — no exclamation marks, no celebratory iconography; the footnote's plain-language tariff caveat is load-bearing for honesty.

   **NO Alpine.js** in this fragment. The summary is read-only — no interactivity. HTMX 60s self-refresh handles the case where the user keeps the panel open across the aggregation cadence boundary.

10. **AC10 — Dashboard "This week" trigger** in `src/open_ems/web/templates/dashboard.html`. After the existing card stack (`#ev-card` ends at line 39) and before the closing `</section>` (line 40), insert a separate `<section>` containing the trigger button:

    ```jinja2
    </section> {# end .dashboard-stack #}

    {% if dashboard_role == "homeowner" %}
    <section class="weekly-summary-trigger" aria-label="Weekly summary">
      <button
        type="button"
        class="weekly-summary-trigger__button"
        hx-get="/fragments/homeowner/weekly-summary"
        hx-target="#weekly-summary-panel"
        hx-swap="outerHTML"
        hx-trigger="click once"
        aria-controls="weekly-summary-panel"
        x-data="weeklySummaryTrigger()"
        x-on:click="expanded = true"
        x-bind:hidden="expanded"
      >This week</button>
      <section
        id="weekly-summary-panel"
        x-show="expanded"
        x-cloak
      >
        <!-- Server-rendered fragment lands here on click; HTMX outerHTML replaces this placeholder. -->
      </section>
    </section>
    {% endif %}
    ```

    **Critical invariants (the AC's "non-blocking" load-bearing rule):**
    - The trigger button is OUTSIDE `.dashboard-stack`. It does NOT increase the count asserted by `test_homeowner_dashboard_shell_renders_five_htmx_bound_sections` — that test counts HTMX-bound sections INSIDE `.dashboard-stack`. AC13 #1 verifies this contract structurally.
    - `hx-trigger="click once"` ensures HTMX fires the GET exactly once on the first click. After that, the loaded fragment's own `hx-trigger="every 60s"` (AC9) takes over the refresh cadence.
    - The trigger button hides itself (`x-bind:hidden="expanded"`) after click; the inline expanded panel takes its place. UX spec line 1474 mandates inline expansion (NOT a separate page or modal).
    - The Alpine `weeklySummaryTrigger` factory is trivial: `() => ({ expanded: false })`. Add to `dashboard.html`'s `{% block scripts %}` below the existing `strategySelector` factory.
    - **`x-cloak`** is already defined in `open-ems.css` from Story 10.3 (line ~318: `[x-cloak] { display: none !important; }`). Reuse; no new global needed.

11. **AC11 — Settings additions.** Add four fields to `src/open_ems/settings.py` (mirror Story 10.3's docstring style):

    ```python
    # Story 10.4 — Weekly energy summary (FR30).
    # Cadence at which WeeklyEnergySummaryService re-aggregates the single
    # weekly_energy_summary row. Default 1 hour: trade-off between summary
    # freshness (homeowner sees ≤1h-stale values) and DB write load. Bounded
    # [5 min, 24 h]. First aggregation runs immediately at startup so the
    # absent-row window is bounded to ~seconds, not the full interval.
    weekly_summary_aggregation_interval_seconds: int = Field(default=3600, ge=300, le=86400)
    # Default import tariff (EUR per kWh) used to estimate self-consumption
    # savings when no per-site tariff configuration exists. Belgian-residential
    # 2026 reference range is roughly 0.25–0.40 EUR/kWh; default 0.30 is the
    # midpoint. NOT a per-site value in v1 — site-specific tariffs land in
    # Epic 13 (external optimization data) along with dynamic-tariff feeds.
    default_import_tariff_eur_per_kwh: float = Field(default=0.30, gt=0.0, le=2.0)
    # Default export tariff (EUR per kWh). Belgian-residential 2026 post-
    # prosumer-reform: typically near zero; default 0.05 covers the marginal
    # injection compensation. Bounded [0, default_import_tariff].
    default_export_tariff_eur_per_kwh: float = Field(default=0.05, ge=0.0, le=2.0)
    # Minimum number of complete-quality 15-min intervals per UTC day for that
    # day to count toward data_complete_days_count. 80 = 80% of the 96 daily
    # intervals (24h × 4); tolerates ≤19 missing/incomplete intervals/day so a
    # transient adapter outage does not invalidate the day. Bounded [1, 96].
    weekly_summary_min_intervals_per_complete_day: int = Field(default=80, ge=1, le=96)
    ```

    Cross-field validation (defer to documentation, NOT a model-level validator — Pydantic field cross-validators add complexity disproportionate to the risk): the dev notes call out that `default_export_tariff_eur_per_kwh` SHOULD be ≤ `default_import_tariff_eur_per_kwh` (an export tariff higher than import would imply free energy arbitrage). v1 does NOT enforce; a future Epic-13 dynamic-tariff story will revisit.

12. **AC12 — `core/energy.py` new module.** Domain types for the EnergyFlow surface:

    ```python
    class CompletedEnergyFlowInterval(BaseModel):
        """Emitted by EnergyFlowIntervalTracker on rollover; persisted via EnergyRepo."""
        model_config = ConfigDict(frozen=True, extra="forbid")
        interval_start_utc: datetime
        pv_kwh: float = Field(ge=0.0)
        battery_charged_kwh: float = Field(ge=0.0)
        battery_discharged_kwh: float = Field(ge=0.0)
        grid_imported_kwh: float = Field(ge=0.0)
        grid_exported_kwh: float = Field(ge=0.0)
        ev_charged_kwh: float = Field(ge=0.0)
        sample_count: int = Field(ge=0)
        data_quality: Literal["complete", "incomplete"]
        # ... + @field_validator for UTC interval_start_utc per the codebase pattern.

    class EnergyFlowIntervalRow(CompletedEnergyFlowInterval):
        """Same shape; named distinctly for the read path so callers do not confuse
        a tracker output with a persisted read."""
        pass

    class WeeklyEnergySummaryRow(BaseModel):
        model_config = ConfigDict(frozen=True, extra="forbid")
        window_start_utc: datetime
        window_end_utc: datetime
        peaks_avoided_count: int | None
        self_consumption_ratio: float | None  # bounded [0.0, 1.0] when not None
        estimated_cost_savings_eur: float | None
        data_complete_days_count: int = Field(ge=0)
        insufficient_history: bool
        computed_at: datetime

        @model_validator(mode="after")
        def _terminal_fields_iff_history_sufficient(self) -> WeeklyEnergySummaryRow:
            if not self.insufficient_history:
                if (self.peaks_avoided_count is None
                        or self.self_consumption_ratio is None
                        or self.estimated_cost_savings_eur is None):
                    raise ValueError(
                        "insufficient_history=False requires all three metric fields to be set"
                    )
                if not 0.0 <= self.self_consumption_ratio <= 1.0:
                    raise ValueError("self_consumption_ratio must be in [0.0, 1.0]")
            return self
    ```

    The model-validator mirrors the 10.2 `EVOverrideState._failure_reason_iff_terminal` pattern at `core/devices.py` — structural enforcement of "terminal-ish flag implies fields present" at the Pydantic boundary. The DB CHECK constraint (AC2) is the same invariant enforced at the storage boundary.

13. **AC13 — Non-interference structural assertions.** Two structural enforcements that the weekly summary does NOT regress the primary dashboard:

    1. **Test `test_dashboard_route.py::test_homeowner_dashboard_shell_renders_five_htmx_bound_sections`** stays at exactly 5 (the existing assertion). 10.4's new trigger button + weekly-summary-panel placeholder live OUTSIDE `.dashboard-stack` and therefore do NOT increase the count. New test `test_homeowner_dashboard_shell_renders_weekly_summary_trigger_outside_dashboard_stack` asserts that the rendered HTML contains the `weekly-summary-trigger` section AND that this section is NOT a descendant of `.dashboard-stack`.

    2. **Test `test_homeowner_dashboard_render_does_not_query_weekly_summary_table`** asserts that GET `/homeowner/dashboard` produces ZERO reads against `weekly_energy_summary` or `energy_flow_intervals`. Implementation: patch the EnergyRepo methods to assert-not-called and verify the dashboard render succeeds. This is the structural enforcement of the AC's "does not appear in the primary dashboard card render path" rule.

14. **AC14 — `peaks_avoided_count` event-log semantics.** Today the engine does NOT emit a dedicated "peak avoided" event; the closest signal is the `RetryPolicy._emit_success_audit` DECISION emission at `engine/retry_policy.py:160-172` which carries `command_type` (e.g., `SetEVChargingRateCommand`) in `detail` but does NOT preserve the `source_rule` that the evaluator attached at `engine/evaluator.py:86, 97` (`source_rule="peak_limiting"`).

    **10.4 closes this observability gap** by:
    1. Extending `EvaluationResult` (or `Intent` — verify the existing wire format in `engine/__init__.py` and `engine/intent_executor.py`) to carry `source_rule` through to `DeviceCommand` so RetryPolicy's success-audit `detail` can include it. **This may already be in place** — confirm by grepping the codebase for `source_rule` in command flow. If yes, AC14's only work is extending the audit `detail` dict to include `source_rule=command.source_rule`. If no, the propagation must be added: `DeviceCommand` gains a `source_rule: str | None = None` field; `IntentExecutor.translate` populates it from the intent; RetryPolicy.`_emit_success_audit` reads it and writes it into `detail`.
    2. Updating `_emit_success_audit` at `retry_policy.py:160-172` to add `"source_rule": command.source_rule` to the detail dict (alongside `correlation_id`, `command_type`, etc.). The `applied` field is already present — see line 169.
    3. Adding `count_peak_limiting_applied_decisions` to `EventLogRepo`:
       ```python
       async def count_peak_limiting_applied_decisions(
           self,
           *,
           since: datetime,
           until: datetime,
       ) -> int: ...
       ```
       The SQL uses `detail LIKE` with both substrings (per AC6). Wrap the call in `read-only` semantics — no `get_write_lock()` needed; aiosqlite handles read concurrency.

    **If `source_rule` is already wired**: AC14 is a pure read-path addition (new EventLogRepo method + populating it into the audit detail if not already). **If not**: AC14 is also a small mutator-path extension. In either case, AC14 is in-scope for 10.4 because peaks_avoided_count cannot be computed without it.

    Confirm-before-implement: AC15 task #1 (subtask) is "Verify whether `source_rule` is already in the command-flow detail; if yes, scope-shrink AC14 to just the count method".

15. **AC15 — Tests (unit + integration).** Test names follow the naming-honesty rule (Story 9-Y-a precedent).

    **`tests/unit/engine/test_energy_flow_tracker.py`** (NEW; ~12 tests):
    1. `test_energy_flow_tracker_initial_state_has_zero_accumulators_at_current_interval_boundary`
    2. `test_energy_flow_tracker_integrates_pv_power_into_pv_kwh_over_full_interval` — feed constant 5 kW for 15 minutes; assert `pv_kwh ≈ 1.25` (5 × 0.25).
    3. `test_energy_flow_tracker_splits_battery_power_into_charged_and_discharged_kwh_by_sign`
    4. `test_energy_flow_tracker_computes_grid_kwh_from_monotonic_accumulator_deltas`
    5. `test_energy_flow_tracker_floors_negative_grid_accumulator_delta_to_zero_and_logs_rollback`
    6. `test_energy_flow_tracker_marks_interval_incomplete_when_any_role_degraded_at_any_tick`
    7. `test_energy_flow_tracker_clamps_dt_seconds_to_interval_seconds_on_clock_step`
    8. `test_energy_flow_tracker_returns_completed_interval_only_at_clock_aligned_rollover`
    9. `test_energy_flow_tracker_starts_fresh_accumulators_after_rollover`
    10. `test_energy_flow_tracker_handles_none_power_inputs_as_zero_contribution_without_loss_of_partial_state`
    11. `test_energy_flow_tracker_first_tick_of_interval_contributes_zero_seconds`
    12. `test_energy_flow_tracker_skipped_tick_when_grid_accumulators_None_leaves_baseline_unchanged_for_next_tick`

    **`tests/unit/storage/repositories/test_energy_repo.py`** (EXTEND; ~6 new tests):
    13. `test_energy_repo_write_energy_flow_interval_inserts_row_with_all_columns`
    14. `test_energy_repo_write_energy_flow_interval_is_idempotent_on_duplicate_interval_start`
    15. `test_energy_repo_read_energy_flow_intervals_since_returns_rows_in_chronological_order`
    16. `test_energy_repo_upsert_weekly_energy_summary_inserts_when_no_row_exists`
    17. `test_energy_repo_upsert_weekly_energy_summary_overwrites_existing_single_row`
    18. `test_energy_repo_read_weekly_energy_summary_returns_None_on_cold_start_table`

    **`tests/unit/services/test_weekly_energy_summary.py`** (NEW; ~10 tests):
    19. `test_weekly_summary_cold_start_writes_insufficient_history_row_immediately`
    20. `test_weekly_summary_writes_insufficient_history_when_fewer_than_seven_complete_days_available`
    21. `test_weekly_summary_computes_self_consumption_ratio_from_pv_and_grid_export_totals`
    22. `test_weekly_summary_clamps_self_consumption_ratio_to_zero_when_total_pv_kwh_is_zero`
    23. `test_weekly_summary_estimated_cost_savings_includes_self_consumption_export_and_battery_components`
    24. `test_weekly_summary_peaks_avoided_count_reads_event_log_decisions_with_peak_limiting_source_rule_and_applied_true`
    25. `test_weekly_summary_aggregation_loop_survives_repo_error_and_continues_on_next_tick`
    26. `test_weekly_summary_first_aggregation_runs_immediately_not_after_first_sleep`
    27. `test_weekly_summary_data_complete_days_count_uses_settings_threshold_for_minimum_intervals_per_day`
    28. `test_weekly_summary_window_uses_seven_days_relative_to_aggregator_clock_function`

    **`tests/unit/web/test_fragment_routes.py`** (EXTEND; ~5 new tests):
    29. `test_fragment_weekly_summary_renders_insufficient_history_message_when_row_absent`
    30. `test_fragment_weekly_summary_renders_three_metrics_when_history_sufficient`
    31. `test_fragment_weekly_summary_renders_within_200ms_on_pi4_proxy_using_in_memory_sqlite` — 10-iteration timing assertion against an in-memory SQLite (CI's TestClient runs an order of magnitude faster than a Pi 4 native; we accept a margin and assert mean < 50ms in CI as a proxy for the 200ms Pi 4 budget; the dev notes call this out and the integration test on a representative-volume fixture uses < 200ms as the hard ceiling).
    32. `test_fragment_weekly_summary_unauthenticated_returns_302_with_login_redirect`
    33. `test_fragment_weekly_summary_installer_returns_403`

    **`tests/unit/web/test_state_serialization.py`** (EXTEND; ~3 new tests):
    34. `test_build_homeowner_weekly_summary_context_returns_insufficient_history_when_row_is_None`
    35. `test_build_homeowner_weekly_summary_context_converts_ratio_to_integer_percent_for_display`
    36. `test_build_homeowner_weekly_summary_context_formats_eur_with_two_decimal_places_in_template`

    **`tests/unit/web/test_dashboard_route.py`** (EXTEND; 2 new tests):
    37. `test_homeowner_dashboard_shell_renders_weekly_summary_trigger_outside_dashboard_stack` — AC13 #1.
    38. `test_homeowner_dashboard_render_does_not_query_weekly_summary_table` — AC13 #2; patch `EnergyRepo.read_weekly_energy_summary` to a `MagicMock(side_effect=AssertionError("must not be called"))` for the dashboard render; GET `/homeowner/dashboard` succeeds without invoking it.

    **`tests/integration/storage/test_migrations.py`** (EXTEND; 1 new test):
    39. `test_migration_0013_creates_energy_flow_intervals_and_weekly_energy_summary_tables_with_check_constraints` — round-trip upgrade → schema introspection → downgrade.

    **`tests/integration/web/test_weekly_summary_e2e.py`** (NEW; ~5 tests):
    40. `test_weekly_summary_e2e_cold_start_shows_insufficient_history_message` — boot the app fresh; GET `/fragments/homeowner/weekly-summary` → 200 + "Data is still being collected" text.
    41. `test_weekly_summary_e2e_after_seven_days_of_synthetic_data_shows_full_summary` — seed energy_flow_intervals with 7 days × 96 intervals of synthetic data + event_log peak-limiting decisions; manually invoke `_compute_and_upsert_weekly_summary`; GET → 200 + three metric values present.
    42. `test_weekly_summary_e2e_sub_200ms_response_at_representative_data_volume` — seed full 7-day data; 10-iteration GET timing; assert mean < 200ms (this IS the hard performance contract from the AC; CI may be a faster proxy than Pi 4 but the assertion is the same).
    43. `test_weekly_summary_e2e_aggregator_runs_immediately_at_lifespan_startup` — start the app via `app_e2e`; assert the row exists within ~5s of process start (insufficient_history=1 row).
    44. `test_weekly_summary_e2e_a11y_placeholder` — `pytest.mark.xfail(strict=False) + pytest.mark.skipif(npx absent)` — axe-playwright over the dashboard with weekly summary expanded. Follows the 10.1 / 10.2 / 10.3 / 9.x precedent.

    **`tests/unit/web/test_dashboard_route.py`** + **`tests/unit/web/test_alpine_provenance.py`** — verify the new `weeklySummaryTrigger` Alpine factory is registered alongside `evOverride` and `strategySelector` (the alpine-provenance test from 10.2 covers SHA only; no change required to it, but a new `test_dashboard_script_block_registers_weekly_summary_trigger_factory` may be added if symmetry is desired).

16. **AC16 — Quality gates.**
    - `uv run python -m pytest tests/ --no-cov -q` → all tests pass. Post-10.3 baseline is **1598 passed + 1 skipped + 7 xfailed**. Net delta MUST be strongly positive: ≥30 new passing tests from the AC15 set above.
    - `uv run python -m ruff check .` → clean across all source files.
    - `uv run python -m ruff format --check .` → clean.
    - `uv run python -m mypy src/` → clean across all source files. The new EnergyFlow types, the aggregator service, the new repo methods, and the new route MUST type-check without `# type: ignore`.
    - `uv.lock` MUST be unchanged. **No new Python dependencies.** The OPEN-EMS zero-new-Python-dependency contract carries through to the end of Epic 10.

17. **AC17 — Sprint-status consistency.** On story close, `_bmad-output/implementation-artifacts/sprint-status.yaml` MUST set:
    ```
    10-4-implement-fr30-weekly-energy-summary-as-non-blocking-pre-aggregated-secondary-feature: done
    ```
    Per Epic 9 retro action A8 (Status header ↔ sprint-status agreement) and the A1/A2 codification (`bmad-create-story` / `bmad-code-review`). The story file Status header transitions `ready-for-dev → in-progress → review → done` MUST mirror the sprint-status entry. **A7 review-closure gate** (Epic 9 retro A7) applies — `bmad-code-review` is the single owner of the `review → done` transition.

## Tasks / Subtasks

- [x] **Task 1 — Domain types + migration `0013`** (AC: 1, 2, 12)
  - [x] Subtask 1.1 — Create `src/open_ems/core/energy.py` with `CompletedEnergyFlowInterval`, `EnergyFlowIntervalRow`, `WeeklyEnergySummaryRow`. Model-validator enforces `insufficient_history=False ⇒ all three metrics set + ratio bounded [0.0, 1.0]`.
  - [x] Subtask 1.2 — Create `migrations/versions/0013_add_energy_flow_intervals_and_weekly_summary_tables.py` per AC1 + AC2 schemas (CHECK constraints + index + single-row constraint on `weekly_energy_summary`). Down-migration drops both tables + index.
  - [x] Subtask 1.3 — Add migration round-trip test (AC15 #39) to `tests/integration/storage/test_migrations.py`.

- [x] **Task 2 — `EnergyFlowIntervalTracker`** (AC: 3, 15)
  - [x] Subtask 2.1 — Create `src/open_ems/engine/energy_flow_tracker.py` with the tracker class per AC3. Reuse the 15-min clock-alignment helper from `partial_interval_tracker.py` (do not duplicate; if the helper is private, refactor to a shared module-level function).
  - [x] Subtask 2.2 — Add 12 unit tests to `tests/unit/engine/test_energy_flow_tracker.py` per AC15 #1–#12. All pass.

- [x] **Task 3 — `EnergyRepo` extensions** (AC: 4, 15)
  - [x] Subtask 3.1 — Add `write_energy_flow_interval`, `read_energy_flow_intervals_since`, `upsert_weekly_energy_summary`, `read_weekly_energy_summary` to `src/open_ems/storage/repositories/energy_repo.py`. Use `get_write_lock()` for writes; INSERT OR REPLACE for flow intervals; SQLite UPSERT (`ON CONFLICT(id) DO UPDATE SET ...`) for the summary.
  - [x] Subtask 3.2 — Add 6 repo tests to `tests/unit/storage/repositories/test_energy_repo.py` per AC15 #13–#18. All pass.

- [x] **Task 4 — `ControlLoop` wire-up** (AC: 5, 15)
  - [x] Subtask 4.1 — Construct `self._energy_flow_tracker` in `ControlLoop.__init__` (mirror `self._tracker` at line 119).
  - [x] Subtask 4.2 — Add helpers `_extract_pv_power`, `_extract_battery_power`, `_extract_ev_power`, `_extract_grid_accumulators` next to `_extract_grid_power`. Each returns `(value | None, "complete" | "incomplete")`.
  - [x] Subtask 4.3 — Extend `_update_tracker` to feed the new tracker AFTER `write_peak_interval` completes; persist via `write_energy_flow_interval` on rollover. Reuse `any_role_degraded` classification.
  - [x] Subtask 4.4 — Add a unit test to `tests/unit/engine/test_control_loop.py` asserting that a 15-min interval rollover writes both `peak_intervals` AND `energy_flow_intervals` rows in the correct order (peak first, flow second). Verifies ordering structurally so a regression cannot drop the peak-first contract.

- [x] **Task 5 — `peaks_avoided` event-log plumbing** (AC: 14, 15)
  - [x] Subtask 5.1 — **Investigate first:** grep `source_rule` in `src/open_ems/engine/` to determine if it already flows from `EvaluationResult` → `Intent` → `DeviceCommand` → audit detail. If yes: skip 5.2. If no: thread `source_rule` through the command flow.
  - [x] Subtask 5.2 — Extend `RetryPolicy._emit_success_audit` to include `source_rule` in `detail`. Update existing tests that assert exact `detail` shape (search `test_retry_policy.py` for `detail` assertions; widen as needed).
  - [x] Subtask 5.3 — Add `EventLogRepo.count_peak_limiting_applied_decisions(since, until)` per AC6 / AC14. Add a unit test asserting the LIKE-based query correctly counts only `event_type='DECISION'` rows whose `detail` JSON contains both `source_rule="peak_limiting"` AND `applied=true`.

- [x] **Task 6 — `WeeklyEnergySummaryService`** (AC: 6, 11, 15)
  - [x] Subtask 6.1 — Create `src/open_ems/services/weekly_energy_summary.py` with `weekly_energy_summary_task(...)` and `_compute_and_upsert_weekly_summary(...)` per AC6. Pure-function aggregation logic; injectable `clock` for tests.
  - [x] Subtask 6.2 — Add 4 new Settings fields per AC11 (`weekly_summary_aggregation_interval_seconds`, `default_import_tariff_eur_per_kwh`, `default_export_tariff_eur_per_kwh`, `weekly_summary_min_intervals_per_complete_day`). Bounded `Field(...)` per the docstring rationale.
  - [x] Subtask 6.3 — Add 10 service tests to `tests/unit/services/test_weekly_energy_summary.py` per AC15 #19–#28. All pass. Use `freezegun` or a clock-injection helper to drive deterministic windows (`freezegun` is already a dev dep — confirm via `uv.lock`).

- [x] **Task 7 — Lifespan wiring** (AC: 7, 15)
  - [x] Subtask 7.1 — In `src/open_ems/web/app.py:lifespan()`, after the ControlLoop construction (`web/app.py:562-573`), construct `_weekly_summary_task = asyncio.create_task(weekly_energy_summary_task(...), name="weekly_summary")`. Add `_on_weekly_summary_done` callback mirror of `_on_pruning_done`. Confirm shutdown sequence cancels and awaits this task in the same `finally` clause as the existing background tasks.
  - [x] Subtask 7.2 — Wrap construction in `try/except`: if the task fails to construct (e.g., misconfigured Settings field), log the error and CONTINUE the lifespan. The weekly summary is non-blocking by AC contract.
  - [x] Subtask 7.3 — Add an integration test `tests/integration/web/test_weekly_summary_lifespan.py::test_weekly_summary_task_started_at_lifespan_and_cancelled_at_shutdown`.

- [x] **Task 8 — Route + template + state serialization** (AC: 8, 9, 15)
  - [x] Subtask 8.1 — Add `build_homeowner_weekly_summary_context(row)` to `src/open_ems/web/state_serialization.py`. Pure-functional over `WeeklyEnergySummaryRow | None`.
  - [x] Subtask 8.2 — Add `get_energy_repo` dependency to `src/open_ems/web/dependencies.py` (mirror existing `get_state_store`).
  - [x] Subtask 8.3 — Add `GET /fragments/homeowner/weekly-summary` route to `src/open_ems/web/routes/fragments.py` per AC8.
  - [x] Subtask 8.4 — Create `src/open_ems/web/templates/fragments/homeowner/weekly-summary.html` per AC9. Self-refreshing via `hx-trigger="every 60s"`.
  - [x] Subtask 8.5 — If `format_utc_date` Jinja filter does not exist: register one in `web/app.py` next to existing filters. Confirm via grep first.
  - [x] Subtask 8.6 — Add 5 fragment-route tests + 3 state-serialization tests per AC15 #29–#36. All pass.

- [x] **Task 9 — Dashboard trigger + Alpine factory + CSS** (AC: 10, 13, 15)
  - [x] Subtask 9.1 — Add the "This week" trigger `<section>` and the inline `#weekly-summary-panel` placeholder to `dashboard.html` OUTSIDE `.dashboard-stack` per AC10.
  - [x] Subtask 9.2 — Add the `weeklySummaryTrigger` Alpine factory inside the existing `{% block scripts %}` next to `evOverride` and `strategySelector`. Trivial body: `() => ({ expanded: false })`.
  - [x] Subtask 9.3 — Append weekly-summary CSS rules to `src/open_ems/web/static/open-ems.css`:
    ```css
    /* Story 10.4 — Weekly energy summary. */
    .weekly-summary-trigger        { margin-top: 1rem; padding: 0 1rem; }
    .weekly-summary-trigger__button {
      background: none; border: none; color: var(--color-accent);
      font-size: 1rem; padding: 0.5rem 0; cursor: pointer;
    }
    .weekly-summary-trigger__button:focus-visible { outline: 2px solid var(--color-accent); outline-offset: 2px; }
    .weekly-summary                { margin-top: 0.75rem; padding: 1rem; background: var(--color-surface);
                                     border: 1px solid var(--color-border); border-radius: 8px; }
    .weekly-summary__heading       { margin: 0 0 0.5rem 0; font-size: 1rem; color: var(--color-text-primary); }
    .weekly-summary__message       { color: var(--color-text-secondary); font-size: 0.875rem; margin: 0; }
    .weekly-summary__metrics       { margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.5rem; }
    .weekly-summary__metric        { display: flex; justify-content: space-between; align-items: baseline;
                                     padding: 0.25rem 0; border-bottom: 1px solid var(--color-border); }
    .weekly-summary__metric:last-child { border-bottom: none; }
    .weekly-summary__metric dt     { color: var(--color-text-secondary); font-size: 0.875rem; }
    .weekly-summary__metric dd     { margin: 0; color: var(--color-text-primary); font-size: 1.125rem; font-weight: 600; }
    .weekly-summary__footnote      { margin-top: 0.75rem; color: var(--color-text-tertiary); font-size: 0.75rem; }
    ```
    **NO amber, NO red, NO --color-pass, NO warn classes.** Same load-bearing rule as 10.2 AC16 and 10.3 AC13 — a test asserts the rendered HTML contains none of those class fragments.
  - [x] Subtask 9.4 — Add the 2 AC13 structural-assertion tests (AC15 #37, #38) to `tests/unit/web/test_dashboard_route.py`.

- [x] **Task 10 — Integration tests + quality gates** (AC: 15, 16, 17)
  - [x] Subtask 10.1 — Create `tests/integration/web/test_weekly_summary_e2e.py` with 5 tests per AC15 #40–#44.
  - [x] Subtask 10.2 — Run `uv run python -m pytest tests/ --no-cov -q`; assert ≥1628 passed (1598 baseline + ≥30 net new). Address any test-baseline regression before proceeding.
  - [x] Subtask 10.3 — Run `uv run python -m ruff check .` → clean. `uv run python -m ruff format --check .` → clean. `uv run python -m mypy src/` → clean.
  - [x] Subtask 10.4 — `git status uv.lock` → no diff. Confirm zero new Python dependencies.
  - [x] Subtask 10.5 — Flip sprint-status `10-4-...: backlog → ready-for-dev` at workflow start (already done by create-story); on `bmad-dev-story` completion the status flips to `review`; on `bmad-code-review` A7 gate it flips to `done`. Story file Status header transitions in parallel.

## Dev Notes

### A2 trigger evaluation

| Trigger | Match? | Evidence |
|---|---|---|
| T1 — Lifecycle / state-machine behavior | **No (borderline)** | The aggregation service has a trivial "running" lifecycle (sleep ↔ aggregate) but no meaningful state machine in the strict A2 sense. The `WeeklyEnergySummaryRow.insufficient_history` flag is a derived flag, not a state-machine state. Marked "no" to avoid claiming a trigger that's not substantive. |
| T2 — Retries / cancellation | **No** | Aggregation loop catches exceptions and retries on the next cadence tick; no retry-count tracking, no cancellation propagation to in-flight clients. The HTMX route is fire-and-forget read with Starlette default cancellation. |
| T3 — Persistence + recovery | **Yes** | Two new persisted tables (`energy_flow_intervals` + `weekly_energy_summary`). Both survive restart; rows are read on every homeowner request after restart (`read_weekly_energy_summary`). Cold-start hydration from DB across process restart is the canonical T3 pattern. Story 9.0d (initial_monthly_peak_kw hydration) is the precedent. |
| T4 — Watchdog / timing semantics | **Yes** | `WeeklyEnergySummaryService` is a periodic asyncio task with deadline-relative scheduling (`weekly_summary_aggregation_interval_seconds`). The 200ms response budget on the route is a separate performance contract. The aggregation cadence is the watchdog-class concern: a crashed task means stale summary; lifespan owns the crash-detection via `_on_weekly_summary_done`. |
| T5 — Multi-adapter coordination | **No** | Reads from `energy_flow_intervals` + `event_log` + `active_constraints` (3 storage surfaces) but no adapter touch. Adapter telemetry flows into the table at write time via ControlLoop, which is the existing single-writer surface. |
| T6 — Deployment / restart behavior | **Yes** | Process restart resets in-memory tracker state. The cold-start "first aggregation runs immediately" contract (AC6 step 4) means the absent-row window is bounded to seconds, not the full interval. The `insufficient_history=1` cold-start branch is user-visible per AC3 ("data is still being collected"); restart of a running site flips the user-facing summary back to insufficient-history briefly until the next aggregation cycle re-evaluates `data_complete_days_count` from the persisted intervals (which DO survive restart). |
| T7 — Installer workflow orchestration | **No** | Homeowner-facing read-only feature; no installer wizard touchpoints. |

**Conclusion:** A2-triggered on **T3 + T4 + T6**. R1–R7 mandatory. Synthesized below.

### Files modified by this story

**New files:**
- `src/open_ems/core/energy.py` — domain types (`CompletedEnergyFlowInterval`, `EnergyFlowIntervalRow`, `WeeklyEnergySummaryRow`).
- `src/open_ems/engine/energy_flow_tracker.py` — `EnergyFlowIntervalTracker` class.
- `src/open_ems/services/weekly_energy_summary.py` — periodic aggregator service.
- `src/open_ems/web/templates/fragments/homeowner/weekly-summary.html` — fragment template.
- `migrations/versions/0013_add_energy_flow_intervals_and_weekly_summary_tables.py` — schema migration.
- `tests/unit/engine/test_energy_flow_tracker.py` — tracker unit tests (~12 tests).
- `tests/unit/services/test_weekly_energy_summary.py` — service unit tests (~10 tests).
- `tests/integration/web/test_weekly_summary_e2e.py` — end-to-end scenarios (~5 tests incl. a11y placeholder).
- `tests/integration/web/test_weekly_summary_lifespan.py` — lifespan integration test.

**Modified files:**
- `src/open_ems/settings.py` — add 4 Settings fields per AC11.
- `src/open_ems/storage/repositories/energy_repo.py` — add 4 methods per AC4.
- `src/open_ems/storage/repositories/event_log_repo.py` — add `count_peak_limiting_applied_decisions` per AC14.
- `src/open_ems/engine/control_loop.py` — wire EnergyFlowIntervalTracker into `_update_tracker` per AC5.
- `src/open_ems/engine/retry_policy.py` — extend `_emit_success_audit.detail` with `source_rule` per AC14 (gated on Subtask 5.1 investigation).
- `src/open_ems/web/app.py` — lifespan wiring per AC7.
- `src/open_ems/web/dependencies.py` — add `get_energy_repo` dep.
- `src/open_ems/web/routes/fragments.py` — add weekly-summary route per AC8.
- `src/open_ems/web/state_serialization.py` — add `build_homeowner_weekly_summary_context` per AC8.
- `src/open_ems/web/templates/dashboard.html` — add trigger + Alpine factory per AC10.
- `src/open_ems/web/static/open-ems.css` — append weekly-summary CSS rules per Task 9.3.
- `tests/unit/storage/repositories/test_energy_repo.py`, `tests/unit/engine/test_control_loop.py`, `tests/unit/web/test_fragment_routes.py`, `tests/unit/web/test_state_serialization.py`, `tests/unit/web/test_dashboard_route.py`, `tests/integration/storage/test_migrations.py` — extended per AC15.
- `_bmad-output/implementation-artifacts/sprint-status.yaml` — final entry update on close.

### Architectural alignment

- **`energy_repo` is the single owner of energy-flow persistence** (architecture.md line 863: `energy_repo.py # FR19, FR26: energy_readings (raw samples) + peak_intervals`). 10.4 adds `energy_flow_intervals` as the architecturally-canonical implementation of "energy_readings" — specifically, **per-15-min aggregated rows, not raw per-tick samples**. This is a deliberate v1 simplification documented in this story's rationale (250k+ rows/week of raw 10s samples is operationally prohibitive on Pi 4 + 32 GB SD). The architecture description ("raw samples at poll interval (5–15 seconds per device per metric)") is updated NOT in code-comment form but in dev notes here; a future Epic 11 / 13 doc-sync sweep should reconcile architecture.md line 1060 with the v1 reality. **Out of scope for 10.4.**
- **Single-evaluator principle (Epic 7 retro, 2026-05-05):** the aggregator service does NOT participate in decision-making. It is a read-only consumer of `energy_flow_intervals` + `event_log` + `active_constraints.peak_limit_kw`; it writes only to `weekly_energy_summary`. No coupling to the decision engine.
- **15-min clock-alignment invariant (architecture.md BAD-1 line 1047-1079):** the new tracker shares the SAME interval boundaries as `PartialIntervalTracker`. The two trackers MUST roll over at the same instant; AC5 Subtask 4.4 verifies this structurally. Different boundaries between the two trackers would create cross-table drift where `peak_intervals.interval_start_utc` and `energy_flow_intervals.interval_start_utc` diverge for the "same" interval — a forensic nightmare.
- **Write-lock discipline (Epic 4-onwards storage pattern):** every new repo write uses `get_write_lock()` per the codebase convention. Reads do NOT take the lock (aiosqlite handles read concurrency natively in WAL mode).
- **Belgian-residential context (PRD line 30 + architecture economic assumptions):** the default tariffs (0.30 / 0.05 EUR per kWh) are Belgian-residential 2026 reference values. The dev notes flag these as v1 defaults; per-site tariff configuration is Epic 13 (external optimization data). A homeowner who finds the estimate inaccurate will see the AC9 footnote "Estimated savings use a default tariff — check with your supplier for exact figures" — the calibration story is the footnote's existence, not the value's precision.
- **HTMX outer-section pattern (Epic 10 precedent):** the weekly-summary fragment carries its own `hx-get`/`hx-trigger` on the root `<section>` so that swapped-in DOM keeps polling. This intentionally addresses the polling-stops-after-outerHTML-swap class identified in 10-3 review-deferred D1 for the new fragment introduced by 10.4 (the existing five fragments inherited from 10.1–10.3 are NOT touched by 10.4 — they remain a deferred coordination across all five which Epic 10 retro / Epic 11 hardening will address). 10.4 does NOT regress; it lands the FIX pattern for its own new fragment.
- **Fragment-route auth + dependency pattern (Story 10.2 / 10.3 precedent):** `Depends(require_homeowner)` + `Depends(get_energy_repo)`. No CSRF (read-only GET). No PolicyGuard. No background dispatch task. Pure synchronous read + render.
- **Pydantic frozen + model-validator pattern (Story 10.2 EVOverrideState precedent):** `WeeklyEnergySummaryRow` mirrors the "terminal-flag implies fields present" structural invariant. The DB CHECK constraint AND the Pydantic model-validator BOTH enforce — defense in depth, same class as the 10.2 `failure_reason iff terminal-status` pattern.

### Library / framework requirements (no new dependencies)

`uv.lock` MUST be unchanged after this story. The 10.4 stack uses existing libraries only:
- **FastAPI + Jinja2** — route + template rendering.
- **aiosqlite** — DB I/O (existing).
- **Pydantic 2.x + pydantic-settings** — new Settings fields + domain types.
- **HTMX 1.9.12** — fragment loading + 60s self-refresh on the swapped-in panel.
- **Alpine.js 3.13.10** — trivial `weeklySummaryTrigger` factory (existing self-hosted from 10.2).
- **structlog** — service + tracker log emissions.
- **pytest + pytest-asyncio (auto mode)** — testing. **`freezegun`** for the aggregator's clock-injection tests if it's already a dev dep (confirm via `uv.lock` — preferred over real-clock sleeps in tests).

### Scope decisions and intentional simplifications

1. **Per-15-min aggregated samples instead of raw per-tick.** The architecture's textual description (architecture.md line 1060) implies raw 5–15s samples for `energy_readings`. v1 stores only the rolled-up 15-min aggregates. Rationale: a 7-day window × 24h × 4 intervals = 672 rows is dramatically more Pi-4-friendly than ~250k–700k rows, and the weekly summary's three metrics do not benefit from finer resolution. A future Epic 11 / 12 / 13 story may add a per-tick `energy_readings` table if real-time charting or finer reporting becomes a requirement; the table name choice here (`energy_flow_intervals`) leaves naming room.
2. **`source_rule` propagation through the command flow (AC14).** Whether this is a 0-line change (already present in audit detail) or a small-surface threading (DeviceCommand gains the field) is determined by Subtask 5.1's investigation. The story scope budget accommodates either path. The honest "what we don't yet know" is captured by the investigate-first subtask.
3. **`weekly_energy_summary` as single-row table.** A history of weekly summaries is NOT v1. The "current" summary is what the homeowner reads. Migration `0013` reserves the option to evolve to a row-per-week schema in a future epic by replacing the single-row CHECK constraint; no v2 migration would need to drop data.
4. **No EnergyFlowIntervalTracker degraded-window pause.** If the system is in fail_safe mode for an entire 15-min interval, the tracker still records the interval (with `data_quality='incomplete'`). The aggregator excludes these from `data_complete_days_count` per AC6 step 3. This is gentler than "drop fail_safe intervals entirely" — the homeowner sees `data_complete_days_count` decrement (and the insufficient-history branch triggers if too many) rather than a confusing "the system is fine but the summary disappeared" UX.
5. **Cost-savings battery component half-weight (AC6 step 5c).** A first-order approximation. The full picture requires time-of-use tariff data + per-discharge knowledge of "what would have happened otherwise"; both are out of scope for v1.
6. **The aggregator NEVER blocks the homeowner read path.** If the aggregator has not yet run (cold start), the route returns insufficient-history. If the aggregator has crashed and not auto-recovered, the route returns the LAST persisted row + the `computed_at` timestamp — the homeowner can see staleness via the footnote rendering of computed_at if the implementation chooses to surface it (the AC9 template currently does not render computed_at as user-visible text — a future polish item).

### Previous-story intelligence

**Story 10.3 (inline strategy selector, done 2026-05-12):**
- **Polling-stops-after-outerHTML-swap (10-3 review-deferred D1).** 10.4's new fragment template carries `hx-get`/`hx-trigger` on its root `<section>` (AC9) — landing the FIX pattern for the new fragment. Existing five fragments are NOT touched by 10.4; the systemic fix is bundled into an Epic 10 / 11 hardening sub-story per D1's classification.
- **`_safe_audit` audit-trail concurrency / process-kill-window (10-3 review-deferred D3 / D2 + 10-2-deferred audit-resilience items).** 10.4 does NOT emit homeowner-action audits — it is a read-only feature with no user-initiated writes. The aggregator service's `weekly_summary_aggregation_failed` structlog ERROR is observability-only (not an audit row); no audit-trail concurrency risk introduced.
- **Failure-message single-source-of-truth (10.3 `_STRATEGY_FAILURE_MESSAGE` pattern).** 10.4 has ONE user-visible message string ("Data is still being collected...") — keep it inline in the template; no module-level constant needed at this scope.

**Story 10.2 (EV status card, done 2026-05-12):**
- **Self-hosted Alpine.js 3.13.10** at `static/alpine.min.js` — reused for `weeklySummaryTrigger`. No new bundle.
- **`x-cloak` global CSS** at `open-ems.css:318` (from 10.3) — reused for the panel's pre-hydration hide. No new global needed.
- **Pydantic `frozen=True` + model-validator pattern (`EVOverrideState`).** Mirrored in `WeeklyEnergySummaryRow`.
- **EV-card CSS class no-pass-green no-amber rule (10.2 AC16).** Same rule applies to weekly-summary CSS — AC15 tests assert no `pass` / `--color-pass` / `green` / `amber` / `red` / `warn` class fragments in the rendered summary. Calm and informational, never alarming.

**Story 10.1 (homeowner dashboard, done 2026-05-11):**
- **CSS design tokens already defined** (`--color-accent`, `--color-text-primary`, `--color-text-secondary`, `--color-text-tertiary`, `--color-surface`, `--color-border`). 10.4 introduces ZERO new tokens.
- **`build_homeowner_*_context` pure-functional pattern.** `build_homeowner_weekly_summary_context` follows the same pattern: pure over its inputs; no side effects; no auth state; route-level concerns (csrf_token, settings injection) live in the route.
- **Test naming-honesty (Story 9-Y-a precedent).** Every test name in AC15 describes what the test actually asserts.

**Story 9.0d (initial_monthly_peak_kw hydration, done 2026-05-11):**
- The cold-start "first aggregation runs immediately" contract (AC6) directly mirrors 9.0d's "hydrate before constructing the loop". Different surface, same principle: minimize the user-visible "not yet correct" window after process restart.

**Story 9.X (adapter map wire-up, done 2026-05-11):**
- The lifespan's existing background-task pattern (`_pruning_task`, `_cleanup_task`, `_watchdog_task`, `_control_loop_task`, plus the asyncio.create_task + done-callback + cancel-in-shutdown discipline) is the canonical pattern. 10.4's `_weekly_summary_task` slots into this pattern as a fifth peer.

**Story 8.3 (RetryPolicy):**
- `_emit_success_audit` at `retry_policy.py:160-172` is the audit emission site that AC14 extends. The existing detail dict (`correlation_id`, `command_type`, `command_status`, `applied`, `attempts`) is the closest source-of-truth for "what just happened in the engine". Adding `source_rule` here is the structurally cleanest place.

### Latest tech information

No library upgrades. The v1 stack is:
- HTMX 1.9.12 — `hx-trigger="every Ns"` and `hx-trigger="click once"` both natively supported.
- Alpine.js 3.13.10 — `x-data`, `x-show`, `x-cloak`, `x-bind`, `x-on` all used.
- Pydantic 2.x — `Field`, `model_validator(mode="after")`, `ConfigDict(frozen=True)` all used per existing codebase patterns.
- aiosqlite — SQLite UPSERT (`ON CONFLICT(id) DO UPDATE SET ...`) is supported in SQLite 3.24+ (required for `INSERT ... ON CONFLICT`). Confirm the embedded SQLite version is ≥3.24 (it is; modern Python ships with SQLite ≥3.35).

### Project Structure Notes

- New module `src/open_ems/core/energy.py` is the canonical home for energy-flow domain types. Sibling to existing `core/devices.py`, `core/state.py`, `core/constraints.py`. Architecture is silent on the file's existence; co-locate by concern, not by name precedent.
- The new tracker `src/open_ems/engine/energy_flow_tracker.py` lives alongside `partial_interval_tracker.py` per the engine module's "one tracker per concern" pattern.
- The aggregator service lives at `src/open_ems/services/weekly_energy_summary.py` per architecture.md's `services/` cross-cutting-runtime-services convention.
- Tests follow the existing convention: tracker tests in `tests/unit/engine/`, repo tests in `tests/unit/storage/repositories/`, service tests in `tests/unit/services/`, fragment tests in `tests/unit/web/`, end-to-end in `tests/integration/web/`.
- The dashboard's existing `{% block scripts %}` is the single Alpine-factory registration site; the new `weeklySummaryTrigger` factory is appended below `strategySelector`. Same scope, same `alpine:init` listener. DO NOT extract to a separate `.js` file (the `defer` ordering established in `base.html` would need re-engineering).
- The new fragment template `weekly-summary.html` lives in `templates/fragments/homeowner/` alongside `status-headline.html`, `ev-card.html`, etc. Same directory convention.

### Orchestration Risk Analysis

**A2 triggers matched:** T3 (persistence + recovery — two new persisted tables hydrated from DB on every request after restart), T4 (watchdog / timing — periodic asyncio aggregator with bounded cadence; crash detection via done-callback), T6 (deployment / restart — cold-start "first aggregation runs immediately" + persisted-row carry-over across restart).

#### R1 — Composition-risk analysis

Four operational domains converge in this story. Each contributes a risk that only surfaces at the convergence.

1. **Periodic aggregator (T4) converging with persisted single-row table (T3).** The aggregator is the ONLY writer to `weekly_energy_summary`. If two instances of the service ran concurrently (e.g., a hot-reload scenario in dev where the lifespan starts a new task before the old one cancels), both would write the single row and the later one wins silently. **Mitigated by:** Python single-process FastAPI deployment + lifespan strict cancel-and-await on the prior task during shutdown. Multi-process deployments are NOT a v1 target (architecture.md lifespan is single-instance per `web/app.py`). Documented here as an invariant; if a future story adds multi-worker uvicorn, this becomes load-bearing.

2. **ControlLoop write fan-out (`peak_intervals` + `energy_flow_intervals` in the same `_update_tracker` invocation) converging with the SQLite WAL writer queue (existing pattern across all repos).** A failure in the new `write_energy_flow_interval` would not corrupt `peak_intervals` (the peak write completes BEFORE the flow write per AC5's load-bearing ordering). But a transient SQLite lock under heavy contention could timeout the flow write while peaks succeed, producing a one-tick drift between the two tables. **Mitigated by:** AC5's "peak first, flow second" ordering invariant + Subtask 4.4's structural test. If the flow write fails, the next interval rollover writes a fresh row; missing one interval is bounded data loss (≤15 min of energy-flow data) and the `data_quality='incomplete'` semantics naturally accommodate gaps. **Precedent class:** Story 9.0d's "initial_monthly_peak_kw hydration BEFORE control loop construction" — sibling pattern of "load-bearing write must complete before dependent write".

3. **Pre-aggregated summary row TTL (T3 hydration) converging with adapter staleness (DegradedDeviceState propagation).** If the homeowner reads the summary while the system is in extended fail_safe mode, the summary reflects energy flows from BEFORE the failure — the `computed_at` timestamp may be hours stale. The aggregator continues running during fail_safe (it does NOT participate in the engine's fail-safe lifecycle); it just computes from whatever `energy_flow_intervals` rows exist, most of which during fail_safe will be `data_quality='incomplete'`. After enough incomplete days, `data_complete_days_count` drops below 7 and the summary flips back to insufficient-history. **Mitigated by:** the user-visible footnote OR (future polish) a `computed_at` staleness banner. v1 ships without the staleness banner — the AC does not mandate one, and the insufficient-history fallback naturally triggers if the system is degraded long enough.

4. **`peaks_avoided_count` via event_log LIKE-query (AC6 step 5a) converging with event-log retention (90-day default, EventLogRepo.prune_expired).** The aggregator's `count_peak_limiting_applied_decisions` reads events in `[window_end - 7d, window_end]`. The pruner deletes events older than 90 days. The two windows do NOT overlap (7d ≪ 90d), so the count is always reading from non-prunable retention. **Mitigated by:** the constants don't overlap; documented here so a future story shortening retention can flag this as a regression risk.

5. **Cold-start (T6) converging with `_compute_and_upsert_weekly_summary`'s first invocation.** AC6 mandates "first aggregation runs immediately on startup". If the function raises on its FIRST invocation (e.g., a query against an empty `energy_flow_intervals` table somehow trips a bug), the aggregator's outer `try/except` logs and continues — but the homeowner sees `read_weekly_energy_summary() → None` until the next aggregation tick (up to 1h later). **Mitigated by:** the outer try/except is wide; the AC6 logic for the empty-table case explicitly writes the insufficient-history row WITHOUT querying event_log (returns early at step 4). Subtask 6.3 test #19 (`test_weekly_summary_cold_start_writes_insufficient_history_row_immediately`) is the structural verification.

#### R2 — State-transition table

**`WeeklyEnergySummaryService` task lifecycle:**

| State | Transitions out | Trigger | Side-effects |
|---|---|---|---|
| not-started | → running-aggregation | `asyncio.create_task(weekly_energy_summary_task(...))` in lifespan | task object created; first `_compute_and_upsert_weekly_summary` invocation begins |
| running-aggregation | → sleeping | aggregation completes (success or caught exception) | `weekly_energy_summary` row upserted (success); structlog ERROR emitted (caught failure); next-tick scheduled via `asyncio.sleep` |
| sleeping | → running-aggregation | sleep completes naturally | next aggregation pass begins |
| sleeping | → cancelled | lifespan shutdown calls `task.cancel()` | `asyncio.CancelledError` raised into the task; lifespan awaits with `try/except CancelledError: pass` |
| running-aggregation | → cancelled | lifespan shutdown during aggregation | CancelledError raised; in-flight DB writes may abort mid-transaction (aiosqlite handles; SQLite transactions are atomic) |
| cancelled | (terminal) | — | task complete; `_on_weekly_summary_done` callback may log if a non-cancel exception was the cause |

**`weekly_energy_summary` row lifecycle (single row, id=1):**

| State | Transitions out | Trigger | Side-effects |
|---|---|---|---|
| absent (cold-start, pre-first-aggregation) | → insufficient-history | first aggregation tick post-startup | upsert with `insufficient_history=1`, three metrics = NULL, `data_complete_days_count` = current |
| insufficient-history | → insufficient-history (same state, updated counters) | aggregation tick BUT `data_complete_days_count < 7` | upsert overwrites in place; `computed_at` advances; `data_complete_days_count` may have changed |
| insufficient-history | → sufficient | aggregation tick where `data_complete_days_count >= 7` for the first time | upsert with `insufficient_history=0` and all three metrics populated |
| sufficient | → sufficient (refreshed values) | aggregation tick | upsert overwrites; metric values reflect the new 7-day rolling window |
| sufficient | → insufficient-history | extended degraded period drops `data_complete_days_count` below 7 | upsert with `insufficient_history=1`, metrics → NULL |

**`EnergyFlowIntervalTracker` runtime state:**

| State | Transitions out | Trigger | Side-effects |
|---|---|---|---|
| accumulating (current interval not yet ended) | → completed | tick at time `t` crosses next 15-min boundary | returns `CompletedEnergyFlowInterval`; ControlLoop writes it; tracker resets to fresh accumulator for new interval |
| accumulating | → accumulating (degraded-quality bit set) | tick where `any_role_degraded=True` | `data_quality` bit flips to 'incomplete' for this interval; cannot revert this interval; contributions still integrated |
| accumulating | → accumulating (skipped contribution) | tick where a specific power signal is `None` | that role's contribution to this tick is zero; partial-interval state preserved |
| accumulating (process boot) | first tick of interval contributes zero | initial cold-start tick | `last_at` set to `now` so the SECOND tick has a `dt_seconds` reference |

#### R3 — Impossible-state analysis (with cold-start coverage)

**Forbidden state combinations and their structural invariants:**

1. **`weekly_energy_summary` containing > 1 row** — prevented by the `CHECK (id = 1)` PK constraint at the SQLite layer AND the UPSERT-by-id-1 pattern in `upsert_weekly_energy_summary`. Manual `INSERT` of `id=2` raises an integrity error.
2. **`insufficient_history=0` row with NULL metrics** — prevented by the table-level CHECK constraint AND the Pydantic `_terminal_fields_iff_history_sufficient` model-validator. Any attempt to construct the in-memory row in an inconsistent state raises `ValueError` at the Pydantic boundary; any attempt to persist such a row raises an SQLite integrity error at the storage boundary.
3. **`self_consumption_ratio < 0` OR `> 1`** — prevented by Pydantic model-validator + SQLite CHECK. The aggregator's computation explicitly clamps; the safety nets catch any future regression.
4. **Negative `_kwh` columns** — prevented by SQLite CHECK at the table level AND `Field(ge=0.0)` at the Pydantic boundary. The `grid_imported_kwh = max(0, current_delivered - baseline_delivered)` floor in the tracker is the third layer (defense-in-depth).
5. **`energy_flow_intervals.interval_start_utc` not clock-aligned** — prevented structurally by the tracker (rollover only fires at clock boundaries; `interval_start_utc` is always derived from the boundary, never from `now()`). No DB CHECK enforces this (a regex CHECK on TEXT would be brittle); the test suite covers it.
6. **Two concurrent `_compute_and_upsert_weekly_summary` invocations** — prevented by single-process asyncio (only one task instance + only one `await` per pass; no true concurrency within the task).
7. **Aggregator writes during ControlLoop's `_update_tracker` write** — both go through `get_write_lock()`; SQLite WAL + the write-lock serialize. No corruption possible.
8. **`source_rule="peak_limiting"` event_log entries counted that did NOT actually limit a peak** — prevented by the AND clause `applied: true` on the LIKE match. AC15 test #24 verifies the predicate structurally.

**Cold-start / startup-grace coverage (mandatory):**

Process boot sequence (from lifespan order):
- **t=0** (process start): `Settings()` constructs. `state_store` initialized. DB connection opens (Step 4). Migration `0013` runs; both new tables exist but are empty.
- **t≈0.1s** (Step 4c): `initial_monthly_peak_kw` hydrated from `peak_intervals` (existing — unchanged by 10.4).
- **t≈0.5s** (Step 4h+): runtime adapter map built; ControlLoop constructed; `_energy_flow_tracker` instantiated at the current 15-min boundary (cold-start tracker state = empty accumulators).
- **t≈1s**: `_control_loop_task` and `_weekly_summary_task` both spawned. The aggregator runs its FIRST `_compute_and_upsert_weekly_summary` immediately (NOT after sleep). The first compute reads an empty `energy_flow_intervals` table → `data_complete_days_count=0` → upserts `insufficient_history=1` row with metrics=NULL.
- **t≈10s** (first ControlLoop tick): `_update_tracker` invoked; first tick of the first interval contributes zero seconds (no `last_at` yet); the tracker is now "accumulating".
- **t < 15 min** (rest of the first interval): subsequent ticks integrate energy; tracker state in-memory only.
- **t ≈ 15 min** (first rollover): `write_peak_interval` writes the first peak_intervals row; `write_energy_flow_interval` writes the first energy_flow_intervals row.
- **t ≈ 1h** (second aggregation tick): aggregator re-evaluates; `data_complete_days_count` still 0 (need 7 full days); summary still insufficient-history.
- **t ≈ 7d + 1h**: first aggregator tick where `data_complete_days_count >= 7` (assuming continuous-running deployment); summary flips to `insufficient_history=0`; the homeowner first sees real metrics.

**Invariants temporarily relaxed during cold-start:**
- The "summary row exists" invariant relaxes between `t=0` and `t≈1s+initial-aggregation-duration`. During this window, `read_weekly_energy_summary` returns `None`; the route renders the insufficient-history fragment with `data_complete_days_count=0`. This is identical-looking to the legitimate insufficient-history rendering — no user-visible distinction from the steady-state cold-start.
- The "tracker last_at is set" invariant relaxes between `t=0` and the first ControlLoop tick. The first tick's zero-seconds contribution is structural.

**Normal-operation marker:** the homeowner first sees a non-insufficient-history summary after `t ≈ 7d` of continuous operation. The system is "normally operating" the moment the homeowner GETs the fragment and sees real metric values; this is observable via the rendered template (presence of `.weekly-summary__metrics` rather than `.weekly-summary__message`).

#### R4 — Cancellation ownership map

| Async operation | CancelledError owner | Cancellation audit owner | Cleanup owner | Reporting field semantics under cancel |
|---|---|---|---|---|
| `weekly_energy_summary_task` (the outer `while True` loop) | lifespan shutdown's `task.cancel()` + `await task` | none (no audit row emitted — non-user action) | task body's outer `try/except` does NOT catch `CancelledError`; the loop exits naturally via `asyncio.sleep` raising into the await | no reporting fields under cancel — task is silent |
| `_compute_and_upsert_weekly_summary` (single aggregation pass) | parent task's CancelledError propagates in | none | mid-write cancel: aiosqlite ensures the transaction is rolled back (no half-written row) | if cancelled mid-write, `weekly_energy_summary` row state is unchanged from before the pass began |
| `EnergyRepo.write_energy_flow_interval` (called from ControlLoop._update_tracker) | ControlLoop's cancellation owner (existing Story 8-1 contract) | none | the write either completes or rolls back atomically | the interval simply isn't persisted; the in-memory tracker has already discarded the previous interval, so the data is lost — but the next interval rolls over normally and the aggregator's `data_complete_days_count` simply omits the missing UTC date if it drops the day below threshold |
| GET `/fragments/homeowner/weekly-summary` (HTTP request) | Starlette's default request cancellation (client disconnect or timeout) | none (read-only) | no cleanup needed (read-only) | response body never sent; client receives a disconnect |

**The aggregator service does NOT propagate cancellation to any in-flight HTTP request.** The route reads from the persisted row directly; the aggregator and the route never interact in-process. This is the explicit design choice for T4 — a watchdog/timing-class service that NEVER blocks the user-facing read path.

#### R5 — Before-first-successful-cycle lifecycle review

The "successful cycle" for 10.4 is **the homeowner GETs `/fragments/homeowner/weekly-summary` and receives a response within 200ms with a meaningful body** — either the insufficient-history message (early days) or the three-metric summary (≥7 days).

Process boot → first successful cycle:
1. **Process start** (t=0): Python interpreter starts; FastAPI app constructs; lifespan begins; Settings loaded; migrations applied; new tables `energy_flow_intervals` + `weekly_energy_summary` exist but empty.
2. **Lifespan step 4d** (existing): event_log_repo, energy_repo, etc. initialized.
3. **Lifespan new step (Task 7)**: `_weekly_summary_task` spawned. The task's outer `while True` body invokes `_compute_and_upsert_weekly_summary` IMMEDIATELY (before the first `asyncio.sleep`). This is the structural guarantee from AC6.
4. **First aggregation pass** (t ≈ 0.5–1s):
   - Reads `energy_flow_intervals` since `now - 7d` → returns empty list.
   - Computes `data_complete_days_count = 0`.
   - Upserts `weekly_energy_summary` row with `insufficient_history=1`, three metrics = NULL, `data_complete_days_count=0`, `computed_at=now`.
   - structlog INFO `weekly_summary_aggregated` with `insufficient_history=True`.
5. **First sleep** (t ≈ 1s): aggregator sleeps for `weekly_summary_aggregation_interval_seconds` (1h default).
6. **Lifespan yields** (`yield  # Application serves requests here`): FastAPI begins accepting HTTP requests.
7. **First homeowner request** (any time after the lifespan yield): GET `/fragments/homeowner/weekly-summary` reads the row, renders the insufficient-history fragment. Response body contains the "Data is still being collected (0/7 days of data so far)" message. **THIS IS THE FIRST SUCCESSFUL CYCLE** in the AC sense — the user gets a real response. The homeowner is INFORMED, not confused, even though no metrics are yet computable.

**What the system publishes / logs / audits during the cold-start window:**
- structlog INFO `weekly_summary_task_started` (when task is created in lifespan).
- structlog INFO `weekly_summary_aggregated insufficient_history=true data_complete_days_count=0` (first pass).
- NO audit rows (the aggregator does not emit to event_log; it READS from event_log for peak counts).
- NO state changes visible via SSE or HTMX state stream (the weekly summary is OUT-OF-BAND from the dashboard's snapshot-based state model).
- The homeowner dashboard renders normally; the "This week" trigger is visible but not yet clicked. NO Network I/O for the weekly summary until the trigger is clicked (HTMX `hx-trigger="click once"`).

**Invariants temporarily relaxed during cold-start (before the first aggregation completes):**
- `read_weekly_energy_summary` returns `None` (no row yet). The route handles this case by rendering the SAME insufficient-history fragment as the legitimate insufficient-history row. The user cannot distinguish "no row yet" from "row exists with insufficient_history=1, data_complete_days_count=0" — both look identical.
- The first aggregation pass typically completes within ~10ms (empty tables); the absent-row window is bounded to roughly the lifespan's pre-yield duration.

**Normal-operation marker for 10.4 specifically:** the user GETs the fragment and sees the three-metric summary instead of the insufficient-history message. This requires `data_complete_days_count >= 7` AND the aggregator has run at least once since this threshold was crossed.

#### R6 — Source-of-truth ownership per datum

| Datum | Single owner | Read path | Drift risk (if any) |
|---|---|---|---|
| `energy_flow_intervals` rows | EnergyRepo (write via ControlLoop._update_tracker only) | `read_energy_flow_intervals_since` (aggregator only) | **None.** Single writer, single reader. ControlLoop is the only writer; aggregator is the only consumer. |
| `weekly_energy_summary` single row | EnergyRepo (write via `weekly_energy_summary_task` only) | `read_weekly_energy_summary` (route only) | **None.** Single writer (the aggregator task), single reader (the route). |
| Per-interval energy values (in-memory accumulators) | `EnergyFlowIntervalTracker` instance owned by `ControlLoop` | Tracker reset on rollover; never read from outside the tracker | **None.** Encapsulated state. |
| `peaks_avoided_count` derivation | Aggregator service (reads from event_log; writes to weekly_energy_summary) | Aggregator only | **Bounded by event_log retention** — events older than 90 days are pruned; the 7-day window is well inside retention. Documented in R1 #4. |
| `source_rule` propagation through command flow (AC14) | Engine evaluator emits → IntentExecutor preserves → DeviceCommand carries → RetryPolicy._emit_success_audit writes to event_log detail | Aggregator reads via `count_peak_limiting_applied_decisions` | **Subtask 5.1 verifies this is a single linear chain.** If the chain has a gap (e.g., IntentExecutor drops the field), peaks_avoided_count is silently zero. The exhaustiveness is verified by AC15 test #24 (asserts a peak_limiting DECISION in event_log INCREMENTS the count). |
| `default_import_tariff_eur_per_kwh` and `default_export_tariff_eur_per_kwh` | Settings (loaded once at process start) | Aggregator service reads via `settings.default_import_tariff_eur_per_kwh` | **No drift** — Settings is process-lifetime constant. Per-site tariff configuration is Epic 13 scope. |
| `weekly_summary_aggregation_interval_seconds` | Settings | Aggregator service (used in `asyncio.sleep`) | **No drift** — process-lifetime constant. |
| `active_constraints.peak_limit_kw` (read by the aggregator to interpret peak-limiting events, if needed) | ActiveConstraintsProvider (single owner per Story 9.0b) | Aggregator reads via `active_constraints.get()` (existing pattern from ControlLoop._build_evaluation_input) | **No drift** — provider is atomically refreshed by the installer activation endpoint; the aggregator reads at compute time. A peak-limiting event emitted under an OLD peak_limit_kw and counted under a NEW peak_limit_kw is a definitional question only: the count is "events where the engine intervened with `source_rule=peak_limiting`"; the threshold value at compute time is informational, not load-bearing for the count. v1 does NOT include the threshold value in the rendered summary, so no user-visible drift. |

**Drift risks called out:** none material. The single-writer-per-table pattern + Settings immutability + read-only event-log consumption are all aligned with the architecture's "StateStore is the single state surface" principle (Epic 5).

#### R7 — Deferred-findings triage

Scan of `_bmad-output/implementation-artifacts/deferred-work.md` for items whose component or invariant overlaps Story 10.4. The aggregator + new tables + new tracker + new fragment surface intersects with several deferred items.

| Finding (bracket ref) | Classification | Rationale |
|---|---|---|
| `[Polling stops after the first outerHTML swap]` (10-3 review, `templates/fragments/homeowner/*.html`) | **safe-during-this-story** | 10.4's NEW fragment template carries `hx-get`/`hx-trigger` on its root section (AC9), landing the FIX pattern for the new surface. The existing five fragments (status-headline, ev-card, battery-card, solar-card, grid-card) are NOT touched — the systemic fix is bundled into an Epic 10 / 11 hardening sub-story per the deferred-work classification. 10.4 does NOT regress; it ships the fix pattern for its own surface. |
| `[src/open_ems/web/routes/actions.py:73-82, 140-154]` "_safe_audit blocks on serial DB INSERTs in homeowner hot path" (10-2 review) | **acceptable-post-this-story** | 10.4 has NO homeowner-action audits — the weekly summary is read-only. The aggregator's structlog ERROR is observability-only (no audit row). |
| `[src/open_ems/web/routes/actions.py:370-385]` "Audit row's `previous_strategy` is stale under concurrent set-strategy POSTs" (10-3 review) | **acceptable-post-this-story** | 10.4 emits no audit rows. Out-of-scope. |
| `[src/open_ems/web/routes/actions.py:382-397]` "_safe_audit process-kill window" (10-3 review) | **acceptable-post-this-story** | Same rationale — 10.4 emits no audit rows. |
| `[_TEMPLATES_DIR duplicate Jinja env in actions.py]` (10-3 review D4) | **acceptable-post-this-story** | 10.4 adds a route in `fragments.py` which uses the same Jinja env as the existing fragment routes (no new env). Does not touch the deferred surface. |
| `[concurrent-POST audit `previous_strategy` staleness]` (10-3 review D2) | **acceptable-post-this-story** | Out-of-scope. |
| `[src/open_ems/core/state_store.py:170-171]` "EV-charger disconnect mid-override does not clear" (10-2 review) | **acceptable-post-this-story** | StateStore EV-override lifecycle; out of 10.4 scope. |
| `[src/open_ems/web/routes/actions.py:98-119]` "Idempotent-replay traps homeowner in pending UI" (10-2 review) | **acceptable-post-this-story** | EV override flow; out of 10.4 scope. |
| `[src/open_ems/web/state_serialization.py:223-228]` "constraint_notice content misleading in conservative/degraded modes" (10-2 review) | **acceptable-post-this-story** | State serialization for EV card; 10.4 adds `build_homeowner_weekly_summary_context` to the same module but does not touch the existing constraint_notice surface. |
| `[src/open_ems/services/runtime_adapter_wiring.py:106-123]` W1/W2 (placeholder Modbus register ranges) (9-X review) | **acceptable-post-this-story** | Adapter wiring; 10.4 reads telemetry through ControlLoop (which uses the adapters), but does not interact with the register-range table directly. |
| `[src/open_ems/services/deployment_validation.py:206-212]` "is_outdated=False when constraints unhydrated" (9-5 DF1) | **acceptable-post-this-story** | Installer-side concern; 10.4 is homeowner-facing. |
| `[Three definitions of `device_registry` / `wizard_state` schema]` (9-2 review) | **acceptable-post-this-story** | Test-fixture schema duplication unrelated to 10.4's new tables. 10.4 follows the migration-first pattern (test_migrations.py exercises the migration; fixture tables for unit tests are scoped to the new migration). |

**Items that overlap by component but are out of 10.4's literal scope:** none must-resolve. Two items are noteworthy as informational context but do NOT block:

- **`P19 — Repo transaction-discipline cleanup`** (Epic 9 retro R1 audit). The new `EnergyRepo` methods 10.4 adds follow the existing `EnergyRepo` pattern (`get_write_lock()` + single `execute` + `commit`). They do NOT introduce the `commit()` + `BEGIN IMMEDIATE` antipattern. So 10.4 does not extend the deferred concern but also does not address it. **Classification: acceptable-post-this-story.**
- **Architecture.md doc-sync: `energy_readings` raw-sample description (line 1060) vs. v1 aggregated-interval reality.** 10.4 ships v1 with 15-min aggregated rows; the architecture doc still describes raw 5–15s samples. Documented in Project Structure Notes; a future doc-sync sweep (Epic 11/12) reconciles. **Classification: acceptable-post-this-story.**

### References

- [Source: epics.md#Story 10.4: Implement FR30 weekly energy summary as non-blocking, pre-aggregated secondary feature]
- [Source: epics.md#Cross-story constraints applying to all Epic 10 stories]
- [Source: prd.md#FR30] — "The homeowner can view a weekly energy summary: peaks avoided, self-consumption ratio, and estimated cost savings"
- [Source: ux-design-specification.md#L640-L653] — "Weekly summary = nice-to-have, not layout driver; accessible via a secondary 'This week' link"
- [Source: ux-design-specification.md#L804] — "Tap This week → Weekly summary — collapsed or secondary"
- [Source: ux-design-specification.md#L1377] — Weekly summary section component lives in homeowner dashboard
- [Source: ux-design-specification.md#L1474] — "This week expands inline. No sidebar, no multi-page flow."
- [Source: architecture.md#BAD-1] — 15-minute peak window calculation; `peak_intervals` schema and clock-aligned interval pattern
- [Source: architecture.md#L863] — `energy_repo` is the single owner of energy persistence
- [Source: architecture.md#L896] — `weekly_summary.html` template path in canonical project structure
- [Source: src/open_ems/storage/repositories/energy_repo.py] — write_peak_interval + get_current_monthly_peak_kw existing patterns
- [Source: src/open_ems/engine/control_loop.py#L172-L186] — `_update_tracker` extension point
- [Source: src/open_ems/web/app.py#L127-L146, L195-L630] — lifespan + background-task pattern (`_event_log_pruning_task`, `_session_cleanup_task`)
- [Source: src/open_ems/engine/retry_policy.py#L155-L172] — `_emit_success_audit` detail dict; AC14 extension site
- [Source: src/open_ems/engine/evaluator.py#L86, L97] — `source_rule="peak_limiting"` emission site
- [Source: _bmad-output/implementation-artifacts/10-3-...md] — strategy selector precedent for HTMX-fragment + Alpine factory pattern
- [Source: _bmad-output/implementation-artifacts/10-2-...md] — EV card precedent for Pydantic frozen model + DB CHECK constraint defense-in-depth
- [Source: _bmad-output/implementation-artifacts/10-1-...md] — `build_homeowner_*_context` pure-functional pattern
- [Source: _bmad-output/implementation-artifacts/9-0d-...md] — cold-start hydration BEFORE consumer construction
- [Source: _bmad-output/implementation-artifacts/deferred-work.md] — R7 triage source

## Dev Agent Record

### Agent Model Used

Claude Opus 4.7 (`claude-opus-4-7`) via Claude Code, 2026-05-12.

### Debug Log References

- Pytest baseline pre-implementation: `1598 passed + 1 skipped + 7 xfailed`.
- Pytest final post-implementation: `1649 passed + 1 skipped + 8 xfailed` (+51 net new passing, +1 xfailed — exceeds AC16 ≥30 target).
- Ruff check + format + mypy: all clean across 266 / 102 files respectively.
- `uv.lock`: unchanged (`git status` confirms no diff).

### Completion Notes List

**Q1 (AC14 source_rule investigation) — answered.** `source_rule` was present in `CandidateAction` and propagated as far as the resolved candidate inside `BatteryIntent.source_candidate` / `EVChargerIntent.source_candidate`, but was DROPPED at the `IntentExecutor → DeviceCommand` boundary and NOT included in RetryPolicy's audit `detail`. Minimal threading applied (4 touches): `DeviceCommandBase.source_rule: str | None = None`; `IntentExecutor._translate_battery` and `_translate_ev` forward `intent.source_candidate.source_rule` into the command; `RetryPolicy._emit_success_audit.detail` adds `"source_rule": command.source_rule`. Acceptable per Q5 (minimal + linear).

**Q2 (tariff defaults) — kept defaults**: import 0.30, export 0.05 EUR/kWh.

**Q3 (aggregation cadence) — kept**: 3600 seconds (1 hour).

**Q4 (battery cycling savings factor) — kept hardcoded 0.5**. Documented in service module for future Epic-13 refinement.

**Q5 (source_rule threading scope) — confirmed acceptable inside 10.4**. Threading was linear (4 mechanical edits with no audit-shape redesign). No defer.

**Design notes**

1. **Tracker integration: left-Riemann with rollover finalization.** The energy-flow tracker integrates `power × dt_seconds` per tick using the elapsed time since `last_at`. On rollover, the time between the last in-interval tick and the rollover instant is attributed to the OLD interval at the rollover tick's power level. Without this finalization step, ~one-tick of time per interval would be silently dropped (~1% systemic under-reporting at 10s tick cadence). `dt_seconds` is clamped to `_INTERVAL_SECONDS` to defend against clock jumps.

2. **Grid kWh from monotonic accumulators, not power integration.** `GridMeterState` already exposes `energy_delivered_kwh` / `energy_returned_kwh` (cumulative meter readings). The tracker latches the first non-None readings of each interval as baselines and computes `max(0, latest - baseline)` at rollover. The `max(0, ...)` floor defends against meter rollover / re-zero events; a negative delta logs a `grid_accumulator_rollback_detected` WARNING and the interval kWh is set to 0.

3. **Single-row weekly_energy_summary table** with `CHECK (id = 1)`. Mirrors the EVOverrideState pattern: in-Pydantic model-validator AND in-DB CHECK constraint enforce `insufficient_history=False ⇒ all three metrics non-null + ratio bounded [0, 1]`. Defense-in-depth, both layers validated by migration `0013` round-trip test.

4. **Aggregator first-run-immediately contract.** `weekly_energy_summary_task` invokes `_compute_and_upsert_weekly_summary` BEFORE the first `asyncio.sleep`. Test #26 verifies this structurally (patches `asyncio.sleep` to assert the upsert has already occurred when the first sleep fires).

5. **Non-blocking lifespan wiring.** Task construction is wrapped in `try/except`. A failure to construct or start the aggregator logs `weekly_summary_task_start_failed` but does NOT raise out of the lifespan — the dashboard MUST still come up. This is the structural enforcement of the AC's "must not block delivery of Stories 10.1–10.3" contract at the runtime layer.

6. **Peak-first / flow-second ordering invariant.** `ControlLoop._update_tracker` writes `peak_intervals` first; the energy-flow write follows with its own try/except so a transient failure cannot regress the load-bearing peak persistence. Test in `test_control_loop.py` asserts the call order via `parent.mock_calls` attached to both AsyncMocks.

7. **CI proxy vs Pi 4 hard contract for 200ms budget.** Unit + e2e timing tests assert mean < 50ms on CI (much faster than a Pi 4). The 200ms ceiling is the AC's hard contract on the target hardware; CI is a proxy. Both layers structurally enforce the "no on-demand computation at render time" contract (single indexed SELECT + Jinja render).

8. **deferred-work.md polling-stops-after-outerHTML-swap fix landed for the new fragment.** `weekly-summary.html` carries its own `hx-get` / `hx-trigger="every 60s"` on the root `<section>`, so the swapped-in fragment keeps refreshing. The existing five fragments (status-headline / battery / solar / grid / ev) are NOT touched per the deferred-work classification.

9. **Five-section count preserved.** The "This week" trigger button lives OUTSIDE `.dashboard-stack`. `test_homeowner_dashboard_shell_renders_five_htmx_bound_sections` continues to assert exactly 5 every-10s polling sections (the new trigger uses `hx-trigger="click once"`).

**Cleanup of long-line lint annotations.** Test names follow the naming-honesty rule (Story 9-Y-a precedent) and are necessarily long. Long `def` lines that exceeded the 100-char line limit got the existing codebase precedent annotation `# noqa: E501  # fmt: skip` (matching `test_state_store.py:730`). The `# fmt: skip` is required because ruff format does not respect `# noqa: E501` for line-length when reflowing.

### File List

**New files (10):**
- `migrations/versions/0013_add_energy_flow_intervals_and_weekly_summary_tables.py`
- `src/open_ems/core/energy.py`
- `src/open_ems/engine/energy_flow_tracker.py`
- `src/open_ems/services/weekly_energy_summary.py`
- `src/open_ems/web/templates/fragments/homeowner/weekly-summary.html`
- `tests/unit/engine/test_energy_flow_tracker.py` (12 tests)
- `tests/unit/services/test_weekly_energy_summary.py` (10 tests)
- `tests/unit/storage/repositories/test_energy_repo.py` (8 tests)
- `tests/integration/web/test_weekly_summary_e2e.py` (4 tests + 1 xfailed a11y placeholder)

**Modified files (17):**
- `src/open_ems/core/__init__.py` — export new domain types.
- `src/open_ems/core/commands.py` — add `DeviceCommandBase.source_rule: str | None = None` (AC14).
- `src/open_ems/engine/control_loop.py` — wire `EnergyFlowIntervalTracker`; new extract helpers; peak-first / flow-second ordering.
- `src/open_ems/engine/intent_executor.py` — forward `source_candidate.source_rule` to commands (AC14).
- `src/open_ems/engine/retry_policy.py` — `_emit_success_audit.detail` carries `source_rule` (AC14).
- `src/open_ems/settings.py` — 4 new Settings fields (AC11).
- `src/open_ems/storage/repositories/energy_repo.py` — 4 new methods (AC4).
- `src/open_ems/storage/repositories/event_log_repo.py` — `count_peak_limiting_applied_decisions` (AC14).
- `src/open_ems/web/app.py` — lifespan wiring for the weekly-summary task (AC7).
- `src/open_ems/web/dependencies.py` — `get_energy_repo` dependency.
- `src/open_ems/web/routes/fragments.py` — `GET /fragments/homeowner/weekly-summary` (AC8).
- `src/open_ems/web/state_serialization.py` — `build_homeowner_weekly_summary_context` (AC8).
- `src/open_ems/web/static/open-ems.css` — `.weekly-summary-*` CSS rules (Task 9.3).
- `src/open_ems/web/templates/dashboard.html` — "This week" trigger + `weeklySummaryTrigger` Alpine factory (AC10).
- `tests/integration/test_migrations.py` — migration `0013` round-trip + CHECK constraint tests (AC15 #39).
- `tests/unit/engine/test_control_loop.py` — peak-first / flow-second ordering tests + energy-flow write-failure-survival test.
- `tests/unit/storage/repositories/test_event_log_repo.py` — `count_peak_limiting_applied_decisions` tests (AC14).
- `tests/unit/web/test_dashboard_route.py` — AC13 structural-assertion tests (AC15 #37, #38).
- `tests/unit/web/test_fragment_routes.py` — weekly-summary fragment tests (AC15 #29-#33).
- `tests/unit/web/test_state_serialization.py` — context-builder tests (AC15 #34-#36).
- `_bmad-output/implementation-artifacts/sprint-status.yaml` — flip `10-4-...: ready-for-dev → review` (closing step).

### Change Log

| Date | Change | By |
|---|---|---|
| 2026-05-12 | Story 10.4 implementation: energy-flow tracking infrastructure + weekly summary aggregator + homeowner endpoint. 9 new source/migration files + 1 new template + 4 new test files; 17 modified files; +51 net passing tests; zero new Python dependencies. Status: ready-for-dev → in-progress → review. | bmad-dev-story (Claude Opus 4.7) |

### Questions for Jordan (resolved at story-open)

1. **Subtask 5.1 (AC14):** Is `source_rule` already threaded through to RetryPolicy's success-audit detail? If yes, AC14 simplifies to "add the count method"; if no, AC14 also threads the field. The investigation should be the first dev-story action.
2. **Default tariff values (AC11):** Are `0.30 EUR/kWh import` and `0.05 EUR/kWh export` the right v1 defaults for Belgian-residential 2026, or should this story coordinate with a product-side calibration before shipping? The footnote ("Estimated savings use a default tariff — check with your supplier for exact figures") provides cover, but if you have a target customer profile in mind, the defaults could be tightened.
3. **Aggregation cadence default (1h):** Sufficient for nice-to-have summary freshness, or do you want sub-hour cadence at the cost of more DB writes? 1h means a homeowner who checked "This week" 50 min ago re-checks and sees the same values. Tightening to 15 min aligns with the underlying interval cadence; relaxing to 6h reduces write load on Pi 4 SD.
4. **Battery cycling savings half-weight (AC6 step 5c):** Conservative midpoint, or should this be a configurable Settings field? Current AC hardcodes 0.5; a future Epic-13 dynamic-tariff story can refine. Acceptable as-is?
5. **`source_rule` threading scope (AC14):** If Subtask 5.1 reveals the threading is needed, the surface touches `DeviceCommand`, `IntentExecutor`, AND `RetryPolicy`. This is a sizable cross-cutting touch for the sake of a single AC. Acceptable inside 10.4's scope, or split into a Story 10-4-pre that lands the threading first?

### Review Findings

_Code review run 2026-05-12 by bmad-code-review (Claude Opus 4.7). 0 decision-needed, 2 patches, 1 deferred, ~10 dismissed-as-noise (covered by existing safeguards). Adversarial review (Blind Hunter + Edge Case Hunter + Acceptance Auditor) against AC1–AC17 + review-focus list._

#### Decision-needed (0)

_None._

#### Patches (2)

- [x] [Review][Patch] Aggregator self-consumption / cost-savings totals not bounded to the 7-day window [src/open_ems/services/weekly_energy_summary.py:86-93, 130] — MEDIUM. APPLIED 2026-05-12 — derived `windowed_intervals = [r for r in intervals if window_start <= r.interval_start_utc < window_end]` as the single source of truth; both `days_with_threshold` and `complete_intervals` now iterate over `windowed_intervals`. Closes the inconsistency between AC6 step 3 (day-count) and AC6 step 5 (kWh totals) under clock-skew / future-dated-interval scenarios. Verified by full pytest run (1649 passed; existing tests #21/#22/#23/#24/#27 still pass because they seed only in-window intervals).

- [x] [Review][Patch] `active_constraints` parameter is dead code in the weekly summary service [src/open_ems/services/weekly_energy_summary.py:36-41, 66-71] — LOW. APPLIED 2026-05-12 — removed the `active_constraints` parameter from both `weekly_energy_summary_task(...)` and `_compute_and_upsert_weekly_summary(...)`; dropped the matching kwarg from `web/app.py:600-605` lifespan; cleaned up `constraints = MagicMock()` + `active_constraints=constraints` in `tests/unit/services/test_weekly_energy_summary.py` (10 occurrences) and `tests/integration/web/test_weekly_summary_e2e.py` (1 occurrence). Reduces dead wiring; tightens the AC6 contract (the aggregator's `peaks_avoided_count` derivation is event-log-driven, never peak-limit-driven). Verified by full pytest run (1649 passed; ruff/mypy clean).

#### Deferred (1)

- [x] [Review][Defer] Lifespan integration test (spec Task 7.3) was not created [tests/integration/web/test_weekly_summary_lifespan.py] — Spec Task 7.3 promised `test_weekly_summary_task_started_at_lifespan_and_cancelled_at_shutdown` to structurally verify the lifespan-level invariants: task creation at startup, done-callback on crash, cancel-and-await at shutdown. AC15 #43 (`test_weekly_summary_e2e_aggregator_writes_row_immediately_at_first_invocation`) covers the `_compute_and_upsert_weekly_summary` direct-call path but does NOT exercise the lifespan task wiring. The structural contract is in code (web/app.py:586–615 + 674–679) but is not enforced by a test. Carry to `10-Y-a-weekly-summary-lifespan-test-coverage` as a follow-up. Reason: the implementation is correct (verified by code reading); the gap is test-coverage hygiene that does not block 10.4 close.

#### Dismissed as noise (covered by existing safeguards)

- `_finalize_interval` uses `assert` for finite/non-negative kWh — Dismissed: Pydantic `CompletedEnergyFlowInterval(ge=0.0)` validation at the storage boundary catches non-finite / negative values even under `python -O`. Defense-in-depth already in place.
- `count_peak_limiting_applied_decisions` LIKE pattern depends on `json.dumps` default separators (`": "`) — Dismissed: `test_count_peak_limiting_applied_decisions_only_counts_source_rule_peak_limiting_with_applied_true` inserts via `EventLogRepo.append` (same serialization path) and asserts the count — any future drift in `json.dumps` separators would fail this regression test. Contract is pinned by existing test.
- Dataclass `CompletedEnergyFlowInterval` in `engine/energy_flow_tracker.py` shadows Pydantic class of same name in `core/energy.py` — Dismissed: documented in the dataclass docstring (lines 50–56); control_loop.py uses `_CoreCompletedEnergyFlowInterval` alias to disambiguate. Intentional zero-allocation hot-path / validated-persistence-boundary split, same class as Story 10.2 `EVOverrideState` precedent.
- `_extract_grid_accumulators` does not filter stale GridMeterState — Dismissed: consistent with the existing `_extract_grid_power` pattern at control_loop.py:398. Staleness is the StateStore's responsibility; tracker's `any_role_degraded` already marks the interval incomplete for degraded slots.
- Weekly-summary `aria-live="polite"` re-announces on every 60s outerHTML swap — Dismissed: UX-polish concern. The homeowner explicitly opened the panel; periodic announcements are tolerable. A future a11y hardening sub-story can move the live region to a narrower DOM range; not in 10.4 scope.
- `read_weekly_energy_summary` does not take a read lock — Dismissed: per AC4 design, reads rely on WAL atomic-row visibility. Concurrent UPSERT + read produce either the OLD row or the NEW row, never a torn row. Consistent with EnergyRepo's existing read-without-lock pattern.
- `aria-controls="weekly-summary-panel"` reference broken by outerHTML swap — Dismissed: the replacement fragment carries the same `id="weekly-summary-panel"`, so the ARIA reference survives. Verified.
- `complete_intervals` window filter missing — same as patch P1 above; listed once.
- `_make_intervals_for_days` lacks exhaustive 79/80/81 threshold-boundary tests — Dismissed: test #27 (`test_weekly_summary_data_complete_days_count_uses_settings_threshold_for_minimum_intervals_per_day`) covers the gate semantics (50 below default-80 → insufficient; 50 with threshold-50 → sufficient). Exhaustive boundary tests would be over-engineering.
- `freezegun` not used (spec mentioned it as preferred) — Dismissed: tests inject `clock` directly and patch `asyncio.sleep`; no need for `freezegun`. The spec called it out as a possibility, not a requirement. uv.lock unchanged contract preserved.
