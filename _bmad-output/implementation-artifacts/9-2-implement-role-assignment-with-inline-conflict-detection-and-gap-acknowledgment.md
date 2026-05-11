# Story 9.2: Implement role assignment with inline conflict detection and gap acknowledgment

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As an installer,
I want to assign energy roles to discovered and manually added devices with inline conflict detection, with grid meter absence hard-blocked and other role gaps requiring explicit acknowledgment,
so that I complete role assignment with full visibility into configuration validity — any gap I proceed past is a deliberate, recorded decision, not an oversight.

## Acceptance Criteria

### AC1 — Role storage extends the device_registry table (no parallel role table)

Story 9.1 established `device_registry` as the **single source of truth** for the installer-acknowledged device set. Story 9.2 extends that contract — it does **not** introduce a parallel `device_role_assignments` table. The role is stored as a nullable column on the existing row.

- Migration `0010_add_role_to_device_registry.py` adds:

  | Column | Type | Constraints |
  |---|---|---|
  | `role` | TEXT | NULLABLE; CHECK IN (`'inverter'`, `'battery'`, `'ev_charger'`, `'grid_meter'`) OR NULL |
  | `role_assigned_at` | TEXT | NULLABLE, ISO 8601 UTC; non-NULL iff `role` is non-NULL |

- The migration adds a partial index `ix_device_registry_role` on `role` filtered to `WHERE role IS NOT NULL` so the duplicate-role conflict scan (AC4) is cheap.
- `src/open_ems/storage/repositories/device_repo.py` is extended:
  - `DeviceRegistryEntry` (existing model) gains two fields: `role: DeviceRole | None = None`, `role_assigned_at: datetime | None = None`. A new `@model_validator(mode="after")` rejects the impossible state `role is not None AND role_assigned_at is None` (and vice versa) at every read — matching the AC6-style structural invariant pattern from Story 9.1 R3 #1.
  - New write method: `async assign_role(device_id, *, role: DeviceRole | None, assigned_at: datetime) -> None`. Setting `role=None` clears both `role` and `role_assigned_at` in the same UPDATE. Setting `role=<DeviceRole>` sets both atomically. Raises `ValueError` if no row matches.
  - Both columns are surfaced in `list_all()` / `get_by_device_id()` SELECT lists; existing tests adjusted to ignore None defaults.
- Migration round-trip (`upgrade head → downgrade -1 → upgrade head`) verified out-of-band and in `tests/integration/test_migrations.py::test_migration_0010_round_trips`. The downgrade drops the partial index and both columns in reverse order.

### AC2 — Wizard state extends to track Step 2 + acknowledged role-gap labels

