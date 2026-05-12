# Story 10.2: Implement EV status card with 4-state optimistic override and StateStore-backed persistence

Status: done
_Status: ready-for-dev → in-progress → review by bmad-dev-story on 2026-05-12._
_Status set to done by bmad-code-review on 2026-05-12 after review-closure gate passed (5b): 16 resolved, 7 dismissed, 10 deferred-and-verified._

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

> **Type:** Epic 10 homeowner control surface. First homeowner-originated `DeviceCommand` in the codebase.
> **A2-triggered:** **Yes.** Three triggers match — **T1 (lifecycle / state-machine), T2 (retries / cancellation / idempotency), T4 (watchdog / timing semantics)**. All seven R1–R7 artifacts are mandatory and rendered below in Dev Notes → "Orchestration Risk Analysis".
> **Prerequisite (hard):** Story 9-X (Wire adapter map into PolicyGuard and ControlLoop, done 2026-05-11) — without 9-X the `Optimistic → Confirmed` transition cannot fire (PolicyGuard would reject every `/actions/ev-override` at P1 `adapter_not_registered`, and no `EVChargerState.session_active=true` would ever appear in StateStore on the populated path). See epics.md:2076 and the 9-X completion notes for the closure of Story 8-2's `[app.py:223-236]` finding.
> **Sibling stories:** 10.1 (status headline + metric cards, done 2026-05-11) established the dashboard shell, the per-card fragment pattern, the AC19-Path-B static CSS bundle, and the `SystemSnapshot.active_strategy` precedent for adding a snapshot-level homeowner-control field with a cold-start default at the StateStore boundary. 10.3 (inline strategy selector, backlog) will add a parallel mutator for `active_strategy`; coordinate the writer-lock contract there with the override mutator added here. 10.4 (weekly summary) is independent.
> **Follow-on:** 10.3 owns `POST /actions/set-strategy`. After both 10.2 and 10.3 land, the dashboard exposes both homeowner write surfaces; 10.4 adds the secondary weekly-summary surface.

## Story

As a homeowner,
I want to see the next scheduled EV charging session and trigger an immediate override with a single tap that gives me instant feedback confirmed by actual device state,
so that I am confident the system received and applied my request — not just that an API call succeeded.

## Acceptance Criteria

**Given** the runtime adapter map is populated (post-9-X), `StateStore` is publishing `EVChargerState` snapshots, and the homeowner is authenticated on `/homeowner/dashboard`

**When** this story is implemented

**Then** the following acceptance criteria must hold:

1. **AC1 — `EVOverrideState` model exists.** A new immutable Pydantic model lives at `src/open_ems/core/state.py` (alongside `SystemSnapshot`, or in a new sibling module `src/open_ems/core/ev_override.py` if the author prefers — placement under `core/` is required; `__init__.py` MUST export the symbol). Fields:
   - `correlation_id: uuid.UUID` — matches the dispatched `SetEVChargingRateCommand.correlation_id` for audit correlation (1:1 round-trip).
   - `requested_at: datetime` (UTC-required via `_require_utc` from `core.devices` — match the existing pattern at `core/devices.py:127–130`).
   - `expires_at: datetime` (UTC-required) — combined "override active window" and idempotency-dedup deadline. Once `captured_at >= expires_at` in a `StateStore.publish(...)` call, the override MUST be cleared (AC8).
   - `dispatch_status: Literal["pending", "failed", "timeout", "rejected"]` — terminal categories drawn from `CommandStatus` (`core/commands.py:57-69`). `pending` covers both pre-dispatch and post-success-pre-session-active windows. `success` is NOT a value: a successful dispatch leaves `dispatch_status="pending"` and the UI transitions to `Confirmed` when `EVChargerState.session_active=true` is observed in the snapshot (AC6 / AC10).
   - `failure_reason: str | None = None` — the exact `CommandResult.reason` string when `dispatch_status in {failed, timeout, rejected}`; `None` when `dispatch_status == "pending"`. **Model validator MUST assert** `failure_reason is None iff dispatch_status == "pending"`.
   - `session_observed_active: bool = False` — flipped to `True` by `StateStore.publish(...)` the first time `EVChargerState.session_active=true` is observed in a snapshot while this override is the active one (AC8 natural-completion clearing depends on this).
   - `model_config = ConfigDict(frozen=True, extra="forbid")` — match the immutability invariant used everywhere else in `core/`. Updates by other writers use `model_copy(update={...})`.

2. **AC2 — `SystemSnapshot.active_ev_override` field added.** `SystemSnapshot` (`src/open_ems/core/state.py:146`) gains `active_ev_override: EVOverrideState | None = None`. **Note the design deviation from 10.1's `active_strategy`:** this field DOES carry a Pydantic-level default (`= None`) because the cold-start value (`None` — no active override) is a structural sentinel, not a domain enum member. The model validator at `state.py:168` is unaffected (no new mapping field). Immutability semantics preserved.

3. **AC3 — `StateStore` gains `_active_ev_override` plus `set_ev_override(...)` mutator.** `StateStore.__init__` (`src/open_ems/core/state_store.py:39`) stores `self._active_ev_override: EVOverrideState | None = None`. The initial `SystemSnapshot` constructed at lines 51–63 carries `active_ev_override=None`. New mutator:

   ```python
   async def set_ev_override(self, override: EVOverrideState | None) -> None:
       """Install or clear the active EV override under the writer lock.

       The next publish() call will carry the new value into the published
       snapshot. Setting to ``None`` clears the override immediately.
       """
       async with self._writer_lock:
           self._active_ev_override = override
   ```

   The mutator MUST take the writer lock — this closes the same drift-risk class flagged in the 10.1 review-deferred item `[src/open_ems/core/state_store.py:46, 92]` for `_active_strategy` (which is now the canonical pattern for 10.3 to follow). The 10.2 mutator is the first concrete proof of the pattern; document the contract in `StateStore`'s class docstring with a one-line "All single-field writes covered by the writer lock; readers (publish) acquire the same lock — no torn reads" note.

4. **AC4 — `StateStore.publish(...)` carries `active_ev_override` and enforces expiry + natural-completion clearing.** Inside `publish(...)` (`src/open_ems/core/state_store.py:69`), under the existing writer lock, evaluate the override before constructing the new snapshot:

   - **Expiry clear:** if `self._active_ev_override is not None and self._active_ev_override.expires_at <= captured_at`, set `self._active_ev_override = None` (log at INFO once: event `ev_override_expired`, fields `correlation_id`, `dispatch_status`).
   - **Session-active flip detection:** if `self._active_ev_override is not None` AND `slots[DeviceRole.ev_charger]` is an `EVChargerState` AND `slots[DeviceRole.ev_charger].session_active is True` AND `self._active_ev_override.session_observed_active is False`, update `self._active_ev_override = self._active_ev_override.model_copy(update={"session_observed_active": True})` and log INFO event `ev_override_confirmed_observed` with `correlation_id`.
   - **Natural-completion clear:** if `self._active_ev_override is not None` AND `self._active_ev_override.session_observed_active is True` AND (slot is missing OR not `EVChargerState` OR `session_active is False`), set `self._active_ev_override = None` and log INFO event `ev_override_session_completed` with `correlation_id`. (Rationale: the override served its purpose; the next snapshot naturally returns the EV card to Idle without homeowner action.)
   - **Carry-through:** the published `SystemSnapshot` carries the (possibly-mutated, possibly-cleared) `self._active_ev_override` reference.

   These three clauses are evaluated IN ORDER inside the same lock. The ordering is load-bearing: an expired override clears before session-flip detection runs (so `session_observed_active=True` on an expired override does not leak into a downstream snapshot).

5. **AC5 — `POST /actions/ev-override` route exists.** A new router file `src/open_ems/web/routes/actions.py` (new file — there are no `/actions/*` routes today; this is the first) exposes:

   ```python
   @router.post("/actions/ev-override")
   async def post_ev_override(
       request: Request,
       _user: HomeownerUser = Depends(require_homeowner),
       state_store: StateStore = Depends(get_state_store),
       policy_guard: PolicyGuard = Depends(get_policy_guard),
       settings: Settings = Depends(get_settings_dep),
   ) -> JSONResponse:
       ...
   ```

   The router MUST be registered in `web/app.py:create_app()` between `homeowner_router` and `stream_router` (matching the route-loading order convention). The route MUST be protected by `CsrfMiddleware` (it is, automatically — `CsrfMiddleware` is universal except for the explicit `_CSRF_EXEMPT_PATHS = {"/login", "/api/stream/state"}` at `web/csrf.py:15`; do NOT add `/actions/ev-override` to the exempt set).

   A new dependency helper `get_policy_guard(request: Request) -> PolicyGuard` is added to `web/dependencies.py` (mirror `get_state_store` at line 83): reads `request.app.state.policy_guard`, raises `HTTPException(503, "PolicyGuard unavailable")` when missing. A new dependency helper `get_settings_dep(request: Request) -> Settings` reads `request.app.state.settings` OR re-imports `get_settings()` from `open_ems.settings` (mirror the pattern in `routes/setup.py` — confirm the existing convention and follow it; do not invent a new one). **DO NOT** use `Depends(get_settings)` from `open_ems.settings` directly unless that function is already FastAPI-dependency-compatible — verify by grep before using.

   **Route name in epics.md is canonical: `/actions/ev-override`.** The architecture document and UX spec use the older name `/actions/charge-now` in some sections; epics.md is the more recent authoritative spec, and the dev agent MUST use `/actions/ev-override`. The architecture doc (`architecture.md:270, 459, 665, 880`) and UX spec (`ux-design-specification.md:406, 837`) reference `/actions/charge-now` — these are pre-Epic-10-finalization references and need NOT be updated as part of this story (a follow-up doc-sync pass can address them; tracked as AC21).

6. **AC6 — Route flow (happy path).** On POST:

   1. Read `snapshot = state_store.get_snapshot()`. If `snapshot.ev_charger` is NOT an `EVChargerState` (None or `DegradedDeviceState`), return `400` JSON `{"status": "failed", "message": "EV charger is not available right now.", "action_id": None, ...}` — no override is installed. **Audit:** emit a CONTROL audit row via `request.app.state.observability.audit(...)` event `ev_override_rejected_no_charger` with no PII.
   2. Read `snapshot.active_ev_override`. If it is not `None` AND its `dispatch_status == "pending"` AND `now < expires_at`, return `200` with the existing override's `correlation_id` as `action_id` and a `success` status, **without** installing a new override or dispatching a new command. This is the idempotency-window contract: duplicate POSTs received during an active override are treated as success (AC of the story: "duplicate requests received within an active override window are treated as success without re-triggering the override"). Audit: emit `ev_override_idempotent_replay` with the existing `correlation_id`.
   3. If the existing override is `None`, OR has `dispatch_status in {failed, timeout, rejected}`, OR `now >= expires_at`: build a NEW `EVOverrideState` with a fresh `uuid.uuid4()`, `requested_at=datetime.now(UTC)`, `expires_at=requested_at + timedelta(seconds=settings.ev_override_window_seconds)`, `dispatch_status="pending"`, `failure_reason=None`, `session_observed_active=False`. Call `await state_store.set_ev_override(new_override)`.
   4. Build a `SetEVChargingRateCommand` with:
      - `device_id = snapshot.ev_charger.device_id`
      - `device_role = DeviceRole.ev_charger`
      - `origin = CommandOrigin.homeowner` (already exists at `core/commands.py:53` — first production caller of this enum member)
      - `correlation_id = new_override.correlation_id` (1:1 round-trip)
      - `rate_kw = settings.ev_override_default_rate_kw` (new Settings field — AC17)
   5. Return `200` JSON immediately: `{"status": "success", "message": "EV charging has been requested.", "action_id": str(new_override.correlation_id), "timestamp": "<iso>", "details": {}}`. This satisfies the UX spec's 2-second route-response contract (line 1840).
   6. Use `asyncio.create_task(...)` to fire-and-forget the dispatch task `_dispatch_and_settle_ev_override(...)` defined in `routes/actions.py`. The task name MUST be `f"ev_override_dispatch_{correlation_id}"` for log traceability. **Add a `task.add_done_callback(_log_task_completion)` so unhandled exceptions in the background task surface in logs** (Story 8-1 / 9-X precedent — see `web/app.py:574–577` `make_on_control_loop_done`).

