# Story 9.3: Implement constraint configuration with staged validation and single-increment config_version

Status: in-progress

_Round 2 code review (2026-05-11) applied 14 focused patches in this session; 6 substantive findings carried over to dedicated sub-stories (see Round 2 Patches section). All 1300 unit + integration tests pass; mypy + ruff clean. Status reverts from `done` to `in-progress` per bmad-code-review workflow because patch findings remain as deferred sub-stories._

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

> **A2-triggered:** True. Matched triggers: **T1** (lifecycle / state-machine — draft → validated → activated transitions plus `step_3_complete` wizard transition), **T3** (persistence + recovery — `active_constraints` row + `config_audit_log` rows + `draft_constraints` row + `wizard_state.step_3_*` columns all DB-backed; provider re-hydrates across restart), **T6** (deployment / restart behavior — `ActiveConstraintsProvider` re-reads on every boot; mid-draft restart preserves the draft surface), **T7** (installer workflow orchestration — Step 3 of the 4-step wizard mutates persisted safety configuration AND activates runtime behavior on the next PolicyGuard/ControlLoop cycle via `provider.reload()`). All seven R1–R7 orchestration artifacts are mandatory and present below.
>
> **Prerequisite gate (Epic 8 retro 2026-05-10):** Story 9.0b is done; the `ActiveConstraintsProvider` contract, `ConfigRepo.activate()` atomicity, and the seed-only `Settings` enforcement are all in place. 9.3's activation surface plugs into these as the change-trigger.

## Story

As an installer,
I want to configure per-site safety constraints using a staged draft → validate → confirm → activate flow with layered validation,
so that no constraint takes effect until I have explicitly reviewed and confirmed it, each activation is atomically committed with a single `config_version` increment, and every changed field is audit-logged.

## Acceptance Criteria

### AC1 — Schema: `draft_constraints` table + `active_constraints` EV-window columns + `wizard_state.step_3_*`

Story 9.0b explicitly deferred the `draft_constraints` table to this story (architecture BAD-3: "`config_repo` holds: `active_constraints` (current, versioned) + `draft_constraints` (nullable, one at a time)"). The schema delta is a single migration `migrations/versions/0011_add_constraint_configuration_tables.py`:

**New table — `draft_constraints`** (per-session draft; one row per active session at most):

| Column | Type | Constraints |
|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT |
| `session_id` | TEXT | NOT NULL; **UNIQUE** (`uq_draft_constraints_session_id`); FK → `sessions(id)` ON DELETE CASCADE |
| `peak_limit_kw` | REAL | NOT NULL; CHECK > 0 |
| `battery_reserve_floor_percent` | REAL | NOT NULL; CHECK ≥ 0 AND ≤ 100 |
| `ev_charging_window_start` | TEXT | NULLABLE; ISO `HH:MM` format when non-NULL |
| `ev_charging_window_end` | TEXT | NULLABLE; ISO `HH:MM` format when non-NULL |
| `validation_status` | TEXT | NOT NULL DEFAULT `'pending'`; CHECK IN (`'pending'`, `'valid'`, `'failed'`) |
| `validation_report` | TEXT | NULLABLE; JSON-encoded `ConstraintValidationReport` (the most recent server-side validation result); NULL until a `validate` call has run |
| `created_at` | TEXT | NOT NULL; ISO 8601 UTC |
| `updated_at` | TEXT | NOT NULL; ISO 8601 UTC |

**Existing table extension — `active_constraints` gains two nullable columns:**

| Column | Type | Constraints |
|---|---|---|
| `ev_charging_window_start` | TEXT | NULLABLE; `HH:MM` |
| `ev_charging_window_end` | TEXT | NULLABLE; `HH:MM` |

Both columns are nullable so 9.0b's cold-start seed path (no DB row yet → seed from Settings with `config_version=0`) continues to work without Settings additions. `ActiveConstraints` Pydantic model is extended with optional `ev_charging_window_start: str | None = None`, `ev_charging_window_end: str | None = None` fields plus a paired-NULL model_validator (either both set or both NULL).

**Existing table extension — `wizard_state` gains three columns:**

| Column | Type | Constraints |
|---|---|---|
| `step_3_complete` | INTEGER (0/1) | NOT NULL DEFAULT 0; CHECK IN (0, 1) |
| `step_3_completed_at` | TEXT | NULLABLE; ISO 8601 UTC |
| `step_3_activated_config_version` | INTEGER | NULLABLE; the `config_version` produced by THIS wizard step's activation (NULL until `step_3_complete=1`) |

The `step_3_activated_config_version` column links a wizard step completion to the specific `active_constraints` row it activated. Story 9.4 reads this in the Step-4 validation flow to detect outdated validation results (when site `config_version` differs from the one captured at validation time).