`wizard_state` is extended in the same migration (precedent: Story 9.1's single migration adding `device_registry` + `wizard_state` together).

| Column | Type | Constraints |
|---|---|---|
| `step_2_complete` | INTEGER (0/1) | NOT NULL DEFAULT 0; CHECK IN (0, 1) |
| `step_2_completed_at` | TEXT | NULLABLE, ISO 8601 UTC |
| `step_2_acknowledged_gaps` | TEXT | NULLABLE — JSON-encoded `list[str]` of acknowledged role-gap labels (sorted, deduplicated); NULL = no acknowledgments yet |

The `step_2_acknowledged_gaps` column uses a JSON-encoded list rather than a separate `wizard_step_2_acknowledgments` table because:
1. Acknowledgments are per-session (FK CASCADE on session delete already disposes of the wizard state, and a separate table would need the same CASCADE).
2. The set of valid labels is small and bounded (`battery_missing`, `inverter_missing`, `ev_charger_missing` — see AC5). Promoting to a table would introduce join complexity without per-row value.
3. The list is always written atomically by the route handler — there is no concurrent-row-write concern that a table would solve.

- `WizardState` Pydantic model (existing) gains: `step_2_complete: bool`, `step_2_completed_at: datetime | None = None`, `step_2_acknowledged_gaps: frozenset[str] = frozenset()`.
- Storage encoding: JSON `null` or an absent column → `frozenset()`. JSON array → `frozenset(parsed)`. A `@model_validator(mode="after")` rejects unknown gap labels (whitelist enforced at the Pydantic boundary, not just in the route — exactly the same anti-tamper guard pattern as Story 9.1 R3 #1).
- `WizardStateRepo` gains:
  - `async set_step_2_complete(session_id, *, now: datetime) -> None` — raises `ValueError` if no row exists (precedent: `set_step_1_complete`).
  - `async record_acknowledged_gaps(session_id, *, gaps: frozenset[str], now: datetime) -> None` — replaces the entire list (idempotent; the route handler always passes the full desired set). Raises `ValueError` if any gap label is outside the allowed whitelist `{'battery_missing', 'inverter_missing', 'ev_charger_missing'}` (the whitelist is enforced at the repo boundary AS WELL AS at the Pydantic model — defense-in-depth, so a future caller bypassing the model still cannot persist garbage).

### AC3 — `RoleAssignmentService` encapsulates conflict detection + gate evaluation

A new service `RoleAssignmentService` lives at `src/open_ems/services/role_assignment.py`. It is the **single evaluator** for conflicts and Step-2 gate state — route handlers, templates, and the future Step 4 validation service all call into the same surface.

- Public surface (all async unless noted):
  - `evaluate_assignments() -> RoleAssignmentSnapshot` — runs a single registry read and produces a `frozen=True, extra="forbid"` Pydantic model carrying:
    - `assignments: tuple[RoleAssignmentRow, ...]` — one per `device_registry` row, ordered by `(first_seen_at, device_id)` (same ordering as `DeviceRepo.list_all()` — so the UI is deterministic across renders).
    - `conflicts: frozenset[RoleConflict]` — one entry per role that has ≥2 assignments, listing the colliding `device_id`s.
    - `gaps: frozenset[RoleGap]` — typed model carrying `role: DeviceRole`, `severity: Literal['hard_block', 'acknowledgeable_warn']`, `label: str` (the canonical label persisted in `wizard_state.step_2_acknowledged_gaps`).
    - `unassigned_count: int` — number of registry rows whose `role is None`.
  - `evaluate_gate(*, acknowledged_gaps: frozenset[str]) -> RoleGateOutcome` — composes `evaluate_assignments()` with the acknowledged-gap set; returns `(can_advance: bool, blocking_gaps: tuple[RoleGap, ...], unacknowledged_warnings: tuple[RoleGap, ...], conflicts: tuple[RoleConflict, ...])`.

- **Hard block vs. acknowledgeable** is a function of role only:
  - `DeviceRole.grid_meter` → `severity='hard_block'` when missing; the gate cannot be bypassed.
  - `DeviceRole.battery`, `DeviceRole.inverter`, `DeviceRole.ev_charger` → `severity='acknowledgeable_warn'` when missing; the gate clears once the corresponding label is in `acknowledged_gaps`.

- **Conflict definition:** two or more rows with the **same non-NULL role**. A conflict is independent of the acknowledgment system — conflicts are **always blocking** for Step 2 advance, regardless of acknowledgments. The installer must resolve them by changing one of the colliding rows' roles.

- The role assignment label whitelist is centralised as a module-level constant `_VALID_GAP_LABELS: frozenset[str] = frozenset({"battery_missing", "inverter_missing", "ev_charger_missing"})`. The same constant is referenced by:
  - `WizardStateRepo.record_acknowledged_gaps` (repo-level whitelist)
  - `WizardState` model validator (Pydantic-level whitelist)
  - The route handler at `POST /installer/setup/roles/acknowledge-gap` (route-level whitelist for the form-submitted label)
  - `RoleAssignmentService` (when constructing `RoleGap.label`)

  All four readers import from `role_assignment.py` — there is **one** authoritative definition. This is the same pattern Story 9.0c locked for capability profile labels (single module-level registry, every consumer imports it).

### AC4 — Inline conflict detection (no modal, no page navigation)

When the installer changes a role via the role picker:

- The change is submitted to `POST /installer/setup/roles/{device_id}/assign` with form fields `csrf_token`, `role` (one of `inverter`, `battery`, `ev_charger`, `grid_meter`, or empty string for unassigned).
- The server: (1) validates `role` value (rejects garbage with a 400 + inline error fragment, exact-match reason `"role_invalid: {value!r}"`), (2) calls `DeviceRepo.assign_role(device_id, role=<resolved>, assigned_at=now)`, (3) re-runs `RoleAssignmentService.evaluate_assignments()`, (4) renders `_setup_roles_list.html` (the full list region — not just one row, because a role change to row A can clear or create a conflict on row B; the entire region's conflict indicators must refresh atomically).
- The HTMX response targets `#roles-list-region` with `hx-swap="innerHTML"`. No modal, no full-page nav.
- **Conflict indicator semantics:** each `RoleAssignmentRow` carries `conflicting_device_ids: tuple[str, ...]`. When non-empty, the row template renders an inline error: `"Another device is already assigned this role: <device_id>, ..."` — using the exact peer device_ids, not a generic "another device". Accessibility: the inline error is a sibling element with `role="alert"` so screen readers announce the new conflict.
- The role picker `<select>` is server-rendered (HTMX response carries the new state); no client-side JS is required for the conflict-display path. (Conformance with the architecture-mandated server-rendered HTML + HTMX baseline; no SPA.)

### AC5 — Grid meter hard block + non-blocking gap acknowledgment

- **Grid meter HARD block:** if `RoleAssignmentService.evaluate_gate(...)` reports a `RoleGap` with `role=DeviceRole.grid_meter` and `severity='hard_block'`:
  - The "Continue to Step 3" submit button is server-rendered with `disabled` and `aria-disabled="true"`.
  - An inline hard-block notice is rendered: exact text `"Grid meter is required for peak limiting and safety behavior — this cannot be bypassed in v1."` This is verbatim from the epic spec (lines 1947–1948).
  - The acknowledgment route MUST refuse to accept a label `"grid_meter_missing"` — the form-submitted label is matched against `_VALID_GAP_LABELS` which deliberately excludes the grid_meter case. Attempted submission returns a 400 with the exact-match reason `"gap_label_not_acknowledgeable: grid_meter_missing"`.
- **Non-blocking role gaps** (battery / inverter / ev_charger missing):
  - Inline WARN notice per missing role, with plain-language impact text (architecture decision precedent: BAD-7 system status messages):
    - `"battery_missing"` → `"No battery assigned — battery dispatch optimization and reserve enforcement are unavailable."`
    - `"inverter_missing"` → `"No inverter assigned — solar production data and PV-driven optimization are unavailable."`
    - `"ev_charger_missing"` → `"No EV charger assigned — EV scheduling and charge-rate control are unavailable."`
  - Each WARN notice carries an `Acknowledge` button (a form posting to `POST /installer/setup/roles/acknowledge-gap` with `gap_label=<label>`).
  - On acknowledge: the server reads the current `step_2_acknowledged_gaps`, adds the label, writes the union back via `WizardStateRepo.record_acknowledged_gaps(...)`, re-evaluates the gate, and returns the updated `_setup_roles_gap_panel.html` fragment. The acknowledgment is persisted — surviving page reload, browser restart, and process restart (until the session expires).
  - **Acknowledgment is sticky for the session.** An acknowledged label remains acknowledged even if the installer later **assigns** a device to that role (i.e., the gap closes). This is intentional — the acknowledgment is a per-session "I have considered this gap" signal. If the gap later re-opens (installer unassigns), the prior acknowledgment is **invalidated**: `RoleAssignmentService.evaluate_gate(...)` only treats an acknowledgment as effective for the **currently open** gaps; a stored label for a no-longer-open gap is silent dead state, and on the next advance it is filtered out by the gate evaluator (see AC6 below).

### AC6 — Step 2 advance gate + sticky acknowledgment semantics

A new `POST /installer/setup/roles/advance` route is the only path that flips `wizard_state.step_2_complete=1`. The server-side gate:

1. Re-reads `device_registry` and `wizard_state` (no caching — same write-lock-acquire-then-read pattern as Story 9.1 AC7 to avoid concurrent-write races).
2. Calls `RoleAssignmentService.evaluate_gate(acknowledged_gaps=wizard_state.step_2_acknowledged_gaps)`.
3. **Fail conditions** (any one fails the gate):
   - Any `conflict` exists.
   - Any `gap` with `severity='hard_block'` exists (grid meter missing).
   - Any `gap` with `severity='acknowledgeable_warn'` exists AND its label is NOT in `acknowledged_gaps`.
4. **Gap-label drift cleanup:** before the pass write, the gate filters `step_2_acknowledged_gaps` down to labels matching **currently open** gaps. If the filtered set differs from the stored set (i.e., the installer acknowledged a gap that they later closed by assigning the role), the route updates `step_2_acknowledged_gaps` to the filtered set in the same write-lock window. This keeps the persisted state honest: an audit reading `step_2_acknowledged_gaps` at any time sees only acknowledgments for gaps that **actually exist** at that moment.
5. **Pass:** writes `step_2_complete=1`, `step_2_completed_at=now()`, and the filtered `step_2_acknowledged_gaps` atomically via a single `WizardStateRepo.set_step_2_complete(...)` call (extend the method signature to accept the filtered set). Returns a 302 redirect to `/installer/setup/constraints` (Story 9.3's responsibility — 9.2 ships a placeholder route at that URL, paralleling Story 9.1's roles placeholder).
6. **Fail:** returns the roles page with the inline error banner listing the specific blockers; the "Continue to Step 3" button is server-rendered disabled. No modal.

The Step-2 gate is **idempotent**: a passing call followed by another passing call leaves the state unchanged (`step_2_completed_at` is preserved from the first pass — `set_step_2_complete` is a no-op if already complete; this prevents the timestamp from shifting on every page reload).

### AC7 — Routes (HTML + HTMX fragments) and placeholder for Step 3

New routes are added under `src/open_ems/web/routes/setup.py`. All require `Depends(require_installer)`. All POSTs are CSRF-protected by the existing middleware (Story 2-4). All HTML responses extend `installer/setup_layout.html` (the persistent 4-step indicator inherited from Story 9.1).

| Method | Path | Returns |
|---|---|---|
| GET | `/installer/setup/roles` | full HTML page rendering all `device_registry` rows with the role picker, capability badge (from Story 9.1's `get_profile(...)` render-time re-resolution per R6), inline conflict indicators, the gap panel, and the Continue-to-Step-3 control |
| POST | `/installer/setup/roles/{device_id}/assign` | HTMX fragment `_setup_roles_list.html` re-rendering the full roles list region (see AC4 rationale) |
| POST | `/installer/setup/roles/acknowledge-gap` | HTMX fragment `_setup_roles_gap_panel.html` re-rendering only the gap panel; the form body carries `csrf_token` + `gap_label` |
| POST | `/installer/setup/roles/advance` | on pass: 302 redirect to `/installer/setup/constraints` (302 — matching Story 9.1's `/advance` precedent corrected via review patch). On fail: 400 with the inline error banner fragment |
| GET | `/installer/setup/constraints` | placeholder route returning a "Step 3 will be implemented in Story 9.3" page extending `setup_layout.html` (parallel to Story 9.1's `roles_placeholder`; the file is named `setup_constraints_placeholder.html`). Story 9.3 deletes this placeholder. |

The **existing** placeholder route `/installer/setup/roles` (returning `setup_roles_placeholder.html` in Story 9.1) and its template are **replaced** by the real implementation in this story. The placeholder template file `setup_roles_placeholder.html` is deleted; the new full page lives at `setup_roles.html`.

**Discovery → Roles backward navigation:** the existing `setup_layout.html` step indicator renders Step 1 as a hyperlink when `step_2_complete=0` so the installer can return to discovery. This is a behaviour change in the layout — Story 9.1's layout shipped without back-navigation hyperlinks. The link uses a normal `<a href>` (not an HTMX form), so the browser back button + history work normally per UX spec line 1470 ("Browser back works").

### AC8 — Templates and accessibility

New / modified Jinja2 templates under `src/open_ems/web/templates/installer/`:

- **DELETE** `setup_roles_placeholder.html` — replaced by the real implementation.
- **NEW** `setup_roles.html` — extends `setup_layout.html` (`active_step='roles'`); renders the page body with three sections:
  1. "Devices and roles" — the role-picker list (uses `_setup_roles_list.html`).
  2. "Configuration validity" — the gap panel (uses `_setup_roles_gap_panel.html`).
  3. "Continue" — the advance form with the server-disabled-state Continue-to-Step-3 button.
- **NEW** `_setup_roles_list.html` — the full list region, target of the assign-route HTMX swap. Iterates `assignments` and renders one `_setup_roles_row.html` per device.
- **NEW** `_setup_roles_row.html` — single row: device name + capability badge (re-resolved at render time via the existing `get_profile` Jinja global from Story 9.1 — R6 inheritance), role picker `<select>`, inline conflict indicator (rendered only when `row.conflicting_device_ids` is non-empty).
- **NEW** `_setup_roles_gap_panel.html` — the gap panel, target of the acknowledge-gap HTMX swap. Renders one notice per `gap` from `RoleAssignmentSnapshot.gaps`. Hard-block notices have no acknowledge button; acknowledgeable notices have an `Acknowledge` button when the label is NOT in `acknowledged_gaps`, and an `Acknowledged` indicator (with a visible "Re-evaluate" un-acknowledge form — see below) when it IS in `acknowledged_gaps`.
- **NEW** `setup_constraints_placeholder.html` — minimal Step-3 placeholder.

**Un-acknowledgment path:** per AC5's "sticky for the session" rule, the installer needs a path to revoke an acknowledgment they no longer agree with. The `_setup_roles_gap_panel.html` renders a `Revoke acknowledgment` form for each already-acknowledged gap. Submitting it posts to `POST /installer/setup/roles/acknowledge-gap` with `action=revoke&gap_label=<label>` (the same route accepts both add and revoke actions to keep the surface narrow). Server-side: the route reads the current set, removes the label, writes back, re-evaluates, returns the panel fragment. This satisfies the "installer may proceed past a non-blocking role gap **only after explicitly acknowledging it**" rule while remaining reversible — the installer is never trapped by a prior decision.

**Accessibility requirements** (per UX spec §Accessibility + UX component #7):
- Each role `<select>` has `aria-label` = `"Role for {device_id}"` per UX component #7.
- The inline conflict indicator uses `role="alert"` so screen readers announce dynamic changes.
- The hard-block notice is rendered inside an element with `aria-live="polite"` so screen readers pick it up on appearance.
- Every Continue-to-Step-3 control state (disabled, enabled) is server-rendered — color is never the sole signal that the gate is closed (visible text accompanies every state).
- Badge contrast (FULL / REDUCED inherited from Story 9.1) and Continue button states meet ≥4.5:1 against background.
- Keyboard reachability for every interactive element: role picker, conflict indicator's "Resolve" hyperlink (if added — optional polish), Acknowledge button, Revoke button, Continue button. All tab order is deterministic; no implicit `tabindex` mutation.
- Automated axe-core scan: same deferred-CI-hardening pattern as Story 9.1 AC10. A placeholder test at `tests/integration/web/test_setup_roles_a11y.py` is marked `pytest.mark.xfail(reason="axe-playwright wiring deferred — same as Story 9.1 AC10", strict=False)`. The outer `pytest.mark.skipif(npx absent)` is retained. The other accessibility bullets above are exercised by `tests/unit/web/test_setup_roles_routes.py` and the template-rendering tests.

### AC9 — Tests

Each test asserts the **exact** contract clause it covers (no substring `in` assertions on rejection reasons — exact match, per Story 9-0c AC7 / Story 9-1 AC11 precedent).

**Unit tests (`tests/unit/`):**
- `storage/repositories/test_device_repo.py` (extend) — assign_role round-trip: `None → DeviceRole → None` chain leaves `role_assigned_at` consistent at each step; assign_role on a missing device_id raises `ValueError` with the exact message `f"No device_registry row for device_id={device_id!r}"`; model-validator rejects the impossible state `role is not None AND role_assigned_at is None` at every Pydantic round-trip.
- `storage/repositories/test_wizard_state_repo.py` (extend) — `set_step_2_complete` is idempotent (`step_2_completed_at` does not shift on a second call); `record_acknowledged_gaps` round-trip through JSON serialization preserves frozenset equality; `record_acknowledged_gaps` rejects an out-of-whitelist label with the exact `ValueError("acknowledged_gap_label_invalid: ...")`.
- `services/test_role_assignment_service.py` (new) — covers:
  - Empty registry → `gaps` contains all four roles (grid_meter as `hard_block`, others as `acknowledgeable_warn`); `conflicts=frozenset()`; `unassigned_count=0`.
  - Single grid_meter assigned, no others → only acknowledgeable warns remain; gate evaluates to `can_advance=False` until all three acknowledgeable gaps are acknowledged.
  - All four roles assigned, no conflicts, no acknowledgments needed → gate `can_advance=True`.
  - Two devices assigned the same role (e.g., both `inverter`) → conflict reported with both device_ids; gate `can_advance=False` regardless of acknowledgments.
  - Grid_meter missing AND acknowledgments present for the other three → gate still `can_advance=False` (hard block dominates).
  - `evaluate_gate` filters out acknowledgments for already-closed gaps — the returned `acknowledged_gaps` field reflects only currently-open-and-acknowledged labels.
  - Stable ordering: `assignments` sorted by `(first_seen_at, device_id)` matches `DeviceRepo.list_all()` ordering.
- `services/test_role_assignment_gap_labels.py` (new) — assert `_VALID_GAP_LABELS` is exactly `{'battery_missing', 'inverter_missing', 'ev_charger_missing'}` — `'grid_meter_missing'` MUST NOT appear (anti-regression: the hard-block label cannot be acknowledgeable).
- `web/test_setup_roles_routes.py` (new) — covers:
  - Auth: non-installer → 302/403; missing session → 401-style redirect to login.
  - CSRF: every POST without a valid token → 403 (asserts the existing middleware behaviour at this route).
  - Role assignment: form submission with valid role → 200 + fragment swap; invalid role value → 400 with exact-match reason `f"role_invalid: {value!r}"`.
  - Conflict surface: assigning the same role to a second device produces an inline conflict indicator on both rows (assert both `device_id`s appear in the rendered fragment).
  - Acknowledge-gap: valid label → 200 + panel fragment; out-of-whitelist label (e.g., `'grid_meter_missing'`) → 400 with exact-match reason `f"gap_label_not_acknowledgeable: grid_meter_missing"`.
  - Acknowledge-gap revoke: `action=revoke` removes the label; re-acknowledging adds it back.
  - Advance pass: assign all four roles → POST advance → 302 to `/installer/setup/constraints`; `wizard_state.step_2_complete=True`; `step_2_completed_at` set.
  - Advance fail — hard block: no grid_meter → 400 + banner; `step_2_complete=False`.
  - Advance fail — conflict: two devices same role → 400 + banner; `step_2_complete=False`.
  - Advance with stale acknowledgment: acknowledge `battery_missing`, then assign a battery, then POST advance with no conflicts → 302 (pass); `wizard_state.step_2_acknowledged_gaps` is filtered to remove `battery_missing` (because the gap is no longer open) — assert the persisted set has been pruned.
  - Idempotent advance: a second POST advance after a passing first → still 302; `step_2_completed_at` is **unchanged** from the first pass.

**Integration tests (`tests/integration/`):**
- `web/test_setup_roles_e2e.py` (new) — full request → assign → conflict-clear → acknowledge → advance flow against in-memory SQLite. Cases:
  - End-to-end happy path: discovery → role assignment with all four roles → advance to constraints.
  - End-to-end mixed path: 3 roles + grid_meter missing → hard block surfaced; assign grid_meter → advance succeeds.
  - End-to-end gap-acknowledgment path: 1 device assigned grid_meter, no others; acknowledge battery_missing + inverter_missing + ev_charger_missing → advance succeeds with all three labels persisted to `wizard_state.step_2_acknowledged_gaps`.
  - End-to-end persistence path: assign roles + acknowledge gaps → simulate session re-load (new request with same `session_id`) → roles + acknowledgments are restored from DB; the gate state is identical before and after.
  - Conflict-and-resolve path: assign duplicate roles → see conflict indicator on both rows → reassign one to a different role → conflict indicator disappears from both rows in the next fragment swap.
- `web/test_setup_roles_a11y.py` (new — `pytest.mark.xfail` per AC8) — axe-core placeholder.
- `test_migrations.py` (extend) — migration 0010 round-trip case + schema-shape assertions on `device_registry.role`, `device_registry.role_assigned_at`, `wizard_state.step_2_*` columns.
- `web/test_app_startup.py` (extend) — assert `RoleAssignmentService` is wired into `app.state.role_assignment_service` after lifespan completion.

**Quality gates:** `pytest tests/ --no-cov -q`, `mypy src/`, `ruff check .`, `ruff format --check .` all clean before status moves to `review`.

### AC10 — Adversarial 3-layer review with severity tagging (process AC)

Before `Status: review → done`, run `/bmad-code-review` and apply the three-layer review (Blind Hunter / Edge Case Hunter / Acceptance Auditor) with severity tagging (HIGH / MEDIUM / LOW / deferred / dismissed). Same gate semantics as Story 9-0c AC9 / Story 9-0d AC11 / Story 9-1 AC12: **zero unresolved HIGH/MEDIUM permitted before merge**. All deferred findings logged to `_bmad-output/implementation-artifacts/deferred-work.md` under a dated "code review of 9-2-..." heading.

## Tasks / Subtasks

- [x] Task 1 — Schema + repository extensions (AC1, AC2)
  - [x] Subtask 1.1 — Author migration `0010_add_role_to_device_registry.py` adding `device_registry.role` + `device_registry.role_assigned_at` + the partial index, and `wizard_state.step_2_complete` + `wizard_state.step_2_completed_at` + `wizard_state.step_2_acknowledged_gaps`
  - [x] Subtask 1.2 — Extend `DeviceRegistryEntry` with the two new fields + model_validator + extend `DeviceRepo` with `assign_role`
  - [x] Subtask 1.3 — Extend `WizardState` with the three new fields + model_validator (gap-label whitelist) + extend `WizardStateRepo` with `set_step_2_complete` + `record_acknowledged_gaps`
  - [x] Subtask 1.4 — Migration round-trip verification (out-of-band) + `tests/integration/test_migrations.py::test_migration_0010_round_trips`
- [x] Task 2 — `RoleAssignmentService` (AC3, AC5, AC6)
  - [x] Subtask 2.1 — Author `src/open_ems/services/role_assignment.py` with `_VALID_GAP_LABELS`, `RoleConflict`, `RoleGap`, `RoleAssignmentRow`, `RoleAssignmentSnapshot`, `RoleGateOutcome`, and `RoleAssignmentService`
  - [x] Subtask 2.2 — Implement `evaluate_assignments` (single registry read; deterministic ordering; conflict detection; gap detection)
  - [x] Subtask 2.3 — Implement `evaluate_gate` (composes acknowledged_gaps; computes blocking_gaps + unacknowledged_warnings + conflicts; performs the stale-acknowledgment filter)
  - [x] Subtask 2.4 — Unit tests per AC9
- [x] Task 3 — Routes (AC7)
  - [x] Subtask 3.1 — Delete the placeholder GET `/installer/setup/roles` route body and the `setup_roles_placeholder.html` template
  - [x] Subtask 3.2 — Implement `GET /installer/setup/roles` (full page render)
  - [x] Subtask 3.3 — Implement `POST /installer/setup/roles/{device_id}/assign` (validate role value with exact-match rejection; call `DeviceRepo.assign_role`; re-evaluate; render `_setup_roles_list.html`)
  - [x] Subtask 3.4 — Implement `POST /installer/setup/roles/acknowledge-gap` (handles `action=acknowledge` and `action=revoke`; whitelist enforcement at route level with exact-match rejection)
  - [x] Subtask 3.5 — Implement `POST /installer/setup/roles/advance` (write-lock-acquire-then-read pattern; gate evaluation; stale-acknowledgment filter persistence; 302 redirect on pass; banner fragment on fail; idempotence)
  - [x] Subtask 3.6 — Add `GET /installer/setup/constraints` placeholder route + template (Story 9.3 will replace)
- [x] Task 4 — Templates + accessibility (AC4, AC8)
  - [x] Subtask 4.1 — Author `setup_roles.html` body + `_setup_roles_list.html` + `_setup_roles_row.html`
  - [x] Subtask 4.2 — Author `_setup_roles_gap_panel.html` (hard-block notice + acknowledgeable WARN notices with Acknowledge/Revoke buttons)
  - [x] Subtask 4.3 — Author `setup_constraints_placeholder.html`
  - [x] Subtask 4.4 — Update `setup_layout.html` step indicator to render Step 1 as a hyperlink when `step_2_complete=0` (back-navigation)
  - [x] Subtask 4.5 — A11y: `aria-label`, `role="alert"`, `aria-live="polite"`, server-rendered disabled states, keyboard reachability verified manually + via tests
- [x] Task 5 — Lifespan wiring + provider exposure
  - [x] Subtask 5.1 — Construct `RoleAssignmentService(device_repo)` in the lifespan AFTER `DeviceRepo` is constructed; before watchdog/control-loop start (mirrors Story 9.1 step 4d)
  - [x] Subtask 5.2 — Expose via `app.state.role_assignment_service`
  - [x] Subtask 5.3 — Add the `_role_assignment_service` Depends helper in `web/routes/setup.py`
- [x] Task 6 — Tests (AC9)
  - [x] Subtask 6.1 — Unit test files per AC9
  - [x] Subtask 6.2 — Integration test files per AC9
  - [x] Subtask 6.3 — Accessibility (axe-core) test placeholder marked `pytest.mark.xfail` (per AC8)
- [x] Task 7 — Quality gates + review (AC10)
  - [x] Subtask 7.1 — `pytest tests/ --no-cov -q` clean (1228 passed, 2 xfailed)
  - [x] Subtask 7.2 — `mypy src/` clean (no issues found in 90 source files)
  - [x] Subtask 7.3 — `ruff check .` and `ruff format --check .` clean (220 files formatted)
  - [x] Subtask 7.4 — `/bmad-code-review` run 2026-05-11: 10 patch findings applied (2 from resolved decision-needed + 8 patch-direct); 6 deferred to `deferred-work.md`; 13 dismissed as noise/false-positive. Post-patch quality gates clean: pytest 1230 passed (2 xfailed) · mypy clean · ruff check + format clean.

### Review Findings

_Generated 2026-05-11 by `/bmad-code-review` (Blind Hunter + Edge Case Hunter + Acceptance Auditor, three parallel subagents, claude-opus-4-7)._

**Triage summary:** 2 decision-needed (resolved → patch on 2026-05-11) · 10 patch (all applied 2026-05-11, pytest 1230 passed; mypy + ruff clean) · 6 defer · 13 dismissed as noise/false-positive.

<!-- decision-needed → resolved to patch on 2026-05-11; both applied -->
- [x] [Review][Patch] Step 1 gate not enforced on `GET /installer/setup/roles` — added `302 → /installer/setup/discovery` redirect when `step_1_complete == 0`. New positive test `test_get_roles_redirects_to_discovery_when_step_1_incomplete` added. [`src/open_ems/web/routes/setup.py:407-417`]
- [x] [Review][Patch] TOCTOU window between `evaluate_gate` and `set_step_2_complete` — wrapped re-evaluation + write in `async with get_write_lock():`. New `WizardStateRepo.set_step_2_complete_locked` exposes the lock-free variant; the public `set_step_2_complete` delegates to it under the lock. The route re-evaluates the gate inside the held lock and aborts with the failure banner if a concurrent role change flipped the gate. [`src/open_ems/web/routes/setup.py:597-637`, `src/open_ems/storage/repositories/wizard_state_repo.py:set_step_2_complete_locked`]

<!-- patch (unambiguous fix) — all applied 2026-05-11 -->
- [x] [Review][Patch] AC3 violation: `_VALID_GAP_LABELS` consolidated into `open_ems.core.devices` (layer-neutral, next to `DeviceRole`). Both `wizard_state_repo.py` and `services/role_assignment.py` now import from `core.devices`; the runtime drift `assert` is removed. The four readers (route, service, repo, Pydantic model) all resolve to one frozenset object. Existing tests (including `test_valid_gap_labels_consistent_across_modules`) continue to pass.
- [x] [Review][Patch] Module-level `assert` removed; whitelist drift is now structurally impossible (one definition, multiple importers — no drift surface).
- [x] [Review][Patch] Constraints route now fetches `wizard_state` and passes `step_2_complete` to the layout. Back-link is correctly hidden on `/installer/setup/constraints` once Step 2 is finalized. [`src/open_ems/web/routes/setup.py:get_constraints_placeholder`]
- [x] [Review][Patch] Gap-panel warn branch now uses `_WARN_TEXT.get(gap.label, gap.role.value ~ " is missing.")` — symmetric with the acknowledged branch; safe for any future `acknowledgeable_warn` role. [`_setup_roles_gap_panel.html:53`]
- [x] [Review][Patch] `post_assign_role` 404 branch now returns an HTML fragment via `_setup_roles_error.html` with `reason="device_not_found: <id>"` — consistent envelope shape for the htmx-targeted route. [`src/open_ems/web/routes/setup.py:post_assign_role`]
- [x] [Review][Patch] Three substring `in` assertions replaced with exact-match comparisons against `html.unescape(response.text).strip()`. The contract string and its `<div class="error-banner">` wrapper are pinned exactly per Story 9.0c AC7 / 9.1 AC11 precedent. [`tests/unit/web/test_setup_roles_routes.py`]
- [x] [Review][Patch] New negative test `test_assign_role_rejects_naive_assigned_at` mirrors the `test_upsert_manual_rejects_naive_first_seen_at` pattern. [`tests/unit/storage/repositories/test_device_repo.py`]
- [x] [Review][Patch] `_row_to_entry` no longer has the `len(row) > 12` fallback — row shape is fully under our control; missing role columns now fail loud at `IndexError` rather than silently returning `None`. [`src/open_ems/storage/repositories/device_repo.py:_row_to_entry`]

<!-- defer (pre-existing / spec-ratified / out-of-scope) -->
- [x] [Review][Defer] `onchange="this.form.requestSubmit()"` triggers writes on accidental focus/keyboard nav and is unsupported in Safari ≤ 15.4 [`_setup_roles_row.html:43`] — deferred: UX pattern ratified by spec for inline assignment without a confirm button; revisit in a polish pass
- [x] [Review][Defer] `post_acknowledge_gap` read-modify-write race on `step_2_acknowledged_gaps` — two concurrent submits (duplicate tab / double-click) silently overwrite each other; no optimistic-concurrency token [`src/open_ems/web/routes/setup.py:531-553`] — deferred: low practical likelihood with single installer; revisit if multi-installer mode is added
- [x] [Review][Defer] `RoleConflict.device_ids` hash equality depends on `list_all()` ordering; tests use `set(...)` comparison to avoid pinning [`src/open_ems/services/role_assignment.py:137`] — deferred: fragile but not broken today; canonicalize with `tuple(sorted(device_ids))` in a follow-up
- [x] [Review][Defer] `RoleAssignmentService.evaluate_assignments` does a full `list_all()` + pydantic-validate per request; some routes call it twice [`src/open_ems/services/role_assignment.py:122-166`] — deferred: registry bounded to small-N residential install in v1; revisit if multi-site scale arrives
- [x] [Review][Defer] Spec AC3 enumerates `RoleGateOutcome` as 4 fields; implementation has 5 (`effective_acknowledged_gaps` is consumed by routes for AC6 step-4 staleness pruning) [`src/open_ems/services/role_assignment.py:107-115`] — deferred: spec doc drift; code is correct; update AC3 enumeration out-of-band
- [x] [Review][Defer] Three definitions of `device_registry` / `wizard_state` schema (alembic migration + 2 test inlines) [`tests/integration/web/test_setup_roles_e2e.py`, `tests/unit/web/test_setup_roles_routes.py`] — deferred: pre-existing pattern from Story 9.1; not made worse here

## Dev Notes

### Source-tree alignment

- `migrations/versions/0010_add_role_to_device_registry.py` — new; migration style precedent is `0009_add_device_registry_table.py` (single migration adding to two tables together).
- `src/open_ems/storage/repositories/device_repo.py` — extended; the new `assign_role` follows the same `get_write_lock()` / explicit-commit / `cursor.rowcount` pattern as `mark_validated` and `acknowledge_unvalidated`.
- `src/open_ems/storage/repositories/wizard_state_repo.py` — extended; `set_step_2_complete` follows the `set_step_1_complete` pattern; `record_acknowledged_gaps` introduces JSON serialization (use `json.dumps(sorted(list(gaps)))` for deterministic on-disk representation).
- `src/open_ems/services/role_assignment.py` — new; consistent with `services/wizard_gate.py` (frozen dataclass outcomes; pure dependency on `DeviceRepo`; no DB access of its own beyond what the repo offers).
- `src/open_ems/web/routes/setup.py` — extended; the new routes share the same `_device_repo` / `_wizard_state_repo` / `_role_assignment_service` Depends helpers.
- `src/open_ems/web/templates/installer/` — new templates: `setup_roles.html`, `_setup_roles_list.html`, `_setup_roles_row.html`, `_setup_roles_gap_panel.html`, `setup_constraints_placeholder.html`. Delete `setup_roles_placeholder.html`.

### Why role lives on `device_registry` (not a separate join table)

`device_registry` already models the installer-acknowledged device set as a single row per device. A role is a property **of** that device — not a relationship in its own right. A separate `device_role_assignments(device_id, role, assigned_at)` table would introduce:
- A redundant FK indirection for every read (every list_all() becomes a LEFT JOIN).
- A new write-lock surface (the role-assignment service has to coordinate two writes to keep them consistent).
- A drift risk: a device's role could be present in `device_role_assignments` after the parent `device_registry` row was deleted, if FK CASCADE was forgotten.

By contrast, a nullable `role` column on `device_registry`:
- Co-locates the property with its owner (matching Story 9.1's "single source of truth" principle).
- Has zero new write surfaces — `assign_role` is just a column update on a row whose write path is already write-locked.
- Cannot drift: the column is gone when the row is gone, structurally.

This is exactly the same design lesson from Story 9.0b R6 ("source-of-truth ownership per datum"): a datum belongs in one place. The role is a property of the device, so it goes on the device row.

### Why `step_2_acknowledged_gaps` is a JSON column (not a join table)

See AC2 — the value set is bounded (3 elements max), the lifetime is session-scoped (CASCADE already disposes), there are no per-row writes that need their own concurrency, and a separate table would require its own migration + repo class for a value that is always written atomically as a set.

JSON-in-SQLite is fully native (SQLite stores it as TEXT); the encoding is `json.dumps(sorted(list(...)))` for deterministic on-disk representation, decoded by the Pydantic model_validator into a `frozenset[str]`. The whitelist enforcement at the model level + the repo level + the route level is defense-in-depth — any future caller that bypasses one layer is caught by the next.

### Gap label semantics — why `grid_meter_missing` is deliberately not in the whitelist

The grid meter is the most critical device in the system (architecture.md §Degradation matrix). Its absence is a **hard block** at Step 2 — the epic spec is explicit and verbatim: `"Grid meter is required for peak limiting and safety behavior — this cannot be bypassed in v1"`. By **structurally excluding** `'grid_meter_missing'` from `_VALID_GAP_LABELS`, the system cannot enter a state where a missing grid meter is acknowledged-and-bypassed. This is enforced at four layers:
1. The Pydantic model rejects the label.
2. The repository rejects the label.
3. The route handler rejects the label with the exact reason `"gap_label_not_acknowledgeable: grid_meter_missing"`.
4. The gate evaluator treats `severity='hard_block'` as **always** failing, regardless of acknowledgments.

Removing this protection in a future story would require changes at all four layers + an explicit test removal — making the safety property visible in the code's structure, not just in documentation. This is the same lesson from Story 9.0c (capability registry fail-loud gate is a structural guard, not a runtime check).

### Conflict resolution UX rationale

The epic spec says "inline conflict detection runs on each change: duplicate role assignments and incompatible role/device combinations are flagged inline — no modal, no page navigation" (lines 1944–1946). The "incompatible role/device combinations" clause is **out of scope for v1**: a battery device can be assigned the EV charger role in the data model; the system simply enforces role-based behavior in the decision engine. The architecture does not establish a role/device-class compatibility matrix at the persistence layer (and no such matrix exists in the codebase today — see `core/devices.py:DeviceRole`).

This story therefore implements **only the duplicate-role detection** clause of AC4. Future stories may extend `RoleAssignmentService` with role/device-class compatibility checks; the architecture surface (a single evaluator service called from routes, templates, and Step 4 validation) is the right shape for that extension. For now, the test `test_role_assignment_service.py::test_no_role_device_compatibility_check_in_v1` asserts the absence of such a check as a precommit to the v1 scope decision.

### Why role assignment does NOT wire adapters into PolicyGuard

Per `_bmad-output/implementation-artifacts/deferred-work.md` (Story 8-2 entry): `adapters={}` in production silently rejects all commands; `PolicyGuard` + `ControlLoop` are wired with empty adapter maps; the complete fix lands at Story 9.2 + 9.3 (constraint activation gates the loop's runtime adapter set).

**This story does NOT close that gap.** Role assignment is purely a persistence + UI concern in 9.2. Adapter wiring is the responsibility of Story 9.3 (constraint activation) — once constraints are active, the control loop is permitted to invoke device commands, and at that point the adapter map needs to be populated. Wiring adapters at role-assignment time would mean the control loop tries to issue commands against devices with no constraint context — exactly the unsafe state Story 9.3's staged-activation flow exists to prevent.

This rationale is captured in R7 as well — the `adapters={}` deferred-work item is classified `acceptable-post-this-story` (same as Story 9.1's classification).

### Library / framework notes

- No new third-party dependencies. JSON serialization uses the standard library `json` module.
- HTMX usage continues to follow the architecture-mandated server-rendered HTML + HTMX baseline; no client-side JS is required for the conflict-display or acknowledgment paths. The fragment swap targets (`#roles-list-region`, `#gap-panel-region`) are scoped narrowly enough that the "HTMX swap destroys focus" issue tracked in Story 9.1's deferred-work is bounded to the same regions — and the same a11y minor applies. Document in dev notes; do not regress on it.
- `DeviceRole` enum already exists at `src/open_ems/core/devices.py:76-82` — values `inverter`, `battery`, `ev_charger`, `grid_meter`. Story 9.2 uses it directly; no new enum is introduced.

### Previous-story intelligence — what we carry forward

- **Story 9.1 R6 (`get_profile()` re-resolution at render time):** the existing `_templates.env.globals["get_profile"] = get_profile` registration in `setup.py` makes `get_profile` available in the new role-row template too. The capability badge in `_setup_roles_row.html` MUST call `get_profile(row.device_id, row.model or "")` at render time — never read the cached `row.last_capability_status` column for display. Story 9.1's review patch (R6 render-time re-resolution) is a contract; 9.2's templates inherit it.
- **Story 9.1 R3 #1 model-validator pattern:** the `DeviceRegistryEntry` extension for `role` + `role_assigned_at` uses the same `@model_validator(mode="after")` pattern. Reject impossible states **at every read**, not just at write time.
- **Story 9.1 AC11 exact-match reason strings:** all rejection reasons in 9.2's route handlers and repo methods use exact-match assertions in tests. No substring `in` assertions.
- **Story 9.1 AC10 axe-core deferral:** the axe placeholder is `pytest.mark.xfail`, not `pytest.skip`. The AC inventory remains visible.
- **Story 9.1 retry-route registry update lesson (review HIGH patch):** when a route does work that should update persisted state, **do not** rely on a future page refresh to compute the new state. Always write back in the same handler. The `advance` route's stale-acknowledgment filter (AC6 step 4) follows this rule — it writes the filtered set back in the same write-lock window.
- **Story 9.1 retry-route gate recomputation lesson (review HIGH patch):** when a route returns an HTMX fragment that includes gate-dependent UI, recompute the gate after the write. Never reuse a stale gate snapshot. The `assign` and `acknowledge-gap` routes both follow this rule.
- **Story 9.1 OCPP synthesized address:** OCPP rows in `device_registry` currently have `address=f"/{cp_id}"` (deferred-work entry from 9.1). The roles page renders `address` as-is. For OCPP rows, this is the synthesized path — the UI label should be honest about this. **Out of scope for 9.2:** the deferred-work item is classified `acceptable-post-this-story` in R7; do not attempt to fix it here.
- **Story 9.0b lifespan-provider pattern:** `app.state.role_assignment_service` follows the same precedent as `app.state.discovery_orchestrator`.
- **Story 9.0c capability registry fail-loud gate:** `RoleAssignmentService` does NOT call `get_profile()` — capability resolution is purely the rendering layer's concern (the row template). The service operates on `DeviceRegistryEntry.role` only.
- **Story 9.0 (adapter command contracts):** adapter `send_command` is not invoked by this story. PolicyGuard's `adapters={}` situation is unchanged. The 9.0 contract is untouched.

### Project Structure Notes

- All new files map directly onto the architecture-defined directory layout (architecture.md §Project Structure):
  - `services/role_assignment.py` is a permitted addition under `services/` (cross-cutting runtime services; matches the `wizard_gate.py` precedent in Story 9.1).
  - `web/routes/setup.py` is extended (not split); the file remains under the architecture-recommended 300-LOC threshold even after 9.2's additions because the new routes share helpers with the existing ones.
  - `web/templates/installer/` continues to host the wizard's templates.
- No new top-level packages. No new third-party dependencies. `uv.lock` should be unchanged.
- Detected variance: architecture.md §Project Structure (line 909) lists `installer/setup.html` (singular). Story 9.1 already established the multi-page pattern (`setup_layout.html` + `setup_discovery.html` + placeholders). Story 9.2 continues the pattern with `setup_roles.html` + `setup_constraints_placeholder.html`. This is a refinement of the architecture's single-file layout, not a contradiction — multi-page Jinja2 flows with a shared layout are idiomatic, and Story 9.1's retrospective implicitly accepted this variance.

### Orchestration Risk Analysis (A2-triggered — MANDATORY)

**A2 triggers matched:**
- **T1** (lifecycle / state-machine behavior) — per-device role lifecycle (`unassigned → assigned → reassigned → unassigned`); per-session Step-2 wizard state (`step_2_complete=0 → 1`); per-label acknowledgment lifecycle (`unacknowledged → acknowledged → revoked → re-acknowledged`); stale-acknowledgment filter is a non-trivial state transition that mutates persisted state during a gate evaluation.
- **T3** (persistence + recovery) — role assignments and acknowledgments are durably stored in `device_registry` + `wizard_state`; both must survive process restart and page reload; cross-step reads (Step 4 deployment validation will consume `step_2_acknowledged_gaps` as WARN-class check inputs per the epic spec).
- **T6** (deployment / restart behavior, partial) — installer mid-wizard page reload must restore the role-picker state, the acknowledgment state, and the gate state from DB. Cold-start invariants are user-visible.
- **T7** (installer workflow orchestration) — multi-step installer-facing flow that mutates persisted configuration (`device_registry.role`, `wizard_state.step_2_*`) and activates the next wizard step.

#### R1 — Composition-risk analysis

Story 9.2 converges four operational domains in a single shipping unit. Each carries a named, story-specific risk introduced by its convergence with the others:

1. **Persistence + recovery (T3) crossed with installer workflow orchestration (T7).** The role assignment is stored on `device_registry.role`; the per-session acknowledgment is stored on `wizard_state.step_2_acknowledged_gaps`. The two writers (`assign_role` and `record_acknowledged_gaps`) target **different tables** but their values are **read together** by `RoleAssignmentService.evaluate_gate(...)`. Risk: a concurrent write to `device_registry.role` between the gate's registry read and its acknowledgment-filter logic produces an outcome that is internally inconsistent — the filter believes a gap is closed (registry says role assigned) but the route then writes back an acknowledged-gaps set that doesn't include the now-stale label. The hazard surfaces as `step_2_complete=1` written with a `step_2_acknowledged_gaps` that does not match the registry state at any point in real time. Mitigation: the `advance` route acquires `get_write_lock()` BEFORE the registry read, holds it through the gate evaluation, performs the stale-acknowledgment filter, writes both `step_2_complete=1` AND the filtered `step_2_acknowledged_gaps` in the same lock window, then releases. This is the same write-lock-acquire-then-read pattern Story 9.0b's `ConfigRepo.activate()` uses; precedent: Story 9.1 R3 #3.

2. **Lifecycle (T1) crossed with persistence (T3).** Three state machines converge: `DeviceRegistryEntry.role` (per device), `WizardState.step_2_complete` (per session), and `step_2_acknowledged_gaps` (per session, set-valued). Each has independent write paths but they participate in the gate composition. Risk: a future contributor introduces a "clear all role assignments" path (for installer-led re-discovery) and forgets to clear the corresponding `step_2_acknowledged_gaps`; the wizard then reports "all gaps are acknowledged" for gaps that are now newly open. Mitigation: the stale-acknowledgment filter is applied at **every gate evaluation**, not only at advance. Even if the underlying state drifts, the gate evaluator's read-time filter projects the **only** logically consistent view. Filter semantics are locked by the `test_role_assignment_service.py::test_evaluate_gate_filters_stale_acknowledgments` test. Precedent: Story 9.0c (fail-loud at boot is one structural guard; runtime read-time filtering is the other — 9.2 uses the runtime variant because the data is not boot-time).

3. **Installer workflow orchestration (T7) crossed with restart (T6).** The wizard surface must survive process restart: an installer who reloads `/installer/setup/roles` after a restart must see the same role pickers populated from `device_registry.role` and the same gap-acknowledgment state from `wizard_state.step_2_acknowledged_gaps`. Risk: process restart mid-`advance` lands the system in a state where `device_registry.role` is fully assigned but `wizard_state.step_2_complete=0` (the second write didn't land). Mitigation: this is a **safe partial-state** — the next `advance` call will succeed identically. The write-lock keeps the two writes atomic from a concurrent-reader perspective, but `aiosqlite` does not provide cross-statement transactions in our current write-lock pattern; the safety property here is that a `step_2_complete=1` write is the **last** write inside the lock, so partial state is always "complete role state + incomplete advance state" (which is exactly the state an interrupted user-mid-wizard would naturally be in). The user simply re-clicks Continue. Test: `test_setup_roles_e2e.py::test_advance_partial_state_recovery` simulates a process restart between role-assignment and advance and verifies the gate state is recoverable.

4. **Multiple HTMX fragment swap targets (T7).** The roles page has two distinct fragment-swap targets: `#roles-list-region` (assign route) and `#gap-panel-region` (acknowledge-gap route). Risk: an assign-route swap clears the gap panel's state (because the gap panel is OUTSIDE `#roles-list-region`), but a stale gap panel — rendered against the pre-assignment registry — remains on the page until the next interaction. Mitigation: the assign route ALSO re-evaluates and returns the gap panel fragment as a sibling element via `hx-swap-oob="innerHTML:#gap-panel-region"` (HTMX out-of-band swap). The fragment template is shared, so both routes produce identical panel HTML. Test: `test_setup_roles_routes.py::test_assign_route_returns_oob_gap_panel`.

#### R2 — State-transition table

Story 9.2 introduces three state machines: `DeviceRegistryEntry.role` (per row), `WizardState.step_2_complete` (per session), and `WizardState.step_2_acknowledged_gaps` (per session, set-valued).

**`DeviceRegistryEntry.role` (per row):**

| State | Allowed transitions | Trigger | Side-effects |
|---|---|---|---|
| `role=None, role_assigned_at=None` (initial — from Story 9.1's manual-entry path) | → `role=<DeviceRole>, role_assigned_at=now()` | `POST /installer/setup/roles/{device_id}/assign` with non-empty role | structlog `role_assigned`; HTMX fragment swap |
| `role=<X>, role_assigned_at=<t>` | → `role=<Y>, role_assigned_at=now()` (Y ≠ X) | same route, different role value | structlog `role_reassigned` with old/new pair |
| `role=<X>, role_assigned_at=<t>` | → `role=None, role_assigned_at=None` | same route, empty role value | structlog `role_unassigned` |
| `role=None, role_assigned_at=None` | (no transition without an explicit POST) | N/A | N/A — the registry row deletion is not implemented in 9.2 (deferred from 9.1) |

**Critical invariant:** `role` and `role_assigned_at` MUST be both NULL or both non-NULL. Enforced by the model_validator (Pydantic) AND the route handler (always set both together) AND the repo method `assign_role` (atomic UPDATE with both columns).

**`WizardState.step_2_complete` (per session):**

| State | Allowed transitions | Trigger | Side-effects |
|---|---|---|---|
| `step_2_complete=0` (initial — from Story 9.1's `get_or_create`) | → `step_2_complete=1, step_2_completed_at=now()` | `POST /installer/setup/roles/advance` with passing gate | 302 to `/installer/setup/constraints`; structlog `step_2_completed` with the final `step_2_acknowledged_gaps` |
| `step_2_complete=0` | → `step_2_complete=0` (unchanged) | `POST /installer/setup/roles/advance` with failing gate | inline error banner; no DB write |
| `step_2_complete=1` | → `step_2_complete=1` (unchanged, idempotent) | `POST /installer/setup/roles/advance` with passing gate (re-click) | 302 to `/installer/setup/constraints`; `step_2_completed_at` NOT updated (preserves original timestamp) |
| any | → row deleted | `sessions` row deleted (session cleanup from Story 2-5) | CASCADE removes `wizard_state` row |

**`WizardState.step_2_acknowledged_gaps` (per session, set-valued):**

| State | Allowed transitions | Trigger | Side-effects |
|---|---|---|---|
| `step_2_acknowledged_gaps=frozenset()` (initial) | → `frozenset({label})` | `POST /installer/setup/roles/acknowledge-gap` with `action=acknowledge&gap_label=<label>` | gap panel re-render |
| `step_2_acknowledged_gaps=frozenset({...})` | → `frozenset({...} ∪ {label})` | same route, new label | gap panel re-render |
| `step_2_acknowledged_gaps=frozenset({label, ...})` | → `frozenset({...} - {label})` | same route, `action=revoke&gap_label=<label>` | gap panel re-render; gate re-evaluates; if gap is still open and was the last blocker → Continue button re-renders as disabled |
| `step_2_acknowledged_gaps=frozenset({...})` | → `frozenset({...} ∩ {currently-open-gap-labels})` | `POST /installer/setup/roles/advance` — stale-acknowledgment filter applied | `step_2_acknowledged_gaps` overwritten with the filtered set in the same write-lock window |

**Critical invariant:** every element of `step_2_acknowledged_gaps` MUST be in `_VALID_GAP_LABELS`. Enforced at Pydantic + repo + route layers (defense-in-depth — see AC2 / AC3 / AC5).

#### R3 — Impossible-state analysis (with cold-start coverage)

**Forbidden state combinations and their structural invariants:**

1. **`DeviceRegistryEntry.role is not None AND role_assigned_at is None`** (or vice versa) — invariant: `@model_validator(mode="after")` on `DeviceRegistryEntry` raises `ValueError("role and role_assigned_at must both be set or both be NULL")` at every read (including DB round-trip and route-side construction). Test: `test_device_repo.py::test_role_requires_assigned_at`.

2. **`'grid_meter_missing'` present in `step_2_acknowledged_gaps`** — invariant: the label is structurally excluded from `_VALID_GAP_LABELS`. Pydantic model validator + repo whitelist + route whitelist all reject it. Test: `test_role_assignment_gap_labels.py::test_grid_meter_missing_is_not_acknowledgeable`.

3. **`step_2_complete=1` with an unresolved hard-block gap** (i.e., no grid_meter assigned) — invariant: the `advance` route's gate evaluation is the only path that writes `step_2_complete=1`, and it always fails on `severity='hard_block'`. The same write-lock-acquire-then-read pattern from Story 9.1 R3 #3 prevents a race where a `device_registry.role` write between the gate check and the advance write would create this state. Test: `test_setup_roles_routes.py::test_advance_fails_when_grid_meter_unassigned`.

4. **`step_2_complete=1` with two devices assigned the same role** — invariant: same as #3 — the gate fails on `conflicts` regardless of acknowledgments. Conflicts cannot be acknowledged away — the only resolution is to change one of the colliding rows. Test: `test_setup_roles_routes.py::test_advance_fails_on_role_conflict`.

5. **`step_2_acknowledged_gaps` contains a label for a gap that is closed** (e.g., `'battery_missing'` is acknowledged but a battery is assigned) **persisting beyond an advance call** — invariant: the advance route's stale-acknowledgment filter overwrites the persisted set with the projection-onto-currently-open-gaps. A reader after a passing advance sees only acknowledgments for **currently** open gaps. This is read-time consistency, not write-time consistency — between an acknowledge-then-assign sequence and the next advance, the persisted set is allowed to contain stale labels; the filter applies before the advance commits. Test: `test_setup_roles_routes.py::test_advance_filters_stale_acknowledgments_during_persistence`.

6. **Role value bypassing the whitelist** (e.g., HTTP POST with `role=banana`) — invariant: the route handler validates against `DeviceRole.__members__` before calling the repo. Rejected with exact-match reason `"role_invalid: 'banana'"` and a 400. Test: `test_setup_roles_routes.py::test_assign_rejects_invalid_role_value_with_exact_match`.

7. **Gap label value bypassing the whitelist** (e.g., HTTP POST with `gap_label=delete_everything`) — invariant: the route handler validates against `_VALID_GAP_LABELS` before calling the repo. Rejected with exact-match reason `"gap_label_invalid: 'delete_everything'"` and a 400. Test: `test_setup_roles_routes.py::test_acknowledge_rejects_invalid_label_with_exact_match`.

**Cold-start / startup-grace coverage (mandatory):**

Story 9.2 introduces no runtime state that affects the control loop. The cold-start hazards specific to 9.2 are:

1. **t=0 (process boot, fresh deployment):** `device_registry` is empty (a fresh deployment has no devices yet). The installer logs in and may attempt to navigate directly to `/installer/setup/roles`. The roles page renders with **zero device rows** and **all four gaps open** (hard_block for grid_meter, acknowledgeable_warn for the other three). The Continue button is disabled. The installer is implicitly directed to Step 1 first. (Story 9.1's `get_or_create` already creates the `wizard_state` row lazily; 9.2 inherits this — no additional bootstrap needed.)

2. **t=process restart with prior wizard activity:** `device_registry` rows survive (along with any `role` + `role_assigned_at` set). `wizard_state` rows MAY survive (if the parent session has not expired). On the next GET of `/installer/setup/roles`, the role pickers are populated from the persisted `device_registry.role` column; the gap panel reads from the persisted `step_2_acknowledged_gaps`. The installer sees the same UI state as before restart. **There is no `RoleAssignmentService.hydrate(...)` step at boot** — the service is stateless; every gate evaluation reads from the DB. (Contrast: Story 9.0d hydrated monthly peak into in-memory state because the control loop needs synchronous access; 9.2 has no such constraint.)

3. **t=concurrent installer sessions:** two installer browsers open `/installer/setup/roles` simultaneously. Each has its own `wizard_state` row (keyed by `session_id`) and therefore its own `step_2_acknowledged_gaps`. The shared `device_registry.role` is the single source of truth for both. If installer A assigns a role, installer B sees it on the next page render (HTMX polling on the roles list — same `hx-trigger="every 5s"` pattern as Story 9.1's known-configured section, scoped to the list region only). Concurrent `step_2_complete=1` from both sessions is harmless — each advances their own session independently.

4. **t=session expired mid-wizard:** the installer's session expires before they complete Step 2. The next request requires re-login; after login, a new `wizard_state` row is lazily created. The `device_registry.role` persists. The installer's `step_2_acknowledged_gaps` is **lost** (it was on the prior session's row, deleted via CASCADE when the session was cleaned up). The installer must re-acknowledge the same gaps. This is **intended behaviour** — acknowledgments are a per-session "I considered this" signal. Test: `test_setup_roles_e2e.py::test_session_expiry_loses_acknowledgments_preserves_role_assignments`.

5. **t=Story 9.3 has not yet shipped:** if the installer somehow reaches the `/installer/setup/constraints` placeholder, they see a "Step 3 will be implemented in Story 9.3" stub page. No state is mutated. This is identical to Story 9.1's `/installer/setup/roles` placeholder pattern.

**Marker for normal operation:** the first 200 response from `GET /installer/setup/roles` after process boot signals the roles surface is live. There is no separate readiness probe — it inherits the global `/health/ready` from Story 1-3.

#### R4 — Cancellation ownership map

Story 9.2 introduces no novel async cancellation paths. All state-mutating routes are synchronous from the perspective of the route handler (a single `await get_write_lock()`, a single DB write, a return). However, every route is still cancellable at the route-handler level (FastAPI cancels the task on client disconnect):

| Async operation | `CancelledError` owner | Cancellation audit owner | Cleanup owner | Reporting field semantics under cancel |
|---|---|---|---|---|
| `POST /installer/setup/roles/{device_id}/assign` | route handler task | none — assignment is observable via the absence of a `role_assigned_at` update | `get_write_lock()` is released when the route task is cancelled (the `async with` exit handler runs even on `CancelledError`); no partial writes possible because the UPDATE is a single statement | row state unchanged if cancelled before the UPDATE lands; row state fully updated if cancelled after the UPDATE returns from `aiosqlite` (atomic) |
| `POST /installer/setup/roles/acknowledge-gap` | route handler task | none — observable via the column value | same as above; the JSON-encoded list is a single column write | row state unchanged if cancelled mid-write |
| `POST /installer/setup/roles/advance` | route handler task | none — the structlog `step_2_completed` event is emitted **after** the DB write succeeds, so cancellation before that point leaves no audit; cancellation after the DB write but before structlog emit is observable in the DB but not in the structlog — acceptable (the audit is best-effort, the DB is authoritative) | the gate evaluation + stale-acknowledgment filter + `step_2_complete=1` write happen inside a single `async with get_write_lock():` block; cancellation between gate-eval and write leaves `step_2_complete=0` (safe state); cancellation between write and response leaves `step_2_complete=1` (acceptable — the installer's next click will redirect to constraints anyway) | the response is not delivered if cancelled; the installer's next GET runs the gate again or sees the now-complete state |
| `GET /installer/setup/roles` | route handler task | N/A | read-only path; cancellation has no side effects | response not delivered |

**Note on the absence of `RetryPolicy`-style async retries:** Story 9.2 does not invoke any device adapters and therefore introduces no adapter-cancellation surface. The cancellation map is bounded to HTTP request cancellation only — there are no detached `asyncio.create_task(...)` paths added by this story (contrast with Story 9.1's optional detached-probe pattern, which was inline in the dev's chosen implementation).

#### R5 — Before-first-successful-cycle lifecycle review

Story 9.2 does not introduce any new gating step in the lifespan startup sequence, and it does not gate the control loop. The lifespan additions are purely construction:

| Phase | Event | What is published / logged / DB state | What relaxes |
|---|---|---|---|
| Boot | `uvicorn` starts; lifespan generator entered | unchanged from prior stories | — |
| Lifespan step 1–4d | Logging, clock, migrations, DB init, capability registry, active-constraints hydrate, monthly-peak hydrate, **Story 9.1 wizard services** | unchanged | — |
| **Lifespan step 4e (NEW)** | Construct `RoleAssignmentService(device_repo)` | structlog `role_assignment_service_ready` at info | none — stateless service object |
| Lifespan step 5+ | `mark_ready()`, watchdog, control loop | unchanged | — |
| First wizard request to `/installer/setup/roles` | route fires | DB read of `device_registry` + `wizard_state`; no writes unless the route is a POST | none — post-readiness |

**Critical:** lifespan step 4e MUST NOT call `await device_repo.list_all()` or `await role_assignment_service.evaluate_assignments()` at startup. The service is constructed (object instantiation only); the first DB read happens on the first installer request, post-readiness.

**Between lifespan completion and the first installer interaction with Step 2:** the control loop is ticking normally; PolicyGuard rejects every command with `adapter_not_registered` (deferred from Story 8-2; unchanged by 9.2 — see R7 and the Dev Notes "Why role assignment does NOT wire adapters" section). The system is functional in a "devices known, roles assigned, but constraints not yet active" state. This is the documented post-Story-9.2-completion baseline that Story 9.3 will close.

**Marker for "Step 2 wizard surface is operating normally":** the first 200 response from `GET /installer/setup/roles`. There is no separate readiness probe.

#### R6 — Source-of-truth ownership per datum

| Datum | Single owner | Read path | Drift risk (if any) |
|---|---|---|---|
| Device role (`device_registry.role`) | `device_registry.role` column | `DeviceRepo.list_all()` / `get_by_device_id()` | None — single DB-side source; the parallel `core/devices.py:DeviceRole` enum is the **type** of valid values, not a parallel persistence |
| Role-assigned timestamp (`device_registry.role_assigned_at`) | `device_registry.role_assigned_at` column | same | Bound to `role` via model_validator (R3 #1); cannot drift |
| Step 2 completion flag (`wizard_state.step_2_complete`) | `wizard_state.step_2_complete` column | `WizardStateRepo.get(session_id)` | None — single owner |
| Per-session acknowledged gaps (`wizard_state.step_2_acknowledged_gaps`) | `wizard_state.step_2_acknowledged_gaps` column (JSON-encoded) | `WizardStateRepo.get(session_id).step_2_acknowledged_gaps` (decoded to `frozenset[str]`) | **Bounded drift:** between an acknowledge-then-assign sequence and the next advance, the persisted set may contain labels for closed gaps. This is allowed by design; the advance route's stale-acknowledgment filter eliminates the drift before any user-visible advance. The gate evaluator's read-time filter ensures **no consumer ever sees a logically inconsistent view**. Documented in AC6 step 4 + R3 #5 |
| Valid gap label set (`_VALID_GAP_LABELS`) | `services/role_assignment.py:_VALID_GAP_LABELS` (module constant) | direct import from four sites (Pydantic model, repo, route, service) | None — single module-level constant; the four consumers all import the same name |
| Valid role enum (`DeviceRole`) | `core/devices.py:DeviceRole` | direct import | None — single enum; existed before 9.2 |
| Role-assignment conflict set (per evaluation) | `RoleAssignmentSnapshot.conflicts` (ephemeral) | returned by `evaluate_assignments()` | None — pure function of the registry read; never persisted |
| Role-gap set (per evaluation) | `RoleAssignmentSnapshot.gaps` (ephemeral) | returned by `evaluate_assignments()` | None — pure function of the registry read; never persisted |

**Drift risks called out explicitly:**
- **`step_2_acknowledged_gaps` vs. currently-open gaps:** documented above. The drift is bounded by the read-time filter and resolved at every advance. Test: `test_evaluate_gate_filters_stale_acknowledgments`.
- **`DeviceRegistryEntry.role` (Python enum value) vs. `device_registry.role` (DB column value):** the DB column stores the enum's string value (`'inverter'`, etc.). The Pydantic model uses the `DeviceRole` enum. The boundary is in `_row_to_entry`: `role=DeviceRole(row[12])` (with a CHECK constraint at the DB level rejecting non-enum values). If the enum is ever renamed in `core/devices.py`, this is a **breaking change** for any existing DB; the CHECK constraint would still permit the old value, but `DeviceRole(row[12])` would raise. Mitigation: enum string values are part of the public API and treated as immutable; Story 9.0c's same lesson for capability profile labels applies. Add a `tests/integration/test_migrations.py::test_role_enum_values_stable_across_migrations` test that enumerates the expected strings and fails loud if anyone renames them.

#### R7 — Deferred-findings triage

Scanned `_bmad-output/implementation-artifacts/deferred-work.md` (312 lines); items whose component or invariant overlaps with this story:

| Finding (bracket ref) | Classification | Rationale |
|---|---|---|
| `[src/open_ems/web/app.py:223-236]` (Story 8-2) "`adapters={}` in production silently rejects all commands" | **acceptable-post-this-story** | 9.2 is role-assignment persistence + UI only. Wiring adapters into PolicyGuard is the responsibility of Story 9.3 (constraint activation). Closing this gap at 9.2 would require constraints-active semantics that don't exist yet — exactly the wrong scope/sequencing decision. Same classification as Story 9.1's triage. |
| `[src/open_ems/adapters/ocpp/central_system.py:115-122]` (Story 9-1) "`OCPPRegisteredCharger.address` is synthesized `f"/{cp_id}"`" | **acceptable-post-this-story** | The roles page renders `address` as-is for the row label. For OCPP rows, the synthesized path is what the installer sees. The real WS mount path is required for proper UI labeling, but 9.2's surface is unchanged by the synthesis hazard — the role picker doesn't depend on the address. Fix lands when OCPP wiring is added (deferred-work entry tracks Story 9.2/9.3 as the boundary; 9.2 doesn't wire OCPP, so this stays deferred). |
| `[src/open_ems/web/app.py:311]` (Story 9-1) "OCPP `central_system=None` in lifespan; OCPP registry rows would NOT FOUND on every scan" | **acceptable-post-this-story** | Bounded today by the absence of OCPP rows in `device_registry`. 9.2 doesn't change this — the role-assignment surface accepts whatever rows exist. Fix lands with OCPP wiring. |
| `[src/open_ems/storage/repositories/device_repo.py:136-267]` (Story 9-1) "`DeviceRepo` does not use `BEGIN IMMEDIATE`" | **safe-during-this-story** | 9.2's `assign_role` follows the same single-statement `UPDATE` pattern as Story 9.1's `mark_validated` / `update_last_seen` / `acknowledge_unvalidated`. The aiosqlite autocommit model handles single-statement DML correctly. The visual-inconsistency-with-9.0b concern is inherited unchanged; 9.2 does not aggravate it. |
| `[src/open_ems/web/routes/setup.py:146]` (Story 9-1) "`set_last_scan_id` race with admin-initiated session deletion → 500" | **safe-during-this-story** | 9.2 introduces analogous race surfaces (`set_step_2_complete`, `record_acknowledged_gaps` could race with a concurrent session-cleanup). The current behaviour — uncaught `ValueError` → 500 — matches the 9.1 precedent and is documented as deferred. Hardening the entire setup router for this race is out of 9.2 scope; the 9.1 deferral covers it. |
| `[src/open_ems/services/device_discovery.py:313-336]` (Story 9-1) "`_classify_not_found` reads the registry after probes complete" | **safe-during-this-story** | 9.2 does not invoke the discovery orchestrator; this hazard is unaffected. |
| `[src/open_ems/services/device_discovery.py:202-205]` (Story 9-1) "`DiscoveryService` unbounded fan-out for large brownfield sites" | **safe-during-this-story** | 9.2 has no scan path. |
| `[src/open_ems/web/templates/installer/_scan_results.html:2031-2039]` (Story 9-1) "HTMX swap of `#scan-results-region` destroys forms inside it" | **safe-during-this-story** | 9.2 introduces the same a11y-minor for `#roles-list-region` (the assign-route fragment swap destroys the gap panel's siblings if not done out-of-band; mitigated by the OOB swap in R1 #4). The narrow-region swap pattern is documented; the broader a11y polish is the same future UX sweep. |
| `[src/open_ems/web/csrf.py:71-79]` (Story 9-1) "CSRF middleware exhausts the request body" | **safe-during-this-story** | 9.2's new POST routes use the same CSRF middleware path as 9.1. Same pre-existing pattern. |
| `[tests/integration/web/test_setup_discovery_e2e.py:2754-2769]` (Story 9-1) "Cancellation e2e test exercises only one target" | **safe-during-this-story** | 9.2 does not introduce multi-target async paths. The cancellation surface is purely HTTP request cancellation (R4), tested separately. |
| `[src/open_ems/web/csrf.py]`, `[src/open_ems/storage/repositories/user_repo.py, session_repo.py]` (Story 9-0b) "UserRepo and SessionRepo writes not yet routed through the write lock" | **safe-during-this-story** | 9.2's `wizard_state` writes use `get_write_lock()`. The pre-existing UserRepo/SessionRepo gap is unrelated to 9.2's surface. |
| `[src/open_ems/services/watchdog.py:60-69]` (Story 8-4) "`observability.audit` slow DB write blocks watchdog heartbeat" | **safe-during-this-story** | 9.2 emits structlog events only (no `event_log` rows — see audit semantics note below); watchdog interaction is unchanged. |
| `[src/open_ems/adapters/discovery.py:87-101, 187-188]` (Story 9-1) "Unshielded `adapter.close()` in `finally`" | **safe-during-this-story** | 9.2 does not invoke the discovery adapter paths. |

**No findings classified as `must-resolve-in-this-story`.** The `adapters={}` deferred concern (Story 8-2) is the most prominent overlap with Epic 9, but its resolution belongs to Story 9.3 — exactly as the deferred-work entry's text already states ("complete fix lands at Story 9.2 + 9.3 (constraint activation gates the loop's runtime adapter set)"). The boundary is honoured.

### Audit semantics

This story emits NO `event_log` audit rows. Role assignment and gap acknowledgment are **configuration flow** events — operator-observable via the structlog stream (`role_assigned`, `role_reassigned`, `role_unassigned`, `gap_acknowledged`, `gap_revoked`, `step_2_completed`) but **not** part of the FR20 event log surface. That surface is reserved for control decisions, constraint enforcement, degraded-mode transitions, recovery, and installer notes. Audit emission for wizard configuration commits begins in Story 9.3 (constraint activation → `config_audit_log`). Same precedent as Story 9.1.

### References

- [Source: _bmad-output/planning-artifacts/architecture.md#Project-Structure] — `services/`, `web/templates/installer/`, `storage/repositories/device_repo.py` placement
- [Source: _bmad-output/planning-artifacts/architecture.md#Naming-Conventions] — snake_case columns, hyphen-separated URLs
- [Source: _bmad-output/planning-artifacts/architecture.md#BAD-4] — `DeploymentValidationService` precedent: role assignment completeness is a Step-4 input
- [Source: _bmad-output/planning-artifacts/architecture.md#Role-Enforcement-Pattern] — route-level role authorization via `Depends(require_installer)`; not duplicated in business logic
- [Source: _bmad-output/planning-artifacts/architecture.md#Degradation-Matrix] — grid meter is the most critical device; loss removes peak safety guarantee — supports the hard-block decision
- [Source: _bmad-output/planning-artifacts/epics.md#Story-9.2] — story spec lines 1935–1961
- [Source: _bmad-output/planning-artifacts/epics.md#Story-9.4] — Step-4 validation reads acknowledged gaps as WARN-class inputs (forward-compatibility consumer)
- [Source: _bmad-output/planning-artifacts/epics.md#Epic-9-Cross-story-constraints] — lines 2035–2043; "Grid meter absence is a hard block at Step 2 — no bypass exists for v1"
- [Source: _bmad-output/planning-artifacts/ux-design-specification.md#Component-7-Role-Assignment-Row] — UX component anatomy: device name · role selector · capability badge · conflict indicator
- [Source: _bmad-output/planning-artifacts/ux-design-specification.md#Journey-Flow-1] — line 697: "All required roles assigned?" → "Role gap warning inline — no modal"
- [Source: _bmad-output/planning-artifacts/ux-design-specification.md#Component-14-Setup-Wizard-Step-Indicator] — persistent step indicator (inherited from Story 9.1)
- [Source: _bmad-output/planning-artifacts/ux-design-specification.md#Accessibility-Considerations] — WCAG 2.1 AA, ≥4.5:1 contrast, keyboard navigation
- [Source: src/open_ems/core/devices.py:76-82] — `DeviceRole` enum used as the canonical role type
- [Source: src/open_ems/storage/repositories/device_repo.py] — `DeviceRegistryEntry`, `DeviceRepo`, write-lock pattern, model_validator precedent (R3 #1 from Story 9.1)
- [Source: src/open_ems/storage/repositories/wizard_state_repo.py] — `WizardState`, `WizardStateRepo`, `set_step_1_complete` precedent
- [Source: src/open_ems/services/wizard_gate.py] — `WizardGateService` shape precedent (frozen dataclass outcome; depends on a single repo)
- [Source: src/open_ems/web/routes/setup.py] — existing setup router; 9.2 extends it; `get_profile` Jinja global from Story 9.1 R6
- [Source: src/open_ems/web/templates/installer/setup_layout.html] — persistent step indicator; 9.2 adds back-navigation hyperlink on step 1
- [Source: src/open_ems/web/dependencies.py] — `require_installer`, `_resolve_session`
- [Source: src/open_ems/web/csrf.py] — CSRF middleware applied to all state-changing routes
- [Source: src/open_ems/storage/database.py] — `get_connection()` / `get_write_lock()` contract
- [Source: _bmad-output/implementation-artifacts/9-1-implement-device-discovery-step-with-live-probing-capability-classification-and-manual-entry.md] — direct prior story; A1 R1–R7 precedents inherited; gap-label-whitelist design pattern; structural one-way guards
- [Source: _bmad-output/implementation-artifacts/9-0c-capability-profile-failure-semantics.md] — fail-loud structural guard precedent (mirrored by 9.2's defense-in-depth whitelist)
- [Source: _bmad-output/implementation-artifacts/9-0b-db-backed-active-constraints-reload.md] — write-lock + lifespan-provider pattern
- [Source: _bmad-output/implementation-artifacts/epic-8-retro-2026-05-10.md] — A1/A2 enforcement contract origin (memory key `project_a1_a2_codification`)
- [Source: _bmad-output/implementation-artifacts/deferred-work.md] — full deferred-findings list scanned for R7

## Dev Agent Record

### Agent Model Used

claude-opus-4-7

### Debug Log References

- Migration round-trip verified via `tests/integration/test_migrations.py::test_migration_0010_round_trips`.
- `_VALID_GAP_LABELS` consistency between `services.role_assignment` and `storage.repositories.wizard_state_repo` enforced both at import time (assert in the service module) and by `tests/unit/services/test_role_assignment_gap_labels.py::test_valid_gap_labels_consistent_across_modules`.
- CSRF middleware exhausts the request body on form submissions (Story 9.1 deferred-work entry); the new route tests use the `X-CSRF-Token` header pattern to avoid the body-exhaustion path. Same workaround as Story 9.1's `test_setup_routes.py`.

### Completion Notes List

- AC1 — Migration 0010 adds `role` + `role_assigned_at` (with paired CHECK and partial index) to `device_registry` and `step_2_complete` + `step_2_completed_at` + `step_2_acknowledged_gaps` to `wizard_state`. Downgrade reverses cleanly; round-trip tested.
- AC2 — `WizardState` Pydantic model carries `step_2_complete`, `step_2_completed_at`, `step_2_acknowledged_gaps: frozenset[str]`. Whitelist enforced at Pydantic + repo + route layers (defense-in-depth per R3 #2).
- AC3 — `RoleAssignmentService` is the single evaluator: `evaluate_assignments()` (registry snapshot) + `evaluate_gate(acknowledged_gaps)` (composed outcome with stale-acknowledgment filter).
- AC4 — Inline conflict detection: assign route returns the full list region with `hx-swap-oob` gap panel so both regions refresh atomically; conflict indicators carry peer device_ids via `role="alert"`.
- AC5 — Grid meter is hard-blocked verbatim; `grid_meter_missing` is structurally excluded from `_VALID_GAP_LABELS` at four layers. WARN messages match the spec text exactly.
- AC6 — `/advance` re-reads under the write lock, runs the gate, filters stale acknowledgments, and atomically writes `step_2_complete=1` + `step_2_completed_at` (idempotent — preserved on retry) + the filtered ack-set. Pass → 302 to `/installer/setup/constraints`; fail → 400 + banner.
- AC7 — Routes: GET `/installer/setup/roles`, POST `/{device_id}/assign`, POST `/acknowledge-gap` (acknowledge OR revoke), POST `/advance`, GET `/installer/setup/constraints` (placeholder). All `Depends(require_installer)`; all POSTs CSRF-protected.
- AC8 — Templates use `aria-label="Role for {device_id}"`, `role="alert"` for conflict indicators, `aria-live="polite"` for the hard-block notice, server-rendered disabled state for the Continue button. Back-navigation hyperlink added to `setup_layout.html` step 1 when `step_2_complete=0`. Axe-core placeholder retained as `pytest.mark.xfail`.
- AC9 — Tests:
  - `tests/unit/storage/repositories/test_device_repo.py` extended (assign_role round-trip, paired-invariant, DB CHECK rejection cases).
  - `tests/unit/storage/repositories/test_wizard_state_repo.py` extended (gap whitelist + idempotent set_step_2_complete).
  - `tests/unit/services/test_role_assignment_service.py` (new).
  - `tests/unit/services/test_role_assignment_gap_labels.py` (new — anti-regression on `grid_meter_missing` exclusion).
  - `tests/unit/web/test_setup_roles_routes.py` (new — auth/CSRF + every assign / acknowledge / revoke / advance contract).
  - `tests/integration/web/test_setup_roles_e2e.py` (new — happy/mixed/persistence/conflict-resolve/session-expiry/partial-state-recovery flows).
  - `tests/integration/web/test_setup_roles_a11y.py` (axe placeholder xfail).
  - `tests/integration/test_migrations.py` extended (0010 round-trip + role enum stability + schema-shape assertions).
  - `tests/integration/web/test_app_startup.py` extended (lifespan wires `role_assignment_service`).
- Quality gates: `pytest -q` → **1228 passed, 2 xfailed**; `mypy src/` → **clean**; `ruff check .` and `ruff format --check .` → **clean**.
- AC10 — Adversarial 3-layer review (`/bmad-code-review`) is the remaining gate before `review → done`. Subtask 7.4 deliberately left unchecked; per Story 9.1's retro pattern the review runs with a fresh LLM context so the implementer doesn't grade their own work.

### File List

**Migrations (new):**
- `migrations/versions/0010_add_role_to_device_registry.py`

**Source (modified):**
- `src/open_ems/storage/repositories/device_repo.py`
- `src/open_ems/storage/repositories/wizard_state_repo.py`
- `src/open_ems/web/routes/setup.py`
- `src/open_ems/web/app.py`

**Source (new):**
- `src/open_ems/services/role_assignment.py`

**Templates (new):**
- `src/open_ems/web/templates/installer/setup_roles.html`
- `src/open_ems/web/templates/installer/_setup_roles_list.html`
- `src/open_ems/web/templates/installer/_setup_roles_row.html`
- `src/open_ems/web/templates/installer/_setup_roles_gap_panel.html`
- `src/open_ems/web/templates/installer/_setup_roles_advance.html`
- `src/open_ems/web/templates/installer/_setup_roles_error.html`
- `src/open_ems/web/templates/installer/_setup_roles_error_banner.html`
- `src/open_ems/web/templates/installer/setup_constraints_placeholder.html`

**Templates (modified):**
- `src/open_ems/web/templates/installer/setup_layout.html`

**Templates (deleted):**
- `src/open_ems/web/templates/installer/setup_roles_placeholder.html`

**Tests (new):**
- `tests/unit/services/test_role_assignment_service.py`
- `tests/unit/services/test_role_assignment_gap_labels.py`
- `tests/unit/web/test_setup_roles_routes.py`
- `tests/integration/web/test_setup_roles_e2e.py`
- `tests/integration/web/test_setup_roles_a11y.py`

**Tests (modified):**
- `tests/unit/storage/repositories/test_device_repo.py`
- `tests/unit/storage/repositories/test_wizard_state_repo.py`
- `tests/unit/web/test_setup_routes.py`
- `tests/unit/services/test_wizard_gate.py`
- `tests/unit/services/test_manual_entry_probe.py`
- `tests/integration/test_migrations.py`
- `tests/integration/web/test_setup_discovery_e2e.py`
- `tests/integration/web/test_app_startup.py`

**Sprint tracking (modified):**
- `_bmad-output/implementation-artifacts/sprint-status.yaml`

### Change Log

| Date | Change | Author |
|---|---|---|
| 2026-05-11 | Story 9.2 implemented: role assignment + inline conflict detection + gap acknowledgment; status → review (pre-AC10 review gate) | dev-story (claude-opus-4-7) |
| 2026-05-11 | `/bmad-code-review` complete (Blind Hunter + Edge Case Hunter + Acceptance Auditor). 10 patches applied (incl. AC3 whitelist consolidation into `core.devices`, Step-1 gate on `/roles`, TOCTOU close via `set_step_2_complete_locked`, gap-panel `.get` symmetry, constraints route `step_2_complete` context, 404 HTML envelope, three exact-match test conversions, naive-datetime test, `_row_to_entry` fail-loud); 6 findings deferred; 13 dismissed. Quality gates clean (1230 passed; mypy + ruff clean). Status → done. | bmad-code-review (claude-opus-4-7) |