7. **AC7 — Background dispatch task: `_dispatch_and_settle_ev_override`.** The task is the asynchronous half of the route. It runs concurrently with the route's response and is responsible for translating `PolicyGuard.authorize_and_dispatch` into a terminal `EVOverrideState`:

   1. Awaits `result = await policy_guard.authorize_and_dispatch(command)`. PolicyGuard already enforces its own `COMMAND_DISPATCH_TIMEOUT_SECONDS = 10.0` outer timeout (`engine/policy_guard.py:168, 259-262`) so the task does NOT add a second timeout layer.
   2. **Concurrency check:** after awaiting, reads `current = state_store.get_snapshot().active_ev_override`. If `current is None` OR `current.correlation_id != command.correlation_id`, log INFO event `ev_override_dispatch_result_orphan` with both correlation IDs and exit — a newer override (or an expiry/natural-completion clear) has superseded this one; the task MUST NOT clobber it. This closes the race where StateStore's expiry/completion logic clears the override between dispatch start and dispatch finish.
   3. If `result.status is CommandStatus.success and result.applied is True`: log INFO event `ev_override_dispatched_success` and EXIT WITHOUT MUTATING the override (`dispatch_status` stays `"pending"` until the next `StateStore.publish(...)` observes `EVChargerState.session_active=true` per AC4's session-flip detection). This is the structural enforcement of the AC "Confirmed state is entered only when `EVChargerState.session_active = true` is observed in the `StateStore` snapshot — API success response alone is not sufficient" (epics.md:2146).
   4. Else (status in `{failed, timeout, rejected, correlation_broken}`): build `updated = current.model_copy(update={"dispatch_status": <status>, "failure_reason": result.reason})` and call `await state_store.set_ev_override(updated)`. The status mapping is direct: `CommandStatus.failed → "failed"`, `CommandStatus.timeout → "timeout"`, `CommandStatus.rejected → "rejected"`, `CommandStatus.correlation_broken → "failed"` (the correlation_broken case carries the indeterminate-applied semantics into the same Fallback UX state — the homeowner sees "Could not start charging" with the `adapter_correlation_id_mismatch` reason string surfaced via the plain-language mapping in AC11).
   5. **Cancellation:** wrap the awaited `authorize_and_dispatch` in a `try: ... except asyncio.CancelledError: raise` re-raise (do NOT swallow `CancelledError` — Story 8-4 retro precedent and AR15-style discipline). On process shutdown the task is cancelled and the override is left in `pending` state; cold-start clears it (AC9 R5).
   6. Use `structlog.get_logger(__name__).bind(component="actions", correlation_id=str(correlation_id))` so every log line in the task carries the override correlation ID.

8. **AC8 — Settings fields added.** `src/open_ems/settings.py` (the `Settings` `pydantic_settings.BaseSettings` class) gains three new fields:
   - `ev_override_window_seconds: int = 7200` — total active-override window (2 hours by default; the override expires after this regardless of dispatch state). MUST be `> 0`; add a `Field(gt=0)` constraint.
   - `ev_override_confirmation_timeout_seconds: int = 30` — UI-side `aria-live` confirmation budget; **NOT enforced server-side** in 10.2 (the Alpine.js client uses this to display Fallback if no `EVChargerState.session_active=true` observed within the timeout). Exposed via the dashboard shell as a `data-*` attribute or a module-level JS constant rendered into the template. MUST be `>= 5 and <= 60`.
   - `ev_override_default_rate_kw: float = 11.0` — the `SetEVChargingRateCommand.rate_kw` value the route sends. MUST be `>= 0.0`. The PolicyGuard P4 capability check at `engine/policy_guard.py:249` will reject this command if the EV charger's profile lacks `WriteCapability.set_charge_rate` — that is the correct behavior (the override surfaces a Fallback with `reason="capability_missing: set_charge_rate"` and the homeowner sees the plain-language fallback). Document this in the field docstring.

   Settings docs: each field gets a one-line docstring stating its purpose. NO `.env.example` change is required (Settings reads from environment variables but defaults are sufficient for v1).

9. **AC9 — `ControlLoop._build_evaluation_input` wires `homeowner_override_active`.** `src/open_ems/engine/control_loop.py:210` currently hardcodes `homeowner_override_active=False`. Replace with:

   ```python
   homeowner_override_active=(
       snapshot.active_ev_override is not None
       and snapshot.active_ev_override.dispatch_status == "pending"
   ),
   ```

   The decision engine then permits EV charging outside the configured `charging_window` while an override is active (per `engine/rules/ev_scheduling.py:67-81`). This is the structural connection between the homeowner control surface and the decision engine — without this AC, the override would dispatch a single command but subsequent ticks would re-dispatch a `stop_ev_charging` intent if the override falls outside the configured window. **Important:** the override only flips `homeowner_override_active=True` while `dispatch_status="pending"`; once it transitions to a terminal failure state OR is cleared (expiry/natural-completion), the flag falls back to `False` and the scheduling rule resumes window-only evaluation. No other ControlLoop behavior changes.

10. **AC10 — Homeowner snapshot serialization exposes `active_ev_override`.** `serialize_homeowner_snapshot` (`src/open_ems/web/state_serialization.py:51`) adds `"active_ev_override": _serialize_homeowner_ev_override_state(snapshot.active_ev_override)` to the payload. The installer serializer (`serialize_installer_snapshot`, line 69) likewise adds the field. The helper:

    ```python
    def _serialize_homeowner_ev_override_state(
        override: EVOverrideState | None,
    ) -> dict[str, object] | None:
        if override is None:
            return None
        return {
            "correlation_id": str(override.correlation_id),
            "requested_at": _datetime_to_iso(override.requested_at),
            "expires_at": _datetime_to_iso(override.expires_at),
            "dispatch_status": override.dispatch_status,
            "failure_reason": override.failure_reason,
            "session_observed_active": override.session_observed_active,
        }
    ```

    All fields are homeowner-safe — `correlation_id` is an opaque UUID (no PII), `dispatch_status` values are the same audit-vocabulary strings the SSE stream already exposes via `CommandResult` (this is unchanged from the existing `serialize_homeowner_snapshot` payload contract). The installer serializer emits the same shape (no extra fields).

11. **AC11 — `build_homeowner_ev_card_context(...)` derives the 4-state model.** A new builder function in `state_serialization.py`:

    ```python
    def build_homeowner_ev_card_context(
        snapshot: SystemSnapshot,
    ) -> dict[str, object]:
        """Build the homeowner EV-card fragment context (AC11–AC14).

        Reads ``snapshot.active_ev_override`` and ``snapshot.ev_charger`` and
        returns the 4-state UI model. The function MUST NOT reference any
        other device slot or system field; it is the structural enforcement
        of 'EV card state is derived from override + session_active only'.
        """
        ...
    ```

    Returns a dict with these keys (all strings or scalars — Jinja2-safe):
    - `card: "ev"`
    - `title: "EV charger"`
    - `override_state: Literal["idle", "optimistic", "confirmed", "fallback"]` — the 4-state UI model.
    - `button_label: str` — `"Charge EV now"` (idle), `"Starting charging…"` (optimistic), `"Charging now"` (confirmed), `"Could not start charging"` (fallback). Exact strings from UX spec §"EV Override Button" (lines 1090–1095).
    - `button_disabled: bool` — `False` only when `override_state == "idle"`. UX spec idempotency rule.
    - `aria_live_message: str` — short accessible status separate from button text per UX spec line 1619. Empty for idle. `"Starting EV charging."` for optimistic. `"EV charging started."` for confirmed. `"Could not start charging — {plain-language reason}."` for fallback.
    - `constraint_notice: str` — populated ONLY when `override_state == "confirmed"` AND the snapshot indicates a peak-limit-active condition. For v1: emit `"Peak limit still active — charge rate may be adjusted if household load is high."` whenever `override_state == "confirmed"` (the conditional is the snapshot's `operating_mode != fail_safe` — peak limits are always active outside fail-safe). Empty string otherwise. UX spec line 410, 1115.
    - `retry_visible: bool` — `True` only when `override_state == "fallback"`. UX spec line 1095.
    - `failure_reason_plain: str` — empty unless `override_state == "fallback"`. Mapped from `EVOverrideState.failure_reason` (the raw `CommandResult.reason` string) via a module-level `_OVERRIDE_FAILURE_REASONS: Mapping[str, str]` table — see AC12 for the required entries.
    - `next_session_summary: str` — the next-scheduled-session line shown in idle state (FR27). Sourcing: if `active_constraints.ev_charging_window` is configured, render `"Charging tonight at {start_local_time:%H:%M}"` (UX spec line 836) when the window has not yet started today, else `"Charging window active"`. If no window is configured, render `"No charging session scheduled"`. **The builder takes a second argument `active_constraints: ActiveConstraints | None`** for this purpose; the fragment route MUST inject it via `Depends(get_active_constraints)`. Empty string when `override_state != "idle"`.
    - `unavailable: bool` — `True` when `snapshot.ev_charger is None` OR an instance of `DegradedDeviceState`. The template's unavailable branch renders the UX spec line 1503 fallback: `"No EV charger connected"` in `--color-text-tertiary`, button hidden.

    **Determinism contract:** `override_state` is computed exclusively from `snapshot.active_ev_override.dispatch_status` and `snapshot.ev_charger.session_active`. The function MUST NOT consult `snapshot.operating_mode`, `snapshot.data_age_seconds`, or `snapshot.component_states` for override-state derivation (use of `operating_mode` for the `constraint_notice` text is allowed but does not influence `override_state`). A unit test enforces this structurally per AC18.

12. **AC12 — `_OVERRIDE_FAILURE_REASONS` plain-language mapping.** A module-level constant in `state_serialization.py`:

    ```python
    _OVERRIDE_FAILURE_REASONS: Mapping[str, str] = {
        # PolicyGuard P0–P4 (see engine/policy_guard.py module docstring)
        "fail_safe_mode_active": "The system is paused for safety right now.",
        "adapter_not_registered": "The charger is not connected to the system.",
        "capability_check_failed": "The charger did not respond. Try again or check the connection.",
        "unknown_command_type": "The charger did not respond. Try again or check the connection.",
        # P4 has a structured suffix; the lookup MUST strip-prefix-match
        "capability_missing": "Your charger does not support remote charging.",
        # Safety constraints (engine/policy_guard.py docstring lines 76–104)
        "battery_state_unavailable_for_safety_check": "The system is currently checking battery status. Try again shortly.",
        "battery_soc_at_or_below_reserve_floor": "Battery is at the reserved level — charging will resume from grid only.",
        "commanded_rate_exceeds_peak_limit": "Charging rate would exceed your peak limit. Lower the limit or wait.",
        "conservative_mode_blocks_load_increase": "The system is in recovery mode. Try again shortly.",
        # Dispatch failures
        "command_timeout": "The charger did not respond in time. Check that it is powered on.",
        "adapter_correlation_id_mismatch": "The charger response could not be verified. Try again.",
    }
    ```

    Lookup function `_resolve_failure_reason_plain(reason: str | None) -> str`:
    - Returns `""` if `reason is None`.
    - Direct dict-`.get(reason, None)` lookup — if hit, return it.
    - If miss AND `reason.startswith("capability_missing:")` → return `_OVERRIDE_FAILURE_REASONS["capability_missing"]` (P4 structured-suffix handling).
    - Otherwise (unknown reason from a future code path) → return `"Could not start charging. Try again."` (calm catch-all per UX spec "calm over urgency" line 154; no raw error code surfaces to the homeowner).

    A unit test (AC18) asserts every PolicyGuard rejection-reason string named in `engine/policy_guard.py` is covered by this table (exhaustiveness check via grep-derived test fixture — see Test 11 in AC18). New rejection reasons added in future stories MUST extend this table.

13. **AC13 — Fragment endpoint `GET /fragments/homeowner/ev-card` re-purposed.** `src/open_ems/web/routes/fragments.py:64-70` (the existing `homeowner_ev_card` endpoint added in Story 5-3 and preserved through 10-1) is rewritten to call `build_homeowner_ev_card_context(snapshot, active_constraints)` instead of `build_homeowner_card_context(snapshot, "ev")`. The endpoint signature gains an `active_constraints: ActiveConstraints = Depends(get_active_constraints)` parameter. **Add the dependency helper** `get_active_constraints(request: Request) -> ActiveConstraints` to `web/dependencies.py`:

    ```python
    def get_active_constraints(request: Request) -> ActiveConstraints:
        provider = getattr(request.app.state, "active_constraints_provider", None)
        if provider is None:
            raise HTTPException(503, "Active constraints unavailable")
        return provider.get()
    ```

    The template path remains `fragments/homeowner/ev-card.html` (AC14 rewrites the template content).

14. **AC14 — EV card template rewritten.** `src/open_ems/web/templates/fragments/homeowner/ev-card.html` (currently a one-line `{% include "fragments/homeowner/_card.html" %}` — Story 10-1 left the EV branch in a transitional state) is rewritten as a standalone template — NOT including `_card.html` — because the EV card has structurally different anatomy (button + retry link + constraint notice) than the read-only battery/solar/grid cards. The rewritten template:

    ```jinja2
    <section
      id="ev-card"
      class="state-card state-card--{{ override_state }}{% if unavailable %} state-card--unavailable{% endif %}"
      data-override-state="{{ override_state }}"
      data-correlation-id="{{ active_ev_override.correlation_id if active_ev_override else '' }}"
      aria-label="{{ title }}"
      x-data="evOverride({initialState: '{{ override_state }}'})"
    >
      <header class="state-card__header">
        <h2>{{ title }}</h2>
      </header>
      <div class="state-card__body" aria-live="polite">
        {% if unavailable %}
          <p class="state-card__unavailable">No EV charger connected</p>
        {% else %}
          {% if override_state == "idle" %}
            <p class="state-card__ev-next-session">{{ next_session_summary }}</p>
          {% endif %}
          <button
            type="button"
            class="state-card__ev-button state-card__ev-button--{{ override_state }}"
            hx-post="/actions/ev-override"
            hx-headers='{"X-CSRF-Token": "{{ csrf_token }}"}'
            hx-swap="none"
            x-bind:disabled="state !== 'idle'"
            aria-disabled="{{ 'false' if not button_disabled else 'true' }}"
            x-on:click="submit()"
            x-text="buttonLabel('{{ button_label }}')"
          >{{ button_label }}</button>
          <span class="visually-hidden" aria-live="polite" role="status">{{ aria_live_message }}</span>
          {% if constraint_notice %}
            <p class="state-card__ev-constraint-notice">{{ constraint_notice }}</p>
          {% endif %}
          {% if retry_visible %}
            <p class="state-card__ev-failure-reason">{{ failure_reason_plain }}</p>
            <button
              type="button"
              class="state-card__ev-retry-link"
              hx-post="/actions/ev-override"
              hx-headers='{"X-CSRF-Token": "{{ csrf_token }}"}'
              hx-swap="none"
              x-on:click="retry()"
            >Try again</button>
          {% endif %}
        {% endif %}
      </div>
    </section>
    ```

    **Important template invariants:**
    - The `<section>` is the HTMX swap target. The fragment endpoint returns this exact `<section>` (`outerHTML` swap from `dashboard.html`).
    - `csrf_token` MUST be added to the fragment context (the dashboard-shell HTMX header `X-CSRF-Token` is set by the `htmx:configRequest` listener in `base.html`, BUT in-fragment buttons inserted via `outerHTML` swap need explicit `hx-headers` so that newly-rendered elements pick up the token without relying on listener timing). The fragment route MUST add `"csrf_token": _user.csrf_token` to the context — change `homeowner_ev_card` to inject `_user: HomeownerUser = Depends(require_homeowner)` and pull the token off `_user`.
    - The Alpine.js `x-data="evOverride(...)"` factory is registered via a `<script>` block in `dashboard.html` (NOT inline in the fragment — fragments re-rendered on every poll would re-register the factory N times). See AC15.
    - **Layout stability (UX spec line 1114):** The button is always present in the same position; only `class`/`label`/`disabled`-state changes across `override_state` values. The retry-link is rendered ONLY in fallback state, in a stable DOM slot below the button. The CSS bundle (AC16) MUST reserve the retry-link slot height so the card does not shift when retry appears/disappears (use `min-height` on the wrapper or a `visibility: hidden` placeholder).

15. **AC15 — Alpine.js override factory registered in dashboard shell.** `src/open_ems/web/templates/dashboard.html` (the shell rendered by `routes/homeowner.py`) re-adds the EV card slot AND adds a `{% block scripts %}` with an Alpine.js component factory:

    ```html
    <section
      id="ev-card"
      hx-get="/fragments/homeowner/ev-card"
      hx-trigger="load, every 10s"
      hx-swap="outerHTML"
    >Loading EV charger...</section>
    ```

    Place it between the solar-card and grid-card sections per UX spec §"Homeowner dashboard hierarchy" line 648 (the spec lists: headline → battery → solar → grid → EV → weekly summary; AC10 of Story 10.1 chose `headline → battery → solar → grid` and deferred EV insertion to 10.2 — re-add EV BETWEEN grid and the eventual weekly-summary slot. **Re-read the UX spec ordering: line 648 places EV AFTER grid, line 552 confirms "single column at all breakpoints".** Insert AFTER `grid-card` to match the UX spec ordering).

    The Alpine.js factory is added to the `{% block scripts %}` in `dashboard.html`:

    ```html
    {% block scripts %}
    <script>
      document.addEventListener('alpine:init', () => {
        Alpine.data('evOverride', (config) => ({
          state: config.initialState,
          submit() {
            if (this.state !== 'idle') return;
            this.state = 'optimistic';
            // The HTMX request fires automatically via hx-on; we only mutate the optimistic layer.
          },
          retry() {
            // Re-enable the button (transition fallback → idle so the next tap dispatches).
            this.state = 'idle';
          },
          buttonLabel(serverLabel) {
            // Render server-authored label; client never authors button text.
            return serverLabel;
          }
        }));
      });
    </script>
    {% endblock %}
    ```

    **Alpine.js MUST be served from `/static/`** per UX spec line 1670 ("No CDN for any asset"). Story 10.1 deferred the Alpine.js inclusion because 10.1 added no client-state. Story 10.2 introduces it: add Alpine.js 3.x to `src/open_ems/web/static/alpine.min.js` (download from `https://unpkg.com/alpinejs@3.x.x/dist/cdn.min.js` and commit the pinned file — note version + SHA256 in a static/README.md). Link it from `base.html`:

    ```html
    <script src="/static/alpine.min.js" defer></script>
    ```

    `defer` is required because the `alpine:init` listener relies on Alpine loading after the DOM is ready. Confirm Alpine version compatibility with the `x-on:`, `x-bind:`, `x-data`, `x-text` patterns used in AC14 — Alpine 3.x supports all of them natively.

    **Optimistic-to-server reconciliation:** The Alpine state machine ONLY manages the `Idle → Optimistic` transition. The Server is authoritative for `Optimistic → Confirmed` and `Optimistic → Fallback`: when HTMX polls every 10s, the fragment endpoint returns the freshly-derived `override_state` from `build_homeowner_ev_card_context`. The `<section>` is replaced via `outerHTML` swap, which re-instantiates Alpine state from the new `x-data` config. The Alpine optimistic-state value is OVERWRITTEN on every poll — this is the structural enforcement of the epics.md cross-story constraint "SSE state is authoritative and overrides Alpine optimistic state on receipt" (epics.md:2163).

16. **AC16 — CSS additions for EV card states.** `src/open_ems/web/static/open-ems.css` (the hand-authored CSS bundle established by Story 10.1 AC19 Path B) gains the EV-card-specific rules. Use the existing CSS custom properties (lines 482–486 of UX spec — `--color-accent`, `--color-accent-hover`, `--color-degraded-bg`, `--color-text-secondary`). New rule set:

    ```css
    .state-card__ev-button {
      display: block;
      width: 100%;
      height: 52px; /* UX spec §"EV override button" line 562 */
      border: 0;
      border-radius: 8px;
      font-size: 1rem;
      font-weight: 500;
      cursor: pointer;
      color: white;
    }
    .state-card__ev-button--idle        { background: var(--color-accent); }
    .state-card__ev-button--optimistic  { background: var(--color-accent); animation: ev-pulse 1.4s ease-in-out infinite; cursor: not-allowed; }
    .state-card__ev-button--confirmed   { background: var(--color-accent-hover); cursor: not-allowed; }
    .state-card__ev-button--fallback    { background: var(--color-degraded-bg); color: var(--color-text-primary); cursor: not-allowed; }
    .state-card__ev-button:disabled     { opacity: 0.85; }
    .state-card__ev-button:focus-visible{ outline: 2px solid var(--color-accent); outline-offset: 2px; }
    .state-card__ev-retry-link          { font-size: 0.875rem; color: var(--color-accent); text-decoration: underline; background: none; border: 0; cursor: pointer; padding: 0; }
    .state-card__ev-constraint-notice   { font-size: 0.875rem; color: var(--color-text-secondary); margin-top: 8px; }
    .state-card__ev-failure-reason      { font-size: 0.875rem; color: var(--color-text-primary); margin-top: 8px; }
    .state-card__ev-next-session        { font-size: 1rem; color: var(--color-text-primary); margin-bottom: 12px; }
    .state-card__unavailable            { /* already exists from 10.1; do not duplicate */ }
    .visually-hidden                    { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0; }
    @keyframes ev-pulse                 { 0%, 100% { opacity: 1; } 50% { opacity: 0.6; } }
    @media (prefers-reduced-motion: reduce) {
      .state-card__ev-button--optimistic { animation: none; }
    }
    ```

    **No amber, no red, no warn classes** — UX spec design rule (lines 461, 1115). The optimistic pulse is the only animation and it respects `prefers-reduced-motion` per UX spec §"Motion" line 607. The pass-green color from `--color-pass` is **deliberately NOT used** for the Confirmed state — UX spec line 1097 explicitly mandates `--color-accent-hover` ("'Charging now' is an active operating state — not a PASS validation result. Pass-green is reserved exclusively for PASS validation states.") This is a load-bearing design rule; a regression test (AC18) asserts the rendered HTML for `override_state="confirmed"` contains `--color-accent-hover` CSS class fragment and does NOT contain `pass`, `--color-pass`, or any amber/red class.

17. **AC17 — Audit-log emissions.** `routes/actions.py` MUST emit `EventType.CONTROL` audit rows via `request.app.state.observability.audit(...)` (the existing ObservabilityService pattern — see `audit_log.py` module signature) at three points per Event Log Coverage rule (UX spec line 1915 "Every user action creates a log entry"):

    1. **Override accepted** (route step 3 after the StateStore mutator returns): event `homeowner_ev_override_requested`, fields `correlation_id`, `requested_at_iso`, `expires_at_iso`, `user_id`, `dispatch_status="pending"`. Authored_by="system" (the system records homeowner actions; "installer" / "homeowner" are role tags, not authorship in the audit-log schema).
    2. **Idempotent replay** (route step 2 idempotent return): event `homeowner_ev_override_idempotent_replay`, fields `existing_correlation_id`, `user_id`. (Lower-frequency event but captures the dedup-window successes.)
    3. **No-charger rejection** (route step 1): event `homeowner_ev_override_rejected_no_charger`, fields `user_id`. (No `correlation_id` — no command was built.)

    The background dispatch task emits one additional audit row (already covered by PolicyGuard's CONTROL audit on success/rejection — `engine/policy_guard.py` emits the audit; the route MUST NOT double-emit). The override's terminal transition (pending → failed/timeout/rejected, or natural-completion clear) is logged via structlog but NOT additionally audited — the PolicyGuard-side audit is the canonical record.

18. **AC18 — Unit tests.** All new tests follow the naming-honesty rule established by Story 9-Y-a (test name MUST describe what the test actually asserts).

    **`tests/unit/core/test_ev_override.py`** (new file, ~10 tests):
    1. `test_ev_override_state_constructs_with_required_fields` — model accepts the AC1 fields; correlation_id round-trips.
    2. `test_ev_override_state_rejects_naive_datetimes` — `requested_at` / `expires_at` must be UTC-aware (existing `_require_utc` enforcement).
    3. `test_ev_override_state_failure_reason_iff_terminal` — model validator rejects `dispatch_status="pending"` with non-None `failure_reason`, and rejects `dispatch_status in {failed, timeout, rejected}` with `failure_reason=None`.
    4. `test_ev_override_state_is_frozen` — `model_copy(update=...)` produces new instance; direct attribute set raises `ValidationError` (frozen model invariant).
    5. `test_system_snapshot_active_ev_override_defaults_to_none` — confirms AC2's structural default.
    6. `test_system_snapshot_with_active_ev_override_round_trips_via_model_validate` — JSON round-trip for SSE serialization parity.

    **`tests/unit/core/test_state_store.py`** (extend existing — Story 10.1 added 3 tests; add ~7 more):
    7. `test_state_store_initial_snapshot_carries_no_active_ev_override` — cold-start default per AC2/AC3.
    8. `test_state_store_set_ev_override_installs_override_under_writer_lock` — call mutator; next `publish()` returns a snapshot carrying the override.
    9. `test_state_store_set_ev_override_to_none_clears_override` — explicit None pass clears.
    10. `test_state_store_publish_clears_expired_override` — install override with `expires_at < now`; next `publish(...)` returns snapshot with `active_ev_override=None`.
    11. `test_state_store_publish_flips_session_observed_active_when_session_active_observed` — install pending override; publish snapshot with `EVChargerState(session_active=True)` → next snapshot's override has `session_observed_active=True`.
    12. `test_state_store_publish_clears_override_on_natural_session_completion` — override with `session_observed_active=True`; publish `EVChargerState(session_active=False)` → override cleared.
    13. `test_state_store_publish_does_not_clear_override_when_session_never_observed_active` — override that never saw `session_active=true` is NOT cleared by a `session_active=False` snapshot (would otherwise wrongly clear Optimistic overrides that haven't been confirmed yet).
    14. `test_state_store_clears_expired_override_before_session_flip_detection` — load-bearing ordering invariant from AC4.

    **`tests/unit/engine/test_control_loop.py`** (extend):
    15. `test_evaluation_input_homeowner_override_active_is_true_when_pending_override_exists` — AC9 wiring.
    16. `test_evaluation_input_homeowner_override_active_is_false_when_override_terminal` — covers `dispatch_status in {failed, timeout, rejected}` and `active_ev_override is None`. Parametrized.

    **`tests/unit/web/test_state_serialization.py`** (extend):
    17. `test_homeowner_serializer_includes_active_ev_override_when_present` — AC10.
    18. `test_homeowner_serializer_includes_active_ev_override_as_none_when_absent` — AC10.
    19. `test_build_homeowner_ev_card_context_idle_when_no_override` — AC11 idle path.
    20. `test_build_homeowner_ev_card_context_optimistic_when_pending_and_not_session_active` — AC11 optimistic path.
    21. `test_build_homeowner_ev_card_context_confirmed_when_pending_and_session_active` — AC11 confirmed path (key AC: "Confirmed state is entered only when `EVChargerState.session_active = true` is observed in the `StateStore` snapshot — API success response alone is not sufficient").
    22. `test_build_homeowner_ev_card_context_fallback_when_dispatch_status_failed` — parametrized across `{failed, timeout, rejected}` × failure_reason categories.
    23. `test_build_homeowner_ev_card_context_unavailable_when_ev_charger_is_none` — UX spec line 1503.
    24. `test_build_homeowner_ev_card_context_does_not_read_device_state_other_than_ev_charger` — structural AC11 invariant: pass two snapshots that differ ONLY in inverter/battery/grid_meter fields; assert identical `override_state` and `button_label`.
    25. `test_resolve_failure_reason_plain_covers_every_policy_guard_rejection_reason` — exhaustiveness: scan `engine/policy_guard.py` source for `"<reason>"` string literals matching the rejection-vocabulary pattern; assert each appears in `_OVERRIDE_FAILURE_REASONS` (or matches the `capability_missing:` prefix-rule).
    26. `test_resolve_failure_reason_plain_handles_capability_missing_suffix` — `capability_missing: set_charge_rate` → resolves to the `capability_missing` table entry.
    27. `test_resolve_failure_reason_plain_falls_back_for_unknown_reason` — unknown reason returns the calm catch-all per AC12.

    **`tests/unit/web/test_ev_card_template.py`** (new file or extend `test_fragment_routes.py`):
    28. `test_ev_card_renders_button_in_idle_state` — fragment GET; assert `data-override-state="idle"`, `aria-disabled="false"`, button label exact.
    29. `test_ev_card_renders_no_pass_green_class_in_confirmed_state` — AC16 load-bearing rule; assert rendered HTML for confirmed state contains NO `pass`, NO `--color-pass`, NO `green` token, NO `amber`/`red`/`warn` fragments.
    30. `test_ev_card_renders_retry_link_only_in_fallback_state` — parametrized across all four states.
    31. `test_ev_card_unavailable_when_ev_charger_is_none` — UX spec line 1503.
    32. `test_ev_card_unauth_request_redirects_to_login` — existing route auth contract.
    33. `test_ev_card_installer_request_redirects_to_role_home` — existing role contract.

    **`tests/unit/web/test_ev_override_route.py`** (new file):
    34. `test_post_ev_override_returns_400_when_no_ev_charger` — AC6 step 1.
    35. `test_post_ev_override_installs_pending_override_on_first_call` — AC6 step 3; assert `state_store.get_snapshot().active_ev_override` matches what was returned; correlation_id round-trips.
    36. `test_post_ev_override_idempotent_replay_within_window` — AC6 step 2; two consecutive POSTs within `ev_override_window_seconds` return the same `correlation_id` and the second call does NOT dispatch a new command.
    37. `test_post_ev_override_new_command_after_previous_failed` — AC6 step 3 with prior `dispatch_status="failed"`; new override installed; new correlation_id.
    38. `test_post_ev_override_new_command_after_expiry` — clock past `expires_at` of prior override → new override.
    39. `test_post_ev_override_requires_csrf_token` — POST without `X-CSRF-Token` returns 403 (existing CsrfMiddleware contract; the test confirms the route is in the middleware path, not exempted).
    40. `test_post_ev_override_unauth_returns_401_for_htmx_request` — existing role contract.
    41. `test_post_ev_override_installer_returns_403_for_htmx_request` — existing role contract.
    42. `test_post_ev_override_returns_within_2_seconds` — UX contract line 1840; assert `time.perf_counter()` delta on `TestClient.post(...)` < 2.0 averaged over 5 iterations (the background dispatch task is fire-and-forget so the route itself returns immediately).

    Tests use `pytest-asyncio` auto mode (existing convention). The background dispatch task is exercised through integration tests (AC19) — the unit-route tests stub `PolicyGuard.authorize_and_dispatch` via `monkeypatch` against a fake adapter so the route's synchronous-fast path is the unit of measurement.

19. **AC19 — Integration tests.**

    **`tests/integration/web/test_ev_override_e2e.py`** (new file):
    1. `test_ev_override_happy_path_idle_to_confirmed` — boots the app with the standard `app_e2e` fixture; seeds a populated snapshot via the test seam (`StateStore.publish(...)` with `EVChargerState(session_active=False, status="available")`); POSTs `/actions/ev-override`; awaits the background dispatch task (use `asyncio.wait_for` against `app.state.<test seam>` OR poll for the override's terminal state with a short timeout); publishes a follow-up snapshot with `session_active=True`; fetches `/fragments/homeowner/ev-card` and asserts `data-override-state="confirmed"` + button label "Charging now" + presence of `--color-accent-hover` class fragment + absence of any pass/green/amber class fragment.
    2. `test_ev_override_optimistic_to_fallback_on_dispatch_failure` — seed a fake adapter that returns `CommandResult(status=failed, applied=False, reason="capability_check_failed")`; POST; await background task; fetch fragment; assert `data-override-state="fallback"`, button label "Could not start charging", retry link visible, `failure_reason_plain` matches `_OVERRIDE_FAILURE_REASONS["capability_check_failed"]`.
    3. `test_ev_override_optimistic_to_fallback_on_policy_guard_rejection` — seed a snapshot with `operating_mode=SystemOperatingMode.fail_safe`; POST; assert PolicyGuard rejects at P0 → background task settles override to `dispatch_status="rejected"`, `failure_reason="fail_safe_mode_active"` → fragment renders fallback with the plain-language P0 reason.
    4. `test_ev_override_natural_session_completion_returns_to_idle` — confirmed override; subsequent snapshot with `session_active=False`; fragment renders idle.
    5. `test_ev_override_expiry_returns_to_idle` — pending override whose `expires_at < now`; next snapshot publish clears it; fragment renders idle.
    6. `test_ev_card_renders_unavailable_when_ev_charger_is_degraded` — snapshot with `ev_charger = DegradedDeviceState(...)`; fragment renders the UX spec line 1503 fallback.
    7. `test_ev_override_dispatches_with_homeowner_origin_and_correlation_id_matches_override` — assert the audit row (or the dispatched command captured via fake adapter) carries `origin=CommandOrigin.homeowner` (first production caller of this enum) and `correlation_id == override.correlation_id`.
    8. `test_control_loop_observes_homeowner_override_active_on_next_tick` — installs a pending override; runs one control-loop tick (use the existing `ControlLoop._tick()` test seam established in Story 8-1 / 9-X); asserts the EVSchedulingContext built by `_build_evaluation_input` has `homeowner_override_active=True`.
    9. `test_ev_override_a11y_placeholder` — `pytest.mark.xfail(strict=False) + pytest.mark.skipif(npx absent)` axe-playwright scan over the dashboard rendered with each of the 4 override_state values. Follows the Story 10.1 / 9.x precedent at `tests/integration/web/test_homeowner_dashboard_a11y.py`.

20. **AC20 — Quality gates.**
    - `uv run python -m pytest tests/ --no-cov -q` → all tests pass. Post-10.1 baseline is 1479 passed + 6 xfailed (the test count after 10.1's review-cycle patches; reference `_review_9-0_bundle.md` and the 10.1 completion notes). Net delta MUST be strongly positive: ≥35 new passing tests from the AC18/AC19 set above.
    - `uv run python -m ruff check .` → clean across the project.
    - `uv run python -m ruff format --check .` → clean.
    - `uv run python -m mypy src/` → clean across all source files. The new `EVOverrideState` model, `build_homeowner_ev_card_context`, `_OVERRIDE_FAILURE_REASONS` mapping, and the route module MUST type-check without `# type: ignore`. Any new `Mapping[str, str]` annotation MUST use `collections.abc.Mapping` (project convention — see `state_serialization.py:4`).
    - `uv.lock` MUST be unchanged. Alpine.js is a static asset, not a Python dependency. The OPEN-EMS zero-new-Python-dependency contract carries forward through Epic 10.

21. **AC21 — Sprint-status consistency + reference cleanup.** On story close:
    - `_bmad-output/implementation-artifacts/sprint-status.yaml` MUST set `10-2-implement-ev-status-card-with-4-state-optimistic-override-and-statestore-backed-persistence: done`. Epic 10 transition (`backlog → in-progress`) was made at Story 10.1 creation per the bmad-create-story workflow (Story 10.1 dev notes "Change Log" 2026-05-11).
    - **Stale-reference scan (informational; non-blocking):** the architecture document (`architecture.md:270, 459, 665, 880`) and UX spec (`ux-design-specification.md:406, 837`) reference the old `/actions/charge-now` route name. Epic 10's epics.md is authoritative for `/actions/ev-override`. The doc-sync edit can be done in this story OR deferred to a Epic 11 doc-sync sweep — dev agent's call; document the choice in completion notes. Per Epic 9 retro action A8 (Status header ↔ sprint-status agreement) and Epic 8 retro action A1/A2 (codified in bmad-create-story), the story file Status header transitions `ready-for-dev → in-progress → review → done` MUST mirror the sprint-status entry.

## Tasks / Subtasks

- [x] **Task 1 — Add `EVOverrideState` model and snapshot field** (AC: 1, 2)
  - [x] Subtask 1.1 — Added `EVOverrideState` Pydantic model in `src/open_ems/core/state.py` with all six fields plus the `failure_reason iff terminal` model validator and per-field UTC validators on both datetime fields.
  - [x] Subtask 1.2 — Exported `EVOverrideState` from `src/open_ems/core/__init__.py` and added to `__all__`.
  - [x] Subtask 1.3 — Added `active_ev_override: EVOverrideState | None = None` to `SystemSnapshot`. Frozen/extra=forbid preserved. 11 new tests in `tests/unit/core/test_ev_override.py` pass; existing 40 state tests pass.

- [x] **Task 2 — Wire `active_ev_override` through `StateStore`** (AC: 3, 4)
  - [x] Subtask 2.1 — `StateStore.__init__` stores `self._active_ev_override: EVOverrideState | None = None` and threads it into the initial snapshot.
  - [x] Subtask 2.2 — Added async `set_ev_override(override)` mutator under `self._writer_lock`.
  - [x] Subtask 2.3 — Added `_evaluate_ev_override(...)` helper called inside `publish()` under the same lock, implementing the three load-bearing clauses (expiry-clear → session-flip detection → natural-completion clear) with structlog INFO events. Published snapshot carries `self._active_ev_override`.
  - [x] Subtask 2.4 — Module docstring updated with writer-lock invariant. 8 new state_store tests pass; 11 ev_override tests pass; full suite 1499 passed + 6 xfailed (zero regressions).

- [x] **Task 3 — Settings + dependency helpers** (AC: 8, 13, 5)
  - [x] Subtask 3.1 — Added `ev_override_window_seconds`, `ev_override_confirmation_timeout_seconds`, `ev_override_default_rate_kw` to `Settings` with bounded `Field(...)` constraints.
  - [x] Subtask 3.2 — Added `get_policy_guard`, `get_active_constraints`, `get_settings_dep` helpers in `web/dependencies.py`; all return 503 on missing app state (mirror `get_state_store`). `ActiveConstraintsProvider.get()` `RuntimeError` (pre-hydrate) is caught and surfaced as 503.

- [x] **Task 4 — `POST /actions/ev-override` route** (AC: 5, 6, 7, 17)
  - [x] Subtask 4.1 — `src/open_ems/web/routes/actions.py` exposes the route + `_dispatch_and_settle_ev_override` fire-and-forget task with `_log_task_completion` done-callback.
  - [x] Subtask 4.2 — `create_app()` includes `actions_router` between `homeowner_router` and `stream_router`.
  - [x] Subtask 4.3 — Dispatch task implements AC7: `policy_guard.authorize_and_dispatch`; correlation_id concurrency-check via `state_store.get_snapshot().active_ev_override`; `CommandStatus → dispatch_status` mapping including `correlation_broken → failed`; explicit `CancelledError` re-raise.
  - [x] Subtask 4.4 — Three CONTROL audit emissions implemented via `_safe_audit` (best-effort, never blocks the homeowner flow). PolicyGuard handles dispatch-side audit; no double-emit. 13 new route tests pass.
  - [x] Subtask 4.5 — **Design refinement (caught in test):** `StateStore.set_ev_override(...)` now rebuilds the published snapshot via `model_copy(update=...)` so `get_snapshot()` reflects the mutation immediately. Without this, the background dispatch task's concurrency check read the pre-mutation snapshot and always saw `active_ev_override=None`, logging orphan and dropping every dispatch result.

- [x] **Task 5 — ControlLoop wiring** (AC: 9)
  - [x] Subtask 5.1 — `engine/control_loop.py:_build_evaluation_input` reads `snapshot.active_ev_override` and gates `homeowner_override_active` on `dispatch_status == "pending"`. Inline comment references Story 10.2 AC9.
  - [x] Subtask 5.2 — Added 5 new tests in `test_control_loop.py` covering: pending override → True; failed/timeout/rejected → False (parametrized); no override → False.

- [x] **Task 6 — State serialization + EV card context builder** (AC: 10, 11, 12)
  - [x] Subtask 6.1 — Added `_serialize_homeowner_ev_override_state(...)` helper; `active_ev_override` flows through both homeowner + installer SSE payloads.
  - [x] Subtask 6.2 — Added `build_homeowner_ev_card_context(snapshot, active_constraints)`; structural device-state independence test enforces it reads ONLY `snapshot.ev_charger` + `snapshot.active_ev_override` for `override_state`.
  - [x] Subtask 6.3 — Added `_OVERRIDE_FAILURE_REASONS` table + `_resolve_failure_reason_plain(...)` with `capability_missing:` prefix-rule + calm catch-all. Exhaustiveness test greps `engine/policy_guard.py` for rejection-reason literals. 19 new tests pass (38 total in `test_state_serialization.py`).

- [x] **Task 7 — Fragment route + template rewrite** (AC: 13, 14, 15, 16)
  - [x] Subtask 7.1 — `routes/fragments.py:homeowner_ev_card` re-targeted to `build_homeowner_ev_card_context`; injects `csrf_token` from `user.csrf_token` + `active_constraints` via the tolerant `get_active_constraints_optional` dep (returns None when provider unhydrated so fragment still renders).
  - [x] Subtask 7.2 — Rewrote `templates/fragments/homeowner/ev-card.html` standalone (no `_card.html` include). Alpine.js `x-data="evOverride(...)"` + aria-live status span + HTMX-bound button(s) with per-button `hx-headers` CSRF + retry slot.
  - [x] Subtask 7.3 — Re-added `#ev-card` section to `dashboard.html` between `#grid-card` and stack close. Added `{% block scripts %}` with Alpine factory.
  - [x] Subtask 7.4 — Added pinned `static/alpine.min.js` (Alpine.js 3.13.10 from unpkg, sha256 `fb9b146b…558c4b`) + `static/README.md` provenance. Linked from `base.html` with `defer`.
  - [x] Subtask 7.5 — Appended EV-card CSS rules to `static/open-ems.css` using existing tokens. `--color-accent-hover` for Confirmed (NOT `--color-pass`). `prefers-reduced-motion` guard on the pulse animation. `.state-card--ev { min-height: 12rem; }` reserves retry-link slot height to prevent layout shift.
  - [x] Subtask 7.6 — Updated `test_dashboard_route.py::test_homeowner_dashboard_shell_renders_five_htmx_bound_sections` to flip the expected count from 4 → 5 (EV section re-added) and assert Alpine.js link + `evOverride` factory presence.

- [x] **Task 8 — Tests (unit + integration + a11y placeholder)** (AC: 18, 19)
  - [x] Subtask 8.1 — `tests/unit/core/test_ev_override.py` — 11 tests covering model field validation, UTC enforcement, terminal-status validator, frozen invariant, snapshot field default + round-trip.
  - [x] Subtask 8.2 — Extended `test_state_store.py` with 8 tests for `set_ev_override` mutator + publish-time three-clause evaluation (expiry-clear / session-flip / natural-completion / load-bearing ordering / terminal carry-through).
  - [x] Subtask 8.3 — Extended `test_control_loop.py` with 5 tests for AC9 wiring (pending → True; failed/timeout/rejected parametrized → False; absent → False).
  - [x] Subtask 8.4 — Extended `test_state_serialization.py` with 19 new tests (AC10 serializer additions, AC11 four-state derivation + device-state independence, AC12 plain-language mapping + capability_missing prefix + exhaustiveness scan against `engine/policy_guard.py`).
  - [x] Subtask 8.5 — `test_ev_card_template.py` — 11 tests covering idle/optimistic/confirmed/fallback rendering, no-pass-green-class assertion, retry-visible-only-in-fallback, unavailable branch (None + DegradedDeviceState), auth/role contracts, no-window summary, provider-unavailable graceful render.
  - [x] Subtask 8.6 — `test_ev_override_route.py` — 13 tests covering 400-no-charger, install/idempotent/post-failure/post-expiry POSTs, CSRF/auth/role enforcement, 2-second response contract under slow dispatch, dispatch-task settlement for success/failed/rejected/orphan paths.
  - [x] Subtask 8.7 — `tests/integration/web/test_ev_override_e2e.py` — 8 passing + 1 skipped a11y placeholder covering all 9 AC19 scenarios end-to-end.
  - [x] Subtask 8.8 — Cascade fixture review: AC2's Pydantic-level default `= None` meant zero existing fixtures needed updating.
  - [x] Subtask 8.9 — **Design refinement (caught in test):** `StateStore.set_ev_override` now rebuilds the published snapshot (`previous.model_copy(update=...)`) so the dispatch task's concurrency-check via `get_snapshot()` reads the just-installed override (no race window between mutator and snapshot refresh).

- [x] **Task 9 — Quality gates + sprint-status close** (AC: 20, 21)
  - [x] Subtask 9.1 — `uv run python -m pytest tests/ --no-cov -q` → **1555 passed + 1 skipped + 6 xfailed** (baseline 1479 + 6; **+76 net passing**, +1 skipped a11y placeholder).
  - [x] Subtask 9.2 — `uv run python -m ruff check .` clean.
  - [x] Subtask 9.3 — `uv run python -m ruff format --check .` clean across 255 files.
  - [x] Subtask 9.4 — `uv run python -m mypy src/` clean across 99 source files. No `# type: ignore` added.
  - [x] Subtask 9.5 — `uv.lock` unchanged (Alpine.js is static asset, not Python dep). `_bmad-output/implementation-artifacts/sprint-status.yaml` flipped `10-2 → review`.

## Dev Notes

### A2 trigger evaluation

| Trigger | Match? | Evidence |
|---|---|---|
| T1 — Lifecycle / state-machine behavior | **Yes** | Four explicit named states (Idle / Optimistic / Confirmed / Fallback) with documented transitions, recovery semantics ("Try again"), expiry, and natural-completion clearing. New state-machine on the server side (override lifecycle in StateStore) AND the client side (Alpine.js optimistic layer). |
| T2 — Retries / cancellation | **Yes** | (a) Idempotency-window dedup on duplicate POSTs; (b) "Try again" retry on Fallback; (c) Cancellation: HTMX request can be aborted mid-flight while the background dispatch task continues; the route MUST NOT swallow `CancelledError` in the dispatch task; the override is durable across the cancellation. |
| T3 — Persistence + recovery | **No** | The override is in-memory only (matches 10.1's `active_strategy` treatment). Cold-start clears the override — documented in R5. No DB-backed hydration. |
| T4 — Watchdog / timing semantics | **Yes** | `expires_at` deadline-relative scheduling; `ev_override_confirmation_timeout_seconds` UI-side budget; idempotency-window dedup window; expiry-vs-flip ordering invariant inside `StateStore.publish(...)`. |
| T5 — Multi-adapter coordination | **No** | Single adapter (OCPP EV charger). Cross-component orchestration (route ↔ StateStore ↔ ControlLoop ↔ IntentExecutor ↔ PolicyGuard ↔ EV adapter) is in scope but ALL within one device class. T5's "≥2 protocol adapters" threshold is not met. |
| T6 — Deployment / restart behavior | **No** | No restart-triggered workflow. Cold-start clears override (documented in R5); cold-start invariants are NOT user-visible because the EV card simply renders Idle if no override is present. |
| T7 — Installer workflow orchestration | **No** | Homeowner-facing. No installer wizard touchpoints. |

**Conclusion:** A2-triggered. R1–R7 artifacts mandatory (rendered below).

### Files modified by this story

**New files:**
- `src/open_ems/core/ev_override.py` — `EVOverrideState` model (OR colocated in `core/state.py` — author's call).
- `src/open_ems/web/routes/actions.py` — `POST /actions/ev-override` + `_dispatch_and_settle_ev_override` background task.
- `src/open_ems/web/static/alpine.min.js` — pinned Alpine.js 3.x bundle.
- `src/open_ems/web/static/README.md` (or append to existing) — Alpine.js version + SHA256 + provenance note.
- `tests/unit/core/test_ev_override.py` — model tests.
- `tests/unit/web/test_ev_override_route.py` — route tests.
- `tests/unit/web/test_ev_card_template.py` — fragment-rendering tests (or extend `test_fragment_routes.py`).
- `tests/integration/web/test_ev_override_e2e.py` — end-to-end scenarios.

**Modified files:**
- `src/open_ems/core/state.py` — add `active_ev_override: EVOverrideState | None = None` to `SystemSnapshot`; possibly co-locate the `EVOverrideState` model here.
- `src/open_ems/core/__init__.py` — export `EVOverrideState`.
- `src/open_ems/core/state_store.py` — `_active_ev_override` field; `set_ev_override` mutator; three-clause evaluation in `publish(...)`; class docstring writer-lock invariant note.
- `src/open_ems/engine/control_loop.py` — line 210 `homeowner_override_active` wiring per AC9.
- `src/open_ems/settings.py` — three new `ev_override_*` fields.
- `src/open_ems/web/dependencies.py` — `get_policy_guard`, `get_active_constraints` helpers.
- `src/open_ems/web/state_serialization.py` — `active_ev_override` in both serializers; `build_homeowner_ev_card_context` + `_OVERRIDE_FAILURE_REASONS` + `_resolve_failure_reason_plain` + `_serialize_homeowner_ev_override_state` helpers.
- `src/open_ems/web/routes/fragments.py` — `homeowner_ev_card` endpoint re-targeted to the new builder; injects `csrf_token` + `active_constraints`.
- `src/open_ems/web/app.py` — `create_app()` registers `actions_router`.
- `src/open_ems/web/templates/dashboard.html` — re-add `#ev-card` HTMX-bound section between `#grid-card` and the dashboard-stack closing tag; add `{% block scripts %}` Alpine.js factory.
- `src/open_ems/web/templates/fragments/homeowner/ev-card.html` — standalone EV card template (4-state model + retry link + constraint notice).
- `src/open_ems/web/templates/base.html` — link `/static/alpine.min.js` with `defer`.
- `src/open_ems/web/static/open-ems.css` — EV-card-specific class rules per AC16.
- `tests/unit/core/test_state_store.py`, `tests/unit/engine/test_control_loop.py`, `tests/unit/web/test_state_serialization.py`, `tests/unit/web/test_fragment_routes.py` — extended per AC18.
- `_bmad-output/implementation-artifacts/sprint-status.yaml` — final entry update on close.

### Architectural alignment

- **AR15 — single-dispatch path:** the homeowner override MUST go through `PolicyGuard.authorize_and_dispatch(...)`. The route does NOT bypass PolicyGuard. PolicyGuard's existing P0–P5 vocabulary covers every dispatch-rejection reason this story can produce; no new rejection reasons are added.
- **Single Source of Truth — StateStore (architecture.md §"Pattern: StateStore snapshots are immutable", line 399):** the override flows through `SystemSnapshot` and is read via `state_store.get_snapshot()` everywhere downstream (control loop, fragment renderer, SSE serializer). No direct mutation by route handlers — the only writer is `StateStore.set_ev_override(...)` and the publish-time clearing logic inside `StateStore.publish(...)`.
- **Role-aware serialization at the SSE boundary (architecture.md line 296):** both homeowner and installer serializers include `active_ev_override` — the field is homeowner-safe by construction (correlation_id is opaque UUID; status values are the same audit-vocabulary strings).
- **FastAPI dependency injection enforces role (architecture.md lines 251, 663, 664–677):** the route uses `Depends(require_homeowner)` for browser/HTMX auth; PolicyGuard enforces device-level safety inside the dispatch task. Two separate gates per the Role Enforcement Pattern.
- **CSRF enforcement (architecture.md "every state-changing route" + `web/csrf.py:14`):** the route is NOT exempt from `CsrfMiddleware`; HTMX automatically supplies `X-CSRF-Token` from the `<meta>` tag in `base.html`. The fragment template's `hx-headers='{"X-CSRF-Token": "..."}'` is a belt-and-braces additional header set per-button to survive `outerHTML` swaps that may race the `htmx:configRequest` listener.
- **CommandResult-driven semantics (architecture.md §"CommandResult Pattern", line 700):** the override's `Confirmed` transition is gated on `EVChargerState.session_active=true` from a published snapshot — NOT on PolicyGuard's `CommandResult.status=success`. This matches the architecture's rule that "the decision engine must not assume a command succeeded unless `CommandResult` confirms acceptance AND either observed state confirms effect or the device protocol provides a reliable acknowledgment." The structural enforcement at AC11 is the homeowner UI's expression of this rule.
- **Single-Evaluator Principle (Epic 7 retro, 2026-05-05):** the override does NOT introduce a new evaluator. The decision engine's evaluator is unchanged; only the input flag `EVSchedulingContext.homeowner_override_active` is now wired (AC9) instead of hardcoded.
- **Mapping immutability invariant (Story 8-2 / 9-X):** `active_ev_override` is a single nullable field, not a mapping — no immutability concerns. Pydantic's frozen model handles single-field mutation by yielding a new instance via `model_copy(update={...})`.

### Library / framework requirements

`uv.lock` MUST be unchanged after this story (matches the Epic 9 / 9-X / 10-1 zero-new-Python-dependency contract). New static asset only:
- **Alpine.js 3.x** — pinned binary at `src/open_ems/web/static/alpine.min.js`. Download from `https://unpkg.com/alpinejs@3.x.x/dist/cdn.min.js` and commit the file; record the version + SHA256 in `src/open_ems/web/static/README.md`. Linked from `base.html` with `defer`. Story 10.1 deferred Alpine.js because it added no client-state; 10.2 is the first story to need it (per UX spec line 1099-1110 and the 10.2 AC15 design).

All other libraries already exist:
- **FastAPI + Jinja2** — route + template rendering.
- **HTMX 1.9.12** — pinned in `base.html:27` (CDN today; UX spec recommends self-hosting; track as follow-up — not in 10.2 scope).
- **Pydantic 2.x** — `EVOverrideState` model.
- **structlog** — log emissions.
- **pytest + pytest-asyncio (auto mode)** — testing.

### Strategy persistence: explicit scope split with Story 10.3 (parallel concern)

Story 10.3 (inline strategy selector) adds a sibling mutator `StateStore.set_active_strategy(...)` for `_active_strategy`. The writer-lock contract established by 10.2's `set_ev_override(...)` (and the docstring update in Subtask 2.4) is the canonical pattern for 10.3 to follow. The 10.1 review-deferred item `[src/open_ems/core/state_store.py:46, 92]` ("_`_active_strategy` writer-lock contract for Story 10.3 mutator_") is partially addressed by 10.2 (the docstring + 10.2's mutator demonstrate the pattern); 10.3 closes the remaining surface when its mutator lands. R7 triage of this item: **safe-during-10.2** (the 10.3 mutator does not exist yet; 10.2's pattern + docstring are the prep).

### Strategy: in-memory-only override + cold-start clearing

The override is NOT persisted to SQLite. Cold-start clears it (the new `StateStore` constructed at lifespan startup initializes `_active_ev_override = None`). This is the same trade-off Story 10.1 made for `active_strategy`: persistence would require a schema change (`active_overrides` table + repository + migration) which expands 10.2's scope into Epic 9 storage-layer territory. Splitting at the in-memory boundary keeps 10.2 focused on the homeowner control surface. **R5 documents the cold-start behavior:** a homeowner-triggered override in progress when the process restarts is silently cancelled — the EV charger may have already received the rate-set command at the OCPP layer (which persists session-actively at the charger), so a restart during an active session will see the next published snapshot show `session_active=true` with no `active_ev_override` carrier; the UI renders this as Idle (because override is None) but the charger is still charging at the commanded rate. This is correct: a restart-cleared override falls back to scheduled-only operation, and the EV charger's natural session lifecycle plays out (the next ControlLoop tick may issue a stop intent if outside the configured window — at which point the session ends naturally). Document this in completion notes as a known-and-accepted cold-start behavior.

### Previous-story intelligence

**Story 9-X (Wire adapter map into PolicyGuard and ControlLoop, done 2026-05-11):** unblocked Story 10.2 by closing the Story 8-2 `[app.py:223-236]` finding. `app.state.policy_guard` is now constructed with a real adapter map (or the `_DeferredOCPPAdapter` proxy if the OCPP charger has not yet sent BootNotification). The proxy is critical for 10.2's first-dispatch correctness: a homeowner override fired before BootNotification will hit the proxy → P2 `capability_check_failed` → settles to Fallback with `_OVERRIDE_FAILURE_REASONS["capability_check_failed"]` = "The charger did not respond. Try again or check the connection." That is a correct calm fallback. The dev agent does NOT need to special-case pre-boot; PolicyGuard + the proxy handle it.

**Story 10.1 (Homeowner dashboard layout, done 2026-05-11):** established the dashboard shell, the per-card fragment pattern, the AC19-Path-B hand-authored CSS bundle (no Tailwind / no CDN / no Node.js), and the `_STRATEGY_LABELS` + `_DEGRADED_EXPLANATIONS` exhaustive-table pattern. Story 10.2's `_OVERRIDE_FAILURE_REASONS` follows the same pattern. Notable Story 10.1 review patches that 10.2 inherits as constraints:
- (1) HIGH — orphaned `pytest.raises` block at `tests/unit/core/test_state.py:196-198, 251-252` was restored. **Lesson for 10.2:** when inserting new tests between existing tests, verify the test-method boundaries via re-read; do not graft a `with pytest.raises(...)` block onto an adjacent function unintentionally.
- (2) MEDIUM — defensive `.get(...)` fallbacks for `_STRATEGY_LABELS` / `_DEGRADED_EXPLANATIONS`. **Apply the same pattern to `_OVERRIDE_FAILURE_REASONS`** — the lookup in `_resolve_failure_reason_plain` MUST use `.get(reason, fallback)` not direct subscript.
- (3) MEDIUM — NaN/Inf guard in `_format_grid_power`. The EV card's `next_session_summary` should not surface NaN/Inf either; the helper that formats `ev_charging_window.start_local_time` produces a `datetime.time` value, not a float, so this concern does not directly apply to 10.2. No-op.
- (4) MEDIUM — `test_static_css_bundle_is_servable_at_expected_url`. **10.2 SHOULD add an analogous test** for `alpine.min.js` (assert `GET /static/alpine.min.js → 200` with `Content-Type: application/javascript` and the file body is the pinned version).
- (6) LOW — exact-302 + Location-prefix assertions for unauth redirect tests. **Apply this pattern to 10.2's `test_post_ev_override_unauth_returns_401_for_htmx_request`** — assert the exact `401` status code and the `WWW-Authenticate`-style detail body, not just `in (401, 302)`.
- (8) LOW — EV card's value-field "Unavailable" exception. **10.2 closes this:** the new `build_homeowner_ev_card_context` explicitly handles the unavailable branch and the "No EV charger connected" UX-spec-line-1503 fallback; the old EV branch in `_homeowner_card_rows` becomes unused. The dev agent can delete the EV branch from `_homeowner_card_rows` (lines 233–241 of `state_serialization.py`) — it is replaced by `build_homeowner_ev_card_context`.

**Story 9-Y-a (E2E fail-abort coverage, done 2026-05-11):** the naming-honesty precedent. Every test name in 10.2 MUST describe what the test actually asserts. Bad: `test_ev_override_works`. Good: `test_ev_override_optimistic_to_confirmed_when_session_active_observed_in_next_snapshot`.

**Story 8-2 (PolicyGuard) review finding `[control_loop.py:73-76]` "First tick publishes `operating_mode=None`":** closed structurally because `SystemSnapshot.operating_mode` is non-Optional. 10.2 inherits this — the override is rejected at P0 `fail_safe_mode_active` cleanly during cold-start if `operating_mode == fail_safe`, OR passes through P0 when `operating_mode in {normal, degraded, conservative}`. No special cold-start branch needed in 10.2.

**Story 8-4 (Watchdog) retro (2026-05-10):** the originator of the A2 trigger framework. 10.2's R1–R7 below follow the same shape as 8-4's; review 8-4's R1–R7 if the dev agent needs a worked example before implementing.

### Orchestration Risk Analysis

**A2 triggers matched:** T1 (lifecycle / state-machine — Idle ↔ Optimistic ↔ Confirmed ↔ Fallback ↔ Idle), T2 (retries / cancellation — idempotency window, "Try again", HTMX abort + background dispatch task cancellation), T4 (watchdog / timing — `expires_at` deadline, confirmation timeout, idempotency window).

#### R1 — Composition-risk analysis

This story converges five operational domains. Each contributes a risk that emerges only at the convergence point — none of these risks exists when each domain is considered in isolation.

1. **Override lifecycle state machine (T1)** converging with the **PolicyGuard dispatch contract (AR15)** — the override's `Confirmed` state requires BOTH a successful `CommandResult` AND a subsequent `EVChargerState.session_active=true` observation. A naïve implementation that transitions to Confirmed on `CommandResult.status=success` alone would violate the epics.md AC ("API success response alone is not sufficient") and surface a false "Charging now" to the homeowner when the charger silently failed to start a session. **Mitigated by:** the structural separation in AC7 step 3 — the background task does NOT update `dispatch_status` on success; it relies on the publish-time session-flip detection in AC4 to flip `session_observed_active=True`, and the render-layer AC11 derives Confirmed from `(pending, session_active=true)`.

2. **Idempotency window (T2)** converging with the **route's 2-second response contract** — a duplicate POST during an active override returns immediately (idempotent), but if the underlying dispatch failed asynchronously between two POSTs, the second POST might see `dispatch_status="failed"` and dispatch a NEW command. The race window is exactly: POST 1 → dispatch task starts → POST 1 returns 200 → POST 2 arrives 10ms later → snapshot still shows `dispatch_status="pending"` → POST 2 idempotently returns the existing correlation_id → 5 seconds later, dispatch task settles to `failed`. The homeowner now thinks two POSTs succeeded but actually only one was dispatched and it failed. **Mitigated by:** the structural enforcement that POST 2's return correlation_id matches POST 1's, so the UI's `aria-live` message and subsequent fragment polls converge on the correct Fallback rendering. Both POSTs surface the same correlation_id → both clients observe the same dispatch outcome via the 10s polling.

3. **Background dispatch task (T2)** converging with **cold-start / shutdown lifecycle (T4)** — if the process receives `SIGTERM` while a dispatch task is in-flight, `asyncio.CancelledError` propagates through `PolicyGuard.authorize_and_dispatch` (which is documented to NOT swallow it — `engine/policy_guard.py:223, 277`). The dispatch task's `try: ... except CancelledError: raise` correctly re-raises. The override is left in `dispatch_status="pending"`; on cold-start, the new `StateStore` initializes `_active_ev_override = None`. **Mitigated by:** AC7 step 5 explicit cancellation re-raise + R5 cold-start trace + the in-memory-only design (no persistent leak).

4. **PolicyGuard P0 (fail_safe) (AR15)** converging with **homeowner override origin** — when the system enters `fail_safe` mode mid-override (the override is `pending`, dispatch succeeded, session_active became true, then a critical fault triggers `fail_safe`), the next homeowner override attempt will hit P0 cleanly and surface `_OVERRIDE_FAILURE_REASONS["fail_safe_mode_active"]` = "The system is paused for safety right now." But the EXISTING active override is NOT cleared — the snapshot's `active_ev_override` carries through publishes. **Mitigated by:** the UX outcome is correct: an existing Confirmed override that was active before fail_safe entry continues showing "Charging now" until either expiry OR natural-completion (the EV adapter's session_active will flip false as the engine issues a stop command in fail_safe — except fail_safe blocks all commands at P0). This edge case bears watching but is bounded: `expires_at` will clear it within 2 hours. Document in dev notes as a known trade-off — homeowner sees stale "Charging now" until expiry if system enters fail_safe with an active override; this is preferable to flapping the UI to a misleading state.

5. **Cross-component data flow (UI ↔ StateStore ↔ ControlLoop ↔ IntentExecutor ↔ PolicyGuard ↔ EV adapter)** — five components participate in one homeowner action. A semantic bug in any of them surfaces only at the boundary. **Mitigated by:** the contract surface is small (just `active_ev_override`); each component reads it via a single well-defined API (`get_snapshot()` for read; `set_ev_override(...)` for write); structural tests in AC18/AC19 exercise the full chain (AC19 #1, #2, #3, #8 are end-to-end).

#### R2 — State-transition table

**Server-side override-lifecycle state machine (canonical):** owned by `StateStore` + `routes/actions.py`. The state field is `EVOverrideState.dispatch_status` (or `None` when no override is active).

| State | Transitions out | Trigger | Side-effects |
|---|---|---|---|
| `None` (no active override) | → `pending` | `POST /actions/ev-override` step 3 (new override installed) | `state_store.set_ev_override(new_override)` → next snapshot carries `active_ev_override`; CONTROL audit `homeowner_ev_override_requested`; background dispatch task started |
| `pending` | → `pending` (no-op terminal-state change) | `POST` duplicate within window | CONTROL audit `homeowner_ev_override_idempotent_replay`; route returns existing correlation_id |
| `pending` | → `pending` (session_observed_active flipped True) | `StateStore.publish(...)` observes `EVChargerState.session_active=True` | structlog INFO `ev_override_confirmed_observed`; override's `session_observed_active` flips True |
| `pending` | → `failed` | Background task: `CommandResult.status in {failed, correlation_broken}` | `state_store.set_ev_override(updated)` with `failure_reason=result.reason`; PolicyGuard already emitted CONTROL audit |
| `pending` | → `timeout` | Background task: `CommandResult.status == timeout` | `state_store.set_ev_override(updated)` with `failure_reason="command_timeout"`; PolicyGuard already emitted CONTROL audit |
| `pending` | → `rejected` | Background task: `CommandResult.status == rejected` (P0/P1/P2/P3/P4 + safety constraints) | `state_store.set_ev_override(updated)` with `failure_reason=<P-rejection reason>`; PolicyGuard already emitted CONSTRAINT audit |
| `pending` (with `session_observed_active=True`) | → `None` | `StateStore.publish(...)` observes `EVChargerState.session_active=False` | structlog INFO `ev_override_session_completed`; override cleared |
| `pending` | → `None` | `StateStore.publish(...)` observes `expires_at <= captured_at` | structlog INFO `ev_override_expired`; override cleared |
| `failed`/`timeout`/`rejected` | → `None` | `StateStore.publish(...)` observes `expires_at <= captured_at` | structlog INFO `ev_override_expired`; override cleared |
| `failed`/`timeout`/`rejected` | → `pending` (new override) | New `POST /actions/ev-override` after retry (or after expiry) | New `EVOverrideState` with new `correlation_id`; AC6 step 3 path |

**UI-side render state machine (derived, not stored):** owned by `build_homeowner_ev_card_context(...)`. The state field is `override_state: Literal["idle", "optimistic", "confirmed", "fallback"]`.

| State | Derivation | Server-side correlate |
|---|---|---|
| `idle` | `snapshot.active_ev_override is None` OR `ev_charger is None` | server-state `None` |
| `optimistic` | `active_ev_override.dispatch_status == "pending"` AND `ev_charger.session_active is False` | server-state `pending` not yet confirmed |
| `confirmed` | `active_ev_override.dispatch_status == "pending"` AND `ev_charger.session_active is True` | server-state `pending` + session-flip detected |
| `fallback` | `active_ev_override.dispatch_status in {"failed", "timeout", "rejected"}` | server-state terminal failure |

**Alpine.js client-side optimistic-layer state machine (transient, replaced on every poll):** owned by the `evOverride` factory in `dashboard.html`. State field is `this.state`. Only one client-driven transition: `idle → optimistic` on button tap (eager UI update). All other transitions come from the server via HTMX `outerHTML` swap (which re-initializes `x-data` from server-authored `initialState`). This is the structural enforcement of UX spec line 2163's "Alpine state is the optimistic layer only" and AC15's "the `<section>` is replaced via `outerHTML` swap, which re-instantiates Alpine state from the new `x-data` config".

#### R3 — Impossible-state analysis (with cold-start coverage)

Forbidden state combinations and their structural invariants:

1. `dispatch_status == "pending"` AND `failure_reason is not None`. **Invariant:** `EVOverrideState` model validator (AC1) rejects this combination. Construction-time defense.
2. `dispatch_status in {"failed", "timeout", "rejected"}` AND `failure_reason is None`. **Invariant:** same model validator. Construction-time defense.
3. `active_ev_override is None` AND `homeowner_override_active=True` returned by `_build_evaluation_input`. **Invariant:** AC9 — the wiring reads from `snapshot.active_ev_override`, so `None` deterministically yields `False`. No special-case branches.
4. Two distinct overrides in flight with different correlation_ids. **Invariant:** `_writer_lock`-guarded `set_ev_override(...)` is the only mutator path; the AC6 step 2 idempotency check runs INSIDE the route (under the writer lock implicitly via `set_ev_override`). A new override REPLACES the previous one; there is no "stack" of overrides.
5. Override transitions `failed → pending` without a new `correlation_id`. **Invariant:** AC6 step 3 always constructs a fresh `uuid.uuid4()`; the route's only path from terminal-failure-state to `pending` is via "install a NEW override" — `model_copy(update={"dispatch_status": "pending"})` of an existing failed override is NOT a path the route takes.
6. Override `dispatch_status == "rejected"` from a P5 (`adapter_correlation_id_mismatch`) result, with the override's `correlation_id` matching the (bogus) adapter-returned ID. **Invariant:** PolicyGuard's P5 enforcement at `engine/policy_guard.py` synthesizes a `CommandResult` carrying the ORIGINAL `correlation_id` (not the adapter's bogus one). The background task's AC7 step 4 concurrency check therefore correctly recognizes the result as matching the active override and settles it.
7. Background task settles an override whose `correlation_id` no longer matches the current snapshot's `active_ev_override.correlation_id` (e.g., a newer POST landed between dispatch start and dispatch finish, OR `publish()` cleared the original via expiry/natural-completion). **Invariant:** AC7 step 2's concurrency check — the task exits early via `ev_override_dispatch_result_orphan` log without mutating StateStore. The newer override (or the cleared state) is preserved.
8. `Alpine` client-side state desync from server (client shows `optimistic` while server shows `confirmed` or `fallback`). **Invariant:** every 10s HTMX poll replaces the entire `<section>` via `outerHTML` swap, re-initializing `x-data` from server-authored `initialState`. The Alpine optimistic layer is OVERWRITTEN on every poll. Maximum client-server desync window is one poll interval = 10s.

**Cold-start / startup-grace coverage:**

From process boot until the first successful evaluation cycle, the system is in the following state:
- `StateStore.__init__` constructs the initial snapshot with `active_ev_override=None`, `operating_mode=SystemOperatingMode.degraded`, `ev_charger=None`, all device slots `None`, `component_states[ev_charger]=unavailable`.
- The homeowner dashboard's EV card endpoint `GET /fragments/homeowner/ev-card` returns the **unavailable** branch (AC11's `unavailable=True` path → "No EV charger connected" — UX spec line 1503), because `snapshot.ev_charger is None`.
- The override button is **hidden** in the unavailable branch (per UX spec line 1503: "Override button is hidden (not disabled) — there is no action to offer"). A homeowner cannot trigger an override before the first successful EV charger poll.
- IF the homeowner somehow triggers a POST in this window (e.g., curl from an authenticated session, or a stale page from before the restart): the route's AC6 step 1 returns 400 `"EV charger is not available right now."` — no override is installed.
- The first ControlLoop tick (running approximately 1s after lifespan startup completes — see `engine/control_loop.py` interval) polls all adapters. If the EV charger adapter is `_DeferredOCPPAdapter` (proxy from 9-X AC4), it returns `DegradedDeviceState(reason="ocpp_charger_not_connected")`. The next published snapshot has `ev_charger = DegradedDeviceState(...)`; the homeowner EV card still renders "No EV charger connected" — same UX as `None`.
- Normal operation begins when the EV charger sends BootNotification (OCPP) AND the next ControlLoop tick observes `EVChargerState` in place of the proxy's `DegradedDeviceState`. At that point the homeowner EV card transitions to Idle state and the override button becomes available. The transition is gradual (one HTMX poll cycle from unavailable to idle); no visible "loading" state.
- **No state is "temporarily relaxed" during cold start** — the override field is structurally absent (`None`) until a homeowner explicitly POSTs. The cold-start contract is: "homeowner cannot trigger an override until the EV charger is online; the dashboard shows the calm 'No EV charger connected' state until then."

#### R4 — Cancellation ownership map

| Async operation | CancelledError owner | Cancellation audit owner | Cleanup owner | Reporting field semantics under cancel |
|---|---|---|---|---|
| `routes/actions.py:_dispatch_and_settle_ev_override` task | The task itself — explicit `try: ... except asyncio.CancelledError: raise` (AC7 step 5) | None — process shutdown is the only cancellation source; no per-task audit because the override is left in `pending` and cold-start clears it on restart | None (in-memory state; no resources to release) | `dispatch_status` stays `"pending"` at cancellation; the homeowner UI continues to show `optimistic` until the next 10s poll, which receives the cleared (`None`) override on restart — Idle |
| `PolicyGuard.authorize_and_dispatch` inside the dispatch task | Already documented in `engine/policy_guard.py:222, 277` — `except Exception:` excludes `BaseException` so `CancelledError` propagates. Owner: PolicyGuard's `_writer_lock`-free dispatch path | PolicyGuard does NOT emit a cancellation audit (Story 8-3 contract — RetryPolicy is the audit-emitting layer; 10.2 doesn't use RetryPolicy because homeowner overrides are not retried automatically) | The adapter's `disconnect()` is owned by lifespan shutdown (`web/app.py:596–614`), NOT the dispatch task | Mid-flight `CommandResult` is lost (never returned); the override's `dispatch_status` stays `"pending"` |
| `StateStore.set_ev_override` mutator | `self._writer_lock` is `asyncio.Lock` — if cancelled while waiting on the lock, no state mutation occurs (the lock acquisition fails) | None | None | If cancelled before mutation: override unchanged; if cancelled DURING mutation: impossible (the body is a single assignment, no `await` inside the lock body except the lock acquisition itself) |
| HTMX `POST /actions/ev-override` request (client-side) | Client-aborted requests are owned by Starlette's request lifecycle | None — but the server-side route may complete (the response is just unread by the client) | None — the route's database/audit writes happen regardless | The override IS installed; the client just doesn't see the 200 response. Subsequent fragment polls reveal the state. |
| HTMX `GET /fragments/homeowner/ev-card` request | Client-aborted polls owned by Starlette | None | None | Idempotent GET; no side effects |

**Why this story does NOT use RetryPolicy:** homeowner-triggered commands MUST surface failure to the homeowner directly. RetryPolicy is for `IntentExecutor → ControlLoop` automatic retries. A failed homeowner override surfaces a Fallback button + "Try again" link — the homeowner authors the retry, not the system. This is the Single-Evaluator Principle's natural extension: the decision engine retries; user actions surface failures.

#### R5 — Before-first-successful-cycle lifecycle review

Sequence from process boot to first successful normal cycle, focused on 10.2-relevant surfaces:

1. **Lifespan step 0 — process start.** `web/app.py:lifespan` begins. `app.state` is empty.
2. **Lifespan steps 1–3 — clock check, migrations, DB init.** No 10.2-relevant state. `app.state.state_store` is constructed with `_active_ev_override = None` (AC3) AS PART OF lifespan step (before yield at app.py:580).
3. **Lifespan step 4 — service construction.** `app.state.observability` (for audit emissions), `app.state.policy_guard` (with the runtime adapter map per 9-X AC6), `app.state.active_constraints_provider` are all constructed and assigned.
4. **Lifespan step 4i (post-yield) — actually NO — the assignment happens BEFORE yield.** Routes become live AT yield. From the moment FastAPI starts accepting requests:
   - `GET /homeowner/dashboard` returns the shell with the EV card section pointing at `/fragments/homeowner/ev-card`.
   - `GET /fragments/homeowner/ev-card` returns the **unavailable** branch (`snapshot.ev_charger is None`).
   - `POST /actions/ev-override` returns `400 — EV charger is not available right now.` because AC6 step 1's check rejects.
5. **ControlLoop tick 1 (≈1s after yield).** Polls adapters. EV charger adapter (likely `_DeferredOCPPAdapter` if pre-boot) returns `DegradedDeviceState(reason="ocpp_charger_not_connected")`. Snapshot published with `ev_charger = DegradedDeviceState`. Homeowner dashboard's EV card endpoint now sees `ev_charger` is not None but is `DegradedDeviceState` — AC11 unavailable branch still fires (UX spec line 1503).
6. **Normal operation marker.** The system enters normal operation **when the EV charger sends BootNotification (OCPP) AND the next ControlLoop tick observes `EVChargerState` in place of the proxy's degraded state.** At this point:
   - `snapshot.ev_charger` is `EVChargerState(status="available", session_active=False, ...)`.
   - The EV card endpoint renders **Idle** state with "Charge EV now" button enabled.
   - `POST /actions/ev-override` becomes accepting (AC6 step 1 passes).
   - The first homeowner override after this point can succeed end-to-end.

**Invariants that temporarily relax during the boot-to-first-cycle window:**
- The override button is **structurally hidden** in the unavailable branch (it is never rendered while `ev_charger` is None/DegradedDeviceState). There is NO temporary relaxation — the button is simply absent.
- `homeowner_override_active` in `_build_evaluation_input` is `False` (no override is possible to install, so this is correct).
- `ev_charging_window` from `active_constraints` may be `None` if the installer wizard has not yet activated constraints (pre-installer-wizard-complete state). The EV card's `next_session_summary` renders "No charging session scheduled" — UX spec line 1503 + AC11.

**Process-restart with active override (separately important):** if a homeowner had an active override (Confirmed, charging) before a process restart, the new `StateStore` initializes `_active_ev_override = None`. The EV charger's OCPP session may still be active at the device level. Next ControlLoop tick observes `EVChargerState(session_active=True)`. The homeowner UI renders Idle (no active override + session_active=True is NOT a defined state in AC11 — re-read AC11 carefully). **WAIT:** AC11 `override_state` derivation says `idle` when `active_ev_override is None`. But the session IS active. This is a UX issue: the homeowner sees "Charge EV now" (Idle) while the EV is actually charging. The next ControlLoop tick will issue an EV `stop` intent (because no override is active and we're outside the window — or a `charge` intent if inside the window). **Resolution:** the Idle state when `session_active=True` is acceptable — the homeowner can either wait for the natural session lifecycle, OR tap "Charge EV now" again (which installs a NEW override and dispatches a fresh `SetEVChargingRateCommand`). The UX is "calm over urgency" — no error, no warning, just the system normalizing to its scheduled-mode behavior. Document this in completion notes.

#### R6 — Source-of-truth ownership per datum

| Datum | Single owner | Read path | Drift risk |
|---|---|---|---|
| `EVOverrideState.correlation_id` | Created by route at POST time; never changes | `snapshot.active_ev_override.correlation_id` | None — UUID, frozen field |
| `EVOverrideState.requested_at` | Created by route; never changes | `snapshot.active_ev_override.requested_at` | None — frozen field |
| `EVOverrideState.expires_at` | Created by route; never changes | `snapshot.active_ev_override.expires_at` | None — frozen field |
| `EVOverrideState.dispatch_status` | Written by route (initial `pending`) OR background task (`failed`/`timeout`/`rejected`). Owner: `StateStore.set_ev_override(...)` under `_writer_lock` | `snapshot.active_ev_override.dispatch_status` | None — single writer per override lifecycle (the active correlation_id) |
| `EVOverrideState.failure_reason` | Written by background task only; `None` otherwise | `snapshot.active_ev_override.failure_reason` | None — coupled to `dispatch_status` via model validator |
| `EVOverrideState.session_observed_active` | Written by `StateStore.publish(...)` ONLY (publish-time flip detection in AC4 clause 2) | `snapshot.active_ev_override.session_observed_active` | **POTENTIAL DRIFT — flagged:** the route and the background task MUST NEVER write to this field; if they did, the publish-time flip detection would double-count. Mitigation: AC7 step 4 background task only `model_copy(update={"dispatch_status": ..., "failure_reason": ...})` — explicitly does NOT touch `session_observed_active`. Documented in code comment. |
| `StateStore._active_ev_override` (in-memory) | `StateStore.set_ev_override(...)` and `StateStore.publish(...)` are the only writers — both under `_writer_lock` | `snapshot.active_ev_override` (via `get_snapshot()`) | None |
| `EVChargerState.session_active` (the read-side correlate) | Owned by EVChargerAdapter (OCPP adapter) — sourced from real charger state | `snapshot.ev_charger.session_active` | None — adapter-side authoritative; mirrors physical charger state |
| `EVSchedulingContext.homeowner_override_active` (engine input) | Derived inside `ControlLoop._build_evaluation_input` from `snapshot.active_ev_override` (AC9) | Used by `engine/rules/ev_scheduling.py:67` | None — single derivation site, NO independent state |
| `_OVERRIDE_FAILURE_REASONS` mapping | Module constant in `state_serialization.py` | `_resolve_failure_reason_plain(...)` lookup | **NEEDS-MONITOR:** PolicyGuard rejection-reason strings are duplicated between `engine/policy_guard.py` (production) and this table (UI). AC18 test #25 enforces exhaustiveness via grep-derived fixture, but a NEW rejection reason added in a future story without updating this table would surface the calm catch-all instead of the specific message. **Mitigation:** the exhaustiveness test in `test_state_serialization.py` runs in CI and fails-loud if any PolicyGuard reason is missing. |
| Alpine.js client `state` | Client-side `evOverride` factory; overwritten on every HTMX poll | n/a (client memory) | Maximum desync = one poll interval (10s). Server is authoritative. |

#### R7 — Deferred-findings triage

Scanned `_bmad-output/implementation-artifacts/deferred-work.md` for overlapping items:

| Finding (bracket ref) | Classification | Rationale |
|---|---|---|
| `[src/open_ems/core/state_store.py:46, 92]` "_active_strategy writer-lock contract for Story 10.3 mutator" | **safe-during-this-story** | 10.2 establishes the canonical writer-lock pattern for single-field StateStore mutators (`set_ev_override`). The docstring update in Subtask 2.4 documents the pattern for 10.3 to follow. 10.2 does NOT need to retrofit `_active_strategy` — that is 10.3's scope when its mutator lands. |
| `[src/open_ems/web/routes/fragments.py:73-85]` "HTMX-error UX when /fragments/homeowner/status-headline 500s" | **acceptable-post-this-story** | Same UX gap applies to the new `/fragments/homeowner/ev-card`. The defer item is classified as Epic 11 installer-monitoring UX work; 10.2 inherits the gap. Mitigation: the new fragment route's render path is straightforward (one builder call + template render) and the AC12 `.get(...)` defensive fallback for `_OVERRIDE_FAILURE_REASONS` prevents the most likely 500 path. |
| `[tests/unit/web/test_fragment_routes.py::test_status_headline_degraded_mode_renders_calm_styling]` "Route-level fail_safe parametrization gap" | **acceptable-post-this-story** | Defense-in-depth gap in 10.1's tests; 10.2 ADDS its own parametrized test (AC19 #3) for the `fail_safe` rejection path, which incidentally covers the route-level fail_safe coverage gap for the EV-card route. |
| `[src/open_ems/services/runtime_adapter_wiring.py:106-123]` W1 "PLACEHOLDER register ranges" | **acceptable-post-this-story** | EV chargers do NOT use Modbus register maps (OCPP is event-driven). 10.2's adapter path is OCPP, not Modbus. No overlap. |
| `[src/open_ems/services/runtime_adapter_wiring.py:106-123]` W2 "register-range keys sync with `_SUPPORTED_MODELS`" | **acceptable-post-this-story** | Same reasoning — EV charger path is OCPP. |
| `[tests/unit/services/test_runtime_adapter_wiring.py]` W3 "(modbus_tcp, ev_charger) not exercised" | **acceptable-post-this-story** | `(modbus_tcp, ev_charger)` is structurally not supported (AC3 of 9-X enumerates only `ocpp_1_6 + ev_charger`). The test gap exists but is bounded by the structural rejection. No 10.2 overlap. |
| `[src/open_ems/services/deployment_validation.py:206-212]` DF1 "is_outdated=False when constraints provider unhydrated" | **acceptable-post-this-story** | The new `get_active_constraints` helper (AC13) returns `503` when `active_constraints_provider is None`, which the fragment route surfaces as a render failure. Same class of issue but in a different surface; 10.2 does not change the constraints-provider lifecycle. |
| `[src/open_ems/adapters/ocpp/central_system.py:115-122]` "OCPP synthesized address" | **acceptable-post-this-story** | Address-display issue in installer UI; no homeowner-facing impact. |
| `[src/open_ems/web/csrf.py:71-79]` "CsrfMiddleware exhausts request body" | **acceptable-post-this-story** | Pre-existing pattern; the new route's body is a small JSON POST (or empty body if HTMX submits with no form data). No 10.2 overlap. |

**No must-resolve-in-this-story findings.** The closest match is item 1 (writer-lock contract for 10.3) which 10.2 partially addresses by establishing the pattern.

### Project Structure Notes

- The new `routes/actions.py` is the first file in the `web/routes/` directory under the `/actions/` namespace. Architecture document already provisions this namespace (`architecture.md:211, 269-272, 453, 459`). The directory structure remains flat (no `web/routes/actions/` subdirectory) per the existing convention.
- The `_OVERRIDE_FAILURE_REASONS` table lives in `state_serialization.py` alongside `_STRATEGY_LABELS` and `_DEGRADED_EXPLANATIONS` — same module, same pattern. NO new module.
- `EVOverrideState` placement: `core/state.py` is the natural home (alongside `SystemSnapshot`); placing it in a new `core/ev_override.py` is acceptable but adds one more file. Dev agent's call; document in completion notes.
- Alpine.js bundle in `static/` follows the same self-hosting pattern documented in `base.html:21-25` for HTMX (which is still CDN-hosted in 10.1 — out of 10.2 scope to migrate).
- Tests are colocated with the source module being tested (`tests/unit/<module-mirror>/`). Integration tests under `tests/integration/web/` per existing convention.
- `static/alpine.min.js` provenance + version + SHA256 documented in `static/README.md` — create or extend per existing convention.

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Epic-10 (line 2072)] — Epic 10 prerequisite linkage to 9-X for Story 10.2.
- [Source: _bmad-output/planning-artifacts/epics.md#Story-10.2 (line 2121)] — full BDD acceptance criteria for the EV status card + 4-state override.
- [Source: _bmad-output/planning-artifacts/epics.md (line 2237)] — Epic 10 cross-story constraints (override is StateStore-backed; Confirmed requires `session_active=true`; Alpine optimistic layer).
- [Source: _bmad-output/planning-artifacts/ux-design-specification.md#Status-Headline-Card (line 1041)] — sibling-card context (for layout-stability invariants).
- [Source: _bmad-output/planning-artifacts/ux-design-specification.md#EV-Override-Button (line 1080)] — button anatomy, four-state model, accent-hover for Confirmed, idempotency contract.
- [Source: _bmad-output/planning-artifacts/ux-design-specification.md (lines 1099-1110)] — Alpine.js state-machine pseudocode used as the basis for AC15's factory.
- [Source: _bmad-output/planning-artifacts/ux-design-specification.md (line 559)] — 52px button height, "most prominent interactive element".
- [Source: _bmad-output/planning-artifacts/ux-design-specification.md (line 826)] — Journey Flow 4: Homeowner EV Override (state-machine flowchart).
- [Source: _bmad-output/planning-artifacts/ux-design-specification.md (line 1503)] — "No EV charger connected" unavailable state.
- [Source: _bmad-output/planning-artifacts/ux-design-specification.md (line 1619)] — `aria-live="polite"` state change announcements.
- [Source: _bmad-output/planning-artifacts/ux-design-specification.md (line 1814)] — Action Response Contract (2s response, action_id, plain-language message).
- [Source: _bmad-output/planning-artifacts/ux-design-specification.md (line 1700)] — Real-Time Event Contract (ev_session_update, idempotency, ordering).
- [Source: _bmad-output/planning-artifacts/architecture.md (line 269-272, 459, 665)] — `/actions/` namespace, EV override route example (note: uses old name `/actions/charge-now`).
- [Source: _bmad-output/planning-artifacts/architecture.md (line 700)] — CommandResult Pattern (applied=True iff observed-effect).
- [Source: _bmad-output/planning-artifacts/architecture.md (line 717-734)] — Enforcement Summary (PolicyGuard, immutable snapshots, structured logging).
- [Source: _bmad-output/planning-artifacts/prd.md#FR27 (line 632)] — next scheduled EV session visible without scrolling.
- [Source: _bmad-output/planning-artifacts/prd.md#FR28 (line 633)] — single-tap EV override with confirmed state from device.
- [Source: _bmad-output/planning-artifacts/prd.md#NFR-P3 (line 666)] — 3-second interactive on Pi 4.
- [Source: _bmad-output/planning-artifacts/prd.md#NFR-A1 (line 714)] — WCAG 2.1 AA.
- [Source: src/open_ems/core/state.py:146] — `SystemSnapshot` extension target for AC2.
- [Source: src/open_ems/core/state.py:155] — `active_strategy` field placement precedent (AC2 adds `active_ev_override` immediately after).
- [Source: src/open_ems/core/state_store.py:39] — `StateStore.__init__` extension target for AC3.
- [Source: src/open_ems/core/state_store.py:69-101] — `publish(...)` body — three-clause evaluation insertion site for AC4.
- [Source: src/open_ems/core/commands.py:48-54] — `CommandOrigin.homeowner` enum member; 10.2 is the first production consumer.
- [Source: src/open_ems/core/commands.py:107-110] — `SetEVChargingRateCommand` model used by the route.
- [Source: src/open_ems/core/devices.py:133-156] — `EVChargerState.session_active` field; 10.2 reads it at render time.
- [Source: src/open_ems/engine/policy_guard.py:1-137] — PolicyGuard module docstring with P0–P5 rejection contract; `_OVERRIDE_FAILURE_REASONS` (AC12) MUST cover every reason listed.
- [Source: src/open_ems/engine/control_loop.py:188-214] — `_build_evaluation_input`; line 210 is the wiring target for AC9.
- [Source: src/open_ems/engine/rules/ev_scheduling.py:67-81] — consumer of `homeowner_override_active`; 10.2 wires it but does NOT modify this rule.
- [Source: src/open_ems/web/state_serialization.py:51-118] — `serialize_homeowner_snapshot` + `build_homeowner_headline_context` (10.1) — pattern templates for AC10 and AC11.
- [Source: src/open_ems/web/state_serialization.py:30-48] — `_STRATEGY_LABELS` + `_DEGRADED_EXPLANATIONS` exhaustive-table pattern; `_OVERRIDE_FAILURE_REASONS` (AC12) follows the same shape.
- [Source: src/open_ems/web/routes/fragments.py:64-70] — existing `homeowner_ev_card` endpoint (re-targeted by AC13).
- [Source: src/open_ems/web/templates/dashboard.html] — homeowner dashboard shell; 10.2 re-adds EV section (AC15).
- [Source: src/open_ems/web/templates/fragments/homeowner/_card.html] — generic card template; EV card diverges from this (AC14 — standalone template).
- [Source: src/open_ems/web/templates/base.html:14, 27-38] — static-asset linking + CSRF wiring; Alpine.js added per AC15.
- [Source: src/open_ems/web/csrf.py:14-15] — `_CSRF_EXEMPT_PATHS` does NOT include `/actions/*` — confirmation that the new route IS in the CSRF middleware path.
- [Source: src/open_ems/web/dependencies.py:83-87] — `get_state_store` dependency pattern; AC5 / AC13 follow this for `get_policy_guard` / `get_active_constraints`.
- [Source: src/open_ems/web/dependencies.py:252-280] — `require_homeowner` dependency.
- [Source: src/open_ems/web/app.py:540-578] — lifespan PolicyGuard / ControlLoop / IntentExecutor construction; 10.2 does NOT modify this section (PolicyGuard is already wired with the real adapter map post-9-X).
- [Source: src/open_ems/web/app.py:641-653] — `create_app()` router registration; AC5 adds `actions_router`.
- [Source: src/open_ems/services/audit_log.py] — `ObservabilityService.audit(...)` pattern for AC17.
- [Source: _bmad-output/implementation-artifacts/10-1-implement-homeowner-dashboard-layout-with-systemoperatingmode-driven-headline-and-metric-cards.md] — Story 10.1 — sibling pattern: snapshot field + StateStore default + cold-start contract + exhaustive label table.
- [Source: _bmad-output/implementation-artifacts/9-X-wire-adapter-map-into-policy-guard-and-control-loop.md] — Story 9.X — closes the deferred finding that would have made 10.2 structurally impossible.
- [Source: _bmad-output/implementation-artifacts/deferred-work.md] — overlapping-findings scan (R7).
- [Source: _bmad-output/implementation-artifacts/epic-8-retro-2026-05-10.md] — A1/A2 enforcement framework origin.
- [Source: _bmad-output/implementation-artifacts/epic-9-retro-2026-05-11.md] — A7 review-closure structural enforcement (bmad-code-review will gate 10.2 → done).

## Dev Agent Record

### Agent Model Used

claude-opus-4-7 (Claude Opus 4.7)

### Debug Log References

- Mypy clean across 99 source files (no `# type: ignore` added).
- Ruff check + format clean across 255 files.
- Pytest pre-10.2 baseline: **1479 passed + 6 xfailed** (per Story 10.1 close). Post-10.2: **1555 passed + 1 skipped + 6 xfailed**. Net delta: **+76 passing, +1 skipped** (a11y placeholder).
- Test breakdown of the +76:
  - `tests/unit/core/test_ev_override.py` (new): +11 covering AC1 model fields, UTC enforcement, terminal-status validator, frozen invariant, snapshot field default + JSON round-trip.
  - `tests/unit/core/test_state_store.py`: +8 covering `set_ev_override` mutator + publish-time three-clause evaluation (expiry-clear / session-flip / natural-completion / load-bearing ordering / terminal carry-through).
  - `tests/unit/engine/test_control_loop.py`: +5 covering AC9 wiring (pending → True; failed/timeout/rejected parametrized → False; absent → False).
  - `tests/unit/web/test_state_serialization.py`: +19 covering AC10 serializer additions, AC11 four-state derivation + device-state independence + fail_safe constraint-notice suppression, AC12 plain-language mapping + `capability_missing:` prefix-rule + calm catch-all + exhaustiveness scan against `engine/policy_guard.py`.
  - `tests/unit/web/test_ev_card_template.py` (new): +11 covering rendering for each of the 4 states, no-pass-green-class assertion, retry-visible-only-in-fallback, unavailable branch for None + DegradedDeviceState, auth/role contracts, no-window summary, provider-unavailable graceful render.
  - `tests/unit/web/test_ev_override_route.py` (new): +13 covering 400-no-charger / install / idempotent / post-failure / post-expiry POSTs, CSRF / unauth / installer-role enforcement, 2-second response contract under slow dispatch, dispatch-task settlement for success/failed/rejected/orphan paths.
  - `tests/integration/web/test_ev_override_e2e.py` (new): +8 covering AC19 #1–#8 (happy-path idle→confirmed, optimistic→fallback on dispatch failure, optimistic→fallback on PolicyGuard P0, natural session completion → idle, expiry → idle, DegradedDeviceState → unavailable branch, homeowner origin + correlation_id round-trip, ControlLoop wiring smoke) + 1 skipped a11y placeholder (Story 9.x xfail+skipif precedent).
  - `tests/unit/web/test_dashboard_route.py`: 0 net (Story 10.1's `test_homeowner_dashboard_shell_renders_four_htmx_bound_sections` renamed/updated to assert the FIVE expected slots + Alpine.js wiring; net count unchanged).

### Completion Notes List

- **AC1 implementation note — model placement.** `EVOverrideState` lives in `src/open_ems/core/state.py` (next to `SystemSnapshot`) rather than a sibling `core/ev_override.py` file. Rationale: the model is small (six fields) and tightly coupled to the snapshot field that consumes it; the 10-1 precedent (`active_strategy` on the snapshot, exhaustive label table in `state_serialization.py`) is the same pattern.
- **AC2 deviation from 10-1's `active_strategy` justified.** `SystemSnapshot.active_ev_override` carries a Pydantic-level default of `None` (vs. 10.1's `active_strategy: EnergyStrategy` with no default and StateStore-boundary cold-start enum value). The override is a structural sentinel (None ↔ "no active override"), not a domain enum member; the model-level default is the natural representation. Side-effect: zero existing test fixtures across the suite needed `active_ev_override=None` kwarg additions, because Pydantic supplies the default automatically.
- **AC3 design refinement caught in test.** Original AC3 spec had `set_ev_override` set only `self._active_ev_override` and rely on the next `publish()` call to surface the change. This created a race: the background dispatch task calls `state_store.get_snapshot().active_ev_override` ~50ms after the route returns; without an explicit snapshot rebuild, the get_snapshot() returns a stale snapshot showing `active_ev_override=None`, and the task's concurrency check fires its orphan log on every dispatch, never settling terminal status. **Fix:** `set_ev_override` now rebuilds the published snapshot via `previous.model_copy(update={"sequence_id": ..., "active_ev_override": override})` under the writer lock so `get_snapshot()` reflects the mutation immediately. This is the canonical pattern for Story 10.3's `set_active_strategy` mutator to follow.
- **AC4 load-bearing ordering preserved.** The three clauses inside `_evaluate_ev_override` are evaluated in exactly this order: expiry-clear → session-flip detection → natural-completion clear. The test `test_state_store_clears_expired_override_before_session_flip_detection` exercises the ordering: an expired override with `session_active=True` on the published EV state clears (clause 1) rather than mutating `session_observed_active=True` (clause 2). Reordering would leak a stale `session_observed_active=True` into the next snapshot.
- **AC7 step 3 — Confirmed transition requires snapshot observation, not CommandResult success.** The background dispatch task `_dispatch_and_settle_ev_override` leaves `dispatch_status="pending"` on `CommandResult.status=success` and exits without StateStore mutation. The transition to Confirmed is owned by `StateStore.publish(...)` clause 2 the first time `EVChargerState.session_active=true` is observed. This is the structural enforcement of the epics.md AC ("Confirmed state is entered only when `EVChargerState.session_active = true` is observed in the `StateStore` snapshot — API success response alone is not sufficient", line 2146).
- **AC15 Alpine.js self-hosting.** Pinned Alpine.js 3.13.10 from unpkg (SHA256 `fb9b146b7fbd1bbf251fb3ef464f2e7c5d33a4a83aeb0fcf21e92ca6a9558c4b`) lives at `src/open_ems/web/static/alpine.min.js` with provenance in `src/open_ems/web/static/README.md`. Linked from `base.html` with `defer` so the `alpine:init` listener registered in `dashboard.html`'s `{% block scripts %}` runs after the script parses. NO CDN, no Node.js, no build step — matches the AC19 Path B contract carried forward from Story 10.1.
- **AC16 no-pass-green load-bearing rule.** The Confirmed state uses `--color-accent-hover` (cyan-700) per UX spec line 1097. `pass-green` is reserved exclusively for PASS validation states. Both `test_ev_card_renders_no_pass_green_class_in_confirmed_state` (unit) and `test_ev_override_happy_path_idle_to_confirmed` (integration) assert NO `pass-green` / `color-pass` / `amber` / `color-fail` / `warn` class fragments leak into Confirmed-mode HTML.
- **AC12 exhaustiveness via source grep.** `test_resolve_failure_reason_plain_covers_every_policy_guard_rejection_reason` greps `src/open_ems/engine/policy_guard.py` for `self._reject(command, "<reason>")` literals and asserts each appears in `_OVERRIDE_FAILURE_REASONS` (or matches the `capability_missing:` prefix rule). A new PolicyGuard rejection reason added without a UX mapping will fail this test in CI.
- **AC13 graceful degradation.** Added `get_active_constraints_optional` dependency helper that returns `None` (instead of HTTP 503) when the `ActiveConstraintsProvider` is missing or pre-hydrate. The EV card fragment route uses this so the dashboard still renders during the pre-installer-wizard-complete window (`next_session_summary` falls back to "No charging session scheduled" per UX spec line 1503). The strict `get_active_constraints` dep remains in `dependencies.py` for other route callers that should fail loud.
- **AC17 best-effort audit emissions.** The `_safe_audit` helper wraps every `ObservabilityService.audit(...)` call in a try/except so a failed audit never blocks the homeowner override path. Structlog still receives the structured event regardless of repo write success. PolicyGuard handles dispatch-side audit; the route does NOT double-emit.
- **AC18 cross-loop dispatch-task settlement.** With the synchronous Starlette `TestClient`, the route runs inside a portal event loop; the background dispatch task spawned by `asyncio.create_task(...)` runs on that same portal loop. The portal closes when `TestClient.__exit__` runs, cancelling in-flight tasks. The helper `_wait_for_terminal_status` therefore polls INSIDE the `with TestClient(app) as client:` block so the portal stays alive long enough for the dispatch task to settle.
- **AC21 doc-sync — old route names left for follow-up.** `architecture.md:270, 459, 665, 880` and `ux-design-specification.md:406, 837` reference the older `/actions/charge-now` name. Story 10.2 used the canonical name from `epics.md:2138` (`/actions/ev-override`). The doc-sync edit is informational-only and intentionally deferred to a future doc-sync sweep so the implementation diff stays scoped to code + tests.
- **No new dependencies.** `uv.lock` unchanged. Alpine.js is a static asset, not a Python dependency. Matches the zero-new-Python-dependency contract carried forward from Epic 9 / 9-X / 10-1.

### File List

**New files:**
- `src/open_ems/web/routes/actions.py` — `POST /actions/ev-override` route + `_dispatch_and_settle_ev_override` background task + `_safe_audit` + `_log_task_completion`.
- `src/open_ems/web/static/alpine.min.js` — pinned Alpine.js 3.13.10 bundle.
- `src/open_ems/web/static/README.md` — static-asset provenance (Alpine.js + open-ems.css).
- `tests/unit/core/test_ev_override.py` — 11 EVOverrideState model tests.
- `tests/unit/web/test_ev_card_template.py` — 11 EV-card fragment-template rendering tests.
- `tests/unit/web/test_ev_override_route.py` — 13 route + dispatch-task tests.
- `tests/integration/web/test_ev_override_e2e.py` — 8 end-to-end scenarios + 1 a11y placeholder.

**Modified files:**
- `src/open_ems/core/state.py` — `import uuid`; added `EVOverrideState` (frozen Pydantic, six fields, UTC validators, `failure_reason iff terminal` model validator); added `active_ev_override: EVOverrideState | None = None` to `SystemSnapshot`.
- `src/open_ems/core/state_store.py` — module docstring writer-lock invariant; `import structlog`; `_active_ev_override` field; initial snapshot carries `active_ev_override=None`; new `set_ev_override(override)` mutator (snapshot-rebuilds for immediate `get_snapshot()` visibility); `_evaluate_ev_override(ev_slot, captured_at)` AC4 three-clause helper called inside `publish(...)` under the lock; `publish` now threads `active_ev_override` into the new snapshot.
- `src/open_ems/core/__init__.py` — exports `EVOverrideState`.
- `src/open_ems/settings.py` — three new `ev_override_*` fields with bounded `Field(...)` constraints.
- `src/open_ems/engine/control_loop.py` — `_build_evaluation_input` line 210 wired `homeowner_override_active=(snapshot.active_ev_override is not None and snapshot.active_ev_override.dispatch_status == "pending")`.
- `src/open_ems/web/dependencies.py` — `import` PolicyGuard, ActiveConstraints, ActiveConstraintsProvider, Settings; new `get_policy_guard`, `get_active_constraints`, `get_active_constraints_optional`, `get_settings_dep` dependency helpers.
- `src/open_ems/web/state_serialization.py` — `import EVOverrideState`, `ActiveConstraints`; new `OverrideRenderState` literal; `_OVERRIDE_FAILURE_REASONS` + `_OVERRIDE_FAILURE_FALLBACK` + `_EV_CARD_BUTTON_LABELS` + `_EV_CARD_ARIA_LIVE` + `_EV_CARD_CONSTRAINT_NOTICE` module constants; new `_resolve_failure_reason_plain`, `_serialize_homeowner_ev_override_state`, `build_homeowner_ev_card_context`, `_format_next_session_summary` helpers; `active_ev_override` added to both serializers.
- `src/open_ems/web/routes/fragments.py` — `import ActiveConstraints`, `get_active_constraints_optional`, `build_homeowner_ev_card_context`; `homeowner_ev_card` re-targeted to the new builder with `csrf_token` + constraints injection.
- `src/open_ems/web/routes/__init__.py` — none (router exported from `actions.py`).
- `src/open_ems/web/app.py` — `import actions_router`; `create_app()` includes it between `homeowner_router` and `stream_router`.
- `src/open_ems/web/templates/base.html` — `<script src="/static/alpine.min.js" defer>`.
- `src/open_ems/web/templates/dashboard.html` — re-added `#ev-card` HTMX-bound section between `#grid-card` and stack-close; added `{% block scripts %}` with `Alpine.data('evOverride', ...)` factory.
- `src/open_ems/web/templates/fragments/homeowner/ev-card.html` — completely rewritten as a standalone EV card template (not a `_card.html` include) with `x-data="evOverride(...)"` + four-state class binding + HTMX-bound primary button + aria-live status span + constraint-notice slot + retry-link slot.
- `src/open_ems/web/static/open-ems.css` — appended EV-card class rules using existing design tokens; `--color-accent-hover` for Confirmed; `prefers-reduced-motion` guard on `ev-pulse` keyframe; `.visually-hidden` helper for aria-live status; `.state-card--ev { min-height: 12rem }` reserves retry-link slot height.
- `tests/unit/core/test_state_store.py` — `import uuid`, `import EVOverrideState`; appended +8 tests for Story 10.2 mutator + publish-time evaluation.
- `tests/unit/engine/test_control_loop.py` — `from datetime import ... timedelta`; appended +5 tests for AC9 (`_populate_required_slots` helper + three parametrized variants).
- `tests/unit/web/test_state_serialization.py` — imports consolidated to the top; `_UNSET` sentinel for explicit-None ev_charger override in `_snapshot_with`; appended +19 tests covering AC10/AC11/AC12.
- `tests/unit/web/test_dashboard_route.py` — `test_homeowner_dashboard_shell_renders_four_htmx_bound_sections` renamed to `..._five_...`; assertions flipped to require EV section + Alpine.js link + `evOverride` factory presence.
- `_bmad-output/implementation-artifacts/sprint-status.yaml` — `10-2-...` flipped `ready-for-dev → in-progress → review`; `last_updated` annotated.

### Change Log

| Date | Change |
|---|---|
| 2026-05-11 | Story 10.2 created via `bmad-create-story`. Status: ready-for-dev. A2 evaluation: triggered (T1, T2, T4). R1–R7 orchestration artifacts authored in Dev Notes per A1 enforcement contract. Epic 10 already in-progress (set at Story 10.1 creation). |
| 2026-05-12 | Story 10.2 implemented. Adds `EVOverrideState` model + `SystemSnapshot.active_ev_override` field; `StateStore.set_ev_override` mutator + publish-time three-clause evaluation (expiry / session-flip / natural-completion); `POST /actions/ev-override` route with fire-and-forget background dispatch task + idempotency window + `homeowner` CommandOrigin; `ControlLoop._build_evaluation_input` wired to read pending override; homeowner serializer additions; `build_homeowner_ev_card_context` 4-state derivation; `_OVERRIDE_FAILURE_REASONS` plain-language mapping with `capability_missing:` prefix rule; standalone `ev-card.html` template with Alpine.js `x-data="evOverride(...)"` + HTMX-bound buttons; self-hosted Alpine.js 3.13.10 bundle at `static/alpine.min.js`; EV-card CSS with `--color-accent-hover` for Confirmed (load-bearing — NOT `--color-pass`) + `prefers-reduced-motion` guard. `+76` net tests passing (1479 → 1555) + 1 skipped a11y placeholder. Ruff check + format clean across 255 files; mypy clean across 99 source files. `uv.lock` unchanged. Status: in-progress → review. |

## Review Findings

_Generated by `bmad-code-review` on 2026-05-12. Three layers run in parallel: Blind Hunter (cynical adversarial), Edge Case Hunter (branch/boundary walker), Acceptance Auditor (AC1–AC21 verdict). Acceptance Auditor verdict: **DEVIATIONS-NOTED** — every AC SATISFIED except AC17 which is DEVIATION-OK (spec referenced `EventType.CONTROL` but valid event types are `DECISION/DEVICE/SYSTEM/CONSTRAINT/INSTALLER`; `DEVICE` chosen as best match)._

### Patch (16)

- [x] [Review][Patch] `set_ev_override` torn-snapshot — bumps `sequence_id` but reuses stale `captured_at` / `data_age_seconds`, breaking every downstream age/staleness consumer for the window between override set and next publish [`src/open_ems/core/state_store.py:96-104`]
- [x] [Review][Patch] Dispatch-task TOCTOU race — lock-free `get_snapshot()` read followed by `await set_ev_override(updated)` allows publish() to clear/mutate between; can re-install an expired override or lose `session_observed_active=True` flip. Add `compare_and_set_ev_override(expected_correlation_id, updated)` atomic mutator. [`src/open_ems/web/routes/actions.py:209-242`, `src/open_ems/core/state_store.py:86-104`]
- [x] [Review][Patch] PolicyGuard `reason=repr(exc)` on adapter-exception path bypasses `_OVERRIDE_FAILURE_REASONS` direct lookup AND is not caught by the exhaustiveness test (which scans only `self._reject` literals). Normalize PolicyGuard to `reason="command_dispatch_failed"` (diagnostic already in structlog at line 278), add `command_dispatch_failed` to the UX map. [`src/open_ems/engine/policy_guard.py:277-291`, `src/open_ems/web/state_serialization.py:62-82`]
- [x] [Review][Patch] `ev_override_confirmation_timeout_seconds` Setting is defined with a documented "UI surfaces Fallback if timeout exceeded" contract but is never injected to the client; Alpine factory has no setTimeout — Optimistic state can persist indefinitely on network blip. Wire setting through `build_homeowner_ev_card_context` → `data-confirmation-timeout-seconds` → Alpine `submit()` setTimeout. [`src/open_ems/settings.py:54-58`, `src/open_ems/web/templates/dashboard.html:55-71`, `src/open_ems/web/templates/fragments/homeowner/ev-card.html:9`]
- [x] [Review][Patch] Exhaustiveness test path resolution is cwd-fragile (`Path("src/...")` is relative; FileNotFoundError if pytest cwd ≠ repo root) AND regex only matches `self._reject` literals, missing inline `CommandResult(reason="command_timeout")` and `reason=repr(exc)` paths. Resolve via `policy_guard.__file__`, extend regex, assert non-empty extraction. [`tests/unit/web/test_state_serialization.py:718-738`]
- [x] [Review][Patch] Background dispatch task reference dropped after `asyncio.create_task` — local goes out of scope on route return; per asyncio docs the task can be GC'd mid-execution. Track in module-level `set[asyncio.Task]` with discard callback. [`src/open_ems/web/routes/actions.py:157-165`]
- [x] [Review][Patch] `_OVERRIDE_FAILURE_REASONS` uses `("string")` syntax (parenthesized strings, not 1-tuples) — visual tuple footgun: a future trailing comma silently converts the value into `("text",)` and f-string formats it literally. Strip the parens. [`src/open_ems/web/state_serialization.py:65-82`]
- [x] [Review][Patch] `EVOverrideState` model has no `expires_at > requested_at` temporal-ordering invariant. Trivial `model_validator` add. [`src/open_ems/core/state.py:147-191`]
- [x] [Review][Patch] Nested `aria-live="polite"` regions — outer `<div class="state-card__body">` AND inner `<span role="status" aria-live="polite">` both announce on update, causing double screen-reader announcements. Remove outer aria-live (keep status span). [`src/open_ems/web/templates/fragments/homeowner/ev-card.html:14, 31`]
- [x] [Review][Patch] `aria-disabled` is server-rendered but native `disabled` is Alpine-bound — they disagree for the 10s optimistic window (sighted user sees disabled-styled button while screen readers see `aria-disabled="false"`). Add `x-bind:aria-disabled="state !== 'idle'"`. [`src/open_ems/web/templates/fragments/homeowner/ev-card.html:27-28`]
- [x] [Review][Patch] No test verifies dispatch-task orphan-protection when override is cleared to `None` mid-dispatch (only the "newer override with different correlation_id" path is covered). Add sibling test for the publish-clears-to-None race. [`tests/unit/web/test_ev_override_route.py`]
- [x] [Review][Patch] No test verifies `data-correlation-id="<uuid>"` attribute renders correctly for pending overrides — Jinja2 attribute-vs-subscript fallback on dict is fragile and silent. Add template assertion. [`tests/unit/web/test_ev_card_template.py`]
- [x] [Review][Patch] `_safe_audit` happy path is not asserted — `event_type` typos (or future enum drift) would silently log `ev_override_audit_failed` without test signal. Add a happy-path assertion that an audit row is actually emitted on the success branch. [`tests/unit/web/test_ev_override_route.py`]
- [x] [Review][Patch] `ev_override_default_rate_kw: Field(ge=0.0)` permits 0.0 — sending a 0 kW SetEVChargingRateCommand succeeds at PolicyGuard but never starts a session, stranding the UI in Optimistic for 2h. Change to `gt=0.0`. [`src/open_ems/settings.py:63`]
- [x] [Review][Patch] `x-data="evOverride({initialState: '{{ override_state }}'})"` injects override_state into a JS single-quote literal without escaping — safe today (4 hardcoded Literal values) but pattern is XSS-adjacent. Use `|tojson`. [`src/open_ems/web/templates/fragments/homeowner/ev-card.html:9`]
- [x] [Review][Patch] `static/alpine.min.js` SHA256 documented in `README.md` but not enforced by any test — silent corruption or unverified replacement would never be caught. Add a hash-verification test. [`src/open_ems/web/static/alpine.min.js`, `src/open_ems/web/static/README.md`]

### Deferred (10)

- [x] [Review][Defer] EV charger disconnect mid-override does not clear the override — reconnect resumes; on degradation after `session_observed_active=True` the override survives for 2h while `homeowner_override_active=True` continues authorizing ControlLoop window-bypass. Design question: resume-on-reconnect semantics vs. degrade-clear. [`src/open_ems/core/state_store.py:170-171`] — deferred to deferred-work.md
- [x] [Review][Defer] Idempotent replay with a successful-but-not-session-observed override traps homeowner in Optimistic UI for up to 2h with no retry path — server-side override survives, client-side fallback timer (Patch #4) helps but a fresh POST during the window returns idempotent-replay 200. Needs product/UX decision on "stuck pending" recovery semantics. [`src/open_ems/web/routes/actions.py:98-119`] — deferred to deferred-work.md
- [x] [Review][Defer] `_safe_audit` blocks on DB INSERT serially in hot homeowner path — happy POST awaits two `await observability.audit(...)` writes before returning; under SQLite contention the 2s contract is at risk. Consider fire-and-forget audit emission. [`src/open_ems/web/routes/actions.py:73-82, 140-154`] — deferred to deferred-work.md
- [x] [Review][Defer] `set_ev_override` silently displaces an existing non-None override (e.g., expired-not-yet-swept case) without log/audit emission — audit-trail gap for cross-override lifecycle observability. [`src/open_ems/core/state_store.py:96-104`] — deferred to deferred-work.md
- [x] [Review][Defer] Cold-start POST returns the same 400 phrase for "not configured" / "still booting" / "cable disconnected" — `DegradedDeviceState.reason` could surface a more specific message. UX nuance. [`src/open_ems/web/routes/actions.py:71-92`] — deferred to deferred-work.md
- [x] [Review][Defer] No structlog INFO emission inside `set_ev_override` distinguishing install vs clear — forward-looking gap for Story 10.3's `set_active_strategy` second mutator and observability across the override lifecycle. [`src/open_ems/core/state_store.py:86-104`] — deferred to deferred-work.md
- [x] [Review][Defer] `constraint_notice` shown in confirmed state for `normal`, `degraded`, `conservative` modes — message text ("Peak limit still active...") may be misleading in conservative/degraded modes where the constraint isn't peak-limit. Gate strictly on `normal` or parameterize by mode. [`src/open_ems/web/state_serialization.py:223-228`] — deferred to deferred-work.md
- [x] [Review][Defer] `data-correlation-id` is omitted in the unavailable branch even when an override exists in state_store — diagnostic data lost during the degraded window. [`src/open_ems/web/state_serialization.py:187-201`] — deferred to deferred-work.md
- [x] [Review][Defer] `EVChargerState.status="faulted"` not consulted in the override state machine — `session_active=True ∧ status="faulted"` renders Confirmed. UX may want a fault-specific Fallback. [`src/open_ems/web/state_serialization.py:208`] — deferred to deferred-work.md
- [x] [Review][Defer] `_log_task_completion` returns silently on `task.cancelled()` — no log event records cancellation, defeating post-mortem analysis of in-flight dispatch loss at shutdown. Pairs with the (also-deferred) lifespan-level dispatch-task tracking work. [`src/open_ems/web/routes/actions.py:250-261`] — deferred to deferred-work.md

### Dismissed (7)

- Comparison-operator asymmetry `>` (route idempotency) vs `<=` (publish expiry-clear) — actually consistent: at `expires_at == now`, route falls through AND publish clears; both classify equality as expired. **Dismissed: false positive (symmetric semantics).**
- CSRF rotation inconsistency — hypothetical; current CSRF model is per-session-stable, no rotation feature exists. **Dismissed: speculative.**
- `_format_next_session_summary` does not validate `HH:MM` format — trusts upstream `ActiveConstraints` schema. **Dismissed: defensive overreach; type discipline belongs upstream.**
- `get_active_constraints` vs `get_active_constraints_optional` code duplication — maintainability nit, not a bug. **Dismissed: noise.**
- Dead `and result.applied` check after `result.status is CommandStatus.success` — defensive against a hypothetical CommandResult validator weakening. **Dismissed: harmless defensive code.**
- Two-tab concurrent POST race — `SetEVChargingRateCommand` is idempotent at the adapter, both POSTs converge under the writer lock. **Dismissed: functionally safe.**
- Blind Hunter's HIGH 1 (`current.correlation_id` dereference) and two LOW items (`_log_task_completion` CancelledError, 2s contract test masking) — **withdrawn by the reviewer during their own write-up.**