The migration adds:
- Index `ix_draft_constraints_session_id` (redundant with the UNIQUE constraint but matches 9.1's convention for explicit query indexes).
- CHECK constraint pairing on `active_constraints` ensuring `ev_charging_window_start IS NULL ↔ ev_charging_window_end IS NULL` (same paired-invariant pattern as 9.2's `role` / `role_assigned_at`).

Migration round-trip (`upgrade head → downgrade -1 → upgrade head`) is codified in `tests/integration/test_migrations.py::test_migration_0011_round_trips`. The downgrade drops every added column/constraint/index in reverse order.

### AC2 — `ConfigRepo.activate()` extended for EV window; ActiveConstraintsInput gains EV fields

The 9.0b `ConfigRepo.activate(input: ActiveConstraintsInput, *, actor)` signature is preserved; only the input model gains optional fields:

- `ActiveConstraintsInput.ev_charging_window_start: str | None = None`
- `ActiveConstraintsInput.ev_charging_window_end: str | None = None`
- Pydantic model_validator enforces `start ↔ end` paired-NULL invariant and `HH:MM` regex format.

`_build_audit_changes()` is extended:
- A change to `ev_charging_window_start` OR `ev_charging_window_end` emits one `config_audit_log` row with `field='ev_charging_window'` and `previous_value={"start": ..., "end": ...}` / `new_value={"start": ..., "end": ...}` — **one audit row for the window as a whole**, not two (the window is a single semantic constraint).
- The `SAFETY_RELEVANT_CONFIG_FIELDS` set in `config_audit_repo.py` is extended with `'ev_charging_window'`.

**The single-`config_version`-increment invariant is preserved:** all changed fields (peak limit, reserve floor, EV window) within one `activate()` call share the same `config_version`. AC1 of the epic spec ("config_version is incremented exactly once for this activation, regardless of how many constraint fields changed") is structurally enforced — the `next_version` query in `ConfigRepo.activate()` is the only writer, and the audit-row INSERT and `active_constraints` INSERT happen inside the same `BEGIN IMMEDIATE` transaction (9.0b precedent).

**No-op activation rule (9.0b inheritance):** if NO field changes, `activate()` raises `ValueError("activate called with no changed fields")`. The route handler catches this and returns a friendly 400 inline notice — "No changes to activate. Edit a field or return to Step 2."

### AC3 — `ConstraintDraftRepo` and `ConstraintsService`

A new repository `src/open_ems/storage/repositories/draft_constraints_repo.py` follows the existing repo pattern (mirrors `wizard_state_repo.py` shape — `frozen=True, extra="forbid"` Pydantic model + per-session async methods + write-lock-protected mutations).

**`ConstraintDraft` Pydantic model:**

| Field | Type | Constraint |
|---|---|---|
| `session_id` | `NonEmptyStr` | — |
| `peak_limit_kw` | `float` | `gt=0.0` |
| `battery_reserve_floor_percent` | `float` | `ge=0.0, le=100.0` |
| `ev_charging_window_start` | `str | None` | `HH:MM` format if non-NULL; paired with end |
| `ev_charging_window_end` | `str | None` | same |
| `validation_status` | `Literal['pending', 'valid', 'failed']` | — |
| `validation_report` | `ConstraintValidationReport \| None` | most recent server-side report; NULL if never validated |
| `created_at` | `datetime` | UTC tz-aware |
| `updated_at` | `datetime` | UTC tz-aware |

**`ConstraintDraftRepo`:**

- `async get(session_id) -> ConstraintDraft | None`
- `async upsert(session_id, *, peak_limit_kw, battery_reserve_floor_percent, ev_charging_window_start, ev_charging_window_end, now) -> ConstraintDraft` — INSERT OR REPLACE pattern; always resets `validation_status='pending'` and clears `validation_report` (any field edit invalidates a prior validation).
- `async record_validation_outcome(session_id, *, status: Literal['valid', 'failed'], report: ConstraintValidationReport, now) -> None` — writes the validation result + timestamp.
- `async delete(session_id) -> None` — explicit DELETE used at the end of a successful activation.

All writes acquire `get_write_lock()` (9.0b/9.2 precedent).

**`ConstraintsService`** (new service at `src/open_ems/services/constraints.py`) — the single evaluator for staged draft → validate → activate semantics. Route handlers, templates, and Story 9.4's deployment validation all call into this surface.

```python
class ConstraintsService:
    def __init__(
        self,
        *,
        draft_repo: ConstraintDraftRepo,
        config_repo: ConfigRepo,
        active_constraints_provider: ActiveConstraintsProvider,
        wizard_state_repo: WizardStateRepo,
        device_repo: DeviceRepo,
        state_store: StateStore,
    ) -> None: ...

    async def get_or_default(self, session_id: str) -> ConstraintDraftView:
        """Return the existing draft for this session, or a default-seeded view
        from the currently-active constraints. The returned view is suitable for
        rendering the Step-3 form on initial load."""

    async def upsert_draft(
        self,
        session_id: str,
        *,
        input: ConstraintDraftInput,
        now: datetime,
    ) -> ConstraintDraft: ...

    async def validate_draft(
        self,
        session_id: str,
        *,
        now: datetime,
    ) -> ConstraintValidationReport:
        """Run server-side staged validation (BAD-3 step 3a/b/c):
            (a) Schema correctness: ranges, HH:MM regex, paired-NULL.
            (b) Capability-strategy consistency: e.g., a non-zero peak_limit_kw
                with no grid_meter assigned in device_registry → WARN
                (degrades to FAIL if Story 9.4 hardens it).
            (c) Safety pre-check: would activating this set produce an
                immediately unsafe state? Reads StateStore.get_snapshot() —
                e.g., setting battery_reserve_floor_percent above current
                battery SoC means PolicyGuard would IMMEDIATELY block all
                discharge → WARN with explicit explanation.
        """

    async def activate_draft(
        self,
        session_id: str,
        *,
        actor: Literal["installer"],
        now: datetime,
    ) -> ConstraintActivationResult:
        """Atomic activation. Re-runs validation inside the write lock, then
        commits if and only if validation passes. Returns the new
        config_version, drops the draft, and marks wizard_state.step_3_complete.
        Raises ConstraintActivationError on failure (with structured per-check
        details)."""
```

**`ConstraintValidationReport`** (frozen Pydantic):

| Field | Type | Description |
|---|---|---|
| `overall_status` | `Literal['valid', 'failed']` | derived: `'failed'` if any check is FAIL, else `'valid'` |
| `checks` | `tuple[ConstraintCheckResult, ...]` | one per check (schema, capability_strategy, safety_pre_check) |

**`ConstraintCheckResult`** (frozen):

| Field | Type | Constraint |
|---|---|---|
| `name` | `Literal['schema', 'capability_strategy', 'safety_pre_check']` | — |
| `status` | `Literal['pass', 'warn', 'fail']` | `pass` ⇒ no message |
| `field` | `str \| None` | which input field the message references (`peak_limit_kw`, `battery_reserve_floor_percent`, `ev_charging_window`, or NULL if cross-field) |
| `message` | `str \| None` | plain-language operator-facing message; NULL on `pass` |

**Validation status mapping for the route layer:**
- All checks `pass` → `overall_status='valid'`; activate button is enabled
- Any check `fail` → `overall_status='failed'`; activate button is disabled; field-level messages render inline (UX spec component #8)
- Mixed `pass` + `warn` (no `fail`) → `overall_status='valid'`; activate button is enabled BUT the confirmation summary surfaces the warnings inline; installer activation is allowed (BAD-3 step 4 second bullet: "Any WARN (no safety constraint violated): installer may proceed with explicit confirmation"). The confirmation summary is server-rendered; no JavaScript confirm prompt.

### AC4 — Inline field validation (client-side schema only)

Per UX spec component #8 + epic spec AC ("basic field validation (type, format, range bounds) runs client-side immediately — no server round trip"):

- Each form field carries native HTML attributes for client-side validation: `<input type="number" min="0.01" step="0.01" required>` for `peak_limit_kw`, `<input type="number" min="0" max="100" step="0.1" required>` for `battery_reserve_floor_percent`, `<input type="time">` for EV window start/end.
- The browser's native HTML5 form validation handles `type` / `format` / range bound checks on blur and submit-attempt (UX spec component #8 "Validation on blur and submit attempt — not on every keystroke").
- **The server is the authoritative validator.** All client-side validation is a UX hint; the route handler always re-validates via `ConstraintsService.validate_draft()` before any persistence. Per AC: "client-side validation is supplementary and may be more permissive than the server".
- No JavaScript framework is added. Native HTML5 + HTMX is the architecture-mandated baseline; this story does not introduce new client-side dependencies.

### AC5 — Routes: GET form, draft upsert, validate, activate, placeholder Step 4

New / extended routes are added under `src/open_ems/web/routes/setup.py`. All require `Depends(require_installer)`. All POSTs are CSRF-protected by the existing middleware. All HTML responses extend `installer/setup_layout.html` (the persistent 4-step indicator).

| Method | Path | Returns |
|---|---|---|
| GET | `/installer/setup/constraints` | full HTML page rendering: (1) the constraint form, pre-populated from the existing draft if one exists for the session, OR seeded from the currently-active `provider.get()` values otherwise; (2) the most recent `validation_report` (if any) rendered as the BAD-3 step-3 result list; (3) the activate button with server-rendered enable/disable state matching `validation_status`. **The placeholder `setup_constraints_placeholder.html` template from Story 9.2 is deleted** (parallel to 9.2 deleting `setup_roles_placeholder.html`). |
| POST | `/installer/setup/constraints/draft` | HTMX fragment `_setup_constraints_form.html` re-rendering the form region with the upserted values; resets `validation_status='pending'`. CSRF-protected. |
| POST | `/installer/setup/constraints/validate` | HTMX fragment `_setup_constraints_validation.html` re-rendering ONLY the validation-report region; persists the report; on `valid`, also re-renders the activate button enabled (via `hx-swap-oob`). 400 with exact-match reason on a malformed input (e.g., a missing CSRF token gets the middleware's 403). |
| POST | `/installer/setup/constraints/activate` | on pass: 302 redirect to `/installer/setup/validation` (Story 9.4 will replace its placeholder); on fail: 400 with the inline error banner fragment `_setup_constraints_error_banner.html`. |
| GET | `/installer/setup/validation` | placeholder route returning a "Step 4 will be implemented in Story 9.4" page extending `setup_layout.html`. The file is named `setup_validation_placeholder.html` (parallel to 9.1's `setup_roles_placeholder.html` and 9.2's `setup_constraints_placeholder.html`). Story 9.4 deletes this placeholder. |

**Step-gate enforcement (9.2 precedent):** the GET `/installer/setup/constraints` handler reads `wizard_state` and:
1. If `step_1_complete=0` → 302 redirect to `/installer/setup/discovery`
2. Else if `step_2_complete=0` → 302 redirect to `/installer/setup/roles`
3. Else proceed

This mirrors 9.2's review-patch enforcement on `GET /installer/setup/roles` (which redirects when `step_1_complete=0`).

**Back-navigation hyperlink in `setup_layout.html`:** the step-2 label gains the same conditional-hyperlink treatment as step-1 (Story 9.2). Step-1 link renders when `step_2_complete=0` (existing behaviour). Step-2 link renders when `step_3_complete=0`. Once Step 3 is finalized, both back-links are hidden.

### AC6 — Atomic activation contract (the load-bearing AC)

The activate route's contract:

1. Acquire `get_write_lock()`.
2. Read `wizard_state` for the session — assert `step_1_complete=1 AND step_2_complete=1`. Otherwise return a 400 with reason `step_prerequisites_not_met: step_1_complete={x} step_2_complete={y}` (exact-match).
3. Read the current `ConstraintDraft` for the session via `ConstraintDraftRepo.get()`. If NULL → 400 with reason `draft_not_found_for_session` (exact-match).
4. **Re-run `ConstraintsService.validate_draft()`** — the persisted `validation_status='valid'` from a prior validate click is informational; the route is the authoritative gate. Re-validation reads live `device_repo.list_all()` + live `state_store.get_snapshot()` so any state change between prior validation and activation is caught.
5. If re-validation `overall_status='failed'` → 400 with the banner fragment + the re-validation report; **the draft's persisted `validation_status` is updated to `'failed'` in the same write-lock window** so the next page load reflects the fresh outcome.
6. Build the `ActiveConstraintsInput` from the draft.
7. Call `config_repo.activate(input, actor='installer')` — this is the 9.0b atomic transaction (`BEGIN IMMEDIATE`, INSERT into `active_constraints`, INSERT N rows into `config_audit_log`, single shared `config_version`, COMMIT). Returns the new `config_version`.
8. Call `active_constraints_provider.reload()` — atomic in-memory snapshot replacement. **MUST be called inside the write-lock window**, AFTER the DB commit, BEFORE the wizard-state update. If `reload()` raises, log `constraints_reload_after_activate_failed` at ERROR level and continue (the DB is authoritative; the next process restart re-hydrates correctly). The route still completes successfully — the activation is committed; reload failure is a recoverable in-memory drift.
9. Call `wizard_state_repo.set_step_3_complete_locked(session_id, activated_config_version=new_version, now)` — atomic UPDATE writing `step_3_complete=1`, `step_3_completed_at`, `step_3_activated_config_version`. Idempotent: a re-click of activate on an already-`step_3_complete=1` row preserves the original `step_3_completed_at` (9.2 precedent for idempotent advance).
10. Call `draft_repo.delete(session_id)` — clean up the now-activated draft.
11. Release the lock; emit `constraints_activated` structlog at INFO with the changed-field list and new `config_version`; return 302 to `/installer/setup/validation`.

**Idempotency:** a re-click of activate after a successful first activation finds the draft already deleted (step 3 returns 400 `draft_not_found_for_session`). The wizard advances correctly on the redirect from step 11. The installer sees the same "Step 3 → Step 4" transition on retry. **A passing activation followed by a re-click is NOT a no-op activation at the DB layer** — there is no draft to activate; the route returns 400. The frontend handles this by reading `wizard_state.step_3_complete` on page load and redirecting to `/installer/setup/validation` if already complete (step 2 of the GET handler). This means a re-click of the activate button after activation lands on Step 4 directly without a 400, because the GET handler short-circuits before the POST is even triggered.

### AC7 — Validation semantics (the three checks)

**Check 1: Schema (`name='schema'`).**
- Validates the input through Pydantic `ConstraintDraftInput` (server-side; this is the authoritative re-validation regardless of client-side HTML5 hints).
- Status `pass` on success, `fail` on Pydantic ValidationError with per-field `message` mapping each field error to its plain-language form.
- Example fail message for `peak_limit_kw=-5.0`: `field='peak_limit_kw'`, `message='Peak limit must be greater than 0 kW.'`
- The EV window paired-NULL invariant is checked here: `start=NULL` with `end='17:00'` → `field='ev_charging_window'`, `message='EV charging window start and end must both be set or both empty.'`

**Check 2: Capability-strategy consistency (`name='capability_strategy'`).**
- Reads `device_repo.list_all()` — the currently-assigned device set with their roles (from Story 9.2).
- Status `warn` (not `fail`) when:
  - No `grid_meter` role is assigned → `field=None`, `message='Grid meter role is required for peak limiting to function. Step 2 must complete before constraints take effect.'`
    *(This is technically unreachable because the step-gate prerequisite check in AC6 step 2 fires first; included as defense-in-depth.)*
  - The configured `battery_reserve_floor_percent` is non-zero but no `battery` role is assigned → `field='battery_reserve_floor_percent'`, `message='No battery is assigned, so the reserve floor has no effect on system behavior.'`
  - The EV window is non-NULL but no `ev_charger` role is assigned → `field='ev_charging_window'`, `message='No EV charger is assigned, so the charging window has no effect on system behavior.'`
- These are WARN (not FAIL) because acknowledging a role gap at Step 2 is a legitimate path (Story 9.2 AC5); the constraints can still be activated, they just have no effect on the absent devices.

**Check 3: Safety pre-check (`name='safety_pre_check'`).**
- Reads `state_store.get_snapshot()` (in-memory; no DB I/O).
- Status `warn` (not `fail`) when:
  - `snapshot.battery is BatteryState AND draft.battery_reserve_floor_percent > snapshot.battery.soc_percent` → `field='battery_reserve_floor_percent'`, `message=f'Battery is currently at {soc_percent:.1f}% SoC. Activating a reserve floor of {floor:.1f}% would immediately block discharge until SoC rises above the floor.'`
  - `snapshot.grid_meter is GridMeterState AND snapshot.grid_meter.grid_power_kw > draft.peak_limit_kw` → `field='peak_limit_kw'`, `message=f'Grid is currently importing {grid_power:.1f} kW; activating a peak limit of {limit:.1f} kW would immediately put the site over limit.'`
- Status `fail` when ANY computed safety condition would PERMANENTLY block all dispatch (the architecture's "constraints that produce an impossible operating condition" clause):
  - `draft.peak_limit_kw < 0.5` (a peak limit below 0.5 kW is operationally indistinguishable from 0; the site cannot import anything) → `field='peak_limit_kw'`, `message='Peak limit below 0.5 kW would block all grid imports indefinitely.'` *(The 0.5 kW threshold is a deterministic guard, not a heuristic; documented as a structural FAIL not a WARN because no installer flow recovers from it.)*

**Snapshot ordering invariant:** the three checks run in order schema → capability_strategy → safety_pre_check. If schema FAILs, capability_strategy and safety_pre_check are SKIPPED (their results are absent from the report). This avoids reporting derived failures on input that hasn't yet passed schema validation. Tests assert this order explicitly.

### AC8 — Templates and accessibility

New / modified Jinja2 templates under `src/open_ems/web/templates/installer/`:

- **DELETE** `setup_constraints_placeholder.html` — replaced by the real implementation (parallel to 9.2 deleting `setup_roles_placeholder.html`).
- **NEW** `setup_constraints.html` — extends `setup_layout.html` (`active_step='constraints'`); renders the page body with three sections:
  1. "Site safety constraints" — the constraint form (uses `_setup_constraints_form.html`).
  2. "Validation result" — the most recent validation report (uses `_setup_constraints_validation.html`).
  3. "Activate" — the activate form with the server-disabled-state Continue-to-Step-4 button (uses `_setup_constraints_activate.html`).
- **NEW** `_setup_constraints_form.html` — the form region, target of the draft-upsert HTMX swap. Renders three input groups (peak limit, reserve floor, EV window) per UX spec component #8. Each input group is a `<div>` containing `<label>` / `<input>` / `<span class="unit">` / `<small class="helper">` / `<div class="validation-message">`. The validation-message div is server-rendered with `role="alert"` when populated.
- **NEW** `_setup_constraints_validation.html` — the validation-report region, target of the validate HTMX swap. Renders one row per `ConstraintCheckResult` per UX spec component #9 (Validation Result Row) — status badge (PASS / WARN / FAIL) + check name + summary text + corrective action (WARN/FAIL only). Status badges use the same color/contrast tokens as Story 9.1's discovery badges.
- **NEW** `_setup_constraints_activate.html` — the activate form + button. Server-rendered `disabled` and `aria-disabled="true"` when `validation_status != 'valid'`. When `validation_status='valid'` AND any check has `status='warn'`, the button text reads "Activate with warnings" and the button container is wrapped in a `<div role="region" aria-label="Confirmation">` containing the bullet-list of warnings — per BAD-3 step 4 second bullet ("installer may proceed with explicit confirmation").
- **NEW** `_setup_constraints_error_banner.html` — the inline error banner for the activate-fail path (mirrors `_setup_roles_error_banner.html`).
- **NEW** `_setup_constraints_error.html` — the HTMX-targeted single-error envelope (mirrors `_setup_roles_error.html`).
- **NEW** `setup_validation_placeholder.html` — minimal Step-4 placeholder, parallel to 9.2's `setup_constraints_placeholder.html`.
- **MODIFIED** `setup_layout.html` — step-2 label gains conditional hyperlink when `step_3_complete=0` (mirrors the step-1 hyperlink logic from 9.2).

**Accessibility requirements** (UX spec §Accessibility + UX components #8/#9/#10/#14):
- Each `<input>` has an `aria-describedby` pointing to its helper-text + validation-message siblings, so screen readers announce both the helper and any error.
- Each validation-message `<div>` uses `role="alert"` so screen readers announce dynamic changes after a validate or draft-upsert.
- The activate button's enabled/disabled state is server-rendered — color is never the sole signal that the gate is closed (button text reads "Activate", "Activate with warnings", or "Activate (validation required)" depending on state).
- All input contrast ratios meet ≥4.5:1 against background (the existing `--color-fail` and `--color-warn` tokens from Story 9.1 apply unchanged).
- Keyboard reachability for every interactive element: form inputs, validate button, activate button. Tab order is deterministic.
- Automated axe-core scan: same deferred-CI-hardening pattern as Story 9.1 AC10 / 9.2 AC8. A placeholder test at `tests/integration/web/test_setup_constraints_a11y.py` is marked `pytest.mark.xfail(reason="axe-playwright wiring deferred", strict=False)`. The outer `pytest.mark.skipif(npx absent)` is retained.

### AC9 — Lifespan wiring + provider exposure

`src/open_ems/web/app.py` lifespan is extended at **step 4e+** (after `role_assignment_service`):

```python
# Step 4e (Story 9.3): construct the staged-constraint-draft repo + service.
# Stateless; no DB I/O at construction.
draft_constraints_repo = DraftConstraintsRepo()
constraints_service = ConstraintsService(
    draft_repo=draft_constraints_repo,
    config_repo=ConfigRepo(),  # OR reuse the one already constructed at step 4b
    active_constraints_provider=active_constraints_provider,
    wizard_state_repo=wizard_state_repo,
    device_repo=device_repo,
    state_store=app.state.state_store,
)
app.state.draft_constraints_repo = draft_constraints_repo
app.state.constraints_service = constraints_service
logger.info("draft_constraints_repo_ready", component="startup")
logger.info("constraints_service_ready", component="startup")
```

The `_constraints_service` Depends helper in `web/routes/setup.py` follows the 9.2 `_role_assignment_service` pattern (reads from `request.app.state.constraints_service`; 503 if absent).

**ConfigRepo reuse rationale:** the lifespan already constructs `ConfigRepo()` at step 4b (for the provider). The new `ConstraintsService` should reuse that same instance (passed in via lifespan wiring) so all writes share the database-module write lock. The provider's `_repo` field already references the same instance — no new ConfigRepo construction is needed.

### AC10 — Tests

Each test asserts the **exact** contract clause it covers (no substring `in` assertions on rejection reasons — exact match, per 9.0c AC7 / 9.1 AC11 / 9.2 AC9 precedent).

**Unit tests (`tests/unit/`):**
- `storage/repositories/test_draft_constraints_repo.py` (new) — round-trip upsert / get / record_validation_outcome / delete; UPSERT replaces the row and resets `validation_status='pending'` + clears `validation_report`; CHECK constraint rejection on out-of-range values; FK CASCADE removes the row when its parent session is deleted; paired-NULL invariant on `ev_charging_window_*`.
- `storage/repositories/test_config_repo.py` (extend) — `activate()` with EV window changes emits exactly ONE `config_audit_log` row with `field='ev_charging_window'`; same `config_version` shared with peak/reserve audit rows in a multi-field activation; `_build_audit_changes` returns the correct rows for every changed-field combination.
- `storage/repositories/test_wizard_state_repo.py` (extend) — `set_step_3_complete` is idempotent (preserves `step_3_completed_at` on re-call); the new column `step_3_activated_config_version` round-trips through Pydantic + DB correctly; pairing invariant (`step_3_complete=0 ↔ step_3_completed_at IS NULL ↔ step_3_activated_config_version IS NULL`).
- `services/test_constraints_service.py` (new) — covers:
  - `get_or_default` returns a default-seeded view when no draft exists; values match `provider.get()`.
  - `upsert_draft` resets `validation_status='pending'` regardless of prior status.
  - `validate_draft` schema FAIL skips capability_strategy + safety_pre_check (assert their `ConstraintCheckResult` is absent from the report).
  - `validate_draft` schema PASS + capability_strategy WARN (no battery assigned + reserve_floor=10) → overall_status='valid' (WARN does not fail validation); report contains the WARN.
  - `validate_draft` safety_pre_check WARN (battery SoC < reserve floor) → overall_status='valid'; report contains the WARN.
  - `validate_draft` safety_pre_check FAIL (peak_limit_kw=0.4) → overall_status='failed'; report contains the FAIL.
  - `activate_draft` re-runs validation inside the lock — a state change between prior validate and activate that flips a check to FAIL aborts activation, leaves no `active_constraints` row, and persists the new FAIL report to the draft.
  - `activate_draft` no-op (input equals provider.get()) → `ValueError("activate called with no changed fields")` from `ConfigRepo.activate`; route handler catches and returns 400.
  - `activate_draft` happy path → returns `ConstraintActivationResult` with the new `config_version`; the draft row is DELETEd; `provider.reload()` is called once; `wizard_state.step_3_complete=1`; `step_3_activated_config_version` matches the new version.
- `services/test_constraints_service_audit_emission.py` (new) — assert the exact audit-row count and field names per activation type:
  - peak_limit_kw change only → 1 audit row, `field='peak_consumption_limit'`
  - battery_reserve_floor_percent change only → 1 audit row, `field='battery_reserve_floor'`
  - EV window change only (`start='09:00', end='17:00'` from NULL) → 1 audit row, `field='ev_charging_window'`, `new_value={'start': '09:00', 'end': '17:00'}`
  - All three change → 3 audit rows, all sharing the same `config_version`
- `web/test_setup_constraints_routes.py` (new) — covers:
  - Auth: non-installer → 302/403; missing session → 401-style redirect to login.
  - CSRF: every POST without a valid token → 403 (existing middleware behaviour).
  - Step-gate: GET without `step_1_complete=1` → 302 to discovery; GET with `step_1_complete=1 AND step_2_complete=0` → 302 to roles.
  - Form fields: GET with no draft renders the form seeded from `provider.get()`; GET with a draft renders the draft's values.
  - Draft upsert: POST with valid input → 200 + form fragment + draft persisted; POST with malformed input → 400 + form fragment with field errors; draft `validation_status` is reset to `pending` after every upsert.
  - Validate: POST with `valid` outcome → 200 + report fragment with all checks `pass` (or `pass+warn`); POST with `failed` outcome → 200 + report fragment with the FAIL; report persisted to the draft.
  - Validate without prior draft → 400 with exact-match reason `draft_not_found_for_session`.
  - Activate happy path → 302 to `/installer/setup/validation`; `wizard_state.step_3_complete=True`; new `active_constraints` row exists; `config_audit_log` has the expected rows; `provider.get().config_version` matches the new version; draft row is gone.
  - Activate without prior validation (`validation_status='pending'`) → re-validation is the authoritative gate; if it passes, activation proceeds; if it fails, 400 + banner.
  - Activate with `validation_status='valid'` but live state has shifted to make safety_pre_check FAIL → activation aborts; draft's persisted `validation_status` is updated to `'failed'` in the same write-lock window; 400 + banner.
  - Activate idempotence: second activate after success → GET handler short-circuits to `/installer/setup/validation`; the POST is never reached. Test asserts the redirect via the GET path.
  - Activate with no changes (draft input == provider.get() values) → 400 with exact-match reason `activate_called_with_no_changed_fields`. (The route handler translates the `ValueError` from `ConfigRepo.activate` into this exact-match reason for the inline banner.)
  - Activate with `step_2_complete=0` → 400 with exact-match reason `step_prerequisites_not_met: step_1_complete=1 step_2_complete=0`.
  - Provider snapshot is updated after activation: PolicyGuard's hot path sees the new `peak_limit_kw` on the very next request after activate completes.

**Integration tests (`tests/integration/`):**
- `web/test_setup_constraints_e2e.py` (new) — full request flow against in-memory SQLite with the real `ActiveConstraintsProvider`. Cases:
  - End-to-end happy path: discovery (Story 9.1) → roles (Story 9.2) → constraints → activate → reload provider → assert PolicyGuard reflects the new constraints. Verify by sending a `SetEVChargingRateCommand(rate_kw=X)` where X exceeds the old peak_limit_kw but is below the new one — assert PolicyGuard accepts it after activation.
  - End-to-end mixed path: validate FAILs → re-edit draft → validate passes → activate succeeds.
  - End-to-end persistence path: upsert draft → simulate process restart (close + re-init DB connection via test fixture) → re-load GET — draft row is restored; validation_status is preserved; user can resume from where they left off.
  - End-to-end audit trail: activate with all three fields changing → assert `config_audit_log` has exactly 3 rows with identical `config_version`, distinct `field` values, correct `previous_value` / `new_value` JSON.
  - End-to-end live-state-shift path: validate passes (battery SoC > reserve_floor) → before activate, push a new StateStore snapshot where battery SoC drops below reserve_floor → activate re-validates and FAILs the safety_pre_check; assert no DB write occurred.
  - End-to-end provider drift: validate passes → manually inject a `provider.reload()` failure → activate completes the DB writes but logs `constraints_reload_after_activate_failed`; the next `provider.get()` returns stale data; a follow-up `provider.reload()` resolves it. Asserts the documented degradation mode.
- `web/test_setup_constraints_a11y.py` (new — `pytest.mark.xfail` per AC8) — axe-core placeholder.
- `test_migrations.py` (extend) — migration 0011 round-trip + schema-shape assertions on `draft_constraints.*`, `active_constraints.ev_charging_window_*`, `wizard_state.step_3_*` columns.
- `web/test_app_startup.py` (extend) — assert `ConstraintsService` and `DraftConstraintsRepo` are wired into `app.state` after lifespan completion.
- `engine/test_constraints_reload.py` (extend) — assert that ONE activate via the Step-3 route reaches `ConfigRepo.activate` exactly once + `provider.reload()` exactly once, and that PolicyGuard's next constraint read sees the new value (closes the 9.0b reload contract loop with a real route-driven trigger, not a synthetic one).

**Quality gates:** `pytest tests/ --no-cov -q`, `mypy src/`, `ruff check .`, `ruff format --check .` all clean before status moves to `review`.

### AC11 — Adversarial 3-layer review with severity tagging (process AC)

Before `Status: review → done`, run `/bmad-code-review` and apply the three-layer review (Blind Hunter / Edge Case Hunter / Acceptance Auditor) with severity tagging (HIGH / MEDIUM / LOW / deferred / dismissed). Same gate semantics as 9.0c AC9 / 9.0d AC11 / 9.1 AC12 / 9.2 AC10: **zero unresolved HIGH/MEDIUM permitted before merge**. All deferred findings logged to `_bmad-output/implementation-artifacts/deferred-work.md` under a dated "code review of 9-3-..." heading.

## Tasks / Subtasks

- [x] Task 1 — Schema + repository (AC1, AC2, AC3)
  - [x] Subtask 1.1 — Author migration `0011_add_constraint_configuration_tables.py` adding `draft_constraints` + `active_constraints.ev_charging_window_*` + `wizard_state.step_3_*` columns
  - [x] Subtask 1.2 — Extend `ActiveConstraintsInput` + `ActiveConstraints` Pydantic models with optional EV window fields + paired-NULL model_validator + HH:MM format validator
  - [x] Subtask 1.3 — Extend `ConfigRepo.activate()` audit emission for EV window (one row with `field='ev_charging_window'`); extend `SAFETY_RELEVANT_CONFIG_FIELDS`
  - [x] Subtask 1.4 — Author `src/open_ems/storage/repositories/draft_constraints_repo.py` with `ConstraintDraft` + `DraftConstraintsRepo`
  - [x] Subtask 1.5 — Extend `WizardStateRepo` with `set_step_3_complete` + `set_step_3_complete_locked` (mirror 9.2's idempotent pattern)
  - [x] Subtask 1.6 — Migration round-trip verification (out-of-band) + `tests/integration/test_migrations.py::test_migration_0011_round_trips`
- [x] Task 2 — `ConstraintsService` (AC3, AC6, AC7)
  - [x] Subtask 2.1 — Author `src/open_ems/services/constraints.py` with `ConstraintDraftInput`, `ConstraintCheckResult`, `ConstraintValidationReport`, `ConstraintActivationResult`, `ConstraintActivationError`, `ConstraintsService`
  - [x] Subtask 2.2 — Implement `get_or_default`, `upsert_draft`, `validate_draft` (three checks in order; schema FAIL skips downstream)
  - [x] Subtask 2.3 — Implement `activate_draft` (write-lock-bounded: prerequisite check → re-validate → `ConfigRepo.activate` → `provider.reload` → `set_step_3_complete_locked` → `draft_repo.delete`)
  - [x] Subtask 2.4 — Unit tests per AC10
- [x] Task 3 — Routes (AC5, AC6)
  - [x] Subtask 3.1 — Delete the placeholder GET `/installer/setup/constraints` route body and the `setup_constraints_placeholder.html` template
  - [x] Subtask 3.2 — Implement `GET /installer/setup/constraints` (step-gate enforcement + full page render seeded from draft or provider)
  - [x] Subtask 3.3 — Implement `POST /installer/setup/constraints/draft` (HTMX fragment; resets validation_status)
  - [x] Subtask 3.4 — Implement `POST /installer/setup/constraints/validate` (HTMX fragment; persists report)
  - [x] Subtask 3.5 — Implement `POST /installer/setup/constraints/activate` per AC6 contract
  - [x] Subtask 3.6 — Add `GET /installer/setup/validation` placeholder route + template (Story 9.4 will replace)
- [x] Task 4 — Templates + accessibility (AC4, AC8)
  - [x] Subtask 4.1 — Author `setup_constraints.html` + `_setup_constraints_form.html` + `_setup_constraints_validation.html` + `_setup_constraints_activate.html`
  - [x] Subtask 4.2 — Author `_setup_constraints_error_banner.html` + `_setup_constraints_error.html`
  - [x] Subtask 4.3 — Author `setup_validation_placeholder.html`
  - [x] Subtask 4.4 — Update `setup_layout.html` step indicator to render Step 2 as a hyperlink when `step_3_complete=0` (back-navigation)
  - [x] Subtask 4.5 — A11y: `aria-describedby`, `role="alert"`, server-rendered disabled states, keyboard reachability verified manually + via tests
- [x] Task 5 — Lifespan wiring + provider exposure (AC9)
  - [x] Subtask 5.1 — Construct `DraftConstraintsRepo` and `ConstraintsService` in lifespan step 4e AFTER `role_assignment_service`; reuse the `ConfigRepo` instance that the provider already references
  - [x] Subtask 5.2 — Expose via `app.state.draft_constraints_repo` + `app.state.constraints_service`
  - [x] Subtask 5.3 — Add the `_constraints_service` Depends helper in `web/routes/setup.py`
- [x] Task 6 — Tests (AC10)
  - [x] Subtask 6.1 — Unit test files per AC10
  - [x] Subtask 6.2 — Integration test files per AC10
  - [x] Subtask 6.3 — Accessibility (axe-core) test placeholder marked `pytest.mark.xfail` (per AC8)
- [x] Task 7 — Quality gates + review (AC11)
  - [x] Subtask 7.1 — `pytest tests/ --no-cov -q` clean
  - [x] Subtask 7.2 — `mypy src/` clean
  - [x] Subtask 7.3 — `ruff check .` and `ruff format --check .` clean
  - [x] Subtask 7.4 — `/bmad-code-review` run (completed 2026-05-11; 16 patches applied; 4 deferred to deferred-work.md; 8 dismissed)

### Review Findings

_Generated by `/bmad-code-review` on 2026-05-11 (3-layer: Blind Hunter + Edge Case Hunter + Acceptance Auditor). Per AC11, zero unresolved HIGH/MEDIUM permitted before merge._

#### Decision-needed (resolved 2026-05-11 — converted to patches below)

- [x] [Review][Decision] **D1 → P12** — Hard-reject all three POSTs with `step_prerequisites_not_met` when step_1/step_2 incomplete AND with new exact-match `step_3_already_complete` when step_3_complete=1.
- [x] [Review][Decision] **D2 → P13** — Restructure so `set_step_3_complete_locked` and `delete_locked` run inside `_activate_locked`'s `BEGIN IMMEDIATE` transaction (atomic across all three tables).
- [x] [Review][Decision] **D3 → P14** — Swallow zero-rowcount on FAIL persist path; log `validation_fail_persist_skipped_session_gone`; still raise contractual `constraints_validation_failed`.
- [x] [Review][Decision] **D4 → P15** — Emit a WARN when `snapshot.battery is None AND draft.battery_reserve_floor_percent > 0`; symmetric for grid_meter when peak_limit_kw is set.
- [x] [Review][Decision] **D5 → P16** — Restructure `_check_capability_strategy` and `_check_safety_pre` to return `tuple[ConstraintCheckResult, ...]`; flatten in `_run_validation` so multiple WARNs per check surface in the report.

#### Patches (unambiguous fixes — no decision required)

- [x] [Review][Patch] **P1 — `provider.reload()` regressed-version silent return is invisible to the service (HIGH).** [src/open_ems/services/constraints.py:273–283 + src/open_ems/services/active_constraints.py `reload()` version_regressed branch] `reload()` may keep the OLD snapshot when `MAX(active_constraints.config_version)` is `<` the cached value (deliberate per 9.0b P9). The activate path only `try/except`s reload _exceptions_ — it has no observation of the silent-skip branch. Fix: after `await self._provider.reload()`, assert `self._provider.get().config_version == new_version`; on mismatch, log `constraints_reload_after_activate_drift` at ERROR (mirror the existing failure log) so observability surfaces the stale-snapshot case.

- [x] [Review][Patch] **P2 — `ConstraintActivationResult.activated_at` is read from possibly-stale provider after reload (HIGH).** [src/open_ems/services/constraints.py:309–321] On a reload failure or version_regressed branch, `active_after = self._provider.get()` returns the OLD snapshot, so the result pairs `config_version=new_version` with `activated_at=old_row_timestamp`. Fix: capture `activated_at` from the `now` parameter passed into `activate_draft` (or from `_activate_locked`'s internal write) instead of re-reading via the provider.

- [x] [Review][Patch] **P3 — `_form_view_from_request` silently substitutes 0.0 for unparsable input (HIGH).** [src/open_ems/web/routes/setup.py:849–871] When a user submits non-numeric `peak_limit_kw` (e.g. `"abc"`), the route catches the parse failure and constructs a `ConstraintDraftView(peak_limit_kw=0.0, …)` because `ConstraintDraftView` is a frozen dataclass with no field constraints. The form re-renders with `value="0.00"` — destroying the user's input and injecting a value they didn't enter. Fix: change `ConstraintDraftView` to carry the raw form text (or `Optional[float]` with None on parse error) and have the template render whichever the user typed; never synthesize 0.0.

- [x] [Review][Patch] **P4 — `changed_fields` structlog field is always empty on the happy path (MEDIUM).** [src/open_ems/services/constraints.py:293–308 + helpers at 467–489] `_previous_*_safe()` reads `self._provider.get().*` AFTER step 8 ran `provider.reload()`, so the comparison is always `draft.value == new_value` → set becomes `{""}` → empty. The observability event `constraints_activated` ships with `changed_fields=[]` on every successful activation. Fix: snapshot `provider.get()` BEFORE the lock or before `_activate_locked` runs, then compare the draft to that captured snapshot when building the log payload.

- [x] [Review][Patch] **P5 — Schema-check messages don't match spec-mandated user-facing strings (MEDIUM).** [src/open_ems/services/constraints.py:360–371] `_check_schema` surfaces raw Pydantic `errors()[0]['msg']` (e.g. `"Input should be greater than 0"`). Spec AC7 specifies exact strings — e.g. `"Peak limit must be greater than 0 kW."` and `"EV charging window start and end must both be set or both empty."` Fix: map known Pydantic error codes / `loc` paths to the spec-exact operator-facing strings.

- [x] [Review][Patch] **P6 — Error banner renders raw machine-readable contract strings to the installer (MEDIUM).** [src/open_ems/web/templates/installer/_setup_constraints_error_banner.html:9 + _setup_constraints_error.html] The template prints `{{ reason }}` directly, so users see `activate_called_with_no_changed_fields`, `step_prerequisites_not_met: step_1_complete=1 step_2_complete=0`, `draft_not_found_for_session`. Spec AC2 explicitly demanded "No changes to activate. Edit a field or return to Step 2." for the no-changes case. Fix: route the reason through a translation map in the route handler (or a Jinja filter) so the banner shows operator copy; keep the exact-match string available as the contract reason in a `data-*` attribute / log line.

- [x] [Review][Patch] **P7 — Brittle string-match for "no changed fields" `ValueError` (MEDIUM).** [src/open_ems/services/constraints.py — activate_draft try/except around `_activate_locked`] The service inspects `"no changed fields" in str(exc)` to remap to `activate_called_with_no_changed_fields`. A future edit to the `ConfigRepo._activate_locked` message text silently downgrades the user response from 400 to 500. Fix: introduce a dedicated exception type (e.g. `NoChangedFieldsError`) in `config_repo.py` and catch by class; pin the contract reason on the class.

- [x] [Review][Patch] **P8 — `peak_limit_kw=inf` accepted by Pydantic and DB CHECK; only blocked at audit JSON serialization (MEDIUM).** [src/open_ems/core/constraints.py:164 + migrations/versions/0011_…py:66] `Field(gt=0.0)` permits `+inf`; CHECK `peak_limit_kw > 0` also passes for `inf`. The constraint only fails when `_serialize_config_value` runs `json.dumps(..., allow_nan=False)` — and by then `INSERT INTO active_constraints` already happened (then rolls back). Fix: add `allow_inf_nan=False` to the Pydantic `Field` on `ConstraintDraftInput.peak_limit_kw` (and `battery_reserve_floor_percent`, `ActiveConstraintsInput.peak_limit_kw` / `.battery_reserve_floor_percent` for symmetry).

- [x] [Review][Patch] **P9 — `_check_capability_strategy` WARN copy claims "Step 2 must complete" when Step 2 IS complete (LOW).** [src/open_ems/services/constraints.py:383–392] The grid_meter-missing WARN fires only AFTER the step-2 gate succeeded (i.e., Step 2 was finalized with an acknowledged "no grid meter" gap). The copy "Step 2 must complete before constraints take effect" is misleading in that scenario. Fix: rewrite to "No grid meter is assigned, so the peak limit will not be enforced." (mirrors the battery/ev-charger WARN wording).

- [x] [Review][Patch] **P10 — `get_or_default` 500s if provider not hydrated (LOW).** [src/open_ems/services/constraints.py:124–133] If `provider.get()` raises `RuntimeError("called before hydrate")`, the GET route surfaces a 500 instead of 503. Fix: wrap the `provider.get()` in a try/except mapping `RuntimeError` to a typed exception the route handler translates to 503 with an exact-match reason.

- [x] [Review][Patch] **P11 — `# type: ignore[arg-type]` on `overall_status` (LOW).** [src/open_ems/services/constraints.py — `_run_validation`] `overall = "failed" if any(...) else "valid"` is typed `str`, not `Literal[...]`. Fix: replace with branch-returns or a `cast(ConstraintOverallStatus, overall)` so the type-checker stays honest.

- [x] [Review][Patch] **P12 — Step-gate POST routes (HIGH, resolved from D1).** [src/open_ems/web/routes/setup.py — `post_upsert_constraint_draft`, `post_validate_constraint_draft`, `post_activate_constraints`] All three POSTs gain step-prerequisite checks. If `wizard_state.step_1_complete=0` OR `step_2_complete=0` → return 400 with exact-match `step_prerequisites_not_met: step_1_complete={x} step_2_complete={y}` (mirrors the existing service-level check on activate). If `step_3_complete=1` → return 400 with new exact-match `step_3_already_complete`. Lift the prerequisite check out of `ConstraintsService.activate_draft` and into a shared helper (or keep both — defense in depth) so /draft and /validate share the same gate.

- [x] [Review][Patch] **P13 — Atomic 3-table activation transaction (HIGH, resolved from D2).** [src/open_ems/storage/repositories/config_repo.py `_activate_locked` + src/open_ems/services/constraints.py `activate_draft`] Move `set_step_3_complete_locked` (wizard advance) and `draft_repo.delete_locked` (draft cleanup) INSIDE `_activate_locked`'s `BEGIN IMMEDIATE` transaction so all writes across `active_constraints` + `config_audit_log` + `wizard_state` + `draft_constraints` either all commit or all roll back. This requires either (a) passing `wizard_repo` + `draft_repo` into `_activate_locked` as parameters, or (b) accepting in-transaction callback hooks on `_activate_locked`. Pick (a) for explicitness — the service builds the call with all four repos. Update R3 cold-start case 4 to reflect that mid-activate restart can no longer leave wizard_state behind. Update tests in `test_setup_constraints_e2e.py` / `test_constraints_service.py` that exercise partial-commit recovery — that path now collapses to "transaction aborts; user retries cleanly".

- [x] [Review][Patch] **P14 — Tolerate CASCADE race on FAIL-persist (HIGH, resolved from D3).** [src/open_ems/services/constraints.py `activate_draft` step 5 + src/open_ems/storage/repositories/draft_constraints_repo.py `record_validation_outcome_locked`] Wrap the `record_validation_outcome_locked` call on the FAIL path in a guard: if the helper raises `ValueError` because the row was concurrently CASCADE-deleted, log `validation_fail_persist_skipped_session_gone` at WARN level and still raise the contractual `ConstraintActivationError("constraints_validation_failed", report=report)`. The user is logged out anyway (the session is gone); the persisted draft state is moot. Alternative: change `record_validation_outcome_locked` to return a bool (rowcount > 0) instead of raising, and let the service decide — slightly cleaner.

- [x] [Review][Patch] **P15 — `safety_pre_check` WARNs on unknown device state (MEDIUM, resolved from D4).** [src/open_ems/services/constraints.py `_check_safety_pre`] When `snapshot.battery is None AND draft.battery_reserve_floor_percent > 0`, emit `field='battery_reserve_floor_percent'`, `status='warn'`, `message='Battery state is not yet known. The reserve floor will take effect once the battery reports SoC; if current SoC is below the floor, discharge will be blocked immediately.'` Symmetric for grid_meter when peak_limit_kw is finite: `field='peak_limit_kw'`, `status='warn'`, `message='Grid meter state is not yet known. The peak limit will take effect once telemetry arrives.'` Stack with existing populated-snapshot WARNs (relies on P16 multi-WARN support).

- [x] [Review][Patch] **P16 — Multi-WARN per check (LOW, resolved from D5).** [src/open_ems/services/constraints.py `_check_capability_strategy`, `_check_safety_pre`, `_run_validation`] Change the signature of `_check_capability_strategy` and `_check_safety_pre` to return `tuple[ConstraintCheckResult, ...]`. Each function emits ONE entry per gap (so simultaneous missing-battery AND missing-ev_charger AND ev_window-set surfaces both rows). `_run_validation` flattens by extending the `checks` list with each tuple. `ConstraintValidationReport.checks` already declares `tuple[ConstraintCheckResult, ...]`, so the report shape is unchanged from the caller's perspective. Snapshot-ordering invariant preserved (schema → capability_strategy → safety_pre_check); the within-check rows may appear in deterministic per-check order. Update unit tests in `test_constraints_service.py` to assert multiple rows where applicable.

#### Deferred (real but not actionable in 9.3)

- [x] [Review][Defer] **W1 — `_build_audit_changes(None, new)` doesn't read prior value from `config_audit_log` when `active_constraints` was manually purged (HIGH).** [src/open_ems/storage/repositories/config_repo.py:121–144] An operator-recovery scenario (DBA DELETEs active_constraints rows but keeps audit history) produces an audit row with `previous_value={"kw": None}` even though the actual predecessor exists in `config_audit_log`. Cross-table `next_version` correctly preserves monotonicity but `previous_value` is asymmetric. Deferred: operator-purge recovery is not in 9.3 scope; reading audit-log for derived `previous_value` adds complexity that warrants its own story.

- [x] [Review][Defer] **W2 — Successful re-validation PASS is consumed in-memory and not persisted before the draft is deleted (HIGH).** [src/open_ems/services/constraints.py `activate_draft` step 5 + step 10] The FAIL path calls `record_validation_outcome_locked` and persists the report; the PASS path skips persistence and proceeds to delete the draft at step 10. Audit/observability tools cannot retrieve the validation report that gated the activation. Deferred: depends on Story 9.4's deployment-validation read path — confirm whether 9.4 needs the persisted PASS report or only the `config_audit_log` rows.

- [x] [Review][Defer] **W3 — `record_validation_outcome_locked` exception on the FAIL persistence path masks the underlying validation FAIL (LOW).** [src/open_ems/services/constraints.py:240–248] If the in-lock UPDATE fails (transient DB error), the user sees a 500 instead of the contractual `constraints_validation_failed` 400 with the report payload. Rare path; deferred.

- [x] [Review][Defer] **W4 — Validate button does not auto-save the current form values before validating (LOW).** [src/open_ems/web/templates/installer/_setup_constraints_form.html:91–97] User must click "Save draft" then "Validate" — clicking "Validate" without first saving returns `draft_not_found_for_session`. Spec is silent; current two-click flow is contractual. Deferred to a UX-polish iteration.

#### Dismissed (8 — noise / false positive / handled elsewhere)

Counts only: first-activation provider/DB previous-source disagreement (intended by 9.0b); `_activate_locked` private-member access (documented in spec dev note line 484); `web/app.py` "Step 4f" comment vs spec "Step 4e+" (cosmetic); `_form` template key dead code (defended by route-level paired-NULL guard); wizard_state read-side validator brick-on-tamper (defense-in-depth is design); `_decode_report` JSONDecodeError vs ValueError (type-consistency only); test fixture rebinds private attribute (contract still validated); tampered wizard_state grid_meter WARN (overlaps P9).

#### Round 2 — Re-review findings (2026-05-11)

_Second adversarial pass triggered by user concern about regression risk in integration seams (transactional guarantees, version drift + reload ordering, HTMX/state sync, validation persistence asymmetry, wizard/session race conditions, cross-table consistency under rollback). 3-layer review (Blind Hunter + Edge Case Hunter + Acceptance Auditor) on full `bc74d72..c122aec` diff; 89 raw findings consolidated to 49 unique items after deduplication._

##### Decision-needed (Round 2 — resolved 2026-05-11)

- [x] [Review][Decision] **R2D1 → R2P16** — Update AC6 + R1 #3 case (b) + R3 forbidden state #4 narratives to match P13's atomic 4-table shape. Folds into the bundled narrative-sync patch R2P16. (Option A.)
- [x] [Review][Decision] **R2D2 → R2P20** — Treat `config_version=0` activation as bootstrap-eligible regardless of field diff (auto-promote 0 → 1 even with no changes). Becomes new patch R2P20 below. (Option A.)
- [x] [Review][Decision] **R2D3 → R2P21** — Add upper bound `le=63.0` on `peak_limit_kw` (three-phase 230V × ~91A residential ceiling); rejecting scientific notation in form input was NOT selected. Becomes new patch R2P21 below. (Option A with bound = 63.0 kW.)

##### Patches (Round 2 — unambiguous fixes)

- [x] [Review][Patch] **R2P1 [HIGH] — `record_validation_outcome_locked` is not transaction-neutral despite the `_locked` name.** Helper unconditionally calls `await self._conn.commit()` at start and end; sibling `_locked` helpers (`delete_locked`, `set_step_3_complete_locked`) accept `commit=False`. FAIL-path persist commits outside the activate txn, so a crash between the UPDATE and the `raise` leaves `validation_status='failed'` persisted with no user-visible feedback. Non-`ValueError` exceptions (encoding error on `json.dumps`, transient DB I/O) bubble as 500. Fix: add `commit: bool = True` parameter mirroring sibling helpers; wrap `json.dumps` in try/except to raise typed error. [src/open_ems/storage/repositories/draft_constraints_repo.py:_record_validation_outcome_locked] [Sources: blind #1, #2; edge #2, #17; auditor #10]
- [x] [Review][Patch] **R2P2 [HIGH] — Wizard CASCADE race on happy path → 500.** `_in_txn_post_writes` callback's `set_step_3_complete_locked` raises bare `ValueError("No wizard_state row...")` when rowcount==0 (admin purges session mid-activate after `assert_step_prerequisites` passes). `activate_draft` only catches `NoChangedFieldsError`; the ValueError propagates as 500 with raw trace. P14 from round 1 handled this for the FAIL-persist branch only; the happy-path callback was missed. Fix: catch the rowcount==0 ValueError inside `_in_txn_post_writes`, re-raise as `ConstraintActivationError("session_gone_during_activate")`; route renders 400/410 with error envelope. [src/open_ems/services/constraints.py:activate_draft step 7 + storage/repositories/wizard_state_repo.py:set_step_3_complete_locked] [Sources: blind #14; edge #3, #11]
- [x] [Review][Patch] **R2P3 [HIGH] → carried to sub-story `9-Y-a-e2e-fail-abort-coverage`. R2P3 [HIGH] — E2E test `test_e2e_live_state_shift_aborts_activation_with_safety_fail` does not exercise the FAIL-abort path.** Test name promises FAIL-abort end-to-end; body uses `peak_limit_kw=30` (no structural FAIL) with grid at 50 kW (AC7 classifies as WARN, not FAIL); asserts `activate.status_code == 302` (success). The actual abort-on-fresh-FAIL path has NO end-to-end coverage; only unit-level coverage via `test_activate_draft_re_validates_inside_lock` (which itself tests the WARN path — final assertion is `result.config_version == 1`, success). The most load-bearing AC of the activate route is unexercised. Fix: rewrite the test to hit a real structural FAIL (e.g. `peak_limit_kw=0.4` below the 0.5 kW threshold, OR capability lost between validate and activate); assert HTTP 400, `validation_status='failed'` persisted, no `active_constraints` row written. [tests/integration/web/test_setup_constraints_e2e.py] [Source: blind #11]
- [x] [Review][Patch] **R2P4 [MED] — `previous_snapshot` read OUTSIDE the write lock → phantom `changed_fields` structlog payload on concurrent activate.** `self._snapshot_safe()` in `activate_draft` runs before `async with get_write_lock():`. A concurrent activator landing between snapshot read and lock acquisition produces a structlog `constraints_activated.changed_fields` payload reporting fields the OTHER activation actually changed. Audit log is correct (reads `get_active()` inside transaction); only observability lies. P4 from round 1 moved the read out of the lock to dodge the post-reload trap and missed the symmetric pre-lock race. Fix: move `_snapshot_safe()` inside the write lock. [src/open_ems/services/constraints.py:activate_draft] [Sources: blind #13; edge #53]
- [x] [Review][Patch] **R2P5 [MED] — HX-Redirect missing on activate route + double-click 400.** Activate is plain `method="post"` today; rest of the wizard is HTMX so a future `hx-post` migration breaks the redirect silently. Separately, a double-click after success → second POST hits `step_3_already_complete` → 400 error banner instead of a redirect-to-validation. Fix: detect `HX-Request` header in the route; return `204 + HX-Redirect: /installer/setup/validation` for HTMX clients; on `step_3_already_complete` immediately after first POST succeeded, treat as success-redirect rather than 400. [src/open_ems/web/routes/setup.py:post_activate_constraints] [Sources: blind #7; edge #26, #27]
- [x] [Review][Patch] **R2P6 [MED] — `_check_safety_pre` FAIL fast-return hides downstream WARNs (regression of P16's intent).** Structural FAIL (`peak_limit_kw < 0.5 kW`) causes early return; installer never sees concurrent battery-reserve or capability WARNs until they fix peak first → multi-round-trip. P16 from round 1 explicitly added multi-WARN-per-check; the FAIL fast-return reintroduces single-result behavior for exactly the case where simultaneous safety issues matter most. Fix: accumulate FAIL + WARNs in one result tuple; no early return on FAIL. [src/open_ems/services/constraints.py:_check_safety_pre] [Source: blind #15]
- [x] [Review][Patch] **R2P7 [MED] — AC7 schema-check `field='ev_charging_window'` mandate not deterministically produced.** AC7 check 1 mandates `field='ev_charging_window'` (unified) for paired-NULL violations. `_check_schema` derives field from `Pydantic loc[0]`: model-level validator gives `loc=()` → field becomes `None`; field-level format gives `loc=('ev_charging_window_start'|'_end',)`. Neither matches the AC contract. Fix: detect model-level error type (paired-NULL invariant) and normalize `field='ev_charging_window'`; for format-only errors on either start or end, also normalize. [src/open_ems/services/constraints.py:_check_schema] [Source: auditor #3]
- [x] [Review][Patch] **R2P8 [MED] → carried to sub-story `9-Y-c-ev-window-form-ux`. R2P8 [MED] — EV window error-path UX gaps: synthesized values, absent vs cleared ambiguity, Pydantic-error slot routing.** Float fields preserve user raw via `raw_*_text` (P3 round 1); EV window does not — `<input type="time" value="25:99">` is silently emptied by most browsers. Empty-string sentinel makes "field omitted from form" indistinguishable from "explicitly cleared" → re-saving the form silently nulls a previously-set window. Pydantic non-paired-NULL model error with `loc=()` is routed to the `ev_charging_window` slot unconditionally → future model validators silently land under the wrong field in the UI. Fix: (a) add `raw_ev_charging_window_start_text` / `_end_text` to `ConstraintDraftView` mirroring `raw_peak_limit_kw_text`; (b) inspect Pydantic error `type` before slot assignment; (c) introduce sentinel handling for absent vs cleared. [src/open_ems/web/routes/setup.py:_form_view_from_request + _setup_constraints_form.html] [Sources: blind #12; edge #23, #24]
- [x] [Review][Patch] **R2P9 [MED] — OOB swap fragility on validate response.** `hx-swap-oob="innerHTML"` for `#constraints-activate-region` requires the target to exist in current DOM; an upstream swap removing the container leaves a stale activate button alongside the fresh validation report, defeating the documented "atomic transition" claim. Separately, if the draft is deleted between `validate_draft` returning and the OOB-include `get_or_default` read, the include dereferences a None view → `TemplateError`. Fix: guard the `render_activate_oob` block with `{% if view and view.has_persisted_draft %}`; add an integration test asserting the OOB target exists when the validate fragment is served. [src/open_ems/web/templates/installer/_setup_constraints_validation.html + routes/setup.py:post_validate_constraint_draft] [Sources: blind #6; edge #28]
- [x] [Review][Patch] **R2P10 [MED] — Timestamp divergence: `active_constraints.activated_at` vs `wizard_state.step_3_completed_at`.** `_activate_locked` captures `now_utc = datetime.now(UTC)` internally for the active row INSERT; service callback uses the route's `now` from `_now()` for the wizard row UPDATE. Same activation, two timestamps, despite spec docstring claim of "single moment". Tests do not pin the equality. Fix: thread one `now` through; pass it into `_activate_locked` from the service so all writes use the same instant. [src/open_ems/storage/repositories/config_repo.py:_activate_locked + services/constraints.py:activate_draft] [Source: blind #3]
- [x] [Review][Patch] **R2P11 [MED] — Battery state edge cases silently skip the reserve-floor WARN.** `BatteryState.soc_percent` can be `NaN` from a sensor read error before staleness kicks in; `NaN > x` evaluates False → reserve-floor WARN never fires. If `snapshot.battery` is `DegradedDeviceState` (not None, not BatteryState), neither branch fires → no WARN even though battery is unhealthy. Fix: explicit `isinstance(snapshot.battery, BatteryState)` check; explicit `math.isnan(soc)` guard; emit the P15 "battery state unknown" WARN otherwise. [src/open_ems/services/constraints.py:_check_safety_pre] [Sources: edge #5, #6]
- [x] [Review][Patch] **R2P12 [MED] → carried to sub-story `9-Y-d-migration-0011-populated-round-trip-and-idempotency`. R2P12 [MED] — Migration 0011 round-trip not tested with populated data + upgrade idempotency.** `test_migration_0011_round_trips` runs against an empty DB; populated `active_constraints` rows with non-NULL EV window plus a non-empty `draft_constraints` row are unexercised during downgrade. Separately, re-running `upgrade head` on a DB already at 0011 fails with duplicate-column error (operator recovery scenario). Fix: (a) extend the round-trip test with a populated-data variant; (b) document non-idempotency OR add column-existence guards to the upgrade. [migrations/versions/0011_add_constraint_configuration_tables.py + tests/integration/test_migrations.py] [Sources: blind #16; edge #39, #40]
- [x] [Review][Patch] **R2P13 [MED] — SQLite `OperationalError` ("database is locked") and `rollback()` failures surface as 500.** `_activate_locked` catches generic Exception, rolls back, re-raises; no specific mapping for `aiosqlite.OperationalError`. Separately, `self._conn.rollback()` itself can raise (connection died) and masks the original exception. Fix: catch `OperationalError` specifically, map to `ConstraintActivationError("activation_busy")` with retry semantics; log-and-swallow rollback exceptions so the original surfaces. [src/open_ems/storage/repositories/config_repo.py:_activate_locked] [Sources: edge #12, #13]
- [x] [Review][Patch] **R2P14 [MED] — Error envelope rendering drops `exc.report`; `report` variable referenced in include paths where undefined.** `_render_constraints_error_envelope` discards `exc.report` if present → check-level errors hidden from user, only top-level reason shown. The validation fragment references `report` even when included from envelope paths where the variable was never set. Fix: thread `exc.report` into the envelope render context; add `{% if report is defined %}` guard. [src/open_ems/web/routes/setup.py:_render_constraints_error_envelope + templates/installer/_setup_constraints_validation.html] [Sources: edge #35, #36]
- [x] [Review][Patch] **R2P15 [MED] — Activate button can be enabled with `validation_status='valid'` but `report=None`.** Concurrent draft delete + re-upsert can produce a transient state where the row carries `status='valid'` but `validation_report` was reset to None. The template enables the button on status alone; user clicks Activate against a non-existent report. Fix: gate template button on `status=='valid' and report is not None`. [src/open_ems/web/templates/installer/_setup_constraints_activate.html] [Source: edge #38]
- [x] [Review][Patch] **R2P16 [MED — narrative] → carried to sub-story `9-Y-b-ac-and-r-artifact-narrative-sync`. R2P16 [MED — narrative] — Sync AC text and R-artifact narratives to post-P12/P13 implementation.** Bundle of contract-narrative drifts: AC6 "Idempotency" paragraph still describes the pre-P12 `draft_not_found_for_session` outcome (now `step_3_already_complete`); AC7 grid_meter message text says "Step 2 must complete" (P9 rewrote to "No grid meter is assigned, so the peak limit will not be enforced"); AC8 confirmation `aria-label` says "Confirmation" (code uses "Activation confirmation"); AC9 lifespan step says "4e+" (code labels "4f"); R1 risk #3 case (b) is now structurally impossible post-P13; R3 forbidden state #4 narrative claims an order-of-write that P13 invalidates; the validation fragment merges UX components #9 and #10 (AC8 separates them). Fix: single coordinated AC + R-artifact text update. [Sources: auditor #2, #4, #5, #8, #11, #12, #16]
- [x] [Review][Patch] **R2P17 [LOW] → DISMISSED 2026-05-11 as FALSE POSITIVE on re-read. The auditor's claim was that the inline test-fixture DDL for `draft_constraints` omits the `validation_status` CHECK constraint; the actual file (`tests/unit/storage/repositories/test_draft_constraints_repo.py:40`) DOES declare `CHECK (validation_status IN ('pending', 'valid', 'failed'))`. Round-2 review dismissed. R2P17 [LOW] — Test fixture inline DDL for `draft_constraints` missing `validation_status` CHECK constraint.** Fixture in `test_draft_constraints_repo.py` (~line 6158) diverges from migration 0011 DDL: it omits the `CHECK (validation_status IN ('pending','valid','failed'))` constraint. Test could pass a row with `validation_status='garbage'` even though production rejects it at the DB layer. Fix: add the CHECK to fixture DDL OR use Alembic-applied schema in setup. [Source: auditor #9]
- [x] [Review][Patch] **R2P18 [LOW] — Missing route-level tests for `POST /draft` and `POST /validate` returning `step_3_already_complete` after step-3 completion.** Service layer asserts the contract; route layer has unit tests only for GET short-circuit and prerequisite-not-met (steps 1/2). Fix: add two route-level tests in `test_setup_constraints_routes.py`. [Source: auditor #14]
- [x] [Review][Patch] **R2P19 [LOW] — Single-`config_version`-increment invariant: `active_constraints` row presence at version N not cross-joined with N audit rows in a single test.** `test_all_three_field_change_shares_single_config_version` queries `config_audit_log` only; presence of the matching `active_constraints` row at the same `config_version` is verified by separate tests. R2 line 630 invariant ("all audit rows + the active_constraints row share one config_version") is asserted only one-sidedly. Fix: extend the test with a cross-table join assertion. [Source: auditor #13]
- [x] [Review][Patch] **R2P20 [MED — resolved from R2D2] → carried to sub-story `9-Y-e-bootstrap-activation-config-version-promote`. R2P20 [MED — resolved from R2D2] — Cold-start activation bootstrap: auto-promote `config_version=0 → 1` even with no field diff.** On first-ever activation when the DB-seeded `active_constraints` row at `config_version=0` exactly matches the user's draft, `activate()` raises `activate_called_with_no_changed_fields` and the installer cannot complete Step 3. Fix: in `ConfigRepo._activate_locked` (or in `activate_draft` orchestration), short-circuit the `NoChangedFieldsError` guard when the current `active_constraints.config_version == 0` (i.e. the row is the seed). Emit at least one synthetic audit row (e.g. `field='bootstrap'`, `previous_value=None`, `new_value={"config_version_promoted_from": 0}`) so the audit trail records the bootstrap. Update unit test in `test_config_repo.py` to assert bootstrap path; add E2E test that activates fresh-install defaults without prior edit. [src/open_ems/storage/repositories/config_repo.py:_activate_locked + services/constraints.py:activate_draft] [Source: edge #45 resolved by R2D2.A]
- [x] [Review][Patch] **R2P21 [MED — resolved from R2D3] → carried to sub-story `9-Y-f-peak-limit-upper-bound-and-migration-0012`. R2P21 [MED — resolved from R2D3] — Add upper bound `le=63.0` on `peak_limit_kw` (three-phase 230V × ~91A residential ceiling).** `Field(gt=0.0, allow_inf_nan=False)` blocks NaN/inf but accepts arbitrarily-large magnitudes. Fix: extend the Pydantic `Field` on `ConstraintDraftInput.peak_limit_kw` AND `ActiveConstraintsInput.peak_limit_kw` AND the seed default in `Settings` to `Field(gt=0.0, le=63.0, allow_inf_nan=False)`. Add a DB-level `CHECK (peak_limit_kw > 0 AND peak_limit_kw <= 63.0)` on `active_constraints.peak_limit_kw` and `draft_constraints.peak_limit_kw` (migration 0012 to add the upper-bound CHECK; or amend if 0011 is not yet released — since 9.3 is the most recent migration, amend 0011's CHECK in-place is acceptable). Update spec-mandated AC7 schema-check error message to mention the 63 kW ceiling. Add unit tests at the boundary: `63.0` passes, `63.01` rejects via Pydantic, raw INSERT of `100.0` rejects via DB CHECK. Scientific-notation rejection at the form-parse layer was NOT selected. [src/open_ems/core/constraints.py:ConstraintDraftInput + ActiveConstraintsInput + migrations/versions/0011_*] [Source: edge #22 resolved by R2D3.A]

##### Deferred (Round 2)

- [x] [Review][Defer] **R2W1 — `assert_step_prerequisites` not called by `activate_draft` (duplicated inline).** P12 from round 1 allowed "keep both for defense in depth"; refactor when convenient. [auditor #7]
- [x] [Review][Defer] **R2W2 — `_changed_fields` and `_build_audit_changes` are two implementations of "what counts as changed".** Correct today; drift risk for a one-sided future edit. [blind #10]
- [x] [Review][Defer] **R2W3 — Schema drift can poison a persisted draft permanently.** A future Pydantic input tightening leaves a stale draft un-validatable; no auto-clear path through the wizard. Future-tense. [blind #4]
- [x] [Review][Defer] **R2W4 — `_check_capability_strategy` lock-free read; duplicate-role race.** Re-validation inside the lock is authoritative; transient observability mismatch only. [blind #17, edge #7]
- [x] [Review][Defer] **R2W5 — Concurrent two POSTs in same session — lost update on draft.** Wizard is single-session in practice; multi-tab support is not yet a feature. [edge #9]
- [x] [Review][Defer] **R2W6 — Float-equality in `_build_audit_changes` for peak/reserve.** `math.isclose` could replace `!=`; low-probability binary-different-but-display-identical floats. [edge #32]
- [x] [Review][Defer] **R2W7 — Tampered DB row resilience (corrupt JSON, malformed ISO, non-0/1 step_3 flag, `str(row[5])` Literal bypass).** Defense-in-depth; not in 9.3 scope. [blind #5; edge #14, #18, #20]
- [x] [Review][Defer] **R2W8 — Lock-order documentation (write_lock → provider._lock; never reverse).** Future-tense documentation hardening. [edge #52]
- [x] [Review][Defer] **R2W9 — `set_step_3_complete_locked` updates `updated_at` on idempotent retry.** Observability only; obscures last-real-edit timestamp. [edge #19]
- [x] [Review][Defer] **R2W10 — FAIL-persist exception coverage extends only to `ValueError`, not `IntegrityError` for FK CASCADE race.** Continuation of W3 from round 1, explicitly deferred. [auditor #10]
- [x] [Review][Defer] **R2W11 — Validate POST info-leak: whether a draft exists is observable post-completion via differing rejection reasons.** Negligible information disclosure to scripted callers; browser path is gated by GET short-circuit. [blind #9]
- [x] [Review][Defer] **R2W12 — `_in_txn_post_writes` callback's lock-hold duration could trigger SQLite `OperationalError` on contention.** Long DB I/O inside `BEGIN IMMEDIATE`; concurrent readers may see HTMX 504. [edge #55]
- [x] [Review][Defer] **R2W13 — HH:MM validator inconsistencies: `start==end` allowed; whitespace handling diverges (service strips; model `fullmatch` rejects unstripped).** Add `start != end` validator OR document equal-endpoint semantic. [edge #46, #47, #48]
- [x] [Review][Defer] **R2W14 — `ProviderNotReadyError` 503 missing `Retry-After` header.** Clients can't be advised to back off; observability noise during startup. [edge #25]
- [x] [Review][Defer] **R2W15 — Pydantic `errors[0]` non-dict edge — `first[0]` indexing on non-indexable raises IndexError.** Pydantic v2 internal shape; very rare. [edge #43]
- [x] [Review][Defer] **R2W16 — AC10 partial: no unit test asserts `role="alert"` on populated validation-message div.** a11y contract has zero unit coverage; the placeholder a11y test is `xfail`. [auditor #15]
- [x] [Review][Defer] **R2W17 — Wizard `_row_to_state` positional index shift after adding step_3 columns.** Internal-only; legacy code reading wizard_state by positional index breaks silently. [edge #42]

##### Dismissed (Round 2 — 20 — counts only)

`setup_constraints_placeholder.html` rename vs spec "DELETE" (functional rename, spec-wording quirk); `peak_limit_kw == 0.5` exact boundary (well-defined inclusive/exclusive); `draft == provider` exact match with EV-only change (handled correctly by `_build_audit_changes`); concurrent reload version-replace (drift log is informational, not a false positive); `inf`/`NaN` already guarded by `allow_inf_nan=False` (P8 round 1); `raw_*_text` XSS theoretical (autoescape on, no `|safe` filter); provider-not-hydrated drift log (impossible in production lifespan); `0.00` default if provider seed=0 (first-page state, not data integrity); clock-skew `activated_at` ordering (minor); `assert_step_prerequisites` with None wizard (wizard required by prior steps); validate returning 200 with `overall='failed'` (intentional per spec); `_changed_fields` combined slot for EV window (intentional aggregation); EV-window combined audit row (intentional, mirrors 9.2 paired-NULL pattern); `delete_locked` no-op on missing row (intentional); raw `IntegrityError` from CHECK violation in `upsert` (Pydantic validates first, DB-level violation indicates upstream gap); raw `IntegrityError` from FK violation in `upsert` (auth gates session existence); validation template merging UX components #9 + #10 (also covered in R2P16); reload-after-no-change (idempotent no-op); `_decode_report` JSONDecodeError type-consistency only; tests-pass-with-WARN under populated-snapshot fixtures (intentional per spec).

## Dev Notes

### Source-tree alignment

- `migrations/versions/0011_add_constraint_configuration_tables.py` — new; migration style precedent is `0010_add_role_to_device_registry.py` (extends multiple tables in one migration).
- `src/open_ems/storage/repositories/draft_constraints_repo.py` — new; mirrors `wizard_state_repo.py` shape (`frozen=True, extra="forbid"` model + per-session async methods + write-lock-protected mutations).
- `src/open_ems/storage/repositories/config_repo.py` — extended (`_build_audit_changes` adds EV window handling; the `activate()` body is otherwise unchanged because the BEGIN IMMEDIATE transaction already encloses the audit emission).
- `src/open_ems/storage/repositories/wizard_state_repo.py` — extended with `set_step_3_complete` + `set_step_3_complete_locked` following the 9.2 idempotent-step pattern.
- `src/open_ems/storage/repositories/config_audit_repo.py` — `SAFETY_RELEVANT_CONFIG_FIELDS` gains `'ev_charging_window'`.
- `src/open_ems/services/constraints.py` — new; consistent with `services/role_assignment.py` (frozen dataclass outcomes; depends on multiple repos + the provider + the StateStore).
- `src/open_ems/web/routes/setup.py` — extended; the new routes share `_device_repo` / `_wizard_state_repo` / `_constraints_service` Depends helpers.
- `src/open_ems/web/templates/installer/` — new templates: `setup_constraints.html`, `_setup_constraints_form.html`, `_setup_constraints_validation.html`, `_setup_constraints_activate.html`, `_setup_constraints_error_banner.html`, `_setup_constraints_error.html`, `setup_validation_placeholder.html`. Delete `setup_constraints_placeholder.html`.
- `src/open_ems/web/app.py` — lifespan step 4e+ extension.
- `src/open_ems/core/constraints.py` — `ActiveConstraintsInput` + `ActiveConstraints` gain EV window optional fields.

### Why draft state lives in a dedicated `draft_constraints` table (not on `wizard_state`)

BAD-3 (architecture.md §1112–1156) explicitly defines a `draft_constraints` table separate from `active_constraints`, with the note "single-row staged-draft companion is 9.3's responsibility" (Story 9.0b implementation note line 1154). The competing pattern — JSON-encoded draft fields on `wizard_state` (9.2's `step_2_acknowledged_gaps` precedent) — is rejected here because:

1. **Validation status + report require their own row state.** The draft carries a `validation_status` + `validation_report` JSON; storing these on `wizard_state` would mix per-step lifecycle (step_3_complete) with per-validation lifecycle (validation_status), and a future Step 4 might re-use parts of the report. A dedicated table makes the validation lifecycle obvious.
2. **UPSERT semantics.** Every draft edit replaces the previous draft AND resets `validation_status='pending'`. `INSERT OR REPLACE` on `(session_id)` is the cleanest expression; `wizard_state` column writes would require nullable per-field columns AND a separate `step_3_validation_*` set.
3. **BAD-3 spec conformance.** The architecture document literally names the table; honouring named architectural surfaces avoids cumulative drift between the doc and the code.

### Why EV window goes into `active_constraints` (not a separate preferences table)

The epic story spec ("Given the installer is on Step 3 ... entering values (peak consumption limit, battery reserve floor, EV charging window preference)") groups the three values into a single staged-validate-activate flow. AC1 of the epic ("`config_version` is incremented exactly once for this activation, regardless of how many constraint fields changed") + AC1 ("a `config_audit_log` row is written for each changed field, all referencing the same new `config_version`") collectively require that an EV-window-only change still increments `config_version` and emits an audit row. Either:

- (a) EV window lives in `active_constraints` and inherits the existing single-version + audit machinery — chosen here.
- (b) EV window lives elsewhere, and Story 9.3 builds parallel single-version + audit machinery — rejected (duplication and drift risk).

The two new columns on `active_constraints` are nullable so 9.0b's cold-start seed path keeps working: when no DB row exists, the provider seeds `peak_limit_kw` / `battery_reserve_floor_percent` from Settings and EV window stays NULL. NULL EV window means "no preference"; `ev_scheduling.py` (future story) treats NULL the same as "no charging window configured" and schedules opportunistically.

### Why no `Settings.ev_charging_window_*` field is added

The seed-only-Settings enforcement test (`tests/unit/test_settings_seed_only.py`) is unchanged. EV window has no Settings counterpart because the cold-start default for EV window is NULL (no preference) — there is no operationally meaningful seed value. The architecture's "cold-start seed only" doctrine (9.0b AC9) applies specifically to the two safety values that PolicyGuard reads on the hot path; EV window is a scheduling preference, not a hot-path safety value.

### Provider reload contract

`ActiveConstraintsProvider.reload()` is the single mechanism to refresh the runtime in-memory snapshot. Story 9.3's activate route is the ONLY new caller. The reload is:
- **Change-triggered** (not TTL-based) — the route calls reload exactly once per successful activate.
- **Idempotent** — a reload after no DB change is a no-op (the snapshot's `config_version` already matches the DB).
- **Failure-tolerant** — a reload failure does NOT roll back the activation; the DB is authoritative and the next process restart re-hydrates correctly. The narrow window between DB commit and reload completion is the "documented narrow drift" called out in R3 / R6.
- **Inside the write lock** — called between `ConfigRepo.activate()` commit and `set_step_3_complete_locked` so the route's response correctness depends on a successful reload (or a logged + tolerated failure).

### Adapter wiring is explicitly OUT OF SCOPE

The Story 8-2 deferred-work item (`adapters={}` in production silently rejects all commands) lists Stories 9.2 + 9.3 as the boundary for the fix. **9.3 does NOT close that gap.** Constraint configuration is purely configuration persistence + UI + runtime in-memory provider refresh. Wiring adapters into PolicyGuard requires:
1. Adapter discovery → connection management (already partially implemented in `adapters/discovery.py`).
2. Lifespan-time construction of an adapter map keyed by `DeviceRole` (or `device_id` — the architecture pattern requires clarification first).
3. Connection lifecycle management (open, retry on disconnect, close on shutdown).
4. Possibly hot-reloading the adapter map when devices are reassigned in Step 2.

None of those steps is constraint-configuration scope; making them part of 9.3 would push the story over the A2 risk threshold even further. The 9.2 retro / R7 already classified this concern as `acceptable-post-this-story` and the 9.3 R7 below does the same. Recommend a dedicated follow-up story (provisional name: "9-X-wire-adapter-map-into-policy-guard-and-control-loop") between 9.4 and 9.5 OR as part of Epic 10/11 operational hardening.

### Library / framework notes

- No new third-party dependencies. JSON serialization uses the standard library `json` module. `HH:MM` validation uses a simple regex (`^([01]\d|2[0-3]):[0-5]\d$`) — no `datetime.strptime` round-trip needed.
- HTMX usage continues to follow the architecture-mandated server-rendered HTML + HTMX baseline; no client-side JS is required.
- Server-side authoritative validation is the architecture default; client-side HTML5 form validation is supplementary per UX spec component #8.
- The Pydantic model_validator pattern for paired-NULL invariants is established by Story 9.2 (`DeviceRegistryEntry` `role` / `role_assigned_at`).

### Previous-story intelligence — what we carry forward

- **Story 9.0b's `ConfigRepo.activate()` is the activation primitive.** No replacement, no parallel implementation. The 9.3 activate route is a thin orchestrator around it.
- **Story 9.0b's `ActiveConstraintsProvider.reload()` is the reload primitive.** Story 9.3 calls it exactly once per activation, inside the write lock, after DB commit.
- **Story 9.1's R6 (`get_profile()` re-resolution at render time):** the capability_strategy validation check re-resolves capability profiles per-device at validate time. Profiles can shift between draft creation and activate; re-validation inside the lock is the structural guard.
- **Story 9.1 AC11 / 9.2 AC9 exact-match reason strings:** every rejection reason in 9.3's route handlers and service methods uses exact-match assertions in tests. No substring `in` assertions. Examples: `step_prerequisites_not_met: step_1_complete=1 step_2_complete=0`, `draft_not_found_for_session`, `activate_called_with_no_changed_fields`.
- **Story 9.2's idempotent advance pattern:** `set_step_3_complete_locked` preserves `step_3_completed_at` on idempotent re-call (mirrors `set_step_2_complete_locked`).
- **Story 9.2's TOCTOU close (review patch D1):** the activate route holds `get_write_lock()` across the prerequisite check → re-validate → `ConfigRepo.activate()` → `provider.reload()` → `set_step_3_complete_locked` → `draft_repo.delete()` sequence.
- **Story 9.2's HTMX out-of-band swap pattern:** the validate route returns the validation-report fragment as the primary swap AND the activate-button fragment via `hx-swap-oob` (so the disabled→enabled transition is atomic with the report update).
- **Story 9.1's setup_layout.html step indicator:** extended with the step-2 back-navigation hyperlink (mirrors 9.2's step-1 hyperlink). Same `not step_X_complete` conditional pattern.
- **Story 9.0c's structural fail-loud at boot:** 9.3 does NOT introduce a structural fail-loud (no startup gate). The provider hydration covers the runtime case; the wizard surface is post-startup.
- **Story 9.0b's write-lock pattern:** `ConfigRepo.activate()` already holds the lock; the route handler's outer `async with get_write_lock():` is the OUTER lock that the route owns, and `ConfigRepo.activate()` runs INSIDE that lock without re-acquiring (asyncio.Lock is not reentrant; 9.0b's `ConfigRepo.activate()` uses a private path for this case, OR we restructure so the route doesn't pre-acquire the lock and instead trusts `ConfigRepo.activate()` to acquire it). **CRITICAL IMPLEMENTATION NOTE:** asyncio.Lock is not reentrant; `ConfigRepo.activate()` acquires `get_write_lock()` internally. If the route handler also acquires the same lock outside, the inner `ConfigRepo.activate()` deadlocks. **Solution: factor `ConfigRepo.activate()` into a public wrapper that acquires the lock + a private `_activate_locked()` that doesn't.** Same pattern as Story 9.2's `set_step_2_complete` / `set_step_2_complete_locked` split. Add `_activate_locked` as part of Subtask 1.3, and update `activate()` to delegate.

### Audit semantics

This story emits structured logs (`constraints_draft_upserted`, `constraints_draft_validated`, `constraints_activated`, `constraints_reload_after_activate_failed`) AND `config_audit_log` DB rows (one per changed field per activation, the existing 9.0b/6.3 surface). It emits NO `event_log` audit rows — the FR20 event log is reserved for control decisions, constraint enforcement, degraded-mode transitions, recovery, and installer notes. Constraint activation is **configuration**, not a control decision; the `config_audit_log` is its proper home (architecture.md §342 — "All constraint changes ... are stored ... in a dedicated `config_audit_log` table").

### Project Structure Notes

- All new files map onto the architecture-defined directory layout (architecture.md §Project Structure):
  - `services/constraints.py` is a permitted addition under `services/` (mirrors `services/role_assignment.py` / `services/wizard_gate.py` precedent).
  - `web/routes/setup.py` is extended (not split); even with 9.3's additions the file remains under the architecture's 300-LOC informal threshold because routes share helpers extensively.
  - `web/templates/installer/` continues to host the wizard templates.
- No new top-level packages. No new third-party dependencies. `uv.lock` should be unchanged.
- Detected variance: architecture.md §Project Structure (line 909) lists `installer/setup.html` (singular). Story 9.1 established the multi-page pattern; 9.2 + 9.3 continue it. Same variance call-out as 9.2 — refinement of architecture's single-file sketch into the multi-page Jinja2 + setup_layout.html flow, not a contradiction.

### Orchestration Risk Analysis (A2-triggered — MANDATORY)

**A2 triggers matched:**
- **T1** (lifecycle / state-machine) — three state machines: `draft.validation_status` (`pending → valid → activated/deleted` / `pending → failed → pending`), `wizard_state.step_3_complete` (`0 → 1`, idempotent re-click), `active_constraints` (insert-only versioned history with `config_version` monotonically increasing). The activate route is a multi-state transition (re-validate → activate → reload → mark complete → delete draft) inside a single write-lock window.
- **T3** (persistence + recovery) — `active_constraints` rows + `config_audit_log` rows + `draft_constraints` rows + `wizard_state.step_3_*` columns all DB-backed; all must survive process restart; `ActiveConstraintsProvider` re-hydrates the in-memory snapshot from `active_constraints` on every boot (9.0b precedent inherited).
- **T6** (deployment / restart behavior) — installer mid-wizard page reload must restore: the draft form values, the most recent validation report, the gate state. The provider's snapshot must reflect the highest-`config_version` row after every boot. The narrow window between DB commit and `provider.reload()` is documented as recoverable-via-restart.
- **T7** (installer workflow orchestration) — multi-step installer-facing flow that mutates persisted safety configuration (`active_constraints` + `config_audit_log` + `wizard_state.step_3_*`) AND activates runtime behavior (`provider.reload()` causes PolicyGuard + ControlLoop to read new constraints on the next evaluation cycle).

#### R1 — Composition-risk analysis

Story 9.3 converges five operational domains in one shipping unit. Each carries a named, story-specific risk introduced by its convergence with the others:

1. **Persistence (T3) crossed with runtime control (T1) — the DB → in-memory sync boundary.** The activate route's load-bearing sequence is: `ConfigRepo.activate()` (DB commit) → `provider.reload()` (in-memory snapshot replacement) → `set_step_3_complete_locked` (DB write) → `draft_repo.delete()` (DB write). Risk: if `provider.reload()` runs BEFORE the DB commit, the provider reads stale data; if it runs AFTER the DB commit but is then cancelled, the provider holds stale data until the next process restart. Mitigation: the sequence is enforced inside `get_write_lock()`; `reload()` is called immediately after `activate()` returns (so the DB row is guaranteed visible to the same connection); a `reload()` exception is caught and logged but does NOT roll back the activation (DB authority preserved). Precedent: 9.0b R6 ("PolicyGuard and ControlLoop read from the same provider instance — drift eliminated structurally").

2. **Multiple validation states (T1) crossed with persistence (T3).** `draft.validation_status='valid'` is a per-validate-click outcome; live state can drift between that click and the activate click. Risk: a passing prior validate followed by a state shift (battery SoC drops, a device disconnects, a role gets reassigned) means the persisted `'valid'` status is misleading. Mitigation: the activate route re-runs `validate_draft()` inside the write lock as the AUTHORITATIVE gate; the persisted `validation_status` is informational only. Tests: `test_constraints_service.py::test_activate_re_validates_inside_lock_and_aborts_on_fresh_fail`. Precedent: 9.2's stale-acknowledgment filter pattern — the gate evaluator's read-time projection is the only consistent view.

3. **Installer workflow orchestration (T7) crossed with deployment / restart (T6).** Mid-activation process restart can land the system in: (a) committed `active_constraints` row but un-reloaded provider → fixed by restart-hydrate; (b) committed `active_constraints` + provider reloaded but `wizard_state.step_3_complete=0` → installer's next click re-runs activate, which finds the draft already deleted (step 10) and returns 400; the GET handler then short-circuits to step 4 because the new `provider.get().config_version` matches the active row's version; OR the user sees an inline "no changes to activate" message. Mitigation: the GET handler's idempotent short-circuit + the activate handler's `draft_not_found_for_session` exact-match reason cover this. Test: `test_setup_constraints_e2e.py::test_partial_activation_recovery`.

4. **Write-lock fairness vs. control loop liveness (T1).** `get_write_lock()` is shared between `ConfigRepo.activate()`, `EventLogRepo.append()`, `EnergyRepo.write_peak_interval()`, and the new `DraftConstraintsRepo` writes. Risk: a slow validation (e.g., a misbehaving capability resolver that's slow on every device) holds the lock for seconds, blocking peak-interval writes. Mitigation: the validation phase reads ONLY from `device_repo.list_all()` (already-cached DB row) + `state_store.get_snapshot()` (in-memory) + `get_profile()` (in-memory registry lookup); no live probes. The validation phase is bounded to milliseconds. Test: `test_constraints_service.py::test_validate_draft_is_bounded_io`.

5. **Multiple concurrent installer sessions (T7).** Two installer browsers (different session_ids) each have their own draft. Both can validate independently. Risk: both try to activate near-simultaneously. The first wins the write lock; the second waits. When the second proceeds:
   - Re-validation runs against the NEW state (the first's activation is reflected in `provider.get()`).
   - If the second's draft equals the first's just-activated values → `ConfigRepo.activate` raises `activate_called_with_no_changed_fields`; route returns 400 with the user-facing "no changes to activate" message.
   - If the second's draft differs → a NEW `config_version` is assigned; both audit trails are visible in `config_audit_log`.
   This is the documented v1 multi-installer semantics. Test: `test_setup_constraints_e2e.py::test_concurrent_activations_serialize_correctly`.

#### R2 — State-transition table

Story 9.3 introduces / modifies four state machines.

**`draft_constraints.validation_status` (per-session-draft):**

| State | Allowed transitions | Trigger | Side-effects |
|---|---|---|---|
| (no row) | → `pending` | first `POST /installer/setup/constraints/draft` for the session | INSERT row; structlog `constraints_draft_upserted`; HTMX fragment swap |
| `pending` | → `valid` | `POST /installer/setup/constraints/validate` produces overall_status='valid' | UPDATE `validation_status='valid'`, persist `validation_report`; structlog `constraints_draft_validated_valid`; HTMX swap of validation region + activate button |
| `pending` | → `failed` | `POST /installer/setup/constraints/validate` produces overall_status='failed' | UPDATE `validation_status='failed'`, persist `validation_report`; structlog `constraints_draft_validated_failed`; HTMX swap of validation region; activate button stays disabled |
| `valid` | → `pending` | any `POST /installer/setup/constraints/draft` (form edit) | UPSERT row; resets `validation_status='pending'`; clears `validation_report`; HTMX swap |
| `valid` | → `failed` | activate re-validation finds a new FAIL | UPDATE `validation_status='failed'`, persist new `validation_report`; route returns 400 + banner; **NO** `active_constraints` write |
| `valid` | → row DELETEd | activate succeeds | DELETE row; INSERT into `active_constraints`; INSERT N rows into `config_audit_log`; `provider.reload()`; UPDATE `wizard_state.step_3_complete=1` |
| `failed` | → `pending` | `POST /installer/setup/constraints/draft` (form edit) | same as valid→pending |
| any | → row DELETEd | session row deleted (session cleanup from Story 2-5) | CASCADE removes `draft_constraints` row |

**`active_constraints` (append-only, versioned):**

| State | Allowed transitions | Trigger | Side-effects |
|---|---|---|---|
| (no row) | → row at `config_version=N` (N=1 on fresh DB) | first successful `activate()` | INSERT (active_constraints), INSERT N rows (config_audit_log), `provider.reload()` |
| row at `v=N` | → row at `v=N+1` | subsequent successful `activate()` | same; `next_version = MAX(active_constraints.v, config_audit_log.v) + 1` |
| (no row) | → row at `v=N+1` (post-manual-DELETE) | manual DBA DELETE FROM active_constraints + subsequent activate | same; cross-table monotonic ensures no regression — 9.0b P8 |

**`wizard_state.step_3_complete` (per-session):**

| State | Allowed transitions | Trigger | Side-effects |
|---|---|---|---|
| `step_3_complete=0` | → `step_3_complete=1, step_3_completed_at=now(), step_3_activated_config_version=N` | `POST /installer/setup/constraints/activate` with passing gate | 302 to `/installer/setup/validation`; structlog `step_3_completed` with `config_version=N` |
| `step_3_complete=0` | → unchanged | `POST /installer/setup/constraints/activate` with failing gate | inline error banner; no DB write to wizard_state |
| `step_3_complete=1` | → unchanged (idempotent) | re-click on activate after prior success | GET handler's short-circuit redirects to `/installer/setup/validation` BEFORE the POST is invoked (in practice this transition never fires from a real re-click) |
| any | → row DELETEd | session deletion CASCADE | wizard_state row removed |

**`ActiveConstraintsProvider._current` (in-memory snapshot, per-process):**

| State | Allowed transitions | Trigger | Side-effects |
|---|---|---|---|
| `None` (pre-hydrate) | → `ActiveConstraints(config_version=0, seeded-from-Settings)` | `hydrate()` with empty `active_constraints` | structlog `constraints_hydrate_seeded_from_settings` |
| `None` (pre-hydrate) | → `ActiveConstraints(config_version=N)` from highest DB row | `hydrate()` with one or more rows | — |
| `ActiveConstraints(v=K)` | → `ActiveConstraints(v=M)` with `M > K` | `reload()` after activate commit | atomic attribute write; PolicyGuard/ControlLoop see new snapshot on next `get()` |
| `ActiveConstraints(v=K)` | → unchanged | `reload()` finds empty DB OR `reload()` finds `M < K` (regression) | warning logged (9.0b P9); snapshot preserved |
| `ActiveConstraints(v=K)` | → unchanged | `reload()` cancelled mid-flight | snapshot preserved (single-assignment atomicity); next `reload()` resolves |

**Critical invariants:**
- **Single `config_version` per activation.** All audit rows + the `active_constraints` row share one `config_version`. Enforced by `ConfigRepo.activate()` (9.0b).
- **`step_3_complete=1` IFF `step_3_completed_at` IS NOT NULL IFF `step_3_activated_config_version` IS NOT NULL.** Enforced by `set_step_3_complete_locked` (writes all three atomically) + a CHECK constraint on the table.
- **`draft.validation_status='valid'` never grants activation.** The activate route's authoritative gate is the re-validation inside the lock, not the persisted status.

#### R3 — Impossible-state analysis (with cold-start coverage)

**Forbidden state combinations and their structural invariants:**

1. **`active_constraints` row with `config_version` that already exists in `config_audit_log`** — invariant: cross-table monotonic `next_version` (9.0b P8 patch) computes `MAX(active_constraints.v, config_audit_log.v) + 1`. Test: `test_config_repo.py::test_activate_uses_cross_table_monotonic_version` (already exists from 9.0b; re-asserted in 9.3 with the new EV-window audit row contributing).

2. **Two `active_constraints` rows with the same `config_version`** — invariant: `UNIQUE(config_version)` DB constraint + `BEGIN IMMEDIATE` transaction in `activate()`. Test: `test_config_repo.py::test_activate_unique_config_version_constraint` (9.0b inheritance).

3. **`draft_constraints.validation_status='valid'` AND a contemporaneous `active_constraints` row with the same values** — invariant: a successful activate DELETEs the draft inside the same write-lock window. The window between commit and delete is sub-millisecond and inside the same lock; no other writer can interleave. Test: `test_setup_constraints_e2e.py::test_activate_deletes_draft`.

4. **`wizard_state.step_3_complete=1` AND `step_3_activated_config_version` points to a non-existent `active_constraints.config_version`** — invariant: the activate route writes `step_3_complete=1` AFTER `ConfigRepo.activate()` returns (the new row exists); the wizard write is INSIDE the same write-lock window so no concurrent DELETE can intervene. Test: `test_setup_constraints_routes.py::test_activate_links_wizard_to_real_config_version`.

5. **`wizard_state.step_3_complete=1` AND `step_3_completed_at IS NULL`** (or vice versa) — invariant: the CHECK constraint on the table + `set_step_3_complete_locked` writes both atomically. Test: `test_wizard_state_repo.py::test_step_3_pairing_invariant`.

6. **`ActiveConstraintsProvider.get().config_version` greater than the maximum in `active_constraints`** — invariant: `reload()` only replaces the snapshot when the new row's `config_version >= current` (9.0b P9 patch); a regression preserves the cached value. The provider is the authoritative runtime read; the DB is the authoritative durable store.

7. **A `config_audit_log` row with `field='ev_charging_window'` where `previous_value` and `new_value` are equal** — invariant: `_build_audit_changes()` skips fields where `previous == new` (9.0b precedent), so an EV-window audit row only appears when the value actually changed. Test: `test_config_repo.py::test_audit_emits_no_row_for_unchanged_ev_window`.

8. **Activation with a draft whose `peak_limit_kw <= 0`** — invariant: Pydantic `gt=0.0` on `ConstraintDraftInput.peak_limit_kw` + `gt=0.0` on `ActiveConstraintsInput.peak_limit_kw` + DB `CHECK > 0` on `draft_constraints.peak_limit_kw` AND `active_constraints.peak_limit_kw` (9.0b inheritance). Rejected at the form layer (HTML5 `min=0.01`), the Pydantic layer (server validation), AND the DB layer (CHECK).

9. **`draft_constraints` row referencing a session that no longer exists** — invariant: FK CASCADE deletes the draft when its parent session is deleted. Test: `test_draft_constraints_repo.py::test_cascade_on_session_delete`.

10. **PolicyGuard reading stale `peak_limit_kw` AFTER a successful activate AND a successful reload** — invariant: snapshot replacement is a single CPython attribute write (9.0b R4); PolicyGuard's `provider.get()` returns the new immutable snapshot on the next call. The activate route's response is sent AFTER the reload completes, so any subsequent request that PolicyGuard might evaluate already sees the new snapshot. Test: `test_setup_constraints_e2e.py::test_policy_guard_sees_new_constraints_on_next_request`.

**Cold-start / startup-grace coverage (mandatory):**

1. **t=0 (process boot, fresh deployment, no prior activation):** `active_constraints` is empty. `ActiveConstraintsProvider.hydrate()` seeds the snapshot from `Settings.peak_limit_kw` / `Settings.battery_reserve_floor_percent` with `config_version=0`. EV window is NULL. `wizard_state.step_3_complete=0`. The installer logs in and reaches `/installer/setup/constraints` only after completing Steps 1 + 2 (the step-gate enforcement in AC5). On first render, the draft does not exist; the form is seeded from `provider.get()` — peak_limit and reserve_floor are the Settings defaults, EV window fields are empty. The installer sees the SAME values they'd see if no activation had ever occurred. The activate button is disabled until validate passes. **PolicyGuard rejects every device command** during this period with `adapter_not_registered` (Story 8-2 deferred — unchanged by 9.3); the absence of activated constraints does NOT change PolicyGuard's behavior because the provider's seeded snapshot is already in effect.

2. **t=process restart with prior activation (`active_constraints` has one or more rows):** `hydrate()` reads the highest-`config_version` row. `provider.get()` returns it. PolicyGuard + ControlLoop see the activated constraints on their next read. The installer's wizard state — `step_3_complete` flag, `step_3_activated_config_version` — is restored from `wizard_state`. The Step 3 GET page redirects to Step 4 because `step_3_complete=1`.

3. **t=process restart mid-draft (no activate has occurred for this draft):** `draft_constraints` row persists. The installer reloads `/installer/setup/constraints`. The form is seeded from the draft (UPSERT row exists). `validation_status` may be `'pending'`, `'valid'`, or `'failed'` depending on the last `/validate` click. The validation report renders from the persisted `validation_report` column. **The persisted `'valid'` status is informational only** — activation will re-validate authoritatively.

4. **t=process restart mid-activate (DB commit landed; reload not yet completed):** The `active_constraints` row is in the DB. `wizard_state.step_3_complete` may be 0 (the wizard update is the LAST write inside the lock, so a mid-lock restart leaves the step_3 flag unset). On the next boot, `provider.hydrate()` reads the new active row (correct value). The installer's next `/installer/setup/constraints` GET reads `wizard_state` — `step_3_complete=0` → renders Step 3 again. They click activate; the activate route finds the draft already DELETEd from the prior in-progress run (step 10) → 400 `draft_not_found_for_session`. The installer re-creates the draft from the form (HTMX renders the existing `provider.get()` values which now reflect the prior partial activation) and re-clicks activate. The `_build_audit_changes` finds no differences → `ValueError("activate called with no changed fields")` → 400. **Workaround for this narrow window:** the GET handler also reads `provider.get().config_version`; if `provider.config_version > 0 AND step_3_complete=0 AND step_3_activated_config_version IS NULL`, it auto-rewrites `step_3_complete=1` with `step_3_activated_config_version=provider.config_version` (a fail-forward repair, documented in the GET handler). Alternatively, this state is rare enough that requiring the installer to revisit the form once is acceptable; the auto-repair is OPTIONAL polish — Subtask 3.2 makes the decision based on review feedback. **Recommended baseline: NO auto-repair in 9.3.** The window is microseconds; the user-visible recovery is to re-click activate (which on a fresh draft will succeed cleanly).

5. **t=concurrent installer sessions:** Each session has its own draft (UNIQUE on session_id). Both can validate. Activations serialize via the write lock; the second draft sees the new live state during re-validation and either matches (no-op rejection) or differs (new version). Test: `test_setup_constraints_e2e.py::test_concurrent_activations_serialize_correctly`.

6. **t=session expired mid-wizard:** Installer's session expires before Step 3 completes. The draft row is CASCADE-deleted with the session. New login → new session_id → new wizard_state row created lazily (step_1_complete=1 + step_2_complete=1 reload from the still-existing prior session's row? — NO: 9.1/9.2 acknowledge that `wizard_state` is per-session; the installer must redo Steps 1 + 2 after a session-expiry-mid-wizard scenario). This is acceptable: a session expiry is rare in practice (4-hour timeout per Settings.installer_session_timeout_hours), and the discovery scan + role assignments persist on `device_registry`, so Steps 1 + 2 are mostly visual re-confirmation. Test: `test_setup_constraints_e2e.py::test_session_expiry_loses_draft_preserves_active_constraints`.

**Marker for normal operation:** the first 200 response from `GET /installer/setup/constraints` after process boot signals the Step 3 surface is live. No separate readiness probe — inherits global `/health/ready` from Story 1-3.

#### R4 — Cancellation ownership map

Story 9.3 introduces no novel async cancellation paths beyond HTTP request cancellation. All state-mutating routes await a single sequence of DB writes inside `get_write_lock()`. The `activate` route has the longest critical section.

| Async operation | `CancelledError` owner | Cancellation audit owner | Cleanup owner | Reporting field semantics under cancel |
|---|---|---|---|---|
| `POST /installer/setup/constraints/draft` | route handler task | none — draft state is informational | `async with get_write_lock():` exit handler runs on cancel; single-statement UPSERT is atomic in aiosqlite | row state unchanged if cancelled before UPSERT lands; row state fully updated if cancelled after UPSERT returns |
| `POST /installer/setup/constraints/validate` | route handler task | none — validation result is informational | same; the `record_validation_outcome` UPDATE is a single statement | report unchanged if cancelled mid-write |
| `POST /installer/setup/constraints/activate` | route handler task | structlog `constraints_activated` emitted AFTER `wizard_state.step_3_complete=1` write succeeds. Cancellation before that emit leaves no structlog but the DB is authoritative. | `ConfigRepo.activate()`'s internal `try/except` rolls back the `BEGIN IMMEDIATE` transaction on any exception (including CancelledError); `provider.reload()` is wrapped in `try/except` in the route handler — exceptions are logged + swallowed (DB is authoritative); the `set_step_3_complete_locked` UPDATE + `draft_repo.delete()` are single statements at the end of the lock window | partial-state cases: (a) cancelled before ConfigRepo.activate → no change; (b) cancelled during ConfigRepo.activate → rolled back, no change; (c) cancelled after ConfigRepo.activate commit but before provider.reload → DB has new row, provider stale → recoverable on next boot or next page-load-triggered reload; (d) cancelled after provider.reload but before set_step_3_complete_locked → DB + provider in sync but wizard_state.step_3_complete=0 → installer re-clicks; (e) cancelled after set_step_3_complete_locked but before draft_repo.delete → wizard complete + draft still exists → next GET short-circuits to Step 4; the orphan draft is harmless (it's never read again; CASCADE on session deletion cleans it up; OR a follow-up polish item can run a periodic cleanup) |
| `POST /installer/setup/constraints/activate`'s call to `ActiveConstraintsProvider.reload()` | reload-internal task | none — reload is idempotent | reload's internal `asyncio.Lock` releases on cancel; `_current` is either the old reference or the new — never a torn read (single attribute write) | reload either fully completes or doesn't; next reload picks up the same DB row |
| `GET /installer/setup/constraints` | route handler task | N/A | read-only path | response not delivered |

**Note on the absence of `RetryPolicy`-style async retries:** Story 9.3 does not invoke any device adapters and therefore introduces no adapter-cancellation surface. The cancellation map is bounded to HTTP request cancellation only; there are no detached `asyncio.create_task(...)` paths.

**Cancellation-equivalent failure of `provider.reload()` after DB commit:** documented as Case (c) above. **The route handler MUST catch and log the reload failure**, then continue the rest of the lock window. This is explicitly different from cancelling the route — a `reload()` exception is contained; a route cancellation lets the `finally` of `async with get_write_lock():` release the lock but partial writes are accepted as recoverable (the DB is authoritative; restart fixes it).

#### R5 — Before-first-successful-cycle lifecycle review

Story 9.3 does not introduce any new gating step in the lifespan startup sequence, and it does not gate the control loop. The lifespan additions are pure construction:

| Phase | Event | What is published / logged / DB state | What relaxes |
|---|---|---|---|
| Boot | uvicorn starts; lifespan generator entered | unchanged from prior stories | — |
| Lifespan step 1–4d | logging, clock, migrations, DB init, capability registry, **provider hydrate (Story 9.0b)**, monthly-peak hydrate (9.0d), Story 9.1 wizard services, Story 9.2 role-assignment service | unchanged | — |
| **Lifespan step 4e (NEW)** | construct `DraftConstraintsRepo()` + `ConstraintsService(...)` | structlog `draft_constraints_repo_ready` + `constraints_service_ready` at info | none — stateless wiring only |
| Lifespan step 5+ | `mark_ready()`, watchdog, control loop | unchanged | — |
| First wizard request to `/installer/setup/constraints` | route fires | DB reads of `wizard_state` + `draft_constraints`; `provider.get()` (in-memory); no writes unless the route is a POST | none — post-readiness |

**Critical:** lifespan step 4e MUST NOT call any DB read at startup (no `draft_repo.get_or_default()`, no `state_store.get_snapshot()`). The services are constructed (object instantiation only); the first DB read happens on the first installer request, post-readiness. **The provider hydration (step 4b, owned by 9.0b) is unchanged** — 9.3 does NOT re-trigger hydration.

**Between lifespan completion and the first installer interaction with Step 3:** the control loop is ticking normally; PolicyGuard rejects every command with `adapter_not_registered` (deferred from Story 8-2; unchanged by 9.3 — see R7); the provider's snapshot is whatever `hydrate()` set it to (Settings seed on fresh DB, or the highest-`config_version` row otherwise). The system is functional in a "PolicyGuard knows the constraints, can't dispatch to anyone" state. Story 9.3 does NOT change this baseline — the activation surface is new but the **adapters dispatch surface** is not.

**Marker for "Step 3 wizard surface is operating normally":** the first 200 response from `GET /installer/setup/constraints`. There is no separate readiness probe.

#### R6 — Source-of-truth ownership per datum

| Datum | Single owner | Read path | Drift risk (if any) |
|---|---|---|---|
| Active site `peak_limit_kw` (runtime) | `ActiveConstraintsProvider._current.peak_limit_kw` (in-memory snapshot) | `provider.get().peak_limit_kw` — PolicyGuard + ControlLoop both read from this one instance | **Bounded:** between `ConfigRepo.activate()` commit and `provider.reload()` completion the DB row is ahead; window is microseconds inside the activate route's write lock; restart re-hydrates via `provider.hydrate()`. Documented in R3 cold-start case 4 + R1 risk 1. |
| Active site `battery_reserve_floor_percent` (runtime) | same | same | same |
| Active site `ev_charging_window_start` / `_end` (runtime, NEW) | `ActiveConstraintsProvider._current.ev_charging_window_*` | `provider.get().ev_charging_window_*` (consumed by future ev_scheduling story) | same |
| Active `config_version` | `active_constraints` highest-`config_version` row (durable) / `provider.get().config_version` (in-memory cache) | DB read via `ConfigRepo.get_active()` OR cached via `provider.get()` | same bounded window; 9.0b R6 inheritance |
| Draft `peak_limit_kw` / `battery_reserve_floor_percent` / `ev_charging_window_*` | `draft_constraints` row keyed by session_id | `DraftConstraintsRepo.get(session_id)` | None — single owner; UPSERT is atomic |
| Draft `validation_status` / `validation_report` | `draft_constraints` row columns | same | **Bounded staleness by design:** the persisted `'valid'` reflects validation at a past instant; the `activate` route re-validates inside the write lock and OVERWRITES the persisted status with the fresh outcome (and aborts activation on fresh FAIL). No reader of `validation_status` outside the activate route should treat it as authoritative for activation gating; documented in AC6 step 5. |
| Step 3 completion flag | `wizard_state.step_3_complete` column | `WizardStateRepo.get(session_id).step_3_complete` | None — single owner |
| Step 3 → config_version link (NEW) | `wizard_state.step_3_activated_config_version` column | same | None — single owner; written atomically with `step_3_complete=1` |
| Per-activation audit trail | `config_audit_log` rows (append-only) | direct query | None — append-only; share the single `config_version` per activation (9.0b inheritance) |
| Valid validation-check-name set | enum `Literal['schema', 'capability_strategy', 'safety_pre_check']` in `services/constraints.py` | direct import | None — module constant |
| Valid validation-status set | enum `Literal['pending', 'valid', 'failed']` | direct import | None — module constant |
| Pre-activation safety state snapshot | `StateStore` (in-memory; in-flight via Story 5-1) | `state_store.get_snapshot()` | The safety_pre_check reads the snapshot at validate-time AND again at activate-time (re-validation). Between the two reads, the snapshot CAN change. **This is the intended behavior** — re-validation is the authoritative gate. Drift is bounded by the write-lock window around the activate's re-validate + commit. |
| Valid roles set (for capability_strategy check) | `device_registry.role` column | `DeviceRepo.list_all()` | Inherited from 9.2; documented in 9.2 R6 |

**Drift risks called out explicitly:**
- **Provider vs DB (`config_version`):** microsecond window; restart restores invariant. Documented in R3.
- **Persisted `validation_status='valid'` vs. actual safety:** persistent state is informational; authoritative re-validation runs inside the activate lock. Documented in AC6 step 5.
- **EV window field rename in `ActiveConstraints` vs. DB column:** the Pydantic model field names MUST match the DB column names exactly. The boundary is in `ConfigRepo.get_active()`'s row → model construction. **A future rename would break existing DB rows.** Mitigation: enum/field stability is part of the public API per Story 9.0b/9.2 R6 inheritance; an integration test asserts the field names are stable across migrations.

#### R7 — Deferred-findings triage

Scanned `_bmad-output/implementation-artifacts/deferred-work.md` (321 lines) for items whose component or invariant overlaps with this story:

| Finding (bracket ref) | Classification | Rationale |
|---|---|---|
| `[src/open_ems/web/app.py:223-236]` (Story 8-2) "`adapters={}` in production silently rejects all commands; complete fix lands at Story 9.2 + 9.3" | **acceptable-post-this-story** | The deferred-work text names 9.3 as a boundary, but the actual fix requires adapter discovery + connection management + lifespan-time construction of the adapter map keyed by `DeviceRole`. None of that is constraint-configuration scope. 9.3 is configuration persistence + UI + atomic activation + provider reload — adding adapter wiring would over-scope an already A2-triggered story. The deferred-work entry should be updated to point at a dedicated future story (provisional: "9-X-wire-adapter-map-into-policy-guard"). Same classification as Story 9.1 / 9.2's triage. |
| `[src/open_ems/services/active_constraints.py:163]` (Story 9-0c) "`from_snapshot()` uses `Settings(_env_file=None)` — silently picks up process env" | **safe-during-this-story** | 9.3 doesn't use `from_snapshot()` in production code; only test fixtures. No aggravation. |
| `[src/open_ems/services/active_constraints.py:357-362]` (Story 9-0b W4) "constraints_hydrate_seeded_from_settings log level diverges from Task 4 wording" | **safe-during-this-story** | Cosmetic; unchanged by 9.3. |
| `[tests/unit/storage/repositories/test_config_repo.py]` (Story 9-0b W5) "DB-layer CHECK constraint coverage incomplete for `battery_reserve_floor_percent` and `actor`" | **safe-during-this-story** | 9.3 adds new CHECK constraints (EV window paired-NULL, HH:MM format if added at DB layer); the same audit gap applies but is bounded by 9.3's test coverage of those new constraints. The reserve-floor + actor raw-INSERT coverage gap is unchanged. |
| `[migrations/versions/0008_add_active_constraints_table.py]` (Story 9-0b W6) "Migration 0008 upgrade→downgrade→upgrade round-trip not codified" | **safe-during-this-story** | 9.3's new migration 0011 codifies its OWN round-trip; 0008's coverage is unchanged. |
| `[src/open_ems/web/app.py:229-236]` (Story 9-0b W7) "AC7 fail-loud `SystemExit(1)` branch not exercised by any test" | **safe-during-this-story** | 9.3 doesn't change the hydrate-failure path; the gap is unchanged. |
| `[src/open_ems/storage/repositories/user_repo.py, session_repo.py]` (Story 9-0b) "UserRepo and SessionRepo writes not yet routed through the write lock" | **safe-during-this-story** | 9.3's new `DraftConstraintsRepo` writes DO acquire the write lock. UserRepo / SessionRepo concurrency is unrelated to 9.3's surface. |
| `[src/open_ems/services/watchdog.py:60-69]` (Story 8-4) "`observability.audit` slow DB write blocks watchdog heartbeat" | **safe-during-this-story** | 9.3 emits structlog events only (NO `event_log` rows — see Audit Semantics note above); watchdog interaction is unchanged. The `config_audit_log` writes happen inside `ConfigRepo.activate()` under the same write lock that the watchdog observes; they are bounded to milliseconds. |
| `[src/open_ems/web/csrf.py:71-79]` (Story 9-1) "CSRF middleware exhausts the request body" | **safe-during-this-story** | 9.3 inherits the same CSRF middleware path; tests use the `X-CSRF-Token` header pattern (9.1/9.2 precedent) to avoid body-exhaustion. |
| `[src/open_ems/web/routes/setup.py:531-553]` (Story 9-2 review-deferred) "`post_acknowledge_gap` read-modify-write race on `step_2_acknowledged_gaps`" | **safe-during-this-story** | 9.3 introduces an analogous read-modify-write surface on `draft_constraints` (the draft upsert is an UPSERT, not a read-modify-write; the validation outcome write IS a single UPDATE inside the lock). The narrow race is the same v1 acceptance pattern. |
| `[src/open_ems/storage/repositories/device_repo.py:136-267]` (Story 9-1) "`DeviceRepo` does not use `BEGIN IMMEDIATE`" | **safe-during-this-story** | 9.3's new `DraftConstraintsRepo` writes are single-statement DML (UPSERT, UPDATE, DELETE); the 9.0b `BEGIN IMMEDIATE` pattern in `ConfigRepo.activate()` is inherited and unchanged. The same visual-inconsistency-with-9.0b concern applies to `DraftConstraintsRepo`; not made worse by 9.3. |
| `[tests/integration/web/test_setup_roles_e2e.py, tests/unit/web/test_setup_roles_routes.py, migrations/versions/0010_*]` (Story 9-2) "Three definitions of `device_registry` / `wizard_state` schema" | **safe-during-this-story** | 9.3 adds `wizard_state.step_3_*` columns and may add inline schema definitions in its own test fixtures; the deferral count grows by one but the consolidation work is unchanged. |
| `[src/open_ems/engine/policy_guard.py:143-145]` (Story 8-4) "Peak limit check uses strict `>` — command at exactly `peak_limit_kw` passes" | **safe-during-this-story** | 9.3 doesn't touch PolicyGuard logic; the boundary semantics remain as 8.x defined. |
| `[src/open_ems/services/role_assignment.py:107-115]` (Story 9-2) "Spec AC3 enumerates `RoleGateOutcome` as 4 fields; implementation has 5" | **safe-during-this-story** | Pre-existing spec-vs-code drift; 9.3 doesn't aggravate it. |
| `[src/open_ems/web/templates/installer/_setup_roles_row.html:43]` (Story 9-2) "`onchange='this.form.requestSubmit()'`" | **safe-during-this-story** | 9.3 templates use server-rendered HTML5 form validation + explicit submit buttons for validate + activate; the implicit-submit pattern is NOT introduced for the staged flow (the BAD-3 explicit-confirm design rules it out). The 9.2 deferral is bounded; 9.3 does not regress on it. |

**No findings classified as `must-resolve-in-this-story`.** The Story 8-2 `adapters={}` concern is the most prominent overlap (and the deferred-work entry literally names 9.3 as a boundary), but on closer scope analysis the proper fix is its own story. Updating the deferred-work entry to reflect that scoping is an out-of-band housekeeping action and is included in Task 7's quality gate review.

### References

- [Source: _bmad-output/planning-artifacts/architecture.md#BAD-3] — Constraint Change Validation Flow (staged draft → validate → confirm → activate → audit)
- [Source: _bmad-output/planning-artifacts/architecture.md#Constraint-audit-trail] — line 342, "All constraint changes ... are stored ... in a dedicated `config_audit_log` table"
- [Source: _bmad-output/planning-artifacts/architecture.md#Project-Structure] — `services/`, `web/templates/installer/`, `storage/repositories/` placement
- [Source: _bmad-output/planning-artifacts/architecture.md#Naming-Conventions] — snake_case columns, hyphen-separated URLs
- [Source: _bmad-output/planning-artifacts/epics.md#Story-9.3] — story spec lines 1964–1995
- [Source: _bmad-output/planning-artifacts/epics.md#Epic-9-Cross-story-constraints] — lines 2035–2043; "config_version increments exactly once per constraint activation, regardless of changed field count"
- [Source: _bmad-output/planning-artifacts/epics.md#Story-9.4] — Step-4 reads `step_3_activated_config_version` to detect outdated validation results (forward-compatibility consumer)
- [Source: _bmad-output/planning-artifacts/ux-design-specification.md#Component-8-Constraint-Form-Field] — form field anatomy (label · input · unit · helper · validation message)
- [Source: _bmad-output/planning-artifacts/ux-design-specification.md#Component-9-Validation-Result-Row] — per-check result row (PASS / WARN / FAIL badge + name + summary + corrective action)
- [Source: _bmad-output/planning-artifacts/ux-design-specification.md#Component-10-Validation-State-Banner] — overall state banner (PASS / WARN / FAIL → handoff button state)
- [Source: _bmad-output/planning-artifacts/ux-design-specification.md#Component-14-Setup-Wizard-Step-Indicator] — persistent step indicator (inherited from 9.1/9.2)
- [Source: _bmad-output/planning-artifacts/ux-design-specification.md#Journey-Flow-1] — Step 3 sequence: "Set: peak limit · battery reserve · EV window · default strategy" → "Inline field validation on change" → "Constraint summary — all values visible"
- [Source: _bmad-output/planning-artifacts/ux-design-specification.md#Accessibility-Considerations] — WCAG 2.1 AA, ≥4.5:1 contrast, keyboard navigation, `aria-live` regions
- [Source: src/open_ems/core/constraints.py] — `ActiveConstraints` + `ActiveConstraintsInput` Pydantic models (9.0b)
- [Source: src/open_ems/storage/repositories/config_repo.py] — `ConfigRepo.activate()` atomic transaction with audit emission (9.0b)
- [Source: src/open_ems/storage/repositories/config_audit_repo.py] — `ConfigAuditRepo.append_activation` + `SAFETY_RELEVANT_CONFIG_FIELDS`
- [Source: src/open_ems/services/active_constraints.py] — `ActiveConstraintsProvider.hydrate` / `.reload` / `.get` contract (9.0b)
- [Source: src/open_ems/storage/repositories/wizard_state_repo.py] — `WizardState` + `set_step_2_complete` / `set_step_2_complete_locked` idempotent pattern (9.2)
- [Source: src/open_ems/services/role_assignment.py] — `RoleAssignmentService` shape precedent (frozen Pydantic outcomes; pure dependency on a single repo)
- [Source: src/open_ems/web/routes/setup.py] — existing setup router; 9.3 extends with constraints routes + replaces the Step 3 placeholder
- [Source: src/open_ems/web/templates/installer/setup_layout.html] — persistent 4-step indicator; 9.3 adds back-navigation hyperlink on step 2
- [Source: src/open_ems/web/dependencies.py] — `require_installer`, `_resolve_session`
- [Source: src/open_ems/web/csrf.py] — CSRF middleware applied to all state-changing routes
- [Source: src/open_ems/storage/database.py] — `get_connection()` / `get_write_lock()` contract
- [Source: src/open_ems/engine/policy_guard.py] — PolicyGuard reads constraints via `self._active_constraints.get()` on the hot path (9.0b refactor)
- [Source: src/open_ems/engine/control_loop.py] — ControlLoop reads constraints via the same provider in `_build_evaluation_input` (9.0b refactor)
- [Source: _bmad-output/implementation-artifacts/9-0b-db-backed-active-constraints-reload.md] — provider + ConfigRepo activation contract; cross-table monotonic version; reload regression-preserve semantics
- [Source: _bmad-output/implementation-artifacts/9-1-implement-device-discovery-step-with-live-probing-capability-classification-and-manual-entry.md] — step-1 wizard precedent; exact-match reason strings; setup_layout.html step-indicator pattern
- [Source: _bmad-output/implementation-artifacts/9-2-implement-role-assignment-with-inline-conflict-detection-and-gap-acknowledgment.md] — step-2 wizard precedent; idempotent advance + write-lock TOCTOU close; `_VALID_GAP_LABELS` four-reader pattern; A1 R1–R7 inheritance
- [Source: _bmad-output/implementation-artifacts/deferred-work.md] — full deferred-findings list scanned for R7
- [Source: memory project_epic1_retro] — Epic 8 retro 2026-05-10; A1/A2 codification

## Dev Agent Record

### Agent Model Used

claude-opus-4-7

### Debug Log References

- Story-internal validation (no external bug tracker entries opened).
- Quality gates: `pytest tests/ --no-cov -q` → 1295 passed + 3 xfailed (deferred a11y placeholders for 9.1 / 9.2 / 9.3); `mypy src/` → clean; `ruff check .` + `ruff format --check .` → clean.

### Completion Notes List

- **Migration 0011** adds `draft_constraints` (per-session staged draft, FK CASCADE on `sessions`), extends `active_constraints` with the paired-NULL EV-window columns, and extends `wizard_state` with the `step_3_*` triple. Round-trip (upgrade → downgrade -1 → upgrade) is codified in `tests/integration/test_migrations.py::test_migration_0011_round_trips`. `test_migration_0009_round_trips` adjusted to step back 3 (was 2).
- **Validation-report value types** (`ConstraintCheckResult`, `ConstraintValidationReport`, `ConstraintDraftInput`, `ConstraintActivationResult`, plus the `ConstraintCheckName` / `ConstraintDraftValidationStatus` Literals) live in `src/open_ems/core/constraints.py` so the storage repo and the services layer both import from `core/` without crossing the storage→services line. The story's spec wording placed these in `services/constraints.py`; we kept the service-class + exception there and lifted only the value types to `core/` (functionally identical surface, layer-clean).
- **`ConfigRepo.activate`** is now a public lock-acquiring wrapper over `_activate_locked`. The staged-activation route holds `get_write_lock()` across re-validate → `_activate_locked` → `provider.reload()` → `set_step_3_complete_locked` → `delete_locked` to close the asyncio.Lock-not-reentrant deadlock the dev notes called out as CRITICAL.
- **EV charging window** emits exactly ONE combined `config_audit_log` row per activation under `field='ev_charging_window'` with `previous_value` / `new_value` shaped as `{"start": ..., "end": ...}`. `SAFETY_RELEVANT_CONFIG_FIELDS` adds `'ev_charging_window'` while keeping the legacy `'ev_charging_window_preference'` member (no caller writes it; kept for completeness).
- **`ConstraintsService.activate_draft`** error vocabulary uses exact-match contract strings per AC11: `step_prerequisites_not_met: step_1_complete=<x> step_2_complete=<y>`, `draft_not_found_for_session`, `activate_called_with_no_changed_fields`, `constraints_validation_failed` (carries the fresh report on the `ConstraintActivationError`).
- **Provider reload failure tolerance** (R1 risk #1): `provider.reload()` exceptions inside the activate path are caught + logged at ERROR (`constraints_reload_after_activate_failed`) without rolling back the activation. The DB is authoritative; the documented narrow drift window self-heals on next process restart.
- **Step-gate enforcement** at `GET /installer/setup/constraints` mirrors 9.2's pattern: redirects to `/installer/setup/discovery` (step_1_complete=0), `/installer/setup/roles` (step_2_complete=0), or `/installer/setup/validation` (step_3_complete=1 — idempotent short-circuit). The R3 cold-start case (4) auto-repair was deliberately NOT implemented per the dev-note recommendation.
- **`setup_layout.html`** Step 2 back-link follows the same conditional-hyperlink pattern as Step 1: rendered when not on Step 2 / Step 1 AND `step_3_complete=0`. Hidden once Step 3 is finalized.
- **HTMX out-of-band swap**: `POST /installer/setup/constraints/validate` returns the validation-report fragment as the primary swap and the activate-button fragment via `hx-swap-oob` so the disabled→enabled transition is atomic with the report update.
- **Multiple inline DDL definitions** updated in lockstep across `tests/unit/storage/repositories/test_config_repo.py`, `test_wizard_state_repo.py`, `tests/unit/services/test_active_constraints.py`, `tests/unit/web/test_setup_routes.py`, `test_setup_roles_routes.py`, `test_setup_constraints_routes.py`, `tests/integration/web/test_setup_roles_e2e.py`, `test_setup_discovery_e2e.py`, `test_setup_constraints_e2e.py`, `test_constraints_service.py`. The R7 deferred-finding ("multiple inline copies of `device_registry` / `wizard_state` schema") is unchanged in scope; this story added the new copies for `draft_constraints` + the `step_3_*` columns.
- **Removed obsolete test** `test_get_constraints_renders_placeholder` from `tests/unit/web/test_setup_roles_routes.py` (the placeholder template is gone; the real implementation is covered by the new `tests/unit/web/test_setup_constraints_routes.py`).
- **AC11 `/bmad-code-review`** is the only remaining subtask (Subtask 7.4); per the workflow's Step 10 tip and Epic 8 retro, it should be run by the user with a different LLM than the implementer (Opus 4.7 was the implementer here).

### File List

**New source files:**
- `migrations/versions/0011_add_constraint_configuration_tables.py`
- `src/open_ems/storage/repositories/draft_constraints_repo.py`
- `src/open_ems/services/constraints.py`
- `src/open_ems/web/templates/installer/setup_constraints.html`
- `src/open_ems/web/templates/installer/_setup_constraints_form.html`
- `src/open_ems/web/templates/installer/_setup_constraints_validation.html`
- `src/open_ems/web/templates/installer/_setup_constraints_activate.html`
- `src/open_ems/web/templates/installer/_setup_constraints_error_banner.html`
- `src/open_ems/web/templates/installer/_setup_constraints_error.html`
- `src/open_ems/web/templates/installer/setup_validation_placeholder.html`

**Modified source files:**
- `src/open_ems/core/constraints.py` — EV window fields on `ActiveConstraints` / `ActiveConstraintsInput`; new `ConstraintCheckResult` / `ConstraintValidationReport` / `ConstraintDraftInput` / `ConstraintActivationResult` value types; `_validate_hh_mm` helper.
- `src/open_ems/storage/repositories/config_repo.py` — split into `activate` + `_activate_locked`; EV window persisted on INSERT and read by `get_active`; `_build_audit_changes` emits the combined `ev_charging_window` row.
- `src/open_ems/storage/repositories/config_audit_repo.py` — `SAFETY_RELEVANT_CONFIG_FIELDS` adds `'ev_charging_window'`.
- `src/open_ems/storage/repositories/wizard_state_repo.py` — `step_3_*` triple on the `WizardState` view + the `_step_3_paired` model_validator; `set_step_3_complete` + `set_step_3_complete_locked` (idempotent advance, mirroring 9.2's split).
- `src/open_ems/web/routes/setup.py` — `_constraints_service` Depends helper; placeholder `get_constraints_placeholder` replaced by real `get_constraints_page` + 4 new POST routes + `get_validation_placeholder`; `ConstraintDraftInput` import.
- `src/open_ems/web/app.py` — lifespan step 4f constructs `DraftConstraintsRepo` + `ConstraintsService` reusing the same `ConfigRepo` instance the provider holds; exposes both via `app.state`.
- `src/open_ems/web/templates/installer/setup_layout.html` — Step 2 conditional back-link.

**Deleted source files:**
- `src/open_ems/web/templates/installer/setup_constraints_placeholder.html`

**New test files:**
- `tests/unit/storage/repositories/test_draft_constraints_repo.py`
- `tests/unit/services/test_constraints_service.py`
- `tests/unit/web/test_setup_constraints_routes.py`
- `tests/integration/web/test_setup_constraints_e2e.py`
- `tests/integration/web/test_setup_constraints_a11y.py`

**Modified test files:**
- `tests/integration/test_migrations.py` — new schema-shape tests for `active_constraints` (EV window) + `draft_constraints` + extended `wizard_state`; new `test_migration_0011_round_trips`; existing 0009/0010 round-trip tests adjusted for the new top revision.
- `tests/unit/storage/repositories/test_config_repo.py` — inline DDL extended with EV window columns + paired-NULL CHECK; new EV-window audit emission tests; new `test_activate_locked_does_not_acquire_lock`.
- `tests/unit/storage/repositories/test_wizard_state_repo.py` — inline DDL extended with `step_3_*` columns; new `set_step_3_complete*` tests including pairing invariant; preserves first-completion idempotency.
- `tests/unit/services/test_active_constraints.py` — inline DDL extended with EV window columns.
- `tests/unit/web/test_setup_routes.py`, `tests/unit/web/test_setup_roles_routes.py`, `tests/integration/web/test_setup_roles_e2e.py`, `tests/integration/web/test_setup_discovery_e2e.py` — inline `wizard_state` DDL extended with `step_3_*` columns.
- `tests/unit/web/test_setup_roles_routes.py` — removed obsolete `test_get_constraints_renders_placeholder`.
- `tests/integration/web/test_app_startup.py` — new `test_lifespan_wires_story_9_3_constraints_service_on_app_state`.
- `tests/integration/engine/test_constraints_reload.py` — new `test_route_driven_activate_calls_config_repo_and_provider_reload_exactly_once` (closes the 9.0b reload contract loop with a real route trigger).
