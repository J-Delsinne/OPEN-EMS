# Story 10.3: Implement inline strategy selector with StateStore-authoritative update and specific failure messaging

Status: done
_Status: ready-for-dev → in-progress → review by bmad-dev-story on 2026-05-12._
_Status set to done by bmad-code-review on 2026-05-12 after review-closure gate passed (5b): 9 resolved, 19 dismissed, 4 deferred-and-verified._

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

> **Type:** Epic 10 homeowner control surface (second of two). First non-device homeowner write path — mutates a `SystemSnapshot` field that has no underlying adapter command.
> **A2-triggered:** **Yes.** Two triggers match — **T1 (lifecycle / state-machine)** and **T4 (watchdog / timing semantics — bounded confirmation window)**. R1–R7 are mandatory and rendered below.
> **Prerequisite (soft):** Story 10.1 (homeowner dashboard, done 2026-05-11) and Story 10.2 (EV status card, done 2026-05-12). 10.1 introduced `SystemSnapshot.active_strategy` as the read field with no mutator; 10.2 established the canonical writer-lock mutator pattern (`set_ev_override`) including the **published-snapshot patch via `model_copy(update={...})` so `get_snapshot()` reflects the mutation immediately** (Subtask 4.5 of 10.2). 10.3 lands the mutator the 10.1 review-deferred item flagged.
> **Closes:** the 10.1 review-deferred finding `[src/open_ems/core/state_store.py:46, 92]` "_active_strategy writer-lock contract for Story 10.3 mutator" (deferred-work.md line 7) — classified `must-before-10-3`.
> **Sibling stories:** 10.4 (FR30 weekly summary, backlog) is independent. After 10.3 lands, both homeowner write surfaces (override + strategy) are live and the MVP dashboard contract is complete except for FR30.

## Story

As a homeowner,
I want to switch my energy strategy with a single tap and see the headline update immediately, with a specific failure message if the switch cannot be applied,
so that changing strategy feels instant and I always know definitively whether the change took effect.

## Acceptance Criteria

**Given** the homeowner dashboard renders (post-10.1 / post-10.2), `StateStore` carries an `active_strategy` field that flows through `SystemSnapshot`, and the homeowner is authenticated on `/homeowner/dashboard`

**When** this story is implemented

**Then** the following acceptance criteria must hold:

1. **AC1 — `StateStore.set_active_strategy(...)` mutator.** A new async mutator on `StateStore` (`src/open_ems/core/state_store.py`) mirrors the `set_ev_override` pattern established by Story 10.2:

   ```python
   async def set_active_strategy(self, strategy: EnergyStrategy) -> bool:
       """Install a new active strategy under the writer lock (Story 10.3 AC1).

       Patches the published snapshot's ``active_strategy`` so ``get_snapshot()``
       reflects the change immediately. The snapshot's ``sequence_id`` and
       ``captured_at`` are intentionally NOT advanced — this is a single-field
       patch, not a publish cycle. The next ``publish()`` advances ``sequence_id``
       normally and carries the new ``_active_strategy`` value forward.

       Returns True if the strategy actually changed (the caller can suppress
       a no-op audit emission); False if ``strategy == self._active_strategy``
       under the lock and no mutation was applied.
       """
       async with self._writer_lock:
           if self._active_strategy == strategy:
               return False
           self._active_strategy = strategy
           previous = self._snapshot
           self._snapshot = previous.model_copy(update={"active_strategy": strategy})
           return True
   ```

   The mutator MUST:
   - Take `self._writer_lock` — matches the 10.2 `set_ev_override` contract and the writer-lock invariant documented in the `StateStore` class docstring (`state_store.py:1–9`).
   - Compare against the in-lock `self._active_strategy` (NOT `self._snapshot.active_strategy`) — the in-memory field is canonical; the snapshot field reflects the published copy.
   - Patch the snapshot via `previous.model_copy(update={"active_strategy": strategy})` so `get_snapshot()` returns the new value before the next `publish()`. This closes the same drift-risk class 10.2 hit in its Subtask 4.5 design refinement — without the snapshot patch, `ControlLoop._build_evaluation_input` reads the stale strategy on every tick until the next `publish()`.
   - NOT advance `sequence_id` or `captured_at` (matches 10.2 docstring — single-field patch ≠ publish cycle).
   - Return a `bool` indicating whether mutation occurred; the route uses this to suppress the audit emission on same-strategy POSTs (AC3 idempotency contract).

   Update the `StateStore` class docstring (lines 1–9) to enumerate **both** mutators (`set_ev_override`, `set_active_strategy`) as the canonical pattern.

