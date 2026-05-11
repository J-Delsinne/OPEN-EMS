# Story 10.1: Implement homeowner dashboard layout with SystemOperatingMode-driven headline and metric cards

Status: done
_Status set to done by bmad-code-review on 2026-05-11 after review-closure gate passed (5b): 11 resolved, 0 dismissed, 0 deferred-and-verified (3 defer items marked [x] in-cycle and entered in deferred-work.md)._

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

> **Type:** first homeowner-visible UI story. Epic 10 entry point.
> **A2-triggered:** **No.** Trigger evaluation performed (see Dev Notes → "A2 trigger evaluation"); none of T1–T7 matched. Story is a UI presentation layer over read-only StateStore snapshots and adds one snapshot field (`active_strategy`) wired to the existing ControlLoop. No new state machine, retry path, persistence surface, watchdog, multi-adapter coordination, restart behavior, or installer workflow.
> **Prerequisite:** Story 9-X (Wire adapter map into PolicyGuard and ControlLoop, done 2026-05-11). With 9-X closed, PolicyGuard / ControlLoop are constructed with real adapter maps and StateStore now publishes real device telemetry on the populated path. Story 10.1 consumes those snapshots read-only. Per the Epic 10 introduction (epics.md:2076), Stories 10.1 / 10.3 / 10.4 may begin after 9-X; only Story 10.2's `Optimistic → Confirmed` transition strictly required 9-X.
> **Follow-on:** Story 10.3 owns the write path for `active_strategy` (`POST /actions/set-strategy`). Story 10.1 adds the read field to `SystemSnapshot` and the cold-start default so the headline has a value to render before 10.3 lands; 10.3 layers the mutator on top.

## Story

As a homeowner,
I want a dashboard that shows me the system status in plain language and current energy values with correct units,
so that I can understand the system state in under 10 seconds without any technical knowledge.

## Acceptance Criteria

**Given** the runtime adapter map is populated (post-9-X) and `StateStore` is publishing real or simulated device snapshots

**When** this story is implemented

**Then** the following acceptance criteria must hold:

1. **AC1 — `SystemSnapshot.active_strategy` exists and is non-optional.** `SystemSnapshot` (`src/open_ems/core/state.py:146`) gains a new field `active_strategy: EnergyStrategy` (Pydantic — non-optional, model-validated). The cold-start default is `EnergyStrategy.maximize_self_consumption` (matches the current ControlLoop hardcoded value at `control_loop.py:203`). The field is part of `model_config = ConfigDict(frozen=True, extra="forbid")`; existing immutability semantics MUST be preserved. The model validator at `state.py:168` is unaffected (no new mapping field).

2. **AC2 — `StateStore` carries `active_strategy` across publishes.** `StateStore.__init__` (`src/open_ems/core/state_store.py:39`) gains a keyword-only parameter `active_strategy: EnergyStrategy = EnergyStrategy.maximize_self_consumption`. The constructed initial `SystemSnapshot` at lines 51–63 includes the field. `StateStore.publish(...)` (line 65) MUST preserve `active_strategy` across publishes — the published snapshot's `active_strategy` equals the previously-stored snapshot's `active_strategy`. **Story 10.1 does NOT add a write path** (no `set_active_strategy`, no `publish_strategy`); that surface is Story 10.3's contract. The value is constant from the StateStore's perspective for 10.1's lifetime.

3. **AC3 — `ControlLoop._build_evaluation_input` reads strategy from snapshot.** `src/open_ems/engine/control_loop.py:200-203` currently hardcodes `strategy=EnergyStrategy.maximize_self_consumption`. Replace with `strategy=snapshot.active_strategy`. No other ControlLoop behavior changes. Existing unit tests asserting strategy behavior continue to pass because the default value is identical.

4. **AC4 — Homeowner snapshot serialization exposes `active_strategy`.** `serialize_homeowner_snapshot` (`src/open_ems/web/state_serialization.py:22`) adds `"active_strategy": snapshot.active_strategy.value` to the payload. The installer serializer (`serialize_installer_snapshot`, line 39) likewise adds `"active_strategy"`. Both serializers MUST emit the enum's string value (not the enum object) for JSON-safe SSE payloads.

5. **AC5 — Status headline server-rendered context.** A new builder function `build_homeowner_headline_context(snapshot: SystemSnapshot) -> dict[str, object]` is added to `state_serialization.py`. It returns:
   - `operating_mode`: the snapshot's `operating_mode.value` (`"normal"` / `"degraded"` / `"conservative"` / `"fail_safe"`).
   - `presentation_mode`: `"normal"` when `operating_mode == SystemOperatingMode.normal`; `"degraded"` otherwise. (Maps three non-normal modes into the single calm-degraded UX state per UX spec §"Status Headline Card".)
   - `active_strategy`: the enum's string value.
   - `strategy_label`: a plain-language label — `"Minimize Cost"` / `"Maximize Self-Consumption"` / `"Prioritize EV"` — per UX spec component #2.
   - `headline_text`: when `presentation_mode == "normal"`: `f"Your home is running on solar · {strategy_label}"` (UX spec example exact format). When `presentation_mode == "degraded"`: `"Running with limited functionality"`.
   - `explanation_text`: plain-language one-liner ONLY when `presentation_mode == "degraded"` — derived from the operating mode (e.g., `"conservative"` → `"Some optimization features are paused while the system recovers."`; `"degraded"` → `"Some optimization features are reduced."`; `"fail_safe"` → `"Active control is paused for safety."`). When `presentation_mode == "normal"`: empty string. **No device-state fields are read in this function** — assertion enforced by unit test.

6. **AC6 — Status headline fragment endpoint exists.** A new HTMX endpoint `GET /fragments/homeowner/status-headline` is added to `src/open_ems/web/routes/fragments.py` (mirror the existing per-card pattern at lines 33–66). It depends on `require_homeowner` + `get_state_store`, builds the headline context via AC5, and renders `templates/fragments/homeowner/status-headline.html`. Response is `HTMLResponse`.

7. **AC7 — Headline template renders deterministically.** A new template `src/open_ems/web/templates/fragments/homeowner/status-headline.html` renders:
   - In `normal` presentation mode: a card shell with the `headline_text` as a `<p>` inside `<section class="state-card" data-presentation-mode="normal" aria-label="System status">`; `--color-text-primary` (Tailwind `text-slate-900` or token equivalent); `aria-live="polite"` on the headline span (UX spec §"Status Headline Card" `aria-live="polite"`).
   - In `degraded` presentation mode: same card shell with `data-presentation-mode="degraded"`; left-border in `--color-degraded-border` (slate-300); headline text in `--color-degraded` (slate-600); explanation text below in `--color-text-secondary`. **No amber, no icon prefix, no alert treatment** (UX spec design rule). The visual delta from normal mode is the left-border + slate-600 + explanation line only — confirmed by automated test that asserts the rendered HTML contains NO `amber-` / `red-` / `warn-` CSS class fragments.
   - The headline span has `aria-live="polite"` so SSE in-place updates announce correctly.
   - Layout is stable across modes: the explanation line slot is present in the DOM in both modes (empty `<p>` in normal mode, populated in degraded) so transitions do not shift surrounding content.

8. **AC8 — Three metric cards render battery / solar / grid.** The dashboard layout (see AC10) drives three card slots: battery (% charge), solar (kW PV output), grid (kW grid power). The existing per-card fragments (`battery-card.html`, `solar-card.html`, `grid-card.html`) are kept; the EV card is OUT of the primary render path in Story 10.1 (added back in Story 10.2). The shared template `_card.html` is rewritten to match UX spec component #3 ("Metric Card"):
   - Card shell: `<section>` with Tailwind classes producing `bg-stone-50 rounded-xl p-6 border border-slate-200` (or token-equivalent CSS variables). Tokens use `--color-surface` / `--color-border` per UX spec §4.
   - Header: `<h2>` with the plain-language card title (`"Battery"` / `"Solar"` / `"Grid"` — no technical abbreviations). `text-base font-medium text-slate-700`.
   - Body: `<p>` with the primary value (e.g., `"55%"` for battery, `"3.2 kW"` for solar, `"1.2 kW"` for grid). `text-2xl font-semibold text-slate-900`. Secondary metrics (current battery power, AC output, imported / exported) are NOT in the v1 primary layout per UX spec §"Metric Card" anatomy (single body value + optional caption). The existing `_homeowner_card_rows` returning three rows per card is REDUCED to a single primary row.
   - Caption: `<p>` in `text-sm text-slate-400` for the stale caption when `component_state == STALE`. Stable DOM position below the body — present (empty) in non-stale states so layout does not shift.
   - Unavailable state: body reads `"Unavailable"` in `text-slate-400` (not `0 kW` / `0%`). UX spec §"Metric Card" rule.