2. **AC2 — `POST /actions/set-strategy` route.** Add a new route to the existing `src/open_ems/web/routes/actions.py` (created by Story 10.2 — the homeowner-actions namespace):

   ```python
   @router.post("/actions/set-strategy")
   async def post_set_strategy(
       request: Request,
       user: HomeownerUser = Depends(require_homeowner),
       state_store: StateStore = Depends(get_state_store),
   ) -> HTMLResponse: ...
   ```

   Dependencies follow 10.2's pattern (CSRF-protected via universal `CsrfMiddleware`; `require_homeowner` enforces the role gate). **Do NOT take `policy_guard` or `settings` deps — strategy is not a device command, so there is no PolicyGuard touchpoint and no background dispatch task. The route is synchronous and returns ≤50ms.**

   Request body is a form-encoded `strategy=<value>` (HTMX default form submission). The route MUST:
   1. Parse the strategy value out of `request.form()` (HTMX submits as `application/x-www-form-urlencoded`).
   2. Validate against the `EnergyStrategy` enum — invalid value → return `400` with the AC4 failure-fragment (NOT 422; HTMX 1.9 has a known behavior where 4xx responses with `hx-swap` populate the swap target — we deliberately return a rendered failure fragment with status `200` so HTMX swaps the failure notice into `#status-headline`'s sibling slot; see AC4 for the failure-rendering contract).
   3. Call `await state_store.set_active_strategy(strategy)`. The mutator returns `True` if mutation occurred, `False` for same-strategy idempotent no-op.
   4. **If mutated:** emit a CONTROL-class audit row (AC6).
   5. Re-read `snapshot = state_store.get_snapshot()` and render the **status-headline fragment** (`fragments/homeowner/status-headline.html`) via `build_homeowner_headline_context(snapshot)`. Return as `HTMLResponse` with status `200`. **This is the HTMX swap target** — the response replaces `#status-headline` via `hx-swap="outerHTML"` so the headline updates in one round-trip.
   6. **If no-op (same strategy):** still return `200` with the rendered headline fragment (idempotent success); skip audit emission.

   Register order in `web/app.py:create_app()` is unchanged (the existing `actions_router` already includes both routes once the new endpoint lands).

3. **AC3 — Idempotency contract.** Submitting the currently-active strategy is a no-op `200` success: no `StateStore` mutation, no audit emission, no `sequence_id` advance, no `config_audit_log` entry, no structlog INFO. This matches the UX spec line 1131 ("Non-destructive, bounded, reversible action") — the homeowner should not be punished with audit-log churn for accidentally re-selecting the active option. The mutator's `bool` return value drives this: when `False`, skip the audit branch.

4. **AC4 — Failure-response rendering.** A POST that fails (invalid strategy value, missing CSRF, server-side error during render) MUST surface a calm inline notice without modifying the headline. The failure path:
   - **Invalid `strategy` value** (not a member of `EnergyStrategy`): return a new `fragments/homeowner/strategy-failure.html` partial as `HTMLResponse` status `200`. The partial renders into a new dedicated DOM slot `#strategy-failure-slot` (immediately below the strategy selector panel — see AC8 template). Content: a `<p>` in `--color-text-secondary` with the exact text **"Strategy update failed. Your previous setting is still active."** (UX spec line 1133). No amber, no warn class.
   - **CSRF failure** (`X-CSRF-Token` mismatch): the universal `CsrfMiddleware` returns `403` BEFORE the route runs. HTMX 1.9 swallows 4xx silently. The Alpine.js layer (AC9) MUST listen for `htmx:responseError` and render the same calm notice into `#strategy-failure-slot`.
   - **Server-side render error** (`build_homeowner_headline_context` raises): catch via a top-level `try/except` in the route, log a structlog ERROR, and return the same strategy-failure partial. **Do NOT** propagate the 500 to the homeowner.
   - The Alpine state machine (AC9) treats any HTMX failure event as a transition into `error` state, which:
     - Collapses the selector panel.
     - Leaves the headline unchanged (no DOM mutation on `#status-headline`).
     - Renders the failure notice into `#strategy-failure-slot`.
   - **No spinner left hanging:** the `pending` Alpine state has a hard client-side timeout of `settings.strategy_update_confirmation_timeout_seconds` (AC10 — new Settings field, default `5` seconds). After the timeout, if no HTMX response has arrived, Alpine transitions to `error` and renders the same failure notice. This is the structural enforcement of epics.md line 2196–2199 ("Given the POST fails or the strategy is not confirmed in StateStore within a timeout").

5. **AC5 — `_STRATEGY_LABELS` carries through to the selector.** The existing `_STRATEGY_LABELS` table at `state_serialization.py:34` (`Minimize Cost`, `Maximize Self-Consumption`, `Prioritize EV`) is the single source of the user-visible strategy text — both for the headline AND for the selector option rows. **Do NOT duplicate the label text in the template.** Inject the table into the headline fragment context as `strategy_options: list[dict[str, str]]` where each dict is `{"value": "<enum_value>", "label": "<label>", "active": bool}` (the `active` flag drives the accent-border styling on the currently-selected option per UX spec line 1127). Extend `build_homeowner_headline_context` to include this list:

   ```python
   return {
       # ... existing fields ...
       "strategy_options": [
           {"value": s.value, "label": _STRATEGY_LABELS[s], "active": s is active_strategy}
           for s in EnergyStrategy
       ],
   }
   ```

   Iteration order is the `EnergyStrategy` enum declaration order (`minimize_cost` → `maximize_self_consumption` → `prioritize_ev`) — matches UX spec line 805 and the journey-flow ordering. **Do NOT alphabetize.**

6. **AC6 — Audit emission.** Strategy change is a homeowner-authored action per UX spec line 1916 ("Every user action creates a log entry"). The route emits exactly one CONTROL-class audit row per mutation:

   ```python
   await _safe_audit(
       observability,
       actor="homeowner",
       event_type="DEVICE",  # 10.2 precedent for homeowner-action category
       summary="Homeowner changed energy strategy",
       detail={
           "event": "homeowner_strategy_changed",
           "previous_strategy": previous_strategy.value,
           "new_strategy": strategy.value,
           "user_id": user.user_id,
       },
       device_id=None,  # strategy is system-level, not device-scoped
   )
   ```

   `_safe_audit` is the helper introduced by Story 10.2 at `web/routes/actions.py:292-327`; reuse it directly. The audit emission MUST run AFTER the mutator returns `True` and BEFORE the route reads the post-mutation snapshot — that ordering guarantees the audit row records the correct `previous_strategy` (captured pre-mutation from `state_store.get_snapshot().active_strategy`).

   **Idempotent no-op POSTs do NOT emit an audit row** (AC3) — `_safe_audit` is called only when `mutated is True`.

   **Note on `event_type`:** Story 10.2 documented `EventType.CONTROL` was the spec target but `DEVICE` was the closest enum value (10.2 sprint-status notes 2026-05-12: "AC17 — DEVICE is the closest valid enum value"). 10.3 follows the same precedent — emit `event_type="DEVICE"` (string passed through to `ObservabilityService.audit`); resolve to whichever enum member matches the existing audit-log schema. If `EventType` has gained a `CONTROL`-like member between 10.2 and 10.3, use it — document the deviation in completion notes.

7. **AC7 — `ControlLoop` automatically picks up the change.** `engine/control_loop.py:202` already reads `strategy=snapshot.active_strategy` (Story 10.1 AC3 — line `control_loop.py:202`). **No ControlLoop changes are required for 10.3.** The mutator's snapshot patch (AC1) means the next ControlLoop tick (≤10s after the mutation) reads the new strategy from `state_store.get_snapshot()` and propagates it into `EvaluationInput`. **Do NOT** add a fresh `_build_evaluation_input` call site, do NOT introduce a strategy-change notification path to the loop — `get_snapshot()` is the single read path.

   Add **one new test** in `tests/unit/engine/test_control_loop.py` that asserts:
   - Construct a `StateStore` with default strategy `maximize_self_consumption`.
   - Run one tick → `EvaluationInput.strategy == EnergyStrategy.maximize_self_consumption`.
   - Call `await state_store.set_active_strategy(EnergyStrategy.minimize_cost)`.
   - Run another tick → `EvaluationInput.strategy == EnergyStrategy.minimize_cost`.

   This is the **structural enforcement** of the AC9 invariant ("the mutator's snapshot patch is the single propagation mechanism — no notification, no callback, no ControlLoop hook"). Without this test a future refactor could re-introduce a callback path silently.

8. **AC8 — Status-headline template gains the inline strategy selector.** `src/open_ems/web/templates/fragments/homeowner/status-headline.html` (currently 13 lines, status-only) is extended to include the selector panel. Layout (mobile-first single column, single `<section>` element):

   ```jinja2
   <section
     id="status-headline"
     class="status-headline status-headline--{{ presentation_mode }}"
     data-presentation-mode="{{ presentation_mode }}"
     data-operating-mode="{{ operating_mode }}"
     data-active-strategy="{{ active_strategy }}"
     aria-label="System status"
     x-data="strategySelector({initialStrategy: {{ active_strategy | tojson }}, timeoutSeconds: {{ strategy_update_confirmation_timeout_seconds | tojson }}})"
   >
     <h2 class="status-headline__heading">
       <span class="status-headline__text" aria-live="polite">{{ headline_text }}</span>
     </h2>
     <p class="status-headline__explanation">{% if explanation_text %}{{ explanation_text }}{% else %}&nbsp;{% endif %}</p>

     <button
       type="button"
       class="status-headline__strategy-trigger"
       aria-expanded="false"
       x-bind:aria-expanded="open ? 'true' : 'false'"
       aria-controls="strategy-selector-panel"
       x-on:click="toggle()"
     >Change strategy</button>

     <div
       id="strategy-selector-panel"
       class="strategy-selector"
       role="listbox"
       aria-label="Energy strategy"
       x-show="open"
       x-cloak
       x-on:keydown.escape.window="close()"
     >
       {% for option in strategy_options %}
       <button
         type="button"
         class="strategy-selector__option{% if option.active %} strategy-selector__option--active{% endif %}"
         role="option"
         aria-selected="{{ 'true' if option.active else 'false' }}"
         hx-post="/actions/set-strategy"
         hx-vals='{"strategy": "{{ option.value }}"}'
         hx-headers='{"X-CSRF-Token": "{{ csrf_token }}"}'
         hx-target="#status-headline"
         hx-swap="outerHTML"
         x-on:click="markPending({{ option.value | tojson }})"
       >{{ option.label }}</button>
       {% endfor %}
     </div>

     <div
       id="strategy-failure-slot"
       class="strategy-selector__failure"
       role="status"
       aria-live="polite"
       x-show="errorMessage"
       x-cloak
     >
       <p x-text="errorMessage"></p>
     </div>
   </section>
   ```

   **Critical invariants:**
   - The `<section id="status-headline">` is the HTMX swap target for `POST /actions/set-strategy` AND for the existing 10s polling `GET /fragments/homeowner/status-headline`. **Both paths return this exact `<section>`.** A successful POST swaps the section (new headline + new `active` flag on the selected option + `aria-live="polite"` announces the new strategy via the headline span). The 10s poll likewise refreshes the section if SSE/polling delivers a state change.
   - **`csrf_token` MUST be in the headline context.** Same logic as Story 10.2 AC14: per-button `hx-headers` survives `outerHTML` swap timing races where the `htmx:configRequest` listener in `base.html` may not yet be wired on the swapped-in DOM. Extend the headline-fragment route (`routes/fragments.py:96` `homeowner_status_headline`) to inject `user.csrf_token` from `Depends(require_homeowner)` — the route currently uses `_user` as a noqa-discarded dep; change to `user` and pass `user.csrf_token` through `build_homeowner_headline_context(...)` OR add `csrf_token=user.csrf_token` to the template context dict directly. **Update the route to pass csrf_token; do NOT thread it through the builder** (the builder is pure-functional and a CSRF token is auth state, not state-snapshot data).
   - The selector panel is rendered server-side as part of the headline fragment AND included in every `outerHTML` swap. There is no separate `/fragments/homeowner/strategy-selector` endpoint — the headline fragment is the single render path. This matches Story 10.2's "EV card is one fragment with state machine derived from snapshot" pattern.
   - **`x-cloak` style is mandatory** in `open-ems.css` (`[x-cloak] { display: none !important; }`) so the selector panel and failure slot do not flash visible during initial page load before Alpine.js hydrates. Story 10.2 did NOT need x-cloak (the EV card's optimistic layer is button-state only); 10.3 introduces the first `x-show` usage and the first need for x-cloak.
   - The "Change strategy" trigger button is **always rendered** — there is no degraded-mode hide. UX spec line 1474 mandates the selector is reachable from the homeowner dashboard at all times. In degraded operating mode the homeowner can still change the strategy preference — the decision engine will honor the new strategy on the next tick whenever the system is not in `fail_safe`. (In `fail_safe`, the engine's evaluator structurally ignores strategy per Story 7.x's degradation matrix; the homeowner's preference is recorded but has no immediate effect.)

9. **AC9 — Alpine.js `strategySelector` factory.** Add to the existing `{% block scripts %}` in `dashboard.html` (created by Story 10.2 for the `evOverride` factory). Place the new factory BELOW the existing `evOverride` factory; both inside the same `alpine:init` listener.

   ```html
   Alpine.data('strategySelector', (config) => ({
     open: false,
     pendingStrategy: null,
     errorMessage: '',
     initialStrategy: (config && config.initialStrategy) ? config.initialStrategy : null,
     timeoutSeconds: (config && Number.isFinite(config.timeoutSeconds))
       ? config.timeoutSeconds
       : null,
     _timer: null,

     toggle() { this.open = !this.open; if (!this.open) { this._clearTimer(); } },
     close() { this.open = false; this._clearTimer(); },

     markPending(strategyValue) {
       // Optimistic: mark which option was tapped. The HTMX request fires via
       // the bound hx-post; on success the server-rendered fragment replaces
       // this entire section (and the new x-data initialState reflects the
       // server-authoritative active strategy). On failure, the htmx events
       // below flip us to error and the headline does NOT swap.
       this.errorMessage = '';
       this.pendingStrategy = strategyValue;
       if (this.timeoutSeconds && this.timeoutSeconds > 0) {
         this._timer = setTimeout(() => {
           if (this.pendingStrategy !== null) {
             this._fail('Strategy update failed. Your previous setting is still active.');
           }
         }, this.timeoutSeconds * 1000);
       }
     },

     _fail(msg) {
       this._clearTimer();
       this.open = false;
       this.pendingStrategy = null;
       this.errorMessage = msg;
     },

     _clearTimer() {
       if (this._timer !== null) { clearTimeout(this._timer); this._timer = null; }
     },

     destroy() { this._clearTimer(); },

     // Bind to HTMX events at the section level via x-on:htmx:response-error.window
     // (handled in template) — failure path delegates to _fail().
   }));
   ```

   In the template, add window-level HTMX failure listeners on the `<section>`:

   ```html
   x-on:htmx:response-error.window="_fail('Strategy update failed. Your previous setting is still active.')"
   x-on:htmx:send-error.window="_fail('Strategy update failed. Your previous setting is still active.')"
   ```

   **Cross-section listener concern:** `.window` modifier means ALL htmx errors anywhere on the page flip the strategy selector into error. This is incorrect — an EV-card POST failure should not surface a "strategy update failed" message. **Constrain the listener** to events whose target is one of the strategy-selector option buttons. Two options:

   - (a) Use `htmx:beforeRequest` to mark the request with `event.detail.requestConfig.path === '/actions/set-strategy'` and store a per-request flag; then in `htmx:responseError` check the flag. Implementation: skip `.window` and bind at the panel level (`x-on:htmx:responseError`) so only events bubbling from THIS panel's children trigger the failure. **This is the preferred path.** Bind at `#strategy-selector-panel` level, not `.window`:
     ```html
     <div id="strategy-selector-panel" ...
          x-on:htmx:responseError="_fail('Strategy update failed. Your previous setting is still active.')"
          x-on:htmx:sendError="_fail('Strategy update failed. Your previous setting is still active.')">
     ```
   - (b) Inspect `event.detail.requestConfig.path` inside the `.window` handler and filter. Less elegant; same effect.

   Use path (a). **Document the bubbling contract** in the template: HTMX events from `option` buttons bubble to `#strategy-selector-panel` which is the listener owner.

   **Success path** is handled by the HTMX `outerHTML` swap itself — the whole `<section>` is replaced by the server-rendered response, which destroys the Alpine instance and re-creates it with `initialStrategy` matching the new server-authoritative value. `destroy()` clears the timer; the new instance starts with `errorMessage=''`, `pendingStrategy=null`, `open=false` — clean state.

10. **AC10 — Settings: `strategy_update_confirmation_timeout_seconds`.** Add to `src/open_ems/settings.py`:

    ```python
    # Story 10.3 — Strategy-selector client-side confirmation budget. After the
    # homeowner taps an option, the Alpine optimistic layer waits this many
    # seconds for the HTMX outerHTML swap (success → headline replaced) OR
    # an htmx:responseError event (failure → calm notice rendered). If neither
    # fires within the budget, the layer assumes the request silently dropped
    # and renders the same calm-notice failure UX. NOT enforced server-side —
    # the route itself returns synchronously (no background dispatch task).
    strategy_update_confirmation_timeout_seconds: int = Field(default=5, ge=1, le=30)
    ```

    Default `5` seconds is tighter than the EV override's `30` because (a) the strategy POST does NOT spawn a background dispatch task — the server-side path is a single StateStore write + audit emission + headline render, all in-process and ≤50ms; (b) any timeout >5s is almost certainly a real failure not a slow path. Bounded `[1, 30]` to prevent misconfiguration.

    Thread through the dashboard route the same way 10.2 threaded `ev_override_confirmation_timeout_seconds`: extend `build_homeowner_headline_context(...)` to accept a `confirmation_timeout_seconds: int | None = None` kwarg AND inject it into the template context. Alternatively (cleaner): add to `build_homeowner_headline_context` as an optional kwarg, OR inject via the route's template-context dict directly without threading through the builder. **Prefer the route-level injection** — the builder stays pure-functional (operates on `SystemSnapshot` only); the timeout is a deployment-config value, not snapshot state.

11. **AC11 — Stale-reference cleanup (informational; non-blocking).** The architecture document still names this endpoint `/actions/set-strategy` (`architecture.md:881`); epics.md (line 2192) uses the same name. They agree. **No doc-sync follow-up is needed** for the route name — unlike 10.2 which inherited a naming drift between `charge-now` (arch.md) and `ev-override` (epics.md).

    However, the architecture document at `architecture.md:822-826` references the `strategies/` directory (`minimize_cost.py`, `maximize_self_consumption.py`, `prioritize_ev.py`) which does not exist in the current codebase — strategy parameter sets are inlined in `engine/rules/energy_balancing.py` (lines 99–101 dispatch by enum). This is **out of scope for 10.3**; document in completion notes for a future architecture-doc-sync sweep (likely Epic 11).

12. **AC12 — `dashboard.html` requires no structural change.** The dashboard shell already includes `<section id="status-headline">` with HTMX polling at 10s (line 10–15). The headline fragment endpoint is the swap target for both the poll AND the new strategy POST. **Do not add a `<section id="strategy-failure-slot">` to the dashboard shell** — the failure slot lives INSIDE the headline fragment, so it is replaced atomically on every successful POST or poll. This preserves layout stability (UX spec line 1114: button never repositions; same principle applies to the selector panel).

    Verify the existing `test_homeowner_dashboard_shell_renders_five_htmx_bound_sections` test (Story 10.2 updated count from 4 → 5) still asserts five HTMX-bound sections; 10.3 adds NONE. The test count stays at 5.

13. **AC13 — CSS additions for the strategy selector.** Extend `src/open_ems/web/static/open-ems.css` using existing tokens (no new tokens):

    ```css
    /* Story 10.3 — Inline strategy selector. */
    [x-cloak]                                 { display: none !important; }

    .status-headline__strategy-trigger {
      display: inline-block;
      margin-top: 0.5rem;
      padding: 0.25rem 0.75rem;
      border: 1px solid var(--color-border);
      border-radius: 6px;
      background: var(--color-page-bg);
      color: var(--color-accent);
      font-size: 0.875rem;
      cursor: pointer;
    }
    .status-headline__strategy-trigger:focus-visible {
      outline: 2px solid var(--color-accent);
      outline-offset: 2px;
    }

    .strategy-selector                        { margin-top: 0.75rem; display: flex; flex-direction: column; gap: 0.25rem; }
    .strategy-selector__option {
      display: block;
      width: 100%;
      text-align: left;
      padding: 0.625rem 0.75rem;
      border: 1px solid var(--color-border);
      border-left: 4px solid transparent;     /* reserved slot for active indicator */
      border-radius: 6px;
      background: var(--color-surface);
      color: var(--color-text-primary);
      font-size: 1rem;
      cursor: pointer;
    }
    .strategy-selector__option--active {
      border-left-color: var(--color-accent);
      background: var(--color-surface);       /* same surface; the left-border is the only delta */
    }
    .strategy-selector__option:focus-visible  { outline: 2px solid var(--color-accent); outline-offset: 2px; }

    .strategy-selector__failure {
      margin-top: 0.5rem;
      padding: 0.5rem 0.75rem;
      color: var(--color-text-secondary);     /* slate-600 — calm, NOT amber */
      font-size: 0.875rem;
    }
    ```

    **No amber, no red, no warn classes.** The selector's active state is a thin accent left-border (matches UX spec line 1127: "Accent left-border + `--color-accent-subtle` background"; the spec mentions `--color-accent-subtle` which does NOT exist in the current token table — substitute with the existing `--color-surface` and rely on the left-border as the only visual delta; document the token-naming deviation in completion notes). A regression test (AC14 test #N) asserts the rendered HTML for an active option contains `strategy-selector__option--active` and does NOT contain `pass`, `--color-pass`, `green`, `amber`, `red`, `warn` class fragments — same load-bearing rule as the EV card AC16 in Story 10.2.

14. **AC14 — Unit tests.** All test names follow the naming-honesty rule (Story 9-Y-a precedent — test name MUST describe what the test asserts):

    **`tests/unit/core/test_state_store.py`** (extend; ~6 new tests):
    1. `test_state_store_set_active_strategy_installs_strategy_under_writer_lock` — install via mutator; `get_snapshot().active_strategy` reflects immediately (no publish required).
    2. `test_state_store_set_active_strategy_returns_true_when_changed` — first call returns `True`.
    3. `test_state_store_set_active_strategy_returns_false_on_idempotent_no_op` — same-strategy second call returns `False`; `_active_strategy` and `_snapshot` unchanged (object identity check on the snapshot reference — model_copy NOT invoked on no-op).
    4. `test_state_store_set_active_strategy_does_not_advance_sequence_id` — `previous.sequence_id == updated.sequence_id`; `captured_at` unchanged.
    5. `test_state_store_publish_after_set_active_strategy_carries_new_value` — mutate; then call `publish(...)`; new snapshot's `active_strategy` matches mutator value and `sequence_id` advances by 1.
    6. `test_state_store_set_active_strategy_round_trip_through_get_snapshot_reflects_immediately` — closes the 10.2 Subtask 4.5 lesson structurally; mutate, then `get_snapshot()` MUST NOT return a stale value.

    **`tests/unit/engine/test_control_loop.py`** (extend; 1 new test per AC7):
    7. `test_control_loop_picks_up_strategy_change_on_next_tick_via_snapshot_read` — described in AC7. Asserts the snapshot-patch propagation contract structurally; without this, a future refactor introducing a notification channel could silently break 10.3.

    **`tests/unit/web/test_state_serialization.py`** (extend; ~4 new tests):
    8. `test_build_homeowner_headline_context_includes_strategy_options_in_enum_declaration_order` — `[opts[0].value, opts[1].value, opts[2].value] == ["minimize_cost", "maximize_self_consumption", "prioritize_ev"]`. Asserts NO alphabetization.
    9. `test_build_homeowner_headline_context_marks_only_one_strategy_option_active` — given any `active_strategy`, exactly one option has `active=True`.
    10. `test_build_homeowner_headline_context_strategy_option_labels_match_strategy_labels_table` — each option's `label` equals `_STRATEGY_LABELS[EnergyStrategy(option.value)]`. Closes the "label duplication" anti-pattern structurally.
    11. `test_build_homeowner_headline_context_strategy_options_match_every_enum_member` — `set(opt.value for opt in result["strategy_options"]) == set(s.value for s in EnergyStrategy)`. Future enum additions WITHOUT a label update fail this test AND the existing `_STRATEGY_LABELS` exhaustiveness test.

    **`tests/unit/web/test_set_strategy_route.py`** (new file; ~10 tests):
    12. `test_post_set_strategy_minimize_cost_updates_snapshot_and_returns_headline_fragment` — happy path; assert status `200`; response body contains the new headline text `"Your home is running on solar · Minimize Cost"`; `state_store.get_snapshot().active_strategy == EnergyStrategy.minimize_cost`.
    13. `test_post_set_strategy_idempotent_when_strategy_unchanged` — POST current strategy → status `200`; `_active_strategy` unchanged; **no audit row emitted** (assert via `ObservabilityService` mock / capture).
    14. `test_post_set_strategy_invalid_value_returns_failure_fragment` — POST `strategy=invalid` → response body contains the AC4 failure text and DOES NOT contain a new headline; `state_store` unchanged.
    15. `test_post_set_strategy_emits_homeowner_strategy_changed_audit` — assert one `_safe_audit` call with `event="homeowner_strategy_changed"`, `previous_strategy`, `new_strategy`, `user_id` fields; `actor="homeowner"`.
    16. `test_post_set_strategy_unauthenticated_returns_401_for_htmx` — auth contract (mirror Story 10.2 AC18 test #40).
    17. `test_post_set_strategy_installer_returns_403_for_htmx` — role contract.
    18. `test_post_set_strategy_requires_csrf_token` — POST without `X-CSRF-Token` returns 403 (existing CsrfMiddleware contract; confirms NOT exempted).
    19. `test_post_set_strategy_returns_within_2_seconds` — 10-iteration timing test under `TestClient` (matches Story 10.2 AC18 test #42 precedent); the route is synchronous so this is a margin check, not a contract enforcement. The UX spec 2-second contract (line 1840) covers homeowner write actions globally.
    20. `test_post_set_strategy_response_body_marks_new_strategy_as_active_option` — parse the response HTML; assert exactly one `<button>` with class `strategy-selector__option--active`; assert its `hx-vals` contains the new strategy enum value.
    21. `test_post_set_strategy_response_body_contains_csrf_token_for_outerhtml_swap_survival` — the swapped headline must carry `hx-headers` with the per-button CSRF token so the next strategy POST survives `outerHTML` replacement.

    **`tests/unit/web/test_fragment_routes.py`** (extend; ~2 new tests):
    22. `test_status_headline_fragment_includes_strategy_selector_panel_with_three_options` — GET `/fragments/homeowner/status-headline`; assert exactly three `<button>` elements with `role="option"`; assert their order matches `EnergyStrategy` declaration order.
    23. `test_status_headline_fragment_marks_active_strategy_with_active_class_and_no_pass_green_fragments` — load-bearing AC13 rule; assert no `pass`, `--color-pass`, `green`, `amber`, `red`, `warn` class fragments leak into the rendered selector. Pairs with the 10.2 EV-card no-pass-green test as the second instance of the design-rule enforcement pattern.

15. **AC15 — Integration tests.**

    **`tests/integration/web/test_strategy_selector_e2e.py`** (new file; ~6 tests):
    1. `test_strategy_selector_happy_path_idle_to_new_strategy_via_outerhtml_swap` — boot the app via `app_e2e`; seed homeowner session; GET `/homeowner/dashboard` → 200 with default strategy `maximize_self_consumption` (the lifespan-default in `app.state.state_store`); POST `/actions/set-strategy` with `strategy=minimize_cost` → 200 with headline text containing "Minimize Cost"; assert `app.state.state_store.get_snapshot().active_strategy == EnergyStrategy.minimize_cost`; assert one audit row in the event log with `summary="Homeowner changed energy strategy"`.
    2. `test_strategy_selector_idempotent_no_op_does_not_emit_audit_or_advance_sequence` — POST current strategy twice; assert event log count unchanged after second POST; `snapshot.sequence_id` advances ONLY through normal ControlLoop ticks, not strategy POSTs.
    3. `test_strategy_selector_invalid_value_returns_failure_fragment_and_does_not_mutate_state` — POST `strategy=does_not_exist`; assert response body contains the failure-fragment text; assert `state_store.active_strategy` unchanged.
    4. `test_strategy_selector_change_propagates_to_next_control_loop_tick` — POST a strategy change; run one control-loop tick (use the existing `ControlLoop._tick()` test seam from Story 8-1 / 9-X); assert `EvaluationInput.strategy` equals the new strategy. **End-to-end proof of the snapshot-patch propagation contract.**
    5. `test_strategy_selector_serialized_in_sse_payload_after_change` — POST a change; query `/api/stream/state` (or whatever the SSE endpoint is — confirm via `routes/stream.py`); assert the next SSE message's `active_strategy` field matches the new value. **Verifies the role-aware serialization layer (Story 10.1 AC4) continues to surface the new field correctly.**
    6. `test_strategy_selector_a11y_placeholder` — `pytest.mark.xfail(strict=False) + pytest.mark.skipif(npx absent)` axe-playwright scan over the dashboard with the selector expanded (keyboard-tab to "Change strategy", press Enter, axe scans the rendered panel). Follows the Story 10.1 / 10.2 / 9.x precedent at `tests/integration/web/test_homeowner_dashboard_a11y.py`.

16. **AC16 — Quality gates.**
    - `uv run python -m pytest tests/ --no-cov -q` → all tests pass. Post-10.2 baseline is **1568 passed + 1 skipped + 6 xfailed**. Net delta MUST be strongly positive: ≥18 new passing tests from the AC14/AC15 set above.
    - `uv run python -m ruff check .` → clean across the project.
    - `uv run python -m ruff format --check .` → clean.
    - `uv run python -m mypy src/` → clean across all source files. The new `set_active_strategy` mutator, the route, and the extended `build_homeowner_headline_context` MUST type-check without `# type: ignore`. The `strategy_options` list value type is `list[dict[str, str | bool]]` (heterogeneous values); use `list[dict[str, object]]` per the project convention if mypy resists.
    - `uv.lock` MUST be unchanged. No new Python dependencies. The OPEN-EMS zero-new-Python-dependency contract carries through Epic 10.

17. **AC17 — Sprint-status consistency.** On story close, `_bmad-output/implementation-artifacts/sprint-status.yaml` MUST set:
    ```
    10-3-implement-inline-strategy-selector-with-statestore-authoritative-update-and-specific-failure-messaging: done
    ```
    Per Epic 9 retro action A8 (Status header ↔ sprint-status agreement) and Epic 8 retro A1/A2 (codified in bmad-create-story / bmad-code-review). The story file Status header transitions `ready-for-dev → in-progress → review → done` MUST mirror the sprint-status entry.

## Tasks / Subtasks

- [x] **Task 1 — Add `set_active_strategy` mutator to `StateStore`** (AC: 1)
  - [x] Subtask 1.1 — Implemented `async def set_active_strategy(self, strategy: EnergyStrategy) -> bool` per AC1; writer-lock + in-lock idempotency check + snapshot patch via `previous_snapshot.model_copy(update={"active_strategy": strategy})`; no `sequence_id` / `captured_at` advance. INFO emission `active_strategy_changed` outside the lock with both previous and new values.
  - [x] Subtask 1.2 — Updated `StateStore` class docstring (`state_store.py:1–17`) to enumerate both mutators (`set_ev_override`, `set_active_strategy`) as the canonical writer-lock mutator pattern + the no-sequence-bump invariant.
  - [x] Subtask 1.3 — Added 6 unit tests in `tests/unit/core/test_state_store.py` (lines 668–751) per AC14 #1–#6. All 38 state_store tests pass (32 prior + 6 new).
  - [x] **R7 patch (bundled — Subtask 1.4)** — symmetric lifecycle INFO emissions added to `set_ev_override`: `ev_override_installed` (None→non-None), `ev_override_cleared_via_mutator` (non-None→None), `ev_override_displaced` (different correlation_id). Closes 10.2 deferred items `[state_store.py:96-104]` and `[state_store.py:86-104]` per R7 safe-during-this-story classification.

- [x] **Task 2 — Add `POST /actions/set-strategy` route** (AC: 2, 3, 4, 6)
  - [x] Subtask 2.1 — Added `post_set_strategy` endpoint to `src/open_ems/web/routes/actions.py` (existing router from Story 10.2). Synchronous; reads `request.form()`; validates against `EnergyStrategy`; calls mutator; renders headline fragment via `build_homeowner_headline_context` + `_templates.TemplateResponse`.
  - [x] Subtask 2.2 — Added `_render_strategy_failure(...)` helper that renders the new `fragments/homeowner/strategy-failure.html` partial (AC4); covers invalid-strategy + server-render-error paths (top-level `try/except`); returns `HTMLResponse(status_code=200)`. Module-level constant `_STRATEGY_FAILURE_MESSAGE`.
  - [x] Subtask 2.3 — Emitted one CONTROL audit row per AC6 via `_safe_audit` (reused from 10.2). Pre-mutation snapshot read captures `previous_strategy`; emit ONLY when `mutated is True`. AC3 idempotency contract enforced structurally.
  - [x] Subtask 2.4 — Added 10 unit tests in `tests/unit/web/test_set_strategy_route.py` per AC14 #12–#21. All pass.

- [x] **Task 3 — Extend `build_homeowner_headline_context` with `strategy_options`** (AC: 5, 12)
  - [x] Subtask 3.1 — Modified `build_homeowner_headline_context(snapshot)` in `state_serialization.py` to emit the `strategy_options` list per AC5. Iteration order is `EnergyStrategy` declaration order (verified by test #8). Defensive `.get(...)` fallback per the 10.1 review-patch pattern.
  - [x] Subtask 3.2 — Added 6 unit tests in `tests/unit/web/test_state_serialization.py` per AC14 #8–#11 (the AC #9 test parametrizes 3× active strategies → 6 effective test cases). All pass.

- [x] **Task 4 — Extend the headline fragment template + CSS** (AC: 8, 9, 13)
  - [x] Subtask 4.1 — Rewrote `templates/fragments/homeowner/status-headline.html` per AC8 (selector trigger + selector panel + failure slot + Alpine `x-data="strategySelector(...)"`).
  - [x] Subtask 4.2 — Created `templates/fragments/homeowner/strategy-failure.html` — returns the SAME outer section structure as the headline fragment so HTMX `outerHTML` swap replaces `#status-headline` atomically; the `data-strategy-update-failed="true"` attribute distinguishes failure responses. Uses `x-init="errorMessage = {{ failure_message | tojson }}"` (no factory-signature change required).
  - [x] Subtask 4.3 — Added `strategySelector` Alpine factory to the `{% block scripts %}` in `dashboard.html` (below the existing `evOverride` factory). Inline `failFromHtmx()` method exposed for the template's `x-on:htmx:response-error` / `x-on:htmx:send-error` listeners (bound at `#strategy-selector-panel` level, NOT `.window` — only this panel's child requests trigger the failure UX).
  - [x] Subtask 4.4 — Appended AC13 CSS rules to `src/open_ems/web/static/open-ems.css`. Added `[x-cloak] { display: none !important; }` (first use of x-cloak in the codebase). Reserved transparent 4px left-border on every option for layout stability (only the active option's border-color changes — no width shift).
  - [x] Subtask 4.5 — Updated `routes/fragments.py:homeowner_status_headline` to inject `csrf_token` (from `user.csrf_token`) and `strategy_update_confirmation_timeout_seconds` (from `Settings`) into the template context. Builder stays pure-functional. Added `Settings` dep via `get_settings_dep`.
  - [x] Subtask 4.6 — Added 2 fragment-route tests in `tests/unit/web/test_fragment_routes.py` per AC14 #22–#23. All pass.

- [x] **Task 5 — Settings + dependency wiring** (AC: 10)
  - [x] Subtask 5.1 — Added `strategy_update_confirmation_timeout_seconds: int = Field(default=5, ge=1, le=30)` to `src/open_ems/settings.py` with docstring citing the EV-override-timeout difference rationale.
  - [x] Subtask 5.2 — Threaded into both the headline fragment route AND the strategy POST route's template context (success + failure paths). NOT threaded through the builder.

- [x] **Task 6 — ControlLoop propagation test (no code change)** (AC: 7)
  - [x] Subtask 6.1 — Added `test_control_loop_picks_up_strategy_change_on_next_tick_via_snapshot_read` to `tests/unit/engine/test_control_loop.py`. NO changes to `control_loop.py` — the snapshot read at line 202 already does the right thing; the test asserts the contract structurally. Passes.

- [x] **Task 7 — Integration tests** (AC: 15)
  - [x] Subtask 7.1 — Created `tests/integration/web/test_strategy_selector_e2e.py` with 5 passing tests + 1 a11y-placeholder per AC15. 5 passed + 1 xfailed.

- [x] **Task 8 — Quality gates + sprint-status close** (AC: 16, 17)
  - [x] Subtask 8.1 — `uv run python -m pytest tests/ --no-cov -q` → **1598 passed + 1 skipped + 7 xfailed**. Baseline 1568 + 1 skipped + 6 xfailed → **+30 net passing, +1 xfailed** (target was ≥18 new).
  - [x] Subtask 8.2 — `uv run python -m ruff check .` → All checks passed!
  - [x] Subtask 8.3 — `uv run python -m ruff format --check .` → 258 files already formatted.
  - [x] Subtask 8.4 — `uv run python -m mypy src/` → Success: no issues found in 99 source files.
  - [x] Subtask 8.5 — `uv.lock` unchanged (`git status uv.lock` shows clean).
  - [x] Subtask 8.6 — Sprint-status flipped `ready-for-dev → in-progress` at workflow start; will transition to `review` on Status save below; final `done` transition is owned by `bmad-code-review` per Epic 9 retro A7.

## Dev Notes

### A2 trigger evaluation

| Trigger | Match? | Evidence |
|---|---|---|
| T1 — Lifecycle / state-machine behavior | **Yes** | UX spec §"Strategy Selector" (lines 1120–1138) and Component-State Mapping (line 1885) explicitly define a state machine: IDLE · PENDING (update in-flight) · ERROR (update failed, previous preserved). The "Failure: Panel collapses. Previous strategy remains active and visually reflected in headline" rollback semantic (line 1133) is itself a meaningful state transition — the client tracks the previous-active value to revert to. |
| T2 — Retries / cancellation | **No** | No retry policy. Strategy is bounded + reversible — spamming the selector sends independent POSTs each handled in isolation. No idempotency window (AC3 idempotent-same-strategy is structurally trivial, not a dedup window). HTMX cancellation owned by Starlette default. |
| T3 — Persistence + recovery | **No** | In-memory only; matches 10.1's `active_strategy` treatment. Cold-start clears to `EnergyStrategy.maximize_self_consumption` (StateStore constructor default at `state_store.py:60`). No DB-backed hydration. R5 covers cold-start behavior. |
| T4 — Watchdog / timing semantics | **Yes** | Epics.md line 2196–2199 mandates: "Given the POST fails or the strategy is not confirmed in StateStore within a timeout / When the failure is detected / Then the headline reverts to the previous strategy." This is a deadline-relative invariant — the client-side `strategy_update_confirmation_timeout_seconds` (AC10) enforces the timing contract. The 10.2 confirmation-timeout pattern is the precedent; 10.3 reuses it (tighter default — 5s vs 30s — because no background dispatch task is involved). |
| T5 — Multi-adapter coordination | **No** | UI + StateStore mutator only. No adapter touchpoints (strategy is an evaluator input, not a device command). No PolicyGuard touchpoint. |
| T6 — Deployment / restart behavior | **No** | Same cold-start semantics as 10.1; no new restart-triggered behavior. |
| T7 — Installer workflow orchestration | **No** | Homeowner-facing; no installer wizard touchpoints. |

**Conclusion:** A2-triggered on T1 (primary) + T4 (timing). R1–R7 mandatory. Synthesized below.

### Files modified by this story

**New files:**
- `src/open_ems/web/templates/fragments/homeowner/strategy-failure.html` — failure-fragment partial (small).
- `tests/unit/web/test_set_strategy_route.py` — route tests (~10 tests).
- `tests/integration/web/test_strategy_selector_e2e.py` — end-to-end scenarios (~6 tests including a11y placeholder).

**Modified files:**
- `src/open_ems/core/state_store.py` — add `set_active_strategy` mutator; update class docstring.
- `src/open_ems/web/routes/actions.py` — add `post_set_strategy` route; add `_render_strategy_failure` helper.
- `src/open_ems/web/routes/fragments.py` — `homeowner_status_headline` route gains `csrf_token` and `strategy_update_confirmation_timeout_seconds` in the template context (NOT through the builder).
- `src/open_ems/web/state_serialization.py` — `build_homeowner_headline_context` adds `strategy_options`.
- `src/open_ems/settings.py` — add `strategy_update_confirmation_timeout_seconds`.
- `src/open_ems/web/templates/fragments/homeowner/status-headline.html` — selector panel + failure slot + Alpine `x-data`.
- `src/open_ems/web/templates/dashboard.html` — append `strategySelector` Alpine factory inside existing `{% block scripts %}`.
- `src/open_ems/web/static/open-ems.css` — selector + trigger + failure-notice CSS rules + `[x-cloak]` global.
- `tests/unit/core/test_state_store.py`, `tests/unit/engine/test_control_loop.py`, `tests/unit/web/test_state_serialization.py`, `tests/unit/web/test_fragment_routes.py` — extended per AC14.
- `_bmad-output/implementation-artifacts/sprint-status.yaml` — final entry update on close.

### Architectural alignment

- **Single Source of Truth — StateStore (architecture.md §"Pattern: StateStore snapshots are immutable", line 399):** strategy flows through `SystemSnapshot.active_strategy` and is read via `state_store.get_snapshot()` everywhere downstream (ControlLoop, fragment renderer, SSE serializer). The new mutator is the only writer; no direct mutation by route handlers.
- **Writer-lock invariant for single-field mutators (Story 10.2 docstring, `state_store.py:1–9`):** `set_active_strategy` MUST follow the canonical pattern — writer-lock, in-lock idempotency check, snapshot patch via `model_copy(update={...})`, no `sequence_id` bump. This is the second concrete instance of the pattern; future StateStore mutators added by Epic 11 / 12 should follow this contract.
- **Role-aware serialization at the SSE boundary (architecture.md line 296):** both homeowner and installer serializers already include `active_strategy` (Story 10.1 AC4). The new mutator's snapshot patch means the next SSE message after a strategy change carries the new value automatically.
- **FastAPI dependency injection enforces role (architecture.md lines 251, 663):** the route uses `Depends(require_homeowner)` — no business-logic role checks. Installer-originated strategy changes are NOT a v1 path (UX spec line 1474: homeowner-only navigation surface).
- **CSRF enforcement (architecture.md "every state-changing route" + `web/csrf.py:14`):** the route is NOT exempt from `CsrfMiddleware`. The per-button `hx-headers='{"X-CSRF-Token": "..."}'` is a belt-and-braces additional header (Story 10.2 precedent — survives `outerHTML` swaps that may race the `htmx:configRequest` listener in `base.html`).
- **Single-Evaluator Principle (Epic 7 retro, 2026-05-05):** strategy IS the input that selects the evaluator branch in `engine/rules/energy_balancing.py:99–101`. 10.3 does NOT introduce new evaluators; it changes only the source-of-truth path for the existing `strategy` field on `EvaluationInput`. The decision engine's behavior is unchanged structurally.
- **CommandResult-driven semantics — DOES NOT APPLY here.** Strategy is not a device command; no PolicyGuard, no `CommandResult`, no adapter touchpoint. This is a CONFIG-class change (preferences), not a CONTROL-class command. The audit emission still uses `event_type="DEVICE"` per the 10.2 precedent for homeowner actions (10.2 sprint-status notes 2026-05-12: "DEVICE is the closest valid enum value to the spec-target CONTROL"); document the same deviation in completion notes if it persists.
- **EnergyStrategy enum is closed at three members (FR11).** Any future v2 strategy addition (e.g., `grid_arbitrage`) MUST extend `EnergyStrategy`, `_STRATEGY_LABELS`, AND the strategy-option exhaustiveness test (AC14 #11). The discovery tests close this loop structurally — a new enum member without a label or rule entry fails CI at three different sites.

### Library / framework requirements (no new dependencies)

`uv.lock` MUST be unchanged after this story. The 10.3 stack uses existing libraries only:
- **FastAPI + Jinja2** — route + template rendering.
- **HTMX 1.9.12** — pinned via CDN in `base.html:28` (self-hosting still tracked as a follow-up; out of 10.3 scope).
- **Alpine.js 3.13.10** — already self-hosted at `static/alpine.min.js` (added by Story 10.2). The new `strategySelector` factory is registered in the same `alpine:init` listener as `evOverride`.
- **Pydantic 2.x + pydantic-settings** — `Settings.strategy_update_confirmation_timeout_seconds`.
- **structlog** — log emissions.
- **pytest + pytest-asyncio (auto mode)** — testing.

### Strategy scope split with Story 10.2 and the 10.1 review-deferred item

Story 10.2 established the canonical writer-lock mutator pattern (`set_ev_override`) including the load-bearing snapshot-patch via `model_copy(update={...})`. Story 10.1's review-deferred item `[src/open_ems/core/state_store.py:46, 92]` ("_active_strategy writer-lock contract for Story 10.3 mutator", classified **must-before-10-3** in `deferred-work.md:7`) is explicitly closed by 10.3's AC1. The 10.2 mutator's design refinement — `Subtask 4.5` of 10.2 — caught the snapshot-staleness pitfall in a test (background dispatch task read `get_snapshot()` and always saw `None` because the mutator updated `self._active_ev_override` but not `self._snapshot`). 10.3 inherits this lesson: the AC1 mutator MUST patch the snapshot, not just the in-memory field.

The 10.2 deferred-work item `[src/open_ems/core/state_store.py:96-104]` "`set_ev_override` silently displaces existing non-None override without log/audit" (deferred-work.md line 413, **acceptable-post-story-10-2**) suggests bundling the lifecycle-INFO emission with the 10.3 mutator pattern. **R7 triages this** — see R7 below.

The 10.2 deferred-work item `[src/open_ems/core/state_store.py:86-104]` "No structlog INFO emission inside `set_ev_override` distinguishing install vs clear" (deferred-work.md line 415) similarly suggests 10.3 establish the pattern. **R7 triage:** add a single structlog INFO emission in `set_active_strategy` per AC1; bundle the parallel emission in `set_ev_override` as a small in-cycle patch (see R7).

### Strategy: in-memory-only + cold-start clearing

Same trade-off as Story 10.1's introduction of `active_strategy` and Story 10.2's `active_ev_override` — no SQLite persistence. Cold-start initializes to the constructor default (`maximize_self_consumption`). A homeowner who selected `minimize_cost` and then the process restarts sees `maximize_self_consumption` on the next dashboard load. R5 documents this; in completion notes record it as a known-and-accepted cold-start behavior. The persistence path (an `active_strategy` SQLite column with `ConfigRepo` hydration) is a future-Epic concern — likely Epic 11 or Epic 12 storage-hardening scope.

### Previous-story intelligence

**Story 10.2 (EV status card, done 2026-05-12):** the canonical mutator pattern + the in-cycle review revealed THREE structural lessons that 10.3 inherits directly:
- **Snapshot patch in the mutator is load-bearing** (Subtask 4.5). Without `previous.model_copy(update={...})`, `get_snapshot()` returns stale state until the next `publish()` — and any code path that reads `get_snapshot()` immediately after the mutator (the route's audit emission and headline render in 10.3 do this!) sees the wrong value. AC1 mandates the snapshot patch.
- **Compare-and-set for atomic transitions** (10.2's `compare_and_set_ev_override` at `state_store.py:103–124`). 10.3 does NOT need CAS because the strategy mutation has no async dispatch task / no orphan-detection requirement. A simpler `set_active_strategy(strategy) -> bool` suffices. If a future story adds a multi-step strategy lifecycle (e.g., "strategy pending validation"), it can layer CAS on top following the 10.2 pattern.
- **`_safe_audit` is the audit-emission helper.** Reuse directly from `web/routes/actions.py:292–327`; do NOT re-implement. Audit failures must never block the homeowner flow (Story 8-3 audit-resilience contract).

**Story 10.1 (Homeowner dashboard, done 2026-05-11):**
- Established `_STRATEGY_LABELS` table at `state_serialization.py:34`. 10.3 reuses (does NOT duplicate). The same `_STRATEGY_LABELS` produces both the headline strategy text AND the selector option labels — a single source of truth.
- Established `_DEGRADED_EXPLANATIONS` exhaustive-table pattern. The 10.3 `strategy_options` list follows the same pattern: iterate over `EnergyStrategy` enum and emit one option per member; an exhaustiveness test (AC14 #11) closes the loop structurally.
- The dashboard shell at `dashboard.html` has not changed structurally since 10.2 (5 HTMX-bound sections). 10.3 does NOT add a new section — the selector lives INSIDE the existing `#status-headline` fragment.
- Story 10.1 review patches that 10.3 inherits:
  - **MEDIUM** — defensive `.get(...)` fallback pattern for `_STRATEGY_LABELS` (10.1 review patch #2). Apply the same pattern to the new `strategy_options` list builder: if `EnergyStrategy(option_value)` fails (e.g., a future enum member missing from `_STRATEGY_LABELS`), the builder MUST gracefully omit that option rather than 500 the headline endpoint. Pair with the AC14 exhaustiveness test that catches the gap at CI time.
  - **LOW** — exact `302` + `Location`-starts-with-`/login` assertions for unauth redirect tests (10.1 review patch #6). Apply to AC14 #16 (`test_post_set_strategy_unauthenticated_returns_401_for_htmx`) — assert exact `401` status + the specific `WWW-Authenticate`-style detail body, not `in (401, 302)`.

**Story 9-Y-a (E2E fail-abort coverage, done 2026-05-11):** the naming-honesty precedent. Every 10.3 test name MUST describe what the test actually asserts. The test names in AC14/AC15 above follow this rule.

**Story 7-x (Decision engine):** the strategy enum is consumed in `engine/rules/energy_balancing.py:99–101` via dispatch table. Any v2 enum addition requires updating that table OR the `ValueError("Unhandled EnergyStrategy")` raise at line 105 fires at runtime. 10.3 does NOT change this dispatch — strategy semantics are unchanged; only the write path lands.

### Latest tech information

No library upgrades. Alpine.js 3.13.10 (pinned by 10.2) supports `x-data`, `x-show`, `x-cloak`, `x-on`, `x-bind`, `x-text` natively — all primitives used by the new `strategySelector` factory. HTMX 1.9.12 supports `hx-vals`, `hx-headers`, `hx-target`, `hx-swap`, and the `htmx:responseError` / `htmx:sendError` events natively — confirmed against the pinned version in `base.html:28`.

### Project Structure Notes

- The `POST /actions/set-strategy` route lives in the existing `src/open_ems/web/routes/actions.py` module (created by Story 10.2). No new module is added. The `actions_router` registration in `web/app.py:651` is unchanged.
- The new failure partial `strategy-failure.html` lives alongside the other homeowner fragments at `src/open_ems/web/templates/fragments/homeowner/`. No new directory.
- Tests follow the existing layout: `tests/unit/web/test_set_strategy_route.py` (unit), `tests/integration/web/test_strategy_selector_e2e.py` (integration). Mirrors the 10.2 layout.
- The Alpine `strategySelector` factory is co-located with `evOverride` in `dashboard.html` `{% block scripts %}`. Same `alpine:init` listener; no new script tag, no new external file. The factory is dashboard-scoped; do NOT extract to a separate `.js` file (would require new `<script src=...>` and the `defer` ordering already established in `base.html` for Alpine).

### Orchestration Risk Analysis

**A2 triggers matched:** T1 (lifecycle / state-machine — IDLE ↔ PENDING ↔ IDLE/ERROR with previous-value rollback on failure), T4 (watchdog / timing — `strategy_update_confirmation_timeout_seconds` deadline-relative client-side budget).

#### R1 — Composition-risk analysis

This story converges four operational domains. Each contributes a risk that surfaces only at the convergence point.

1. **Strategy mutation (T1)** converging with the **HTMX `outerHTML` swap pattern (Story 10.2 precedent)** — the route's response IS the new headline fragment. A semantic bug in the rendered HTML (wrong active flag, wrong CSRF token, missing Alpine `x-data` config) yields a UI that LOOKS correct on first paint but breaks on the NEXT strategy POST (e.g., a stale CSRF token in the swapped-in DOM causes the second POST to return 403). **Mitigated by:** AC14 test #21 (`test_post_set_strategy_response_body_contains_csrf_token_for_outerhtml_swap_survival`) — the response is parsed and asserted to carry the CSRF token in the new selector options' `hx-headers` attributes. The 10.2 EV-card review caught the equivalent class of bug (correlation-id round-trip integrity in the swapped fragment); 10.3 inherits the test pattern.

2. **Idempotent same-strategy POST (AC3)** converging with **audit-log integrity (UX spec line 1916, "Every user action creates a log entry")** — the UX spec mandates every user action emits an audit row; 10.3 deliberately violates this for same-strategy POSTs (idempotent no-op skips audit). **Justification:** the UX spec's intent is to capture decisions, not button-mash artifacts. Re-selecting the active strategy is the homeowner equivalent of pressing F5 — it is not a state change and should not pollute the event log. The mutator's `bool` return value drives this contract structurally — the route's audit emission is GATED on `mutated is True`. **Mitigated by:** AC14 test #13 (`test_post_set_strategy_idempotent_when_strategy_unchanged` — assert NO audit row emitted on same-strategy POST). The contract is explicit and tested; future maintainers cannot accidentally enable a "always audit" path without breaking this test.

3. **Client-side timeout (T4, AC10)** converging with **server-side synchronous response (no background task)** — the strategy POST is a single-shot synchronous write. The route returns within ~50ms regardless of timeout-seconds setting. The `strategy_update_confirmation_timeout_seconds` only matters if the network completely drops between request and response. If the server returns `200` within `5s` (default), the Alpine `markPending` timer never fires. If the server is slow or unreachable, the Alpine layer flips to error and shows the calm notice. **Risk:** an adversarial / debug-slow server (e.g., a developer pause in `_safe_audit`) could trigger a false-positive failure notice while the server still completes the mutation successfully. The next 10s headline poll then DOES show the new strategy → momentary "failed, then succeeded" UX. **Mitigated by:** the timeout default is 5s and the server's worst-case path is ~50ms; the gap of 4950ms is enormous in practice. In CI / dev environments where the gap could shrink, the 10s polling poll reconciles within one cycle. Document in completion notes as a known-and-bounded edge case.

4. **Strategy change propagation to ControlLoop (AC7)** converging with the **snapshot-patch contract (AC1)** — `ControlLoop._build_evaluation_input` reads `snapshot.active_strategy` at every tick. The mutator's snapshot patch ensures `get_snapshot()` returns the new value immediately, BUT only if the patch is applied correctly. A regression that drops the `model_copy(update=...)` line (e.g., a "cleanup" refactor that thinks the in-memory `self._active_strategy` is sufficient) would leave `get_snapshot()` returning the OLD strategy until the next `publish()` (≤10s). The decision engine would then make decisions on the OLD strategy for one tick after the homeowner change. **Mitigated by:** AC1 mandates the snapshot patch with a docstring comment citing the 10.2 Subtask 4.5 incident; AC14 test #1 + #6 enforce the contract structurally (mutate; assert `get_snapshot().active_strategy` reflects immediately — failure means the snapshot patch is missing); AC15 test #4 closes the end-to-end loop (mutate + one tick + assert `EvaluationInput.strategy`).

#### R2 — State-transition table

**Server-side `StateStore._active_strategy` field (canonical state):** owned by `StateStore`. State value is the `EnergyStrategy` enum value.

| State | Transitions out | Trigger | Side-effects |
|---|---|---|---|
| `minimize_cost` / `maximize_self_consumption` / `prioritize_ev` | → any of the other two enum values | `await state_store.set_active_strategy(new_strategy)` with `new_strategy != self._active_strategy` | Writer-lock taken; `self._active_strategy = new_strategy`; `self._snapshot = previous.model_copy(update={"active_strategy": new_strategy})`; mutator returns `True`; route emits `homeowner_strategy_changed` audit; next HTMX poll returns new headline; next ControlLoop tick reads new strategy from `snapshot` |
| any | → same (no-op) | `set_active_strategy(same_strategy)` | Writer-lock taken; in-lock idempotency check returns `False`; NO snapshot mutation; NO audit; mutator returns `False`. Route still returns `200` with the rendered (unchanged) headline fragment per AC3 |
| `*` (clear is forbidden) | — | (no API path accepts None) | `set_active_strategy` signature is `strategy: EnergyStrategy` (non-Optional); the only way to "clear" is process restart, which initializes to constructor default |

**Cold-start initialization:** the StateStore constructor at `state_store.py:54–66` accepts `active_strategy: EnergyStrategy = EnergyStrategy.maximize_self_consumption` as a kwarg. The lifespan at `web/app.py:230` constructs the StateStore WITHOUT passing this kwarg, so the default is used. **No change in 10.3.**

**Client-side `strategySelector` Alpine factory state machine:** owned by `dashboard.html`'s `strategySelector` factory. State fields: `open: bool`, `pendingStrategy: string | null`, `errorMessage: string`.

| State (composite) | Transitions out | Trigger | Side-effects |
|---|---|---|---|
| `open=false, pendingStrategy=null, errorMessage=''` (closed-idle) | → open-idle | `toggle()` on "Change strategy" button | `open=true`; selector panel renders |
| `open=true, pendingStrategy=null, errorMessage=''` (open-idle) | → open-pending | `markPending(strategy)` on option button tap | `pendingStrategy=strategy`; timer scheduled for `timeoutSeconds`; HTMX POST in flight |
| `open=true, pendingStrategy=null, errorMessage=''` (open-idle) | → closed-idle | `close()` (Escape key OR click outside — Escape is implemented; click-outside is NOT in v1 per UX spec line 1138 limits) | `open=false` |
| `open=true, pendingStrategy=<s>, errorMessage=''` (open-pending) | → (destroyed by outerHTML swap on success) | Server returns 200 + new headline fragment; HTMX swaps `<section id="status-headline">`; Alpine destroys this instance | `destroy()` clears timer; new instance constructed from server-authored `initialStrategy` reflecting the new strategy |
| `open=true, pendingStrategy=<s>, errorMessage=''` (open-pending) | → closed-error | `htmx:responseError` / `htmx:sendError` from this panel OR timer fires past `timeoutSeconds` | `_fail("Strategy update failed. Your previous setting is still active.")`; timer cleared; `open=false`; `pendingStrategy=null`; `errorMessage=<msg>` |
| `open=false, pendingStrategy=null, errorMessage=<msg>` (closed-error) | → open-idle | User taps "Change strategy" again | `toggle()` clears the error? **NO — leave the failure message visible until the next successful POST.** UX spec line 1133 mandates the calm notice persists; only a new successful action dismisses it. (Alternative: clear `errorMessage` on `toggle()` open — UX call. Decision: keep visible. Document in completion notes.) |
| `closed-error` | → closed-idle (after success) | Next successful POST → outerHTML swap destroys this instance | New instance has `errorMessage=''` |

**Transition: panel open ↔ panel closed is purely client-side.** Opening/closing the panel does not POST anything. Only option-button taps POST.

#### R3 — Impossible-state analysis (with cold-start coverage)

Forbidden state combinations and their structural invariants:

1. `StateStore.set_active_strategy(strategy)` called with a non-`EnergyStrategy` value. **Invariant:** Python type system (`strategy: EnergyStrategy`); mypy enforces at static analysis; runtime: the route's enum validation BEFORE calling the mutator catches invalid values from form input and returns the failure fragment via AC4. The mutator itself trusts the enum-typed signature; defense-in-depth is at the route.
2. `_active_strategy` and `self._snapshot.active_strategy` diverge. **Invariant:** AC1 mandates `self._snapshot = previous.model_copy(update={"active_strategy": strategy})` inside the same writer-lock acquisition as the field update. Both writes happen atomically; readers (`get_snapshot()` is lock-free) cannot observe a torn intermediate state because `model_copy` returns a NEW snapshot reference; the assignment `self._snapshot = <new>` is atomic at the Python level. AC14 test #1 / #6 enforces this structurally.
3. `set_active_strategy` advances `sequence_id`. **Invariant:** AC1 explicitly forbids it. Test #4 asserts `previous.sequence_id == updated.sequence_id`. Without this, every strategy POST would emit a "new tick" signal to SSE consumers, polluting the sequence_id channel.
4. `set_active_strategy` advances `captured_at`. **Invariant:** same as #3 — single-field patch should not lie about being a new measurement cycle. The `data_age_seconds` map derived from `captured_at` would otherwise reset to zero on a strategy change, misleading the per-device staleness UI.
5. Two simultaneous strategy POSTs from the same homeowner session result in a torn state. **Invariant:** writer-lock serializes mutations. The mutator is `await self._writer_lock.acquire()` → assignment → release. Two concurrent POSTs serialize: POST 1 mutates and returns headline-A; POST 2 mutates from headline-A's state and returns headline-B. Both clients receive deterministic results corresponding to their POST order; no torn `_active_strategy`. The next ControlLoop tick reads the LATEST value (headline-B's strategy) — both POSTs are recorded in the audit log; only the second one's effect persists.
6. ControlLoop reads `snapshot.active_strategy` and the value is None or undefined. **Invariant:** `SystemSnapshot.active_strategy` is non-Optional (`active_strategy: EnergyStrategy` at `state.py:213`); Pydantic rejects construction without a value; the StateStore constructor supplies the cold-start default. There is no path where `active_strategy is None` is observable.
7. The Alpine `strategySelector` factory listens for `htmx:responseError.window` and triggers the failure notice on an unrelated POST (e.g., EV override failure). **Invariant:** AC9 mandates the listener is bound at `#strategy-selector-panel` level, NOT `.window`. Events from `option` buttons bubble through the panel; events from the EV card bubble through `#ev-card`. The two listener scopes are disjoint. AC9 explicitly documents this and the dev agent rejects the `.window` modifier.
8. The strategy-failure fragment is rendered into the wrong DOM slot. **Invariant:** the failure slot `#strategy-failure-slot` is INSIDE the headline `<section>`. The headline `outerHTML` swap on a successful POST atomically replaces both the selector and the failure slot, so any prior failure message is cleared with the swap. There is no "stale failure notice plus new headline" combination — they are part of the same fragment.

**Cold-start / startup-grace coverage:**

From process boot until the first successful evaluation cycle, the system is in the following state regarding strategy:
- `StateStore.__init__` constructs the initial snapshot with `active_strategy=EnergyStrategy.maximize_self_consumption` (constructor default at `state_store.py:60`, applied at `state_store.py:76`). The lifespan at `web/app.py:230` does NOT pass an explicit value, so the default is used.
- The homeowner dashboard's headline endpoint `GET /fragments/homeowner/status-headline` returns a headline reading `"Your home is running on solar · Maximize Self-Consumption"` UNLESS `SystemOperatingMode != normal`, in which case the degraded headline (`"Running with limited functionality"`) renders per Story 10.1 AC5. The selector panel renders with `Maximize Self-Consumption` marked active.
- The homeowner CAN tap the "Change strategy" trigger and select a different strategy immediately on first dashboard load — there is no startup-grace gating on the strategy selector. The mutator works against the constructor-default state; the first homeowner action transitions from `maximize_self_consumption` to (e.g.) `minimize_cost`.
- **However:** the FIRST ControlLoop tick (≈1s after lifespan startup) reads the snapshot strategy via `_build_evaluation_input` at `control_loop.py:202`. If the homeowner manages to POST a strategy change within that 1-second window (essentially impossible — the dashboard has to load AND the homeowner has to tap, which is bound to be ≥2s in practice), the engine's first tick uses the constructor default; the next tick (10s later) picks up the homeowner's choice. **Operational impact: zero** — the engine running on `maximize_self_consumption` for one tick is identical to it running on the homeowner's choice for one tick from a safety / correctness standpoint (the strategy only affects optimization preference, never safety boundaries).
- **No state is "temporarily relaxed" during cold start** with respect to the strategy field. The field is structurally always populated (non-Optional) and has a constructor default; the homeowner can always read and write it.
- **Process-restart with a previously-selected strategy:** if the homeowner had selected `minimize_cost` and the process restarts, the new `StateStore` initializes to `maximize_self_consumption` (constructor default). The next dashboard load renders the default; the homeowner sees their previous selection has been forgotten. **This is the inherited in-memory-only trade-off from Story 10.1** — documented in completion notes as a known-and-accepted cold-start behavior.

#### R4 — Cancellation ownership map

| Async operation | CancelledError owner | Cancellation audit owner | Cleanup owner | Reporting field semantics under cancel |
|---|---|---|---|---|
| `routes/actions.py:post_set_strategy` route handler | Starlette's request lifecycle owns request cancellation (e.g., client-aborted HTMX request). The route does NOT spawn a background task — it is synchronous from `await request.form()` through `await state_store.set_active_strategy(...)` through `await observability.audit(...)` through the final template render. CancelledError propagates through these `await`s naturally. | None — no per-request cancellation audit. The mutator's writer-lock takes < 1ms; the audit emission takes a single SQLite INSERT (~5–50ms); the template render is synchronous. If cancellation arrives BEFORE the mutator, no state change happens; if cancellation arrives AFTER the mutator but BEFORE the audit, the state is mutated but no audit row — visible only as a snapshot change without an event-log entry (recovery: the 10s headline poll reaffirms the state; the audit gap is captured in structlog). | None (no resources to release; all state is in-memory or SQLite-row-atomic) | If cancelled mid-`set_active_strategy`: writer-lock is released by `async with` exit; `_active_strategy` either was-mutated (if cancel arrived after the assignment) or was-not (if cancel arrived during lock acquisition). The model is symmetric — either snapshot reflects the change or it doesn't. |
| `StateStore.set_active_strategy` mutator | `self._writer_lock` is `asyncio.Lock` — if cancelled while waiting on the lock, no state mutation occurs (the lock acquisition fails). The two state assignments (`self._active_strategy = strategy` and `self._snapshot = previous.model_copy(update={...})`) are synchronous Python statements with no `await` between them — they CANNOT be partially-applied. | None | None | If cancelled before lock acquisition: no mutation; if cancelled DURING the body: impossible because no `await` between the assignments. |
| `_safe_audit` call (reused from 10.2 at `web/routes/actions.py:292–327`) | The `try: await observability.audit(...) except Exception:` block catches everything except `BaseException` (which includes `CancelledError`), so CancelledError correctly propagates through and surfaces back to the route handler. | None | None | Audit row may or may not be written depending on cancel timing; same semantics as Story 10.2's `_safe_audit` (documented in 10.2 dev notes). |
| HTMX `POST /actions/set-strategy` (client-side abort) | Client-aborted requests owned by Starlette's request lifecycle | None — but the server-side mutation may complete (the response is just unread). | None | The strategy IS mutated server-side; the client just doesn't see the 200 response. The 10s HTMX poll on `#status-headline` reveals the new state. **The Alpine `strategySelector`'s client-side `strategy_update_confirmation_timeout_seconds` timer fires** after 5s and shows the failure notice — even though the server-side mutation succeeded. This is an acceptable false-negative for the client UI; the next poll reconciles. Document in completion notes. |
| HTMX `GET /fragments/homeowner/status-headline` (10s poll, client-aborted) | Client-aborted polls owned by Starlette | None | None | Idempotent GET; no side effects |

**Why this story does NOT use RetryPolicy:** strategy is not a device command; there is no automatic retry path. A failed strategy POST surfaces a calm notice; the homeowner re-taps the option to retry (homeowner-authored retry). This is the same pattern as Story 10.2's EV override "Try again" — Single-Evaluator Principle natural extension: the decision engine retries device commands; user actions surface failures.

#### R5 — Before-first-successful-cycle lifecycle review

Sequence from process boot to first successful normal cycle, focused on strategy-relevant surfaces:

1. **Lifespan step 0 — process start.** `web/app.py:lifespan` begins. `app.state` is empty.
2. **Lifespan steps 1–3 — clock check, migrations, DB init.** No 10.3-relevant state.
3. **Lifespan step at line 230 — `StateStore` construction.** `app.state.state_store = StateStore(system_clock_status=..., stale_threshold_seconds=...)`. `StateStore.__init__` sets `self._active_strategy = EnergyStrategy.maximize_self_consumption` (constructor kwarg default at `state_store.py:60`) and constructs the initial `SystemSnapshot(... active_strategy=EnergyStrategy.maximize_self_consumption ...)`.
4. **Lifespan step 4 — service construction.** `app.state.observability` (for audit emissions), `app.state.policy_guard`, `app.state.active_constraints_provider` are all constructed. **Note:** the strategy route does NOT use `policy_guard` (no device command), so PolicyGuard availability is irrelevant to 10.3 — the strategy POST works even if PolicyGuard hydration failed (theoretically; in practice PolicyGuard failure causes lifespan abort).
5. **Routes become live at yield.** From the moment FastAPI starts accepting requests:
   - `GET /homeowner/dashboard` returns the shell rendering `dashboard.html` with `csrf_token`.
   - `GET /fragments/homeowner/status-headline` returns the headline fragment reading `"Your home is running on solar · Maximize Self-Consumption"` in normal mode (assuming the initial `operating_mode=SystemOperatingMode.degraded` per the StateStore constructor default → the headline actually renders `"Running with limited functionality"` until the first ControlLoop tick brings the system to `normal`). The selector panel renders with `Maximize Self-Consumption` marked active.
   - `POST /actions/set-strategy` works immediately — the StateStore is hydrated; the mutator just needs a valid `EnergyStrategy` value. A homeowner who somehow logs in within the first second of process startup can POST a strategy change, and the mutator succeeds.
6. **ControlLoop tick 1 (≈1s after yield).** Polls adapters; publishes a new snapshot carrying `self._active_strategy` (either the constructor default OR whatever the homeowner POSTed in the first 1s, vanishingly unlikely). Engine evaluates with the strategy value; runs the rule.
7. **Normal operation marker.** The system enters normal operation when `SystemOperatingMode` transitions from `degraded` to `normal` (Story 7.1 — derivation matrix). The strategy field is unchanged through this transition; only the operating-mode-driven headline text changes (from `"Running with limited functionality"` to `"Your home is running on solar · {strategy_label}"`).

**Invariants that temporarily relax during the boot-to-first-cycle window:**
- The headline reads "Running with limited functionality" (Story 10.1 AC5) because `operating_mode == degraded` at startup. The strategy is still ACTIVE on the snapshot — but the headline does not surface the strategy label until the system reaches `normal`. The selector panel renders correctly (active option highlighted) regardless of operating mode.
- `active_constraints` may be unhydrated (`get_active_constraints_optional` returns None pre-installer-wizard-complete). This does NOT affect strategy — the strategy is independent of constraints.
- The decision engine's first tick may produce a degraded-mode evaluation (no commands dispatched). The strategy field is still consumed (used to pick the rule branch in `energy_balancing.py`), but the resulting commands may be filtered downstream.

**Process-restart with a previously-selected strategy:** documented in R5 above and in the "Strategy: in-memory-only" dev-notes section — accepted trade-off for v1; persistence path is Epic 11/12 scope.

#### R6 — Source-of-truth ownership per datum

| Datum | Single owner | Read path | Drift risk (if any) |
|---|---|---|---|
| `StateStore._active_strategy` (in-memory canonical) | `StateStore.set_active_strategy(...)` is the ONLY writer (constructor sets the cold-start default). Reads happen inside `StateStore.publish(...)` under the writer lock at `state_store.py:150`. | n/a — internal field; consumers read `self._snapshot.active_strategy` instead | **NEEDS-MONITOR:** the field exists in two places — `self._active_strategy` (canonical) AND `self._snapshot.active_strategy` (published copy). AC1's mutator MUST keep them in sync via the `model_copy(update={...})` patch. AC14 test #1 + #6 enforce structurally. **Future maintainers MUST follow the canonical pattern** when adding new single-field mutators; the StateStore class docstring documents the invariant. |
| `SystemSnapshot.active_strategy` (published) | Written by `StateStore.set_active_strategy(...)` (via `model_copy`) AND by `StateStore.publish(...)` (via the snapshot constructor at line 150). Both writers happen under the writer lock. | `state_store.get_snapshot().active_strategy` — lock-free read (the snapshot reference is atomic) | None — the snapshot is immutable; replacement is atomic. The mutator's `model_copy` and `publish`'s `SystemSnapshot(...)` constructor BOTH read `self._active_strategy` as the source. |
| `EvaluationInput.strategy` (engine consumer) | Written by `ControlLoop._build_evaluation_input` at `control_loop.py:202` reading `snapshot.active_strategy`. | n/a — internal to the engine evaluation cycle | None — single read path; single derivation site. The 10.3 AC7 test pins this contract. |
| Homeowner SSE payload `active_strategy` field | Written by `serialize_homeowner_snapshot` at `state_serialization.py:140` (`snapshot.active_strategy.value`). Same for installer at line 159. | n/a — payload emitted to SSE clients | None — single derivation site; both serializers use the same source field. |
| `strategy_options[*].label` in headline fragment context | Written by `build_homeowner_headline_context` at `state_serialization.py:278` (AC5 extension). The labels are sourced from `_STRATEGY_LABELS` (`state_serialization.py:34`). | Template renders into the selector option buttons | **POTENTIAL DRIFT — flagged:** the strategy label text now appears in `_STRATEGY_LABELS` (table), `strategy_options[*].label` (builder output), and is rendered in TWO places in the headline fragment (headline span via `strategy_label` AND option buttons via `strategy_options`). All three derive from `_STRATEGY_LABELS` — the table is the single source. AC14 test #10 enforces structurally. |
| `strategy_options[*].value` (enum-value string) | Written by `build_homeowner_headline_context`; sourced from `EnergyStrategy` enum members. | Template renders into option buttons' `hx-vals` AND `value` data attribute. | None — `EnergyStrategy(option_value)` round-trip in the route's form-parsing validates the value. |
| `strategy_options[*].active` flag | Written by `build_homeowner_headline_context` comparing each enum member against `snapshot.active_strategy`. | Template renders into the option button's `class` (`--active` suffix) and `aria-selected`. | None — derived from a single read of `snapshot.active_strategy` at build time. |
| `Settings.strategy_update_confirmation_timeout_seconds` | `pydantic_settings` reads from env/`.env` at process start; constant for the process lifetime. | Read by `routes/fragments.py:homeowner_status_headline` and injected into the template context. | None — process-lifetime constant; no mutation path. |
| Alpine.js `state` ("open" / "pendingStrategy" / "errorMessage") | Client-side `evOverride` factory in `dashboard.html`. Overwritten on every HTMX `outerHTML` swap (success path) OR on `htmx:responseError` (failure path). | n/a — client memory | Maximum desync = one `strategy_update_confirmation_timeout_seconds` window (5s). Server is authoritative; the next HTMX poll (10s) reconciles even if the immediate response was lost. |

#### R7 — Deferred-findings triage

Scanned `_bmad-output/implementation-artifacts/deferred-work.md` for overlapping items:

| Finding (bracket ref) | Classification | Rationale |
|---|---|---|
| `[src/open_ems/core/state_store.py:46, 92]` "_active_strategy writer-lock contract for Story 10.3 mutator" (10.1 review, classified must-before-10-3) | **must-resolve-in-this-story** | This is the explicit prep finding for 10.3. AC1 establishes the writer-lock + snapshot-patch contract for `set_active_strategy`. The deferred-work entry is closed structurally by 10.3's AC1. |
| `[src/open_ems/core/state_store.py:96-104]` "`set_ev_override` silently displaces existing non-None override without log/audit" (10.2 review, classified acceptable-post-story-10-2) | **safe-during-this-story** | The 10.2 deferred entry suggests bundling the lifecycle-INFO emission with the 10.3 mutator pattern. **Bundle into 10.3 scope:** add a `logger.info("active_strategy_changed", previous=..., new=...)` emission inside `set_active_strategy` when `mutated is True`. While in this code path, ALSO add the parallel `logger.info("ev_override_installed", ...)` emission to `set_ev_override` so the two mutators have symmetric observability. This is a small in-cycle patch that closes the 10.2 deferred entry as a side effect. |
| `[src/open_ems/core/state_store.py:86-104]` "No structlog INFO emission inside `set_ev_override` distinguishing install vs clear" (10.2 review, classified acceptable-post-story-10-2) | **safe-during-this-story** | Same logic as above — bundle the symmetric emission into 10.3. The 10.2 entry is closed structurally by the 10.3 in-cycle patch. |
| `[src/open_ems/web/routes/fragments.py:73-85]` "HTMX-error UX when /fragments/homeowner/status-headline 500s" (10.1 review, classified acceptable-post-epic-10) | **acceptable-post-this-story** | The same UX gap (HTMX 1.9 silently ignoring non-2xx headline responses) applies to the 10.3 strategy-selector POST failure path. **Mitigation:** AC4 mandates the route returns 200 + a failure fragment instead of 4xx/5xx, so HTMX swaps the failure notice rather than leaving the headline frozen. Combined with AC9's Alpine `htmx:responseError` listener, the failure UX is robust for the normal failure modes. The original 10.1 defer is about UNEXPECTED 500s; that broader UX pattern remains an Epic 11 installer-monitoring UX scope. |
| `[tests/unit/web/test_fragment_routes.py::test_status_headline_degraded_mode_renders_calm_styling]` "Route-level fail_safe parametrization gap" (10.1 review, classified acceptable-post-epic-10) | **acceptable-post-this-story** | AC15 #3 covers `conservative` / `fail_safe` modes through the integration tests (in those modes the selector panel still renders and is operable — strategy is preference, not a device command, so fail_safe does not block the POST). The route-level test parametrization gap from 10.1 is partially addressed by 10.3's integration coverage but not fully — Epic 11 installer-monitoring UX sweep is the right place. |
| `[src/open_ems/web/routes/actions.py:98-119]` "Idempotent-replay traps homeowner in pending UI for 2h with no retry path" (10.2 review, classified acceptable-post-story-10-2) | **acceptable-post-this-story** | EV-override-specific (2h idempotency window). Strategy POST has no idempotency window — every POST settles synchronously. No overlap. |
| `[src/open_ems/web/routes/actions.py:73-82, 140-154]` "_safe_audit blocks on serial DB INSERTs in the homeowner hot path" (10.2 review, classified acceptable-post-story-10-2) | **acceptable-post-this-story** | 10.3's route emits exactly ONE audit row (or zero for idempotent no-op), so the contention pattern is even lower than 10.2's. No 10.3-specific resolution needed. |
| `[src/open_ems/web/routes/actions.py:71-92]` "Cold-start POST conflates 'not configured' / 'still booting' / 'cable disconnected' 400 phrases" (10.2 review, classified acceptable-post-story-10-2) | **acceptable-post-this-story** | EV-override-specific (no-charger rejection path). Strategy POST has no cold-start gating — works immediately on lifespan yield. No overlap. |
| `[src/open_ems/web/state_serialization.py:223-228]` "`constraint_notice` content misleading in `conservative` / `degraded` modes" (10.2 review, classified acceptable-post-story-10-2) | **acceptable-post-this-story** | EV-card-specific (`constraint_notice` is built in `build_homeowner_ev_card_context`, not in 10.3's surface). No overlap. |
| `[src/open_ems/web/state_serialization.py:208]` "`EVChargerState.status='faulted'` not consulted" (10.2 review, classified acceptable-post-story-10-2) | **acceptable-post-this-story** | EV-card-specific. No overlap. |
| `[src/open_ems/web/state_serialization.py:187-201]` "`data-correlation-id` omitted in unavailable branch" (10.2 review, classified acceptable-post-story-10-2) | **acceptable-post-this-story** | EV-card-specific. No overlap. |
| `[src/open_ems/web/csrf.py:71-79]` "CsrfMiddleware exhausts request body" (10.2 review, pre-existing) | **acceptable-post-this-story** | The 10.3 route reads `request.form()` once, and the CSRF middleware uses cached form data per the existing pattern. Same risk as 10.2; no 10.3-specific resolution. |
| `[src/open_ems/services/runtime_adapter_wiring.py:106-123]` W1/W2 PLACEHOLDER register ranges (9-X review) | **acceptable-post-this-story** | Modbus / capability-registry. Strategy POST has no adapter path. No overlap. |
| `[src/open_ems/services/deployment_validation.py:206-212]` DF1 `is_outdated=False` when constraints unhydrated (9-5 review) | **acceptable-post-this-story** | Deployment-validation-specific. Strategy POST does not consume `active_constraints`. No overlap. |

**Must-resolve-in-this-story: 1 finding** (the 10.1 review-deferred `_active_strategy` writer-lock contract — closed by AC1).
**Safe-during-this-story: 2 findings** (10.2's `set_ev_override` lifecycle-INFO emission gap + the symmetric absent emission — bundled into 10.3 via the symmetric structlog INFO patch in `set_ev_override` AND new emission in `set_active_strategy`).
**Acceptable-post-this-story: 13 findings** (all EV-card-specific, Modbus-specific, validation-specific, or broader-UX-pattern items that have no 10.3 overlap).

### References

- [Source: _bmad-output/planning-artifacts/epics.md#story-103-implement-inline-strategy-selector-with-statestore-authoritative-update-and-specific-failure-messaging] — story spec lines 2178–2206; Epic 10 cross-story constraints lines 2237–2245
- [Source: _bmad-output/planning-artifacts/ux-design-specification.md#5-strategy-selector-homeowner] — selector spec lines 1120–1138; component-state mapping line 1885; homeowner journey flow line 805
- [Source: _bmad-output/planning-artifacts/architecture.md#api-endpoints] — `/actions/set-strategy` named route lines 880–881; strategies module placeholder lines 822–826 (stale; see AC11)
- [Source: _bmad-output/implementation-artifacts/10-1-implement-homeowner-dashboard-layout-with-systemoperatingmode-driven-headline-and-metric-cards.md] — `active_strategy` field introduction (AC1, AC2); `_STRATEGY_LABELS` table established (Subtask 3.3); 10.1 review-deferred writer-lock contract finding
- [Source: _bmad-output/implementation-artifacts/10-2-implement-ev-status-card-with-4-state-optimistic-override-and-statestore-backed-persistence.md] — `set_ev_override` mutator canonical pattern (AC3); snapshot-patch design refinement (Subtask 4.5); `_safe_audit` helper (`web/routes/actions.py:292–327`); `actions_router` registration; Alpine.js bundle + `evOverride` factory precedent
- [Source: _bmad-output/implementation-artifacts/deferred-work.md] — overlapping deferred findings cited in R7
- [Source: src/open_ems/core/state.py#L72-L78] — `EnergyStrategy` enum members (declaration order)
- [Source: src/open_ems/core/state_store.py#L1-L9] — writer-lock invariant docstring (Story 10.2)
- [Source: src/open_ems/core/state_store.py#L87-L101] — `set_ev_override` canonical pattern
- [Source: src/open_ems/web/routes/actions.py#L292-L327] — `_safe_audit` helper reused by 10.3 route
- [Source: src/open_ems/web/state_serialization.py#L34-L38] — `_STRATEGY_LABELS` table
- [Source: src/open_ems/web/state_serialization.py#L278-L309] — `build_homeowner_headline_context` (extended by AC5)
- [Source: src/open_ems/engine/control_loop.py#L202] — `strategy=snapshot.active_strategy` read site
- [Source: src/open_ems/web/templates/fragments/homeowner/status-headline.html] — current 13-line template (rewritten by AC8)
- [Source: src/open_ems/web/templates/dashboard.html#L47-L97] — existing Alpine `evOverride` factory block (extended by AC9)
- [Source: src/open_ems/settings.py#L50-L64] — Story 10.2 Settings pattern for confirmation-timeout fields

## Dev Agent Record

### Agent Model Used

claude-opus-4-7 (Claude Code via bmad-dev-story workflow, 2026-05-12)

### Debug Log References

- All 6 new `set_active_strategy` tests confirmed RED (`AttributeError: 'StateStore' object has no attribute 'set_active_strategy'`) before implementation; flipped to GREEN after Task 1.
- All 6 new `strategy_options` builder tests confirmed RED (`KeyError: 'strategy_options'`); flipped to GREEN after Task 3.
- All 2 new fragment-route tests confirmed RED (assertions on selector-panel markup) before template rewrite; flipped to GREEN after Task 4.
- 10 new `set_strategy_route` tests confirmed RED (`404 Not Found`) before route added; flipped to GREEN after Task 2.
- Full regression suite confirmed green after each task (no regressions introduced at any step).

### Completion Notes List

- **Status: review** — all 8 tasks + R7 bundled patch complete.
- **A1 R7 bundled patch** — added symmetric structlog INFO emissions to `set_ev_override` (`ev_override_installed` / `ev_override_cleared_via_mutator` / `ev_override_displaced`) AND to the new `set_active_strategy` (`active_strategy_changed`). Closes the two 10.2 deferred items at `[src/open_ems/core/state_store.py:96-104]` and `[state_store.py:86-104]` (both classified `acceptable-post-story-10-2` and explicitly re-classified as `safe-during-this-story` in 10.3 R7).
- **Tests delta: +30 net passing, +1 xfailed.** Baseline 1568 → 1598 passed; 6 → 7 xfailed (new a11y placeholder). The +30 exceeds the AC16 target of ≥18 because the AC #9 test parametrizes 3× strategies. Skipped count unchanged (1).
- **R6 drift risk monitored:** the `_active_strategy` ↔ `_snapshot.active_strategy` two-place storage is enforced via the new test `test_state_store_set_active_strategy_round_trip_via_get_snapshot_immediately` — a future refactor that drops the `model_copy(update=...)` line will fail this test and the AC15 #4 integration test. The structural contract is now load-bearing in two places.
- **Cold-start trade-off accepted:** in-memory-only strategy persists exactly as 10.1 designed. Process restart clears the homeowner's selection to the constructor default (`maximize_self_consumption`); persistence is Epic 11/12 scope.
- **Documentation deviation:** the `_STRATEGY_LABELS` table's exhaustiveness coverage is now triple-enforced (AC14 #10 + #11 + the existing 10.1 exhaustiveness test). The defensive `.get(...)` fallback (10.1 review patch #2 pattern) is applied to the new `strategy_options` builder.
- **Architectural alignment confirmed:** no PolicyGuard touchpoint, no command dispatch, no PolicyGuard / IntentExecutor / RetryPolicy involvement. The decision engine picks up the new strategy on its next tick via the existing `snapshot.active_strategy` read path (`control_loop.py:202`).
- **CSRF + outerHTML swap survival:** every option button carries explicit `hx-headers='{"X-CSRF-Token": "..."}'` matching the Story 10.2 EV-card precedent. Test AC14 #21 asserts the response body carries 3 per-button CSRF tokens (one per option) for surviving `outerHTML` replacement.
- **Failure UX:** invalid strategy values + render exceptions both return `HTMLResponse(status_code=200)` with the `strategy-failure.html` partial — HTMX `outerHTML` swaps the failure notice into `#status-headline` rather than freezing on a 4xx. The client-side `strategy_update_confirmation_timeout_seconds` budget (default 5s) is the fallback for total network failure (Alpine factory's `_fail()`).
- **Alpine listener scope:** `htmx:response-error` and `htmx:send-error` are bound at `#strategy-selector-panel` (NOT `.window`) per R3 forbidden-state #7. EV-card failures bubble through `#ev-card`, NOT `#strategy-selector-panel` — the two listener scopes are disjoint by DOM hierarchy.
- **Architecture-doc stale reference noted:** `architecture.md:822-826` still references a `strategies/` directory (`minimize_cost.py`, `maximize_self_consumption.py`, `prioritize_ev.py`) that does not exist — strategy parameter sets are inlined in `engine/rules/energy_balancing.py:99-101` dispatch. Out of 10.3 scope per AC11; flagged for a future Epic 11 architecture-doc-sync sweep.
- **Quality gates:** pytest 1598 passed + 1 skipped + 7 xfailed; ruff check + format clean across 258 files; mypy clean across 99 source files; `uv.lock` unchanged.

### File List

**New files:**
- `src/open_ems/web/templates/fragments/homeowner/strategy-failure.html` — failure-fragment partial returned by the strategy route on validation / render errors.
- `tests/unit/web/test_set_strategy_route.py` — 10 unit tests covering AC14 #12–#21.
- `tests/integration/web/test_strategy_selector_e2e.py` — 5 e2e tests + 1 a11y placeholder per AC15.

**Modified files:**
- `src/open_ems/core/state_store.py` — added `async set_active_strategy(strategy) -> bool` mutator (writer-lock + in-lock idempotency check + snapshot patch via `model_copy(update={...})` + no `sequence_id` bump + INFO emission). Extended class docstring to document both mutators (`set_ev_override`, `set_active_strategy`) as the canonical writer-lock pattern. Added symmetric INFO emissions to `set_ev_override` (R7 bundled patch).
- `src/open_ems/web/routes/actions.py` — added `POST /actions/set-strategy` route (synchronous; form-parse → enum-validate → mutator → audit-if-mutated → render headline) + `_render_strategy_failure` helper + module-level `_STRATEGY_FAILURE_MESSAGE` + Jinja2 templates init. New imports: `pathlib`, `HTMLResponse`, `Jinja2Templates`, `EnergyStrategy`, `build_homeowner_headline_context`.
- `src/open_ems/web/routes/fragments.py` — `homeowner_status_headline` now injects `csrf_token` (from `user.csrf_token`) and `strategy_update_confirmation_timeout_seconds` (from `Settings`). Renamed `_user` to `user` so csrf_token is accessible.
- `src/open_ems/web/state_serialization.py` — extended `build_homeowner_headline_context` to emit `strategy_options` (declaration-order iteration over `EnergyStrategy`; defensive `.get(...)` fallback for labels). Builder stays pure-functional over `SystemSnapshot`.
- `src/open_ems/settings.py` — added `strategy_update_confirmation_timeout_seconds: int = Field(default=5, ge=1, le=30)` with rationale docstring.
- `src/open_ems/web/templates/fragments/homeowner/status-headline.html` — rewrote to include selector trigger + selector panel + failure slot + Alpine `x-data="strategySelector(...)"`. Three `<button role="option">` rows iterating over `strategy_options`; each carries `hx-post="/actions/set-strategy"` + per-button `hx-headers` CSRF + `hx-target="#status-headline"` + `hx-swap="outerHTML"`.
- `src/open_ems/web/templates/dashboard.html` — appended `Alpine.data('strategySelector', ...)` factory inside existing `alpine:init` listener (below `evOverride`). State: `open`, `pendingStrategy`, `errorMessage`. Methods: `toggle()`, `close()`, `markPending(strategyValue)`, `failFromHtmx()`, `_fail()`, `_clearTimer()`, `destroy()`. Client-side timeout via `setTimeout` cleared on swap-destroy.
- `src/open_ems/web/static/open-ems.css` — appended Story 10.3 selector CSS: `[x-cloak] { display: none !important; }` (first use in codebase), `.status-headline__strategy-trigger`, `.strategy-selector`, `.strategy-selector__option` with reserved transparent 4px left-border (only active option's border-color changes — no width shift on activation), `.strategy-selector__option--active`, `.strategy-selector__failure`. No new tokens.
- `tests/unit/core/test_state_store.py` — added 6 tests (AC14 #1–#6).
- `tests/unit/engine/test_control_loop.py` — added 1 test (AC14 #7 / AC7).
- `tests/unit/web/test_state_serialization.py` — added 4 tests covering strategy_options (AC14 #8–#11; AC #9 parametrized 3×).
- `tests/unit/web/test_fragment_routes.py` — added 2 tests covering selector panel + no-pass-green design rule (AC14 #22–#23).
- `_bmad-output/implementation-artifacts/sprint-status.yaml` — flipped `10-3` entry: `ready-for-dev → in-progress` at workflow start; `in-progress → review` on Step 9 completion.

### Change Log

| Date | Change | Author |
|---|---|---|
| 2026-05-12 | Story created (A2-triggered T1+T4; R1–R7 substantive). Status: ready-for-dev. | bmad-create-story |
| 2026-05-12 | Implementation complete. 8 tasks + R7 bundled patch. 22 new unit tests + 6 integration tests (5 passing + 1 xfail a11y placeholder). 1598 passed + 1 skipped + 7 xfailed (baseline 1568 + 1 skipped + 6 xfailed → +30 net passing). Ruff check + format clean across 258 files; mypy clean across 99 source files; uv.lock unchanged. Status: ready-for-dev → in-progress → review. | bmad-dev-story |
| 2026-05-12 | Adversarial review complete (Blind Hunter + Edge Case Hunter + Acceptance Auditor). 9 patches applied in-cycle (1 HIGH `markPending` timer leak; 3 MEDIUM — invalid-strategy silent path with whitespace strip + structlog warning, optimistic `aria-selected` x-bind, AC15 #4 integration test now instantiates ControlLoop and asserts `EvaluationInput.strategy` end-to-end; 5 LOW — test name rename to match spec, WWW-Authenticate body assertion on test #16, single-source `_STRATEGY_FAILURE_MESSAGE` threaded via Alpine factory config, `import re` hoisted to module top, all four mutator INFO emissions moved inside the writer lock for ordering correctness). 4 deferred to `deferred-work.md` under "code review of 10-3-..." heading (homeowner-cards polling-stops-after-swap pre-existing systemic from 10.1; concurrent-POST audit `previous_strategy` staleness; `_safe_audit` post-mutation process-kill window pre-existing from 10.2; `_TEMPLATES_DIR` duplicate Jinja env pre-existing from 10.2). 19 findings dismissed-with-rationale inline. Final test counts: 1598 passed + 1 skipped + 7 xfailed (same baseline as dev cycle — no regressions). Ruff check + format clean across 258 files; mypy clean across 99 source files; uv.lock unchanged. A7 review-closure gate PASSED (fifth proof-of-enforcement story after 9-Y-a, 9-X, 10-1, 10-2). Status: review → done. | bmad-code-review |

### Review Findings

_Adversarial review run on 2026-05-12 via `bmad-code-review` (three parallel layers: Blind Hunter, Edge Case Hunter, Acceptance Auditor). The Acceptance Auditor verdict on AC1–AC17 was: 11 PASS / 3 DEVIATION (all minor) / 3 PARTIAL (AC4 defensive redundancy, AC12 implicit-verification, AC17 pending-A7) / 0 FAIL. R7 promises CLOSED (must-before-10-3 `_active_strategy` writer-lock contract + two 10.2 lifecycle-INFO bundled deferred items all structurally satisfied). Counts: 1 HIGH patch (Alpine timer leak), 3 MEDIUM patches (silent failure path + a11y regression + integration-test weakening), 5 LOW patches (style + observability ordering + test hygiene), 4 deferred (3 pre-existing, 1 polling pattern), 19 dismissed-with-rationale._

This is the **fifth A7 review-closure proof-of-enforcement target** (after 9-Y-a, 9-X, 10-1, 10-2).

#### Patches (9 — apply during the review-closure cycle)

- [x] [Review][Patch] **HIGH — `markPending` does not clear the prior pending timer before scheduling a new one; rapid double-clicks leak a stale fallback timer that fires a premature failure notice after a successful POST** [`src/open_ems/web/templates/dashboard.html:129-144`] — User clicks option A → timer_A scheduled. User clicks option B within 5s → timer_A is NOT cleared; `this._timer` is overwritten with timer_B id only. At t=5s, timer_A's callback runs, sees `pendingStrategy === 'B'` (still non-null because B is in flight), and calls `_fail('Strategy update failed…')` even though B's POST is healthy. Resulting UX: false failure flicker. Fix: call `this._clearTimer()` at the top of `markPending()` before assigning a new timer. Sources: Blind Hunter + Edge Case Hunter (duplicate finding).
- [x] [Review][Patch] **MEDIUM — Invalid `strategy` form value silently emits no audit/log signal AND catches an unreachable `KeyError`; also fails on trailing whitespace** [`src/open_ems/web/routes/actions.py:362-368`] — `EnergyStrategy("does_not_exist")` raises `ValueError`; `KeyError` is unreachable (StrEnum value-coercion raises ValueError only). Invalid values silently route to the failure fragment with zero observability — operators cannot correlate UX failures with bad payloads. Trailing whitespace from clipboard/extension proxies (`minimize_cost ` → ValueError) is silently rejected too. Fix: drop `KeyError` from the except clause, `.strip()` the raw value before enum conversion, and emit `logger.warning("set_strategy_invalid_value", raw=raw_strategy, user_id=user.user_id, component="actions")` before returning the failure fragment. Sources: Blind Hunter + Edge Case Hunter (duplicate finding).
- [x] [Review][Patch] **MEDIUM — `aria-selected` does not reflect the optimistic-pending state; screen-reader users hear the OLD active option as selected during the 50ms–5s server round-trip** [`src/open_ems/web/templates/fragments/homeowner/status-headline.html:42-50`] — Static `aria-selected="true"` from server render does not update via Alpine while `pendingStrategy` is non-null. A11y regression: a screen reader scanning the panel during the request reads the wrong "selected" option. Fix: add `x-bind:aria-selected="(pendingStrategy === '{{ option.value }}' || (pendingStrategy === null && {{ option.active|tojson }})) ? 'true' : 'false'"` on each option button. Pairs with the AC15 a11y placeholder test (currently xfail). Sources: Blind Hunter + Edge Case Hunter (duplicate finding).
- [x] [Review][Patch] **MEDIUM — AC15 integration test #4 does NOT actually instantiate ControlLoop or call `_build_evaluation_input`; it only asserts snapshot reflection (already covered by the AC7 unit test)** [`tests/integration/web/test_strategy_selector_e2e.py:178-220`] — Spec AC15 #4 promises an "End-to-end proof of the snapshot-patch propagation contract" via `ControlLoop._build_evaluation_input` + `EvaluationInput.strategy` assertion. The diff's integration test instead asserts `store.get_snapshot().active_strategy == ...` after the POST and a follow-up `publish()` — duplicating the AC1 snapshot-patch contract, not the AC7 end-to-end propagation contract. The AC7 unit test in `tests/unit/engine/test_control_loop.py::test_control_loop_picks_up_strategy_change_on_next_tick_via_snapshot_read` DOES exercise the full path; the integration layer should match. Fix: instantiate a real `ControlLoop` after the POST, populate required slots, call `_build_evaluation_input(snapshot, datetime.now(UTC))`, and assert `eval_input.strategy is EnergyStrategy.prioritize_ev`. Source: Acceptance Auditor.
- [x] [Review][Patch] **LOW — Test name rename to match AC14 #6 spec** [`tests/unit/core/test_state_store.py:735`] — Diff named the test `test_state_store_set_active_strategy_round_trip_via_get_snapshot_immediately`; spec mandates `test_state_store_set_active_strategy_round_trip_through_get_snapshot_reflects_immediately`. Semantically equivalent, but spec-fidelity is the naming-honesty contract (Story 9-Y-a precedent). Fix: rename. Source: Acceptance Auditor.
- [x] [Review][Patch] **LOW — Test #16 `test_post_set_strategy_unauthenticated_returns_401_for_htmx` checks status code only; spec mandates exact 401 + WWW-Authenticate-style body assertion (Story 10.1 review patch #6 precedent)** [`tests/unit/web/test_set_strategy_route.py:188-199`] — The 10.1 review tightened unauth assertions; 10.3 inherits the same pattern. Fix: also assert the response body / `WWW-Authenticate` header content (e.g., `assert "login_required" in resp.json()["detail"]` or the project's specific 401 contract). Source: Acceptance Auditor.
- [x] [Review][Patch] **LOW — `_STRATEGY_FAILURE_MESSAGE` Python constant is duplicated as inline string literals in `dashboard.html` `markPending` setTimeout + `failFromHtmx` paths; no test asserts the three usages agree** [`src/open_ems/web/routes/actions.py:53`, `src/open_ems/web/templates/dashboard.html:140, 147`] — A future maintainer who edits the Python constant will leave the two JS literals stale; server-rendered failure shows the new message; client-detected (timeout / htmx-error) failures show the old. Fix: pass the message through the Alpine factory config (e.g., `strategySelector({initialStrategy: ..., timeoutSeconds: ..., failureMessage: {{ _STRATEGY_FAILURE_MESSAGE | tojson }}})` and reference `this.failureMessage` in `_fail`). Inject the constant from the route into the headline-fragment context. Sources: Blind Hunter + Edge Case Hunter (duplicate finding).
- [x] [Review][Patch] **LOW — `import re` inside test functions; move to module top** [`tests/unit/web/test_fragment_routes.py:339`, `tests/unit/web/test_set_strategy_route.py:297`] — Style: inline `import re` inside test bodies suggests copy-paste from another module. Fix: hoist to the file's top-level imports. Source: Blind Hunter.
- [x] [Review][Patch] **LOW — `logger.info("active_strategy_changed", ...)` is emitted AFTER the writer-lock release; two concurrent mutators can interleave their log emissions vs their mutations, producing out-of-order observability records** [`src/open_ems/core/state_store.py:115-121`] — Mutations themselves serialize under the lock (correct); only the log ordering can differ from the mutation ordering. Cheap fix: move the `logger.info(...)` call inside the `async with self._writer_lock:` block (structlog is non-blocking; safe under the lock). The symmetric `set_ev_override` emissions added by the R7 bundle (`ev_override_installed` / `_cleared_via_mutator` / `_displaced`) have the same shape — fix all four call sites at once. Source: Blind Hunter.

#### Deferred (4 — pre-existing or carried forward)

- [x] [Review][Defer] **Polling stops after the first outerHTML swap (all homeowner card fragments — `status-headline.html`, `_card.html`, `ev-card.html` — lack `hx-get`/`hx-trigger` on their root `<section>` while the dashboard.html shell carries the polling attributes)** [`src/open_ems/web/templates/fragments/homeowner/status-headline.html`, `_card.html`, `ev-card.html`] — When the first HTMX poll swaps `outerHTML`, the replacement element has no `hx-trigger`, so the 10s timer is not re-attached. Pre-existing systemic issue from Story 10.1 (introduces `status-headline.html`) and propagated through 10.2 (`ev-card.html`). Story 10.3's POST swap makes the same surface load-bearing for the strategy headline. Out of 10.3 scope — affects every homeowner card; needs a coordinated fix across all card fragments. Tracked in `deferred-work.md`. Source: Edge Case Hunter.
- [x] [Review][Defer] **Audit row's `previous_strategy` field is stale under concurrent set-strategy POSTs (lock-free `get_snapshot()` read happens BEFORE the writer-lock-protected mutator; two near-simultaneous POSTs both observe the same `previous`, producing a contradictory audit trail)** [`src/open_ems/web/routes/actions.py:370-385`] — T1 reads previous=A, T2 reads previous=A; T1 mutates A→B; T2 mutates B→C. T2's audit logs `previous=A, new=C` but the actual mutation moment was B→C. Mitigations require either (a) changing the mutator signature to return `(mutated, previous)` as a tuple (captured in-lock), or (b) accepting and documenting the audit-interpretation. Bundle with the broader audit-trail concurrency review during Epic 11 observability work. Source: Edge Case Hunter.
- [x] [Review][Defer] **`_safe_audit` runs AFTER the mutator commits; process kill in the ~5ms window between mutation and audit emission leaves an un-auditable strategy change** [`src/open_ems/web/routes/actions.py:382-397`] — Same pattern as Story 10.2's EV-override audit path. Audit-trail SLO concern is broader than 10.3 — applies to every CONTROL-class audit emission in `_safe_audit` callers. Bundle with the 10.2-deferred `_safe_audit blocks on serial DB INSERTs in the homeowner hot path` item ([src/open_ems/web/routes/actions.py:73-82, 140-154]) during a future audit-resilience pass. Source: Blind Hunter.
- [x] [Review][Defer] **`_TEMPLATES_DIR` + module-global `_templates = Jinja2Templates(...)` in `actions.py` creates a second Jinja environment disjoint from `fragments.py`'s `_templates`; future global filters/helpers won't propagate** [`src/open_ems/web/routes/actions.py:212-213`] — Pre-existing 10.2 pattern (10.2 introduced the same module-global). Cleaner approach is `request.app.state.templates` (single Jinja env); requires a project-wide template-init consolidation. Bundle with the broader templating singleton work during Epic 11 / 12. Source: Blind Hunter.

#### Dismissed (19 — with rationale)

- **Alpine `x-on:htmx:response-error` needs the `.camel` modifier to fire (Blind Hunter HIGH)** — **VERIFIED FALSE.** HTMX 1.9 dispatches every event with BOTH camelCase AND kebab-case names (per HTMX docs §Events). The kebab-case listener fires correctly. Acceptance Auditor explicitly endorses kebab-case as more idiomatic than spec's camelCase example.
- **`hx-vals` JSON injection: raw `option.value` interpolated into a JSON-in-HTML attribute is fragile re: future enum values containing HTML-special chars (Blind Hunter HIGH)** — Enum is closed at 3 members (`EnergyStrategy.{minimize_cost, maximize_self_consumption, prioritize_ev}`), all safe lowercase identifiers. Exhaustiveness tests at three sites catch future enum additions. Defense-in-depth refactor not warranted for the current value set.
- **`tojson` filter into `x-data` attribute fragile re: U+2028/U+2029, single-quote contamination (Blind Hunter HIGH)** — Same reasoning as above; current `active_strategy.value` is constrained to a safe enum literal. The `tojson` filter handles JSON escaping correctly for ASCII identifiers.
- **`app.state.observability` injection verification gap in tests (Blind Hunter MEDIUM)** — `_resolve_observability(request)` at `actions.py:292-294` uses `getattr(request.app.state, "observability", None)` + `isinstance(obs, ObservabilityService)`. Tests inject `AsyncMock(spec=ObservabilityService)` — `isinstance` with `spec=` returns True; the wiring is correct via standard mock semantics. Test pass-count attests.
- **`self._active_strategy` may not be initialized on `StateStore` construct (Blind Hunter MEDIUM)** — **VERIFIED FALSE.** `state_store.py:74` sets `self._active_strategy = active_strategy` inside `__init__`; constructor signature includes `active_strategy: EnergyStrategy = EnergyStrategy.maximize_self_consumption` (line 68).
- **`errorMessage` reset semantics / `data-strategy-update-failed` attribute persistence across failure → success cycle (Blind Hunter MEDIUM)** — HTMX `outerHTML` swap replaces the WHOLE `<section id="status-headline">` atomically. Both the `data-strategy-update-failed` attribute and the Alpine state are reset by the swap; the new instance's `x-data` re-runs the factory with `errorMessage: ''`. No cross-instance state leakage.
- **Test `test_post_set_strategy_response_body_contains_csrf_token_for_outerhtml_swap_survival` passes for the wrong reason due to Jinja autoescape (Blind Hunter MEDIUM)** — **VERIFIED FALSE.** Jinja autoescape escapes ONLY the output of `{{ csrf_token }}` expressions; literal template content (the surrounding `"` characters inside the single-quoted attribute) is passed through verbatim. The test substring assertion `body.count(f'X-CSRF-Token": "{csrf}"') == 3` reads exactly what's rendered.
- **Static `aria-expanded="false"` flickers before Alpine attaches (Blind Hunter LOW)** — Initial state IS closed; the static value matches the post-Alpine value. No observable flicker.
- **`_resolve_observability(request)` is not wrapped in a try/except; if it raises, user sees 500 (Blind Hunter MEDIUM)** — **VERIFIED FALSE.** The helper at `actions.py:292-294` is `getattr(..., None)` + `isinstance(...)` — both calls are total (cannot raise). Returns `None` on missing or non-`ObservabilityService` instance.
- **Strategy panel collapses every 10s after first poll because new Alpine instance defaults `open: false` (Edge Case Hunter MEDIUM)** — Depends on the deferred polling fix (D1). Not currently load-bearing because polling stops after the first swap regardless. When D1 is addressed, this collapse semantics will need a `localStorage` persistence hook (or `data-open` round-trip); it will surface organically as part of that fix.
- **Success POST clobbers a still-displayed user-visible failure notice from a prior attempt (Edge Case Hunter MEDIUM)** — Failure notice is informational; a subsequent successful action implies the user resolved the underlying issue. `aria-live="polite"` announces the failure once; removal-via-swap is standard ARIA semantics. Acceptable UX.
- **`failFromHtmx()` listens for ANY descendant HTMX response-error / send-error; a future non-strategy button inside `#strategy-selector-panel` would trigger the strategy-failure UX (Edge Case Hunter LOW)** — Current panel hosts only the three strategy option buttons. Forward-compat concern; trivially addressable when a new button class lands by adding `event.detail.pathInfo.requestPath === '/actions/set-strategy'` filter.
- **`publish({DeviceRole.inverter: None})` in integration test passes None as device state — contract violation (Blind Hunter MEDIUM)** — **VERIFIED FALSE.** `StateStore.publish`'s signature accepts `Mapping[DeviceRole, DeviceState | None]` (None = unavailable). The test exercises a valid contract. The new tests pass on this signature.
- **AC4 failure fragment renders failure text both statically (`<p>{{ failure_message }}</p>`) AND via `x-init="errorMessage = ..."` Alpine seed (Acceptance Auditor LOW)** — Defensive redundancy: the static `<p>` survives Alpine-hydration failure (script blocked, parse error in factory). Visual outcome is single notice in either case. Intentional belt-and-braces pattern.
- **AC6: `device_id=None` kwarg omitted from `_safe_audit` call (Acceptance Auditor LOW)** — Behaviorally identical (`_safe_audit` defaults `device_id` to None). Spec was explicit for clarity; implementation relies on the default. Style choice, not a defect.
- **AC9: Spec used camelCase event name `htmx:responseError`; diff uses kebab-case `htmx:response-error` (Acceptance Auditor LOW)** — Kebab-case is more idiomatic for Alpine's lowercased event-name parser. HTMX dispatches both forms. Spec was non-idiomatic; diff is more correct.
- **AC12: No explicit verify-step comment about `test_homeowner_dashboard_shell_renders_five_htmx_bound_sections` (Acceptance Auditor LOW)** — The 1598 passing test count includes this test (it would have failed had 10.3 inadvertently added a 6th HTMX-bound section); implicit confirmation. An explicit re-assertion in completion notes would be belt-and-braces but adds no signal.
- **AC15 #1: Test builds an inline minimal `_build_app(...)` instead of the project-wide `app_e2e` fixture (Acceptance Auditor LOW)** — Mirrors the Story 10.2 integration-test pattern. Both approaches assert the required behaviour; the inline fixture has lower setup cost and isolates the test from full-lifespan side effects.
- **AC17: Sprint-status entry is `review` not `done` (Acceptance Auditor LOW)** — The `done` transition is owned by `bmad-code-review` per Epic 9 retro A7 (codified 2026-05-11). Current `review` is the correct mid-flow state; the A7 review-closure gate (next section) flips it to `done` once every finding is resolved/dismissed/deferred-and-verified.