9. **AC9 — Per-card primary value formatting.**
   - Battery (`card == "battery"`): primary value = `f"{soc_percent:.0f}%"` (existing formatter `_format_percent`).
   - Solar (`card == "solar"`): primary value = `f"{pv_power_kw:.1f} kW"` (existing `_format_kw`).
   - Grid (`card == "grid"`): primary value = `f"{abs(grid_power_kw):.1f} kW"` with a one-word direction suffix — `"importing"` if positive, `"exporting"` if negative, `"idle"` if zero (rounded to one decimal). Grid power sign convention per architecture.md §"Energy sign convention" — positive = import, negative = export. The header label is `"Grid"`; the body reads e.g., `"1.2 kW importing"`. Unit suffix is mandatory per AC of FR26.
   - The `_homeowner_card_rows` helper at `state_serialization.py:143` is refactored to return a single `{"label": str, "value": str}` dict (or a tuple of one row) instead of three. Existing tests in `test_state_serialization.py` are updated to match.

10. **AC10 — Dashboard layout is the headline + three-card vertical stack.** `src/open_ems/web/templates/dashboard.html` is restructured to render, in order on mobile-first single column:
    1. Status headline fragment via `hx-get="/fragments/homeowner/status-headline" hx-trigger="load, every 10s" hx-swap="outerHTML"`.
    2. Battery metric card (existing `/fragments/homeowner/battery-card` endpoint).
    3. Solar metric card (existing `/fragments/homeowner/solar-card` endpoint).
    4. Grid metric card (existing `/fragments/homeowner/grid-card` endpoint).
    The EV card section currently at dashboard.html:28-33 is REMOVED from the primary render path. The `/fragments/homeowner/ev-card` endpoint is NOT deleted (Story 10.2 will re-add the EV card section in the dashboard template, with the override button). The `.dashboard-grid` CSS class on the outer wrapper is replaced with a single-column flex stack per UX spec §"Homeowner dashboard hierarchy" — a single vertical card stack, not a grid (UX spec line 642).

11. **AC11 — SSE + HTMX polling fallback both update the same fragments.** Two paths MUST be supported:
    - **HTMX polling fallback** (default): each card and the headline poll every 10s via `hx-trigger="load, every 10s"` (matches existing pattern at dashboard.html:13). This works without SSE.
    - **SSE primary** (already implemented via Story 5-2 at `/api/stream/state`): the homeowner page already receives full snapshots over SSE. Story 10.1 does NOT add new SSE event types. The HTMX polling is the visible update mechanism for v1 (the SSE stream's `state_update` payload now ALSO carries `active_strategy` per AC4 — but Story 10.1 does NOT add client-side SSE consumption logic for in-place metric updates; that is acceptable for v1 because HTMX polling at 10s matches the SSE interval and the UX spec explicitly says "Components must be designed so either transport can update the same server-rendered fragments without layout shift" — UX spec line 1326). The HTMX polling at 10s satisfies the "values update every 10 seconds via SSE; HTMX polling at the same 10-second interval takes over automatically" contract (UX spec §"Homeowner Dashboard" line 398).

12. **AC12 — Partial device availability does not cascade.** When one device slot has `component_state == UNAVAILABLE` or `STALE`, only that device's card renders the corresponding state; the other two cards continue rendering live values. Verified by integration test that publishes a snapshot with `battery=None` (UNAVAILABLE) and asserts the solar and grid cards render fresh values while the battery card renders `"Unavailable"`.

13. **AC13 — Stale caption layout stability.** When a card transitions from fresh → stale, the card shell (border, background, header) MUST NOT change; only the caption text appears in the stable bottom slot. When stale → fresh, the caption text empties but the slot remains present. Verified by a snapshot test that compares the rendered HTML of fresh vs. stale states and asserts the only diff is the caption content. The stale caption uses `text-slate-400 text-sm` (UX spec §"Metric Card" line 1069).

14. **AC14 — Headline reads only from `SystemOperatingMode` + `active_strategy` — no device-state inference.** `build_homeowner_headline_context` MUST NOT reference `snapshot.inverter`, `.battery`, `.ev_charger`, `.grid_meter`, `.component_states`, or `.data_age_seconds`. A static test in `test_state_serialization.py` performs the following structural assertion: pass two snapshots that differ ONLY in device-state fields (same `operating_mode` + `active_strategy`); assert the returned `headline_text` / `explanation_text` are identical. This is the structural enforcement of Epic 10's cross-story constraint: "Status headline is derived exclusively from `SystemOperatingMode` + active strategy" (epics.md:2238).

15. **AC15 — Accessibility — WCAG 2.1 AA target.**
    - Every card has `<section aria-label="...">` (UX spec §"Metric Card" line 1076).
    - Headline span has `aria-live="polite"` (UX spec line 1054).
    - Card body span has `aria-live="polite"` (UX spec line 1076).
    - Headings (`<h1>` for the page, `<h2>` for cards including the headline). One `<h1>` per page.
    - Color contrast for body text ≥ 4.5:1 against the card background — verified by manual assertion in the dev notes (token values `--color-text-primary` on `--color-surface` = slate-900 on stone-50 ≈ 16.8:1; `--color-degraded` on `--color-surface` = slate-600 on stone-50 ≈ 7.1:1; both clear the 4.5:1 bar).
    - A placeholder axe-core test at `tests/integration/web/test_homeowner_dashboard_a11y.py` follows the existing Story 9.x precedent: `pytest.mark.skipif(npx absent) + pytest.mark.xfail(reason="axe-playwright wiring deferred", strict=False)` — same pattern as `test_handoff_guide_a11y.py`. The test body asserts zero axe violations on `/homeowner/dashboard` once axe-playwright is wired.
    - All interactive elements on the homeowner dashboard MUST be keyboard-reachable. Story 10.1 does not add new interactive elements (the override button is 10.2, strategy selector is 10.3); the only navigable elements are the `<a href="/logout">` link (existing) and the dashboard URL itself. Confirmed by manual keyboard-tab test documented in the dev notes; no automated test required for 10.1 (will be re-asserted via axe-playwright once wired in 10.2 / 10.3).

16. **AC16 — Performance (NFR-P3, 3-second interactive on Pi 4).** Dashboard initial HTML response (the `/homeowner/dashboard` route returning the shell template) MUST be under 200ms on a developer-class machine when StateStore is populated. Subsequent fragment loads via HTMX each MUST be under 100ms (no DB I/O, snapshot read only). Verified by a synchronous timing test in `tests/unit/web/test_dashboard_route.py` using `time.perf_counter()` around `TestClient.get(...)` (10-iteration average). The 3-second Pi 4 budget is not directly enforced by CI (no Pi 4 hardware in CI per existing project pattern); the developer-machine ceiling is a proxy.

17. **AC17 — Unit tests.**
    - `tests/unit/core/test_state.py` — assert `SystemSnapshot(... active_strategy=EnergyStrategy.minimize_cost ...)` constructs; assert default is `maximize_self_consumption` only via constructor explicit pass (Pydantic itself does not provide a default for non-Optional fields without `Field(default=...)`; AC1 requires the default at the StateStore boundary, not on the model).
    - `tests/unit/core/test_state_store.py` — assert StateStore constructor accepts `active_strategy` kwarg; assert `publish(...)` preserves the field across multiple publishes; assert the initial snapshot at `sequence_id=0` carries the constructor's `active_strategy` value.
    - `tests/unit/engine/test_control_loop.py` — at least one test asserts `_build_evaluation_input` returns an `EvaluationInput` whose `strategy` field equals the snapshot's `active_strategy` (parametrize across all three `EnergyStrategy` values).
    - `tests/unit/web/test_state_serialization.py` — adds tests for: (a) `serialize_homeowner_snapshot` includes `active_strategy` as a JSON-safe string; (b) `build_homeowner_headline_context` produces the AC5 fields for both normal and degraded operating modes (parametrize across all `SystemOperatingMode` values); (c) the AC14 structural invariant — two snapshots with identical operating_mode + active_strategy but different device-state fields produce identical headline output; (d) `_homeowner_card_rows` for grid card includes the direction suffix per AC9.
    - `tests/unit/web/test_fragment_routes.py` — adds test for `/fragments/homeowner/status-headline` (200 with HTMX request; rejects unauthenticated; rejects installer); plus a test that asserts the rendered HTML contains the expected `data-presentation-mode` attribute and (for degraded mode) the left-border CSS class fragment.
    - `tests/unit/web/test_dashboard_route.py` (new file) — asserts the dashboard shell template includes exactly four HTMX-bound `<section>` elements with the correct `hx-get` URLs (headline + battery + solar + grid) and that the EV card section is absent from the homeowner branch.

18. **AC18 — Integration tests.**
    - `tests/integration/web/test_homeowner_dashboard.py` (new file) — boots the app via the existing `app_e2e` pattern, seeds a homeowner session, publishes a populated snapshot, and asserts:
      - `GET /homeowner/dashboard` → 200, body contains `data-presentation-mode="normal"` and the strategy label.
      - With `StateStore.publish(operating_mode=SystemOperatingMode.degraded, ...)`, the next headline fragment fetch returns `data-presentation-mode="degraded"` with slate-600 and the explanation line populated; no amber CSS class fragment appears.
      - With one device slot UNAVAILABLE (e.g., `battery=None`), the battery card renders `"Unavailable"` and the other two cards render fresh values (AC12).
      - With one device slot STALE (`read_at` > stale_threshold ago), the corresponding card renders the slate-400 caption and the body remains the last-known value (AC13).
    - Tests use `structlog.testing.capture_logs` if log assertions are added (per 9-X review patch precedent).

19. **AC19 — Tailwind / token plumbing.** Tailwind CSS is "a development-time compilation tool only ... served locally — no CDN, no runtime Node.js dependency, no build step required to run the application" (UX spec §"Component-Level Theme System" line 257). Story 10.1 MUST NOT introduce a CDN-loaded Tailwind, a runtime build step, or a Node.js requirement to run the FastAPI app. Two acceptable paths:
    - **Path A (preferred):** add a pre-compiled `static/open-ems.css` (the UX spec's named output) containing the utility classes used by 10.1 templates, served from `src/open_ems/web/static/` (Story 1.4 / 1.5 have not yet mounted StaticFiles — confirm in `app.py`; if not mounted, add the StaticFiles mount in this story as a scoped change). Link from `base.html`. Document the compilation command in `README.md` or a `static/README.md`.
    - **Path B (acceptable fallback):** inline a minimal hand-authored CSS file at `src/open_ems/web/static/open-ems.css` using CSS custom properties (the UX spec's design tokens at lines 482–486) and class selectors targeting the new `state-card` / `state-card--degraded` / `state-card__caption` / `state-card__metric` classes. NO Tailwind utility classes in templates if Path B is taken — use semantic class names that map to the custom-property tokens.
    - The choice is the dev agent's call but MUST be documented in completion notes with rationale. The user-facing requirements (slate-600 degraded, stone-50 surface, slate-400 caption) are identical for both paths.

20. **AC20 — Quality gates.**
    - `uv run python -m pytest tests/ --no-cov -q` → all tests pass. Post-9-X baseline is 1445 passed + 5 xfailed; net delta MUST be positive (new tests for the new builder, route, and template).
    - `uv run python -m ruff check .` → clean across the project.
    - `uv run python -m ruff format --check .` → clean.
    - `uv run python -m mypy src/` → clean across all source files. The new `build_homeowner_headline_context` and the `SystemSnapshot.active_strategy` field MUST type-check without `# type: ignore`.

21. **AC21 — Sprint-status consistency.** On story close, `_bmad-output/implementation-artifacts/sprint-status.yaml` MUST set `10-1-implement-homeowner-dashboard-layout-with-systemoperatingmode-driven-headline-and-metric-cards: done` and `epic-10: in-progress` (the epic transition was made at story creation per the bmad-create-story workflow). Per Epic 9 retro action A8 (story-file Status header ↔ sprint-status entry must agree) and Epic 8 retro action A1/A2 (codified in bmad-create-story).

## Tasks / Subtasks

- [x] **Task 1 — Add `active_strategy` to the state model** (AC: 1, 2, 17)
  - [x] Subtask 1.1 — Add `active_strategy: EnergyStrategy` to `SystemSnapshot` in `src/open_ems/core/state.py`. No `Field(default=...)`; the default is applied at the StateStore boundary per AC2.
  - [x] Subtask 1.2 — Extend `StateStore.__init__` (`src/open_ems/core/state_store.py:39`) with `active_strategy: EnergyStrategy = EnergyStrategy.maximize_self_consumption`. Store on `self._active_strategy`. Pass into the initial `SystemSnapshot(...)` at lines 51–63.
  - [x] Subtask 1.3 — Modify `StateStore.publish(...)` to carry `self._active_strategy` into the new snapshot at line 83–95. **No public mutator added in 10.1.**
  - [x] Subtask 1.4 — Update `tests/unit/core/test_state.py` and `tests/unit/core/test_state_store.py` for the new field (AC17). Also updated 12 additional snapshot fixtures across the test suite (see Completion Notes for the cascade list).

- [x] **Task 2 — Wire ControlLoop to read strategy from snapshot** (AC: 3, 17)
  - [x] Subtask 2.1 — `src/open_ems/engine/control_loop.py:203` — replace `strategy=EnergyStrategy.maximize_self_consumption` with `strategy=snapshot.active_strategy`.
  - [x] Subtask 2.2 — Remove the now-unused `EnergyStrategy` import from `control_loop.py:49`.
  - [x] Subtask 2.3 — Parametrize `tests/unit/engine/test_control_loop.py` to cover all three `EnergyStrategy` values via the snapshot.

- [x] **Task 3 — Homeowner snapshot serialization + headline context** (AC: 4, 5, 14, 17)
  - [x] Subtask 3.1 — Add `"active_strategy": snapshot.active_strategy.value` to both `serialize_homeowner_snapshot` (line 22) and `serialize_installer_snapshot` (line 39) in `state_serialization.py`.
  - [x] Subtask 3.2 — Add `build_homeowner_headline_context(snapshot: SystemSnapshot) -> dict[str, object]` per AC5. The implementation MUST NOT reference `snapshot.inverter` / `.battery` / `.ev_charger` / `.grid_meter` / `.component_states` / `.data_age_seconds` (AC14). Enforced by `test_headline_ignores_device_state_per_ac14`.
  - [x] Subtask 3.3 — Implement `_STRATEGY_LABELS` and `_DEGRADED_EXPLANATIONS` as module-level `Mapping[EnergyStrategy, str]` and `Mapping[SystemOperatingMode, str]` constants. Exhaustive-coverage tests assert each table covers every enum member.
  - [x] Subtask 3.4 — Update `_homeowner_card_rows` per AC9 to return a single-row primary value; added `_format_grid_power` helper for the magnitude+direction suffix; removed now-unused `_format_kwh` helper.

- [x] **Task 4 — Status headline fragment endpoint + template** (AC: 6, 7, 14, 15)
  - [x] Subtask 4.1 — Added `GET /fragments/homeowner/status-headline` route to `src/open_ems/web/routes/fragments.py` per AC6.
  - [x] Subtask 4.2 — Created `src/open_ems/web/templates/fragments/homeowner/status-headline.html` per AC7. Includes `data-presentation-mode`, `data-operating-mode`, `data-active-strategy`, `aria-label="System status"`, `aria-live="polite"` on the headline span, stable explanation slot in both modes.
  - [x] Subtask 4.3 — `test_status_headline_degraded_mode_renders_calm_styling` asserts no `amber` / `color-fail` / `state-card--fail` / `warn` CSS fragments leak into the degraded path.

- [x] **Task 5 — Restructure metric card template + dashboard layout** (AC: 8, 9, 10, 12, 13, 19)
  - [x] Subtask 5.1 — Rewrote `src/open_ems/web/templates/fragments/homeowner/_card.html` per AC8. Single primary value (rendered from `rows[0].value`). Slate-400 caption in stable bottom slot via `state-card__freshness` (always present). `"Unavailable"` body text. `<section aria-label="{{ title }}">` and `aria-live="polite"` on the body wrapper.
  - [x] Subtask 5.2 — Verified `_card_title` mapping is already correct (`Battery` / `Solar` / `Grid` / `EV charger`).
  - [x] Subtask 5.3 — Rewrote `src/open_ems/web/templates/dashboard.html` per AC10: headline + battery + solar + grid; EV card section removed from the homeowner branch (10.2 will re-add).
  - [x] Subtask 5.4 — **AC19 Path B chosen.** Hand-authored `src/open_ems/web/static/open-ems.css` using CSS custom-property design tokens (no Tailwind, no CDN, no Node.js, no build step). Mounted `StaticFiles` at `/static` in `create_app()`. Linked from `base.html`. Rationale documented in Completion Notes.

- [x] **Task 6 — Tests** (AC: 12, 13, 14, 16, 17, 18)
  - [x] Subtask 6.1 — Added 11 new tests to `test_state_serialization.py` covering AC4/AC5/AC8/AC9/AC14 + exhaustive label/explanation coverage.
  - [x] Subtask 6.2 — Added 4 new tests to `test_fragment_routes.py` for the status-headline route (auth, role, normal-mode HTML, degraded-mode HTML with no-amber assertions).
  - [x] Subtask 6.3 — Created `tests/unit/web/test_dashboard_route.py` with 4 tests (installer redirect, unauth redirect, shell HTMX bindings, AC16 perf ceiling).
  - [x] Subtask 6.4 — Created `tests/integration/web/test_homeowner_dashboard.py` with 4 end-to-end scenarios (normal, degraded, partial-UNAVAILABLE no-cascade, STALE stable-layout).
  - [x] Subtask 6.5 — Created `tests/integration/web/test_homeowner_dashboard_a11y.py` placeholder per AC15 (xfail+skipif following Story 9.x precedent).

- [x] **Task 7 — Quality gates + sprint-status close** (AC: 20, 21)
  - [x] Subtask 7.1 — `uv run python -m pytest tests/ --no-cov -q` → **1478 passed + 6 xfailed** (baseline 1445 + 5 xfailed → **+33 passed, +1 xfailed**).
  - [x] Subtask 7.2 — `uv run python -m ruff check .` → clean.
  - [x] Subtask 7.3 — `uv run python -m ruff format --check .` → clean (250 files formatted).
  - [x] Subtask 7.4 — `uv run python -m mypy src/` → clean across 98 source files.
  - [x] Subtask 7.5 — Updated `_bmad-output/implementation-artifacts/sprint-status.yaml` per AC21.

## Dev Notes

### A2 trigger evaluation

Evaluated each trigger against this story's scope (AC1–AC21) and the files it modifies:

| Trigger | Match? | Evidence |
|---|---|---|
| T1 — lifecycle / state-machine behavior | **No** | No new state machine. `SystemOperatingMode` and `ComponentState` are pre-existing enums consumed read-only. The presentation-mode derivation (normal vs. degraded) is a pure function, not a state machine. |
| T2 — retries / cancellation | **No** | No retry policy, no `attempts` field, no new `CancelledError` propagation surface. HTMX polling + SSE both have existing cancellation contracts owned by FastAPI / Starlette. |
| T3 — persistence + recovery | **No** | `active_strategy` is in-memory only in 10.1. Default-constant at restart; no DB-backed hydration. Story 10.3 may persist it; 10.1 does not. |
| T4 — watchdog / timing | **No** | No heartbeat, no deadline scheduling, no missed-cycle detection changes. |
| T5 — multi-adapter coordination | **No** | UI presentation only. Adapters are not referenced. |
| T6 — deployment / restart | **No** | No restart-triggered workflow. Cold-start carries the default `active_strategy` value — already covered by existing StateStore initial-snapshot semantics. |
| T7 — installer workflow orchestration | **No** | Homeowner-facing dashboard; no installer wizard touchpoints. |

**Conclusion:** Not A2-triggered. R1–R7 artifacts are not mandatory for this story.

### Files modified by this story

**New files:**
- `src/open_ems/web/templates/fragments/homeowner/status-headline.html` — status headline server-rendered fragment.
- `src/open_ems/web/static/open-ems.css` — pre-compiled or hand-authored CSS bundle (per AC19; only if not already present from Story 1.4 / 1.5).
- `tests/unit/web/test_dashboard_route.py` — dashboard route + shell template assertions.
- `tests/integration/web/test_homeowner_dashboard.py` — populated, degraded, UNAVAILABLE, STALE scenarios.
- `tests/integration/web/test_homeowner_dashboard_a11y.py` — placeholder axe-core scan (xfail+skipif).

**Modified files:**
- `src/open_ems/core/state.py` — add `active_strategy: EnergyStrategy` to `SystemSnapshot`.
- `src/open_ems/core/state_store.py` — add `active_strategy` constructor parameter; carry across publishes.
- `src/open_ems/engine/control_loop.py` — read strategy from snapshot (line 203 only); remove the hardcoded `EnergyStrategy.maximize_self_consumption` literal.
- `src/open_ems/web/app.py` — pass `active_strategy=settings.<TBD or hardcoded>` into `StateStore(...)` at line ~230 (if Settings is extended; otherwise rely on StateStore default and document). Mount `StaticFiles` if not already mounted (per AC19).
- `src/open_ems/web/state_serialization.py` — add `active_strategy` to both serializers; add `build_homeowner_headline_context`; refactor `_homeowner_card_rows` to single-row.
- `src/open_ems/web/routes/fragments.py` — add `/fragments/homeowner/status-headline` route.
- `src/open_ems/web/templates/dashboard.html` — restructure to headline + 3 cards; remove EV card from primary render path (will be re-added by Story 10.2).
- `src/open_ems/web/templates/fragments/homeowner/_card.html` — single primary value, slate-400 caption stable slot, "Unavailable" body text.
- `src/open_ems/web/templates/base.html` — link `static/open-ems.css` (if Path A or B from AC19 adds a new stylesheet not already present).
- `tests/unit/core/test_state.py`, `tests/unit/core/test_state_store.py`, `tests/unit/engine/test_control_loop.py`, `tests/unit/web/test_state_serialization.py`, `tests/unit/web/test_fragment_routes.py` — extended per AC17.
- `_bmad-output/implementation-artifacts/sprint-status.yaml` — final entry update on close.

### Architectural alignment

**StateStore is the single source of truth for snapshot reads** (architecture.md §"Pattern: StateStore snapshots are immutable", line 399). 10.1's `active_strategy` addition obeys this — the field flows through `SystemSnapshot` and is read via `state_store.get_snapshot()` everywhere downstream. No direct mutation by route handlers.

**Role-aware serialization at the SSE boundary** (architecture.md line 296). Both `serialize_homeowner_snapshot` and `serialize_installer_snapshot` get `active_strategy` because the strategy is a homeowner-safe field (it's the user's own preference). No installer-only data leaks into homeowner payloads.

**Two distinct visual registers — homeowner = calm autopilot** (UX spec line 280, 628). The status headline is the FIRST element the homeowner reads (UX spec line 1043); the metric cards reinforce it but do not replace it. Story 10.1 establishes this hierarchy.

**FastAPI dependency injection enforces role** (architecture.md line 251, 663). All new routes in `fragments.py` use `Depends(require_homeowner)` — no business-logic role checks.

**Single-Evaluator Principle (Epic 7 retro).** 10.1 does NOT introduce new evaluators, command origins, or PolicyGuard touchpoints. The ControlLoop change at AC3 is a one-line strategy source swap; ControlLoop remains the only consumer of `EvaluationInput`.

**Mapping immutability invariant (Story 8-2 / 9-X).** The `active_strategy` field is a single enum value, not a mapping — no immutability concerns.

### Library / framework requirements (no new dependencies)

`uv.lock` MUST be unchanged after this story (matches the Epic 9 / 9-X zero-new-dependency contract). All required libraries already exist:
- **Pydantic** (`pydantic >= 2.x`) — `SystemSnapshot` model extension.
- **FastAPI + Jinja2** — route + template.
- **HTMX 1.9.12** — already pinned in `base.html:27` (CDN today; UX spec recommends self-hosting — out of scope for 10.1; track as follow-up).
- **structlog** — for any log emissions.
- **pytest, pytest-asyncio (auto mode)** — testing.
- **No new Alpine.js usage in 10.1** — the static headline + cards do not need client-side state. Story 10.2 will introduce Alpine.js for the EV override button.

### Strategy persistence: explicit scope split with Story 10.3

Story 10.1 adds the **read field** (`SystemSnapshot.active_strategy`) and a constant default value at the StateStore boundary. Story 10.3 owns:
- The `POST /actions/set-strategy` endpoint.
- The `StateStore.set_active_strategy(...)` (or equivalent) mutator.
- The persistence path (likely as part of the `active_constraints` table extension or a dedicated single-row table — Story 10.3's call).
- The HTMX optimistic-update flow described in epics.md §10.3.

**Why split this way:** if Story 10.1 attempted to persist the strategy, it would either need to (a) extend `active_constraints` schema (a migration, an `ActiveConstraints` model change, and `ConfigRepo` extension — significant scope creep) or (b) add a parallel single-row settings table (also schema work). Both expand 10.1's scope into Epic 9 storage-layer territory. Splitting at the in-memory boundary keeps 10.1 focused on the homeowner UI surface while preserving the cross-story contract that all UI values flow through `StateStore`. Story 10.3 will pick up the persistence work.

### Previous-story intelligence

**Story 9-X (Wire adapter map into PolicyGuard and ControlLoop, done 2026-05-11):** the architectural prep that makes Epic 10 possible. StateStore now publishes real device telemetry on the populated path. 10.1's metric cards now display real data when device adapters succeed; degraded states when adapters return `DegradedDeviceState`. The 9-X review introduced `app.state.runtime_adapters` and `app.state.policy_guard` test seams (9-X completion note vii); 10.1 may use them in integration tests if needed (not strictly required for a presentation-layer story).

**Story 9-Y-a (E2E fail-abort coverage, done 2026-05-11):** the first A7 review-closure proof-of-enforcement target. Notable precedent: **naming honesty** in tests — don't name a test for behavior it doesn't actually exercise. 10.1 must follow this: every test name MUST describe what the test actually asserts. The `test_homeowner_dashboard.py` integration tests should be named like `test_dashboard_renders_normal_headline_when_operating_mode_is_normal`, not generic `test_dashboard_works`.

**Story 5-2 (SSE state stream, done 2026-04-something):** established the `/api/stream/state` endpoint and role-aware serialization. The homeowner SSE stream is already in place; 10.1 only adds `active_strategy` to the payload. The SSE event format `event: state_update\nid: <sequence_id>\ndata: <json>\n\n` (stream.py:51-58) is unchanged.

**Story 5-3 (HTMX polling + per-device STALE / UNAVAILABLE detection, done 2026-04-something):** established the per-card fragment endpoints (`/fragments/homeowner/{battery,solar,grid,ev}-card`) and the `_card.html` template with `component_state` / `stale_caption` / `unavailable` context. 10.1 inherits these endpoints, refactors the template's primary value layout, and adds the headline fragment alongside.

**Story 8-2 (PolicyGuard) review finding `[control_loop.py:73-76]` "First tick publishes `operating_mode=None`":** closed structurally for 10.1 because `SystemSnapshot.operating_mode` is non-optional (`SystemOperatingMode`, not `SystemOperatingMode | None`); the StateStore default is `SystemOperatingMode.degraded`. The homeowner headline in this cold-start window renders `"Running with limited functionality"` with the calm `degraded` explanation — exactly the correct UX per UX spec ("calm over urgency", line 154). No special-case handling needed.

**Story 9-X completion note (vii) re: deferred-work `[retry_policy.py] RetryPolicy.execute has no per-device serialization`:** the deferred finding noted Epic 10 stories adding command origins would surface this. **10.1 does NOT add command origins.** Story 10.2 (EV override POST) and 10.3 (set-strategy POST) are the relevant stories. R7 triage of this story: **no overlap.**

### Project Structure Notes

The new headline fragment endpoint lives alongside the existing per-card endpoints in `routes/fragments.py` — consistent with Story 5-3's organization. The headline template lives in `templates/fragments/homeowner/` — same directory as the per-card fragments. No new top-level directory introduced.

The `state_serialization.py` module gains one new public function (`build_homeowner_headline_context`) plus two new module-level constants (`_STRATEGY_LABELS`, `_DEGRADED_EXPLANATIONS`). No new file; the existing module is the right home for "build display context from a snapshot."

The new `SystemSnapshot.active_strategy` field is alphabetically between `operating_mode` and the device slots — placement is editorial; the canonical positioning is right after `operating_mode` (semantically related: both are "global system context" fields, distinct from per-device slots).

### A2 evaluation reminder

A2 evaluation was performed (see "A2 trigger evaluation" table above). None of T1–T7 matched. R1–R7 orchestration artifacts are NOT mandatory for this story per the bmad-create-story workflow's enforcement contract. If during implementation a new behavior emerges that DOES match a trigger (e.g., the dev agent decides to persist `active_strategy` via a migration or add Alpine.js client-state), the story MUST be re-evaluated and the orchestration artifacts authored before `Status: ready-for-dev` is re-asserted on a future revision.

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Epic-10 (line 2072)] — Epic 10 scope, prerequisite linkage to 9-X, and Story 10.1 acceptance criteria.
- [Source: _bmad-output/planning-artifacts/epics.md#Story-10.1 (line 2082)] — full BDD acceptance criteria for the headline + metric cards.
- [Source: _bmad-output/planning-artifacts/ux-design-specification.md#Status-Headline-Card (line 1041)] — headline component anatomy, styling, `aria-live="polite"`, normal vs. degraded contract.
- [Source: _bmad-output/planning-artifacts/ux-design-specification.md#Metric-Card (line 1058)] — card shell, single body value, slate-400 stale caption, "Unavailable" body text.
- [Source: _bmad-output/planning-artifacts/ux-design-specification.md (lines 256–313)] — design tokens, two visual registers, homeowner = calm autopilot.
- [Source: _bmad-output/planning-artifacts/architecture.md (line 294)] — SSE state stream + HTMX polling fallback.
- [Source: _bmad-output/planning-artifacts/architecture.md (line 296)] — role-aware SSE serialization.
- [Source: _bmad-output/planning-artifacts/architecture.md (line 759)] — homeowner interface FR25–FR30 component mapping.
- [Source: _bmad-output/planning-artifacts/prd.md#FR25 (line 630)] — FR25 strategy selection (10.3 scope; 10.1 displays the value).
- [Source: _bmad-output/planning-artifacts/prd.md#FR26 (line 631)] — FR26 view current system state (battery / solar / grid).
- [Source: _bmad-output/planning-artifacts/prd.md#FR29 (line 634)] — FR29 plain-language status indicator.
- [Source: _bmad-output/planning-artifacts/prd.md#NFR-P3 (line 666)] — 3-second interactive on Pi 4.
- [Source: _bmad-output/planning-artifacts/prd.md#NFR-A1 (line 714)] — WCAG 2.1 AA accessibility.
- [Source: src/open_ems/core/state.py:62] — `SystemOperatingMode` enum.
- [Source: src/open_ems/core/state.py:71] — `EnergyStrategy` enum.
- [Source: src/open_ems/core/state.py:146] — `SystemSnapshot` model (extension target for AC1).
- [Source: src/open_ems/core/state_store.py:39] — `StateStore.__init__` (extension target for AC2).
- [Source: src/open_ems/core/state_store.py:65] — `StateStore.publish` (carry-through target for AC2).
- [Source: src/open_ems/engine/control_loop.py:203] — hardcoded strategy (replacement target for AC3).
- [Source: src/open_ems/web/state_serialization.py:22] — `serialize_homeowner_snapshot` (extension target for AC4).
- [Source: src/open_ems/web/state_serialization.py:56] — `build_homeowner_card_context` (sibling to new `build_homeowner_headline_context`).
- [Source: src/open_ems/web/state_serialization.py:143] — `_homeowner_card_rows` (refactor target for AC9).
- [Source: src/open_ems/web/routes/fragments.py:33] — existing per-card fragment endpoint pattern (template for AC6).
- [Source: src/open_ems/web/templates/dashboard.html] — current homeowner dashboard shell (rewrite target for AC10).
- [Source: src/open_ems/web/templates/fragments/homeowner/_card.html] — existing card template (rewrite target for AC8).
- [Source: src/open_ems/web/templates/base.html] — HTMX + CSRF wiring; static asset link target for AC19.
- [Source: src/open_ems/web/routes/stream.py:51] — SSE event format `state_update` (unchanged; payload now carries `active_strategy`).
- [Source: tests/integration/web/test_handoff_guide_a11y.py] — a11y test xfail+skipif precedent (AC15).
- [Source: tests/unit/web/test_fragment_routes.py:35] — existing fragment-route test helper pattern.
- [Source: _bmad-output/implementation-artifacts/9-X-wire-adapter-map-into-policy-guard-and-control-loop.md] — 9-X completion (architectural prep gate; AC18 amended epics.md Epic 10 intro with the prerequisite linkage).
- [Source: _bmad-output/implementation-artifacts/epic-9-retro-2026-05-11.md] — Epic 9 retro: A7 review-closure structural enforcement (bmad-code-review will gate 10.1 → done).
- [Source: _bmad-output/implementation-artifacts/deferred-work.md] — overlapping-findings scan: no must-resolve overlap with 10.1's surface.

## Dev Agent Record

### Agent Model Used

claude-opus-4-7 (Claude Opus 4.7)

### Debug Log References

- Mypy clean across 98 source files (no `# type: ignore` added; `_DeferredOCPPAdapter`-style proxy work not needed).
- Ruff check + format clean across 250 files.
- Pytest pre-10.1: 1445 passed + 5 xfailed (per Story 9-X close). Post-10.1: **1478 passed + 6 xfailed**. Net delta: **+33 passed, +1 xfailed** (1 placeholder a11y test).
- Test breakdown of the +33:
  - `tests/unit/core/test_state.py`: +2 (active_strategy required at model level, accepts all 3 strategy members).
  - `tests/unit/core/test_state_store.py`: +3 (cold-start default, explicit-kwarg honored, publish preserves across multiple snapshots and operating-mode changes).
  - `tests/unit/engine/test_control_loop.py`: +3 parametrized (one case per `EnergyStrategy` member proving `_build_evaluation_input` reads from `snapshot.active_strategy`).
  - `tests/unit/web/test_state_serialization.py`: +11 (homeowner + installer serializer emit `active_strategy` as string; AC5 headline normal-mode parametrized × 3 strategies; AC5 degraded-mode parametrized × 3 operating modes; AC14 ignores-device-state structural assertion; AC9 grid direction-suffix importing/exporting/idle; AC8 single-row primary value across battery/solar/grid; `_STRATEGY_LABELS` exhaustiveness; `_DEGRADED_EXPLANATIONS` exhaustiveness).
  - `tests/unit/web/test_fragment_routes.py`: +4 (status-headline route 401/redirect for unauth, 403 for installer, normal-mode HTML attributes, degraded-mode HTML calm styling with no-amber assertions).
  - `tests/unit/web/test_dashboard_route.py` (new file): +4 (installer redirected, unauth redirected, shell renders 4 HTMX-bound sections without EV, AC16 perf ceiling under 200ms 10-iter average).
  - `tests/integration/web/test_homeowner_dashboard.py` (new file): +4 (normal-mode shell + 3 cards happy path with importing direction, degraded-mode calm styling end-to-end, AC12 partial-UNAVAILABLE no cascade, AC13 STALE caption with stable layout and preserved last-known value).
  - `tests/integration/web/test_homeowner_dashboard_a11y.py` (new file): +1 xfail (axe-playwright placeholder following Story 9.x precedent).

### Completion Notes List

- **AC1 implementation note — model-level non-Optional with no default.** Per AC17's explicit wording, `SystemSnapshot.active_strategy` is declared as `active_strategy: EnergyStrategy` (no `Field(default=...)`). The default lives at the StateStore boundary. Consequence: 14 pre-existing test fixtures that constructed `SystemSnapshot(...)` directly required a one-line `active_strategy=EnergyStrategy.maximize_self_consumption,` addition each. All 14 sites updated; full suite still passes.
- **AC14 structural enforcement.** `build_homeowner_headline_context` is intentionally written to take a snapshot and read only `.operating_mode` and `.active_strategy`. The function never references device slots, `component_states`, or `data_age_seconds`. `test_headline_ignores_device_state_per_ac14` verifies this structurally by constructing two snapshots that differ exclusively in device-state fields and asserting identical headline output.
- **AC19 Path B chosen** (hand-authored CSS with custom-property tokens; no Tailwind compilation). Rationale: (a) avoids any Node.js / npm dependency at runtime or build time; (b) the homeowner dashboard surface is small enough that hand-authored CSS is more honest than introducing a build pipeline; (c) the design tokens in the UX spec map cleanly onto CSS custom properties; (d) future stories can promote to Tailwind compile if/when the surface area justifies the tooling cost. New file: `src/open_ems/web/static/open-ems.css`. New mount: `app.mount("/static", StaticFiles(directory=...), name="static")` in `create_app()`. Linked from `base.html` `<head>`.
- **EV card preserved but removed from primary render path.** Per AC10, the EV card section was removed from `dashboard.html`'s primary layout but the `/fragments/homeowner/ev-card` endpoint and the EV branch in `_homeowner_card_rows` remain in place for Story 10.2 to use. The card now renders a single primary row (power-kw or `"Unavailable"`); the previous status/session breakdown was dropped because UX spec §"Metric Card" mandates a single body value.
- **Grid sign convention surfaced as user-facing language.** Per AC9, the grid card now appends `" importing"` (positive grid power), `" exporting"` (negative), or `" idle"` (rounds to 0.0 kW). The architecture.md energy sign convention (positive=import) is consumed correctly. This is the first homeowner-facing surface where grid sign is exposed; future displays should reuse `_format_grid_power` or duplicate the same convention.
- **Cold-start window note (R5-relevant context).** `SystemSnapshot.operating_mode` is non-Optional; the StateStore default `degraded` carries through cold start. The homeowner headline renders `"Running with limited functionality"` with the calm `degraded` explanation during the cold-start window between process boot and the first evaluation cycle. This matches UX spec §"calm over urgency" and is the intended behavior — not a defect. Story 8-1's deferred finding `[control_loop.py:73-76]` about first-tick `operating_mode=None` does not surface here because the field is structurally non-None.
- **`_format_kwh` removed.** Was used only by the old three-row grid card (showing imported/exported kWh). With AC8's single-primary-value refactor, the helper has no remaining call sites and was deleted; the now-unused import line was also cleaned up. Future stories that need a kWh formatter should reintroduce it as needed.
- **No new dependencies.** `uv.lock` unchanged. Matches the zero-new-dependency contract carried forward from Epic 9 / 9-X.
- **Sprint-status update.** Per Epic 9 retro action A8 (Status header ↔ sprint-status agreement) and Epic 8 retro action A1/A2 (codified in bmad-create-story), the story file Status header transitions `ready-for-dev → in-progress → review` mirror the sprint-status development_status entry. The `last_updated` annotation will be refreshed on review-cycle close.
- **A7 review-closure gate.** Story 10.1 is the first Epic 10 story; the A7 enforcement codified in `bmad-code-review` (per Epic 9 retro action A7) will gate `review → done`. No deferred-work entries are expected because the surface is small, but `bmad-code-review` should still walk the three closure criteria.

### File List

**New files:**
- `src/open_ems/web/templates/fragments/homeowner/status-headline.html` — status headline server-rendered fragment.
- `src/open_ems/web/static/open-ems.css` — hand-authored CSS bundle with design tokens (AC19 Path B).
- `tests/unit/web/test_dashboard_route.py` — dashboard route + shell template + AC16 perf assertions.
- `tests/integration/web/test_homeowner_dashboard.py` — populated, degraded, partial-UNAVAILABLE, STALE scenarios.
- `tests/integration/web/test_homeowner_dashboard_a11y.py` — placeholder axe-core scan (xfail+skipif per Story 9.x precedent).

**Modified files:**
- `src/open_ems/core/state.py` — added `active_strategy: EnergyStrategy` field to `SystemSnapshot`.
- `src/open_ems/core/state_store.py` — imported `EnergyStrategy`; added `active_strategy` kwarg-only constructor parameter with default `EnergyStrategy.maximize_self_consumption`; stored on `self._active_strategy`; threaded into initial snapshot (line 54+) and every subsequent `publish(...)` snapshot.
- `src/open_ems/engine/control_loop.py` — replaced hardcoded `strategy=EnergyStrategy.maximize_self_consumption` with `strategy=snapshot.active_strategy`; removed now-unused `EnergyStrategy` import.
- `src/open_ems/web/app.py` — imported `StaticFiles`; mounted `/static` directory in `create_app()`.
- `src/open_ems/web/state_serialization.py` — added `active_strategy` to both serializers; added `build_homeowner_headline_context` builder; added `_STRATEGY_LABELS` and `_DEGRADED_EXPLANATIONS` module-level constants; refactored `_homeowner_card_rows` to single-row primary value; added `_format_grid_power` helper; removed unused `_format_kwh`.
- `src/open_ems/web/routes/fragments.py` — imported `build_homeowner_headline_context`; added `GET /fragments/homeowner/status-headline` route.
- `src/open_ems/web/templates/dashboard.html` — restructured to single-column flex stack (`dashboard-stack`); replaced 4-card layout (battery/solar/grid/EV) with headline + battery + solar + grid; EV card removed from primary render path.
- `src/open_ems/web/templates/fragments/homeowner/_card.html` — rewrote per AC8: single primary value rendered from `rows[0].value`; slate-400 caption in stable bottom slot via `state-card__freshness`; `"Unavailable"` body text; `<section aria-label="{{ title }}">`; `aria-live="polite"` on body wrapper.
- `src/open_ems/web/templates/base.html` — added `<link rel="stylesheet" href="/static/open-ems.css">` in `<head>`.
- `tests/unit/core/test_state.py` — added imports for `EnergyStrategy`; added `active_strategy` to `_snapshot()` fixture; added two new tests (`test_snapshot_active_strategy_is_required_no_model_level_default`, `test_snapshot_accepts_all_energy_strategy_values`).
- `tests/unit/core/test_state_store.py` — added `EnergyStrategy` import; added 3 new tests (`test_state_store_initial_snapshot_carries_cold_start_strategy_default`, `test_state_store_initial_snapshot_carries_explicit_strategy`, `test_state_store_publish_preserves_active_strategy`).
- `tests/unit/engine/test_control_loop.py` — added `EnergyStrategy` import; added parametrized `test_evaluation_input_strategy_reads_from_snapshot_active_strategy` (3 cases).
- `tests/unit/web/test_state_serialization.py` — added `pytest`, `EnergyStrategy`, `GridMeterState` imports; added `active_strategy` to fixture; added 11 new tests covering AC4/AC5/AC8/AC9/AC14 plus exhaustive coverage tests for `_STRATEGY_LABELS` and `_DEGRADED_EXPLANATIONS`.
- `tests/unit/web/test_fragment_routes.py` — added `EnergyStrategy` and `SystemOperatingMode` imports; added 4 new tests for the status-headline route.
- `tests/integration/web/test_setup_validation_e2e.py` — added `EnergyStrategy` import; added `active_strategy` to `_populated_snapshot()` fixture.
- `tests/integration/web/test_setup_constraints_e2e.py` — added `EnergyStrategy` import; added `active_strategy` to `_empty_snapshot()` fixture + one inline `SystemSnapshot(...)` construction.
- `tests/integration/web/test_handoff_guide_e2e.py` — added `EnergyStrategy` import; added `active_strategy` to `_snapshot()` fixture.
- `tests/unit/web/test_setup_validation_routes.py` — added `EnergyStrategy` import; added `active_strategy` to `_populated_snapshot()` fixture.
- `tests/unit/web/test_setup_constraints_routes.py` — added `EnergyStrategy` import; added `active_strategy` to `_empty_snapshot()` fixture.
- `tests/unit/web/test_handoff_guide_route.py` — added `EnergyStrategy` import; added `active_strategy` to `_snapshot()` fixture.
- `tests/unit/services/test_deployment_validation_service.py` — added `EnergyStrategy` import; added `active_strategy` to both `_empty_snapshot()` and `_populated_snapshot()` fixtures.
- `tests/unit/services/test_constraints_service.py` — added `EnergyStrategy` import; added `active_strategy` to `_empty_snapshot()` fixture and three inline `SystemSnapshot(...)` constructions.
- `tests/unit/engine/test_intent_executor.py` — added `EnergyStrategy` import; added `active_strategy` to `_snapshot()` fixture.
- `tests/unit/engine/rules/test_energy_balancing.py` — added `active_strategy` to the inline `SystemSnapshot(...)` construction in `test_from_snapshot_requires_strategy_keyword_only` (the file already imports `EnergyStrategy`).
- `tests/unit/engine/test_models.py` — added `active_strategy` to `_snapshot()` fixture (the file already imports `EnergyStrategy`).
- `tests/unit/engine/test_operating_mode.py` — added `active_strategy` to the inline `SystemSnapshot(...)` construction (the file already imports `EnergyStrategy`).
- `_bmad-output/implementation-artifacts/sprint-status.yaml` — `10-1-...` flipped `ready-for-dev → in-progress → review`; `last_updated` annotated.

### Change Log

| Date | Change |
|---|---|
| 2026-05-11 | Story 10.1 created via `bmad-create-story`. Status: ready-for-dev. A2 evaluation: not triggered (no T1–T7 matches). Epic 10 status flipped from `backlog` to `in-progress` at story creation. |
| 2026-05-11 | Story 10.1 implemented. Adds `SystemSnapshot.active_strategy` field + StateStore plumbing; ControlLoop reads strategy from snapshot; new homeowner status-headline fragment + template; metric cards refactored to single-primary-value layout per UX spec; dashboard restructured to vertical stack (headline + 3 cards); EV card temporarily removed from primary render path (Story 10.2 will re-add). `+33` net tests (1445 → 1478 passing) + 1 xfailed (a11y placeholder). Ruff, format, mypy clean. AC19 Path B chosen (hand-authored CSS bundle, no Tailwind/CDN/Node). Status: in-progress → review. |

### Review Findings

_Adversarial review run on 2026-05-11 via `bmad-code-review` (three parallel layers: Blind Hunter, Edge Case Hunter, Acceptance Auditor). The Acceptance Auditor verdict on AC1–AC21 was ✅ clean (every AC SATISFIED). All findings below are quality / hygiene / hardening items raised by the Blind Hunter and Edge Case Hunter layers. Counts: 1 HIGH patch (test orphaning), 3 MEDIUM patches (runtime hardening + asset test), 4 LOW patches (cleanup/clarification), 3 deferred, 17 dismissed-with-rationale._

#### Patches (8 — apply during the review-closure cycle)

- [x] [Review][Patch] **HIGH — Restore orphaned non-UTC-tzinfo `pytest.raises` block to `test_snapshot_requires_utc_captured_at`** [`tests/unit/core/test_state.py:196-198, 251-252`] — The original second assertion in `test_snapshot_requires_utc_captured_at` (rejecting `tzinfo=timezone(timedelta(hours=1))`) was grafted onto the new `test_snapshot_accepts_all_energy_strategy_values` function body when the new tests were inserted between them. Python's indentation-based parser attaches the trailing `with pytest.raises(ValidationError): _snapshot(captured_at=..., tzinfo=non-UTC)` to whichever function precedes it at matching indent. Result: `test_snapshot_requires_utc_captured_at` silently lost non-UTC tzinfo coverage, AND `test_snapshot_accepts_all_energy_strategy_values` is now a multi-purpose test whose body contradicts its name and docstring.
- [x] [Review][Patch] **MEDIUM — Defensive `.get(...)` fallback for `_STRATEGY_LABELS` and `_DEGRADED_EXPLANATIONS` lookups** [`src/open_ems/web/state_serialization.py:97-104`] — `build_homeowner_headline_context` uses direct dict-subscript (`_STRATEGY_LABELS[active_strategy]`, `_DEGRADED_EXPLANATIONS[operating_mode]`). If a future enum member ships ahead of the label table, the homeowner status-headline endpoint 500s. The exhaustiveness tests catch the issue at test time but provide no runtime guard. Use `.get(active_strategy, active_strategy.value.replace("_", " ").title())` and `.get(operating_mode, "")` so unknown enum values degrade gracefully to a sensible plain-language fallback.
- [x] [Review][Patch] **MEDIUM — Guard `_format_grid_power` against NaN/Inf input** [`src/open_ems/web/state_serialization.py:238-246`] — `GridMeterState.grid_power_kw` is a plain `float` with no NaN constraint. A degraded DSMR parse path that defaults to `float("nan")` or `float("inf")` produces homeowner-facing strings like `"nan kW idle"` or `"inf kW importing"`. Add `math.isnan(kw) or math.isinf(kw)` guard returning `"Unavailable"` (or routing through the existing unavailable branch).
- [x] [Review][Patch] **MEDIUM — Add test that `/static/open-ems.css` returns 200** [`tests/integration/web/test_homeowner_dashboard.py` — new test] — `StaticFiles(directory=str(static_dir))` does not raise at construction if the directory is missing; it raises 404 at request time. No current test asserts the asset is actually servable, so a future rename/move of `src/open_ems/web/static/` would silently un-style the entire homeowner dashboard. Add an integration test that boots `create_app()` and asserts `GET /static/open-ems.css → 200` with `Content-Type: text/css`.
- [x] [Review][Patch] **LOW — Remove dead `.state-card__metrics` / `.state-card__metric` CSS rules** [`src/open_ems/web/static/open-ems.css:166-190`] — The `_card.html` template no longer renders a `<dl class="state-card__metrics">` element (single-primary-value refactor per AC8). The corresponding CSS rules are unreachable.
- [x] [Review][Patch] **LOW — Tighten unauth dashboard redirect assertion to exact 302 + Location header check** [`tests/unit/web/test_dashboard_route.py:1571-1572`] — Current assertion is `status_code in (302, 401)`. The actual code path (browser GET, no `HX-Request` header) ALWAYS produces 302 → `/login`. If a future change accidentally flips this to 401 for browser navigations (breaking the redirect-to-login UX), this test would not catch it. Assert `status_code == 302` and `response.headers["location"].startswith("/login")`.
- [x] [Review][Patch] **LOW — Comment in `_format_grid_power` documenting that `abs()` after rounding is load-bearing for `-0.0` normalization** [`src/open_ems/web/state_serialization.py:243-246`] — `round(-0.04, 1)` is `-0.0`. Without the `abs()` call, the f-string would render `"-0.0 kW idle"`. The defensive intent should be stated so a future refactor cannot innocently strip the `abs()` call.
- [x] [Review][Patch] **LOW — Reconcile EV card `"Unavailable"` value-field path with documented unavailable-branch contract** [`src/open_ems/web/state_serialization.py:288-336`] — For battery / solar / grid, the "Unavailable" body text comes from the template's `unavailable` branch (when `_homeowner_card_rows` returns empty). For the EV card's `current_power_kw is None` case, "Unavailable" appears as a value field while the device is technically `available` — inconsistent visual surface. Either route EV's None case through the `unavailable` branch OR update `_homeowner_card_rows`' docstring to acknowledge the EV value-field exception.

#### Deferred (3 — pre-existing or carried forward)

- [x] [Review][Defer] **`_active_strategy` writer-lock contract for Story 10.3 mutator** [`src/open_ems/core/state_store.py:46, 92`] — deferred, prep gate for Story 10.3. Tracked in `deferred-work.md` under the code-review-of-10-1 heading.
- [x] [Review][Defer] **Route-level `fail_safe` parametrization gap in `test_status_headline_*`** [`tests/unit/web/test_fragment_routes.py`] — deferred, serializer-layer test (`test_state_serialization.py`) already parametrizes all three operating modes. Tracked in `deferred-work.md`.
- [x] [Review][Defer] **HTMX-error UX when `/fragments/homeowner/status-headline` 500s** [`src/open_ems/web/routes/fragments.py`] — deferred, broader UX pattern not specific to 10.1. Tracked in `deferred-work.md`.

#### Dismissed (17 — with rationale)

- **`pathlib` import missing in `app.py`** — False positive. `pathlib` already imported at `app.py:5` (verified by direct read).
- **Strict `401` vs `(302, 401)` inconsistency between headline-fragment test and dashboard test** — Intentional and correct. Headline fragment test sets `HX-Request: true` and gets 401; dashboard test makes a browser-style GET and gets 302. Two paths, two correct behaviors.
- **Metric-card row labels computed but never rendered** — False positive. The `label` key is still consumed by the EV branch of `_homeowner_card_rows` (preserved intact for Story 10.2's EV card re-introduction).
- **`StateStore._active_strategy` is a plain mutable attribute with no read-only protection** — Python convention. The "no public mutator" contract is documented; Story 10.3 will land the controlled setter under the writer lock.
- **`StateStore.__init__` should mark new kwargs `*` keyword-only** — Defensive hardening, not a defect. No positional caller exists past `stale_threshold_seconds` in the codebase today.
- **`_format_kw` does not normalize `-0.0` for signed power fields** — No current consumer. Battery / solar / grid primary values are either unsigned or routed through `_format_grid_power` (which already normalizes). EV is out of the primary render path until Story 10.2.
- **`_format_kw` renders `"inf kW"` for positive infinity** — Defensive; no current production path produces inf-valued power readings. Re-evaluate when DSMR / Modbus adapters expose new failure modes.
- **Cold-start "Running with limited functionality" headline shown before first ControlLoop tick** — Intentional per UX spec §"calm over urgency" and explicitly documented in this story's completion notes ("matches UX spec §'calm over urgency' and is the intended behavior — not a defect").
- **`test_snapshot_active_strategy_is_required_no_model_level_default` does not pin the ValidationError to the active_strategy field** — Test still proves the AC1 claim (it would fail if the model had a default). Tightness improvement only; no behavioral risk.
- **`body.count('hx-trigger="load, every 10s"') == 4` is brittle to future poll-interval changes** — Will be updated when Story 10.2 re-adds the EV card; current assertion is correct for the current scope.
- **`"Unavailable" not in solar.text` substring fragility** — Speculative; no current path produces `"Unavailable"` substrings in non-unavailable bodies.
- **`test_card_rows_are_single_primary_value_per_ac8` does not cover EV** — Out of AC8's primary render path. EV is intentionally excluded until Story 10.2.
- **`/fragments/homeowner/status-headline` non-HTMX path returns 302** — Documented `require_homeowner` behavior; matches the dual-path auth contract for fragment routes.
- **`data-active-strategy="minimize_cost"` leaks engine-internal enum value into the homeowner DOM** — All current `EnergyStrategy` values are homeowner-safe by construction (they ARE the user-facing strategy names). The enum itself is the contract.
- **`SystemSnapshot.active_strategy` non-Optional breaks pre-10.1 persisted snapshot deserialization** — No codebase path consumes legacy persisted snapshots. The model is in-memory only; AC1's design explicitly mandates the non-Optional shape.
- **SSE payload shape change (new top-level `active_strategy` key) is implicitly breaking for strict external consumers** — No strict external SSE consumer exists today.
- **`test_stale_caption_appears_with_stable_layout` doesn't verify the 3-way (fresh/stale/unavailable) layout-stability invariant** — Minor coverage gap; the freshness slot is always rendered (verified by template inspection) and AC13's strict claim is covered for the fresh→stale direction.
